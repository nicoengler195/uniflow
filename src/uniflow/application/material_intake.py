"""Deterministic local file intake shared by CLI and future callers."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from uniflow.domain.enums import InformationStatus, MaterialState
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, Material, MaterialArtifact, SourceArtifact, SourceReference,
)
from uniflow.infrastructure.write_lock import project_write_lock


@dataclass(frozen=True)
class IntakeResult:
    status: str
    sha256: str
    stored_path: Path
    material_stable_id: str | None
    version_number: int | None
    detail: str = ""


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _artifact_path(root: Path, sha256: str) -> Path:
    return root / "sha256" / sha256[:2] / sha256 / "content"


def _verified_file(path: Path, sha256: str, size: int) -> bool:
    return path.is_file() and _hash_file(path) == (sha256, size)


def import_material(
    source: Path | str, config: AppConfig, *, course_stable_id: str | None = None,
    material_stable_id: str | None = None, title: str | None = None,
    material_type: str | None = None, new_version: bool = False,
    dry_run: bool = False,
) -> IntakeResult:
    if dry_run:
        return _import_material_unlocked(source, config, course_stable_id=course_stable_id,
            material_stable_id=material_stable_id, title=title, material_type=material_type,
            new_version=new_version, dry_run=True)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        return _import_material_unlocked(source, config, course_stable_id=course_stable_id,
            material_stable_id=material_stable_id, title=title, material_type=material_type,
            new_version=new_version)


def _import_material_unlocked(
    source: Path | str,
    config: AppConfig,
    *,
    course_stable_id: str | None = None,
    material_stable_id: str | None = None,
    title: str | None = None,
    material_type: str | None = None,
    new_version: bool = False,
    dry_run: bool = False,
) -> IntakeResult:
    """Copy one file into immutable storage and optionally associate a Material.

    A newly published blob is removed if the DB transaction fails. Existing blobs
    are never removed. This service assumes one writer per project at a time.
    """
    path = Path(source).expanduser().resolve(strict=False)
    if not path.is_file():
        raise ValueError(f"INVALID_SOURCE: expected a readable regular file: {path}")
    if any((course_stable_id, title, material_type)) and not material_stable_id:
        raise ValueError("--stable-id is required when Material metadata or course is supplied")
    if new_version and not material_stable_id:
        raise ValueError("--new-version requires --stable-id")
    for value, label, limit in ((material_stable_id, "stable ID", 160), (title, "title", 240), (material_type, "material type", 80)):
        if value is not None and (not value.strip() or len(value) > limit):
            raise ValueError(f"{label} must be nonempty and at most {limit} characters")
    if not config.database_path.is_file():
        raise ValueError("Database is not initialized; run uniflow init")
    sha256, size = _hash_file(path)
    target = _artifact_path(config.artifact_root, sha256)
    Session = create_session_factory(get_engine(config.database_path))
    created_target = False
    result: IntakeResult | None = None
    try:
        with Session.begin() as session:
            course = None
            if course_stable_id:
                course = session.scalar(select(Course).where(Course.stable_id == course_stable_id))
                if course is None:
                    raise ValueError(f"Unknown course stable ID: {course_stable_id}")
            artifact = session.scalar(select(SourceArtifact).where(SourceArtifact.sha256 == sha256))
            material = None
            current = None
            if material_stable_id:
                material = session.scalar(select(Material).where(Material.stable_id == material_stable_id))
                if material is not None:
                    if course_stable_id and material.course_id != course.id:
                        return IntakeResult("CONFLICT", sha256, target, material_stable_id, None, "Material belongs to another course")
                    if title and material.title != title:
                        return IntakeResult("CONFLICT", sha256, target, material_stable_id, None, "Material title differs")
                    if material_type and material.material_type != material_type:
                        return IntakeResult("CONFLICT", sha256, target, material_stable_id, None, "Material type differs")
                    current = session.scalar(select(MaterialArtifact).where(
                        MaterialArtifact.material_id == material.id,
                        MaterialArtifact.is_current.is_(True),
                    ))
                    if current and current.artifact.sha256 == sha256:
                        if not _verified_file(target, sha256, size):
                            raise ValueError(f"Stored artifact missing or corrupt: {target}")
                        return IntakeResult("ALREADY_IMPORTED", sha256, target, material_stable_id, current.version_number)
                    if current and not new_version:
                        return IntakeResult("CONFLICT", sha256, target, material_stable_id, current.version_number,
                                            "Different bytes require --new-version")
                    if current is None and material.content_hash and material.content_hash != sha256 and not new_version:
                        return IntakeResult("CONFLICT", sha256, target, material_stable_id, None,
                                            "Legacy content hash differs; use --new-version")
                    if current and new_version:
                        prior = session.scalar(select(MaterialArtifact).where(
                            MaterialArtifact.material_id == material.id,
                            MaterialArtifact.artifact_id == artifact.id,
                        )) if artifact else None
                        if prior is not None:
                            return IntakeResult("CONFLICT", sha256, target, material_stable_id, prior.version_number,
                                                "This artifact is already in Material history")
            if artifact and not _verified_file(target, sha256, size):
                raise ValueError(f"Stored artifact missing or corrupt: {target}")
            if target.exists() and not _verified_file(target, sha256, size):
                raise ValueError(f"Artifact path collision or corruption: {target}")
            version = (max((link.version_number for link in material.artifacts), default=0) + 1) if material else (1 if material_stable_id else None)
            if current and new_version:
                status = "NEW_MATERIAL_VERSION"
            elif material_stable_id and artifact:
                status = "LINKED_EXISTING_ARTIFACT"
            elif artifact:
                status = "ALREADY_IMPORTED"
            else:
                status = "IMPORTED"
            result = IntakeResult(status, sha256, target, material_stable_id, version)
            if dry_run:
                return IntakeResult("DRY_RUN_" + status, sha256, target, material_stable_id, version)

            # Publish only after the plan is valid. Hard-link creation is atomic and
            # fails rather than overwriting an existing hash path.
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
                temp = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as output, path.open("rb") as input_stream:
                        for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                            output.write(block)
                    if not _verified_file(temp, sha256, size):
                        raise ValueError("Source changed during copy; import aborted")
                    try:
                        os.link(temp, target)
                        created_target = True
                    except FileExistsError:
                        if not _verified_file(target, sha256, size):
                            raise ValueError(f"Artifact path collision or corruption: {target}")
                finally:
                    temp.unlink(missing_ok=True)
            if artifact is None:
                artifact = SourceArtifact(
                    sha256=sha256, original_filename=path.name, original_source_path=str(path),
                    extension=path.suffix.lower() or None,
                    mime_type=mimetypes.guess_type(path.name)[0], size_bytes=size,
                    stored_path=str(target),
                )
                session.add(artifact)
                session.flush()
            if material_stable_id:
                source_ref = SourceReference(source_type="material_import", uri=str(path),
                                             information_status=InformationStatus.OBSERVED)
                session.add(source_ref)
                if material is None:
                    material = Material(stable_id=material_stable_id, title=title or path.name,
                                        material_type=material_type, course=course,
                                        lifecycle_state=MaterialState.DOWNLOADED, source=source_ref)
                    session.add(material)
                    session.flush()
                if current:
                    current.is_current = False
                    session.flush()  # release partial unique index before inserting current version
                session.add(MaterialArtifact(material=material, artifact=artifact, version_number=version,
                                             is_current=True, source=source_ref))
    except Exception:
        if created_target:
            target.unlink(missing_ok=True)
        raise
    assert result is not None
    return result


def list_materials(config: AppConfig) -> list[tuple[str, str, str | None, int | None, str | None]]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        materials = session.scalars(select(Material).order_by(Material.stable_id)).all()
        return [
            (material.stable_id or material.id, material.title,
             material.course.stable_id if material.course else None,
             next((link.version_number for link in material.artifacts if link.is_current), None),
             next((link.artifact.sha256 for link in material.artifacts if link.is_current), None))
            for material in materials
        ]
