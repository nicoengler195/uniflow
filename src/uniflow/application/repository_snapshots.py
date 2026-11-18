"""Read-only local Git observation and immutable repository snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, Course, RepositoryBlob, RepositoryFile, RepositoryRemote,
    RepositorySnapshot, RepositorySource, RepositoryAcademicLink, ClassificationProposal, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock

POLICY_VERSION = "1"
GIT_TIMEOUT = 30
MAX_GIT_OUTPUT = 32 * 1024 * 1024
LANGUAGES = {".java": "JAVA", ".hs": "HASKELL", ".py": "PYTHON", ".kt": "KOTLIN", ".md": "MARKDOWN", ".xml": "XML"}


class RepositoryError(ValueError):
    pass


def _git(root: Path, *args: str, missing_ok: bool = False, limit: int = MAX_GIT_OUTPUT) -> bytes:
    git = shutil.which("git")
    if not git:
        raise RepositoryError("GIT_NOT_AVAILABLE")
    env = os.environ.copy()
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_LFS_SKIP_SMUDGE="1", GIT_EXTERNAL_DIFF="")
    command = [git, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
               "-c", "diff.external=", "-c", "diff.trustExitCode=false", *args]
    try:
        with tempfile.TemporaryFile() as output_file:
            process = subprocess.run(command, cwd=root, env=env, stdout=output_file,
                stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False)
            if output_file.tell() > limit:
                raise RepositoryError("GIT_OUTPUT_LIMIT")
            output_file.seek(0)
            output = output_file.read()
    except FileNotFoundError:
        raise RepositoryError("GIT_NOT_AVAILABLE") from None
    except subprocess.TimeoutExpired:
        raise RepositoryError("GIT_TIMEOUT") from None
    if process.returncode and not missing_ok:
        raise RepositoryError("GIT_OBSERVATION_FAILED")
    return output if process.returncode == 0 else b""


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise RepositoryError("PATH_ENCODING_ERROR") from None


def _path(raw: bytes) -> str:
    value = _decode(raw)
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value)
            or any(p in ("", ".", "..", ".git") for p in parts) or "\x00" in value):
        raise RepositoryError("UNSAFE_REPOSITORY_PATH")
    return value


def _root(path: Path | str) -> Path:
    location = Path(path).expanduser().resolve()
    if not location.is_dir():
        raise RepositoryError("NOT_A_DIRECTORY")
    result = _git(location, "rev-parse", "--show-toplevel", missing_ok=True)
    if not result:
        raise RepositoryError("NOT_A_GIT_WORKTREE")
    root = Path(_decode(result).strip()).resolve()
    if not root.is_dir():
        raise RepositoryError("NOT_A_GIT_WORKTREE")
    return root


def sanitize_remote(raw: str) -> str:
    """Return a public identity; callers must never print `raw`."""
    if any(ord(c) < 32 for c in raw) or not raw.strip():
        raise RepositoryError("UNSAFE_REMOTE")
    if "://" in raw:
        try:
            parsed = urlsplit(raw)
            if parsed.scheme not in ("https", "http", "ssh", "git") or not parsed.hostname:
                raise ValueError
            host = parsed.hostname.lower()
            port = parsed.port
            if ":" in host:
                host = "[" + host + "]"
            authority = host + (f":{port}" if port else "")
            if not parsed.path or "\\" in parsed.path:
                raise ValueError
            return urlunsplit((parsed.scheme.lower(), authority, parsed.path, "", ""))
        except ValueError:
            raise RepositoryError("UNSAFE_REMOTE") from None
    match = re.fullmatch(r"(?:[^@/:\s]+@)?([A-Za-z0-9.-]+):([^?#\s]+)", raw)
    if match and not match.group(2).startswith("/") and ".." not in PurePosixPath(match.group(2)).parts:
        return f"{match.group(1).lower()}:{match.group(2)}"
    raise RepositoryError("UNSAFE_REMOTE")


def _remotes(root: Path) -> list[tuple[str, str]]:
    raw = _git(root, "config", "--local", "--null", "--get-regexp", r"^remote\..*\.url$", missing_ok=True)
    found: list[tuple[str, str]] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        try:
            key, value = _decode(item).split("\n", 1)
            name = key[len("remote."):-len(".url")]
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", name):
                raise ValueError
            found.append((name, sanitize_remote(value)))
        except (ValueError, UnicodeDecodeError):
            raise RepositoryError("UNSAFE_REMOTE") from None
    return sorted(found)


def register_repository(path: Path | str, config: AppConfig, *, stable_id: str | None = None,
                        course_stable_id: str | None = None, assessment_stable_id: str | None = None) -> tuple[str, str]:
    root = _root(path)
    remotes = _remotes(root)
    if stable_id is not None and (not stable_id.strip() or len(stable_id) > 160):
        raise RepositoryError("INVALID_REPOSITORY_ID")
    Session = create_session_factory(get_engine(config.database_path))
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with Session.begin() as session:
            existing = session.scalar(select(RepositorySource).where(RepositorySource.local_path == str(root)))
            if existing and stable_id and existing.stable_id != stable_id:
                raise RepositoryError("REPOSITORY_ID_CONFLICT")
            if not existing and not stable_id:
                stable_id = "REPO_" + hashlib.sha256(os.fsencode(root)).hexdigest()[:16].upper()
            course = session.scalar(select(Course).where(Course.stable_id == course_stable_id)) if course_stable_id else None
            assessment = session.scalar(select(Assessment).where(Assessment.stable_id == assessment_stable_id)) if assessment_stable_id else None
            if course_stable_id and not course:
                raise RepositoryError("UNKNOWN_COURSE")
            if assessment_stable_id and not assessment:
                raise RepositoryError("UNKNOWN_ASSESSMENT")
            if assessment and course and assessment.course_id != course.id:
                raise RepositoryError("ASSESSMENT_COURSE_MISMATCH")
            active_link = session.scalar(select(RepositoryAcademicLink).where(
                RepositoryAcademicLink.repository_source_id == existing.id,
                RepositoryAcademicLink.is_active.is_(True))) if existing else None
            if active_link and ((course and course.id != active_link.course_id) or
                                (assessment and assessment.id != active_link.assessment_id)):
                raise RepositoryError("CONFLICT_REQUIRES_CORRECTION")
            if existing:
                if course_stable_id and existing.course_id not in (None, course.id):
                    raise RepositoryError("COURSE_CONFLICT")
                if assessment_stable_id and existing.assessment_id not in (None, assessment.id):
                    raise RepositoryError("ASSESSMENT_CONFLICT")
                existing.course_id = existing.course_id or (course.id if course else (assessment.course_id if assessment else None))
                existing.assessment_id = existing.assessment_id or (assessment.id if assessment else None)
                source, outcome = existing, "ALREADY_REGISTERED"
            else:
                source = RepositorySource(stable_id=stable_id, local_path=str(root),
                    course_id=course.id if course else (assessment.course_id if assessment else None),
                    assessment_id=assessment.id if assessment else None)
                session.add(source)
                session.flush()
                outcome = "REGISTERED"
            if source.course_id and not session.scalar(select(RepositoryAcademicLink).where(
                    RepositoryAcademicLink.repository_source_id == source.id,
                    RepositoryAcademicLink.is_active.is_(True))):
                identity = hashlib.sha256(json.dumps({"kind": "REPOSITORY_ACADEMIC_LINK",
                    "repository_source_id": source.id, "course_id": source.course_id,
                    "assessment_id": source.assessment_id, "classifier": "MANUAL/1"},
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                proposal = ClassificationProposal(proposal_kind="REPOSITORY_ACADEMIC_LINK",
                    status="ACCEPTED", identity_sha256=identity,
                    subject_repository_source_id=source.id, proposed_course_id=source.course_id,
                    proposed_assessment_id=source.assessment_id, classifier_name="MANUAL",
                    classifier_version="1", decided_at=utc_now(), decision_actor="MANUAL_USER",
                    decision_reason="Explicit repo-register academic options")
                session.add(proposal)
                session.flush()
                session.add(RepositoryAcademicLink(repository_source_id=source.id,
                    course_id=source.course_id, assessment_id=source.assessment_id,
                    classification_proposal_id=proposal.id, is_active=True))
            # Replace only sanitized identities, never raw URLs.
            for remote in list(source.remotes):
                session.delete(remote)
            session.flush()
            for name, identity in remotes:
                session.add(RepositoryRemote(repository_source_id=source.id, name=name, sanitized_identity=identity))
            return outcome, source.id


def _entries(raw: bytes) -> list[bytes]:
    return [item for item in raw.split(b"\x00") if item]


def _tree(root: Path) -> dict[str, tuple[str, str]]:
    raw = _git(root, "ls-tree", "-r", "-z", "HEAD", missing_ok=True)
    out = {}
    for item in _entries(raw):
        meta, name = item.split(b"\t", 1)
        mode, kind, oid = _decode(meta).split(" ")
        if kind == "blob" or kind == "commit":
            out[_path(name)] = (mode, oid)
    return out


def _index(root: Path) -> dict[str, tuple[str, str]]:
    out = {}
    for item in _entries(_git(root, "ls-files", "--stage", "-z")):
        meta, name = item.split(b"\t", 1)
        mode, oid, stage = _decode(meta).split(" ")
        if stage != "0":
            raise RepositoryError("UNMERGED_INDEX")
        out[_path(name)] = (mode, oid)
    return out


def _status(root: Path) -> tuple[dict[str, tuple[str, str, str | None]], bytes]:
    raw = _git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=all")
    entries = _entries(raw)
    status: dict[str, tuple[str, str, str | None]] = {}
    index = 0
    while index < len(entries):
        item = entries[index]
        if item.startswith((b"1 ", b"2 ")):
            kind = item[:1]
            fields = item.split(b" ", 8 if kind == b"1" else 9)
            xy = _decode(fields[1])
            name = _path(fields[-1])
            original = None
            if kind == b"2":
                index += 1
                original = _path(entries[index])
            status[name] = (xy[0], xy[1], original)
        elif item.startswith(b"? "):
            status[_path(item[2:].rstrip(b"/"))] = ("?", "?", None)
        index += 1
    return status, raw


def _safe_work_path(root: Path, name: str) -> Path:
    _path(name.encode("utf-8"))
    target = root.joinpath(*name.split("/"))
    # Check parents without following a link that escapes the repository.
    current = root
    for part in name.split("/")[:-1]:
        current = current / part
        if current.is_symlink():
            raise RepositoryError("UNSAFE_REPOSITORY_PATH")
    return target


def _work_bytes(root: Path, name: str, max_bytes: int) -> tuple[bytes | None, str, tuple | None]:
    path = _safe_work_path(root, name)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, "DELETED", None
    fingerprint = (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino)
    if stat.S_ISLNK(before.st_mode):
        data = os.fsencode(os.readlink(path))
        kind = "SYMLINK"
    elif stat.S_ISREG(before.st_mode):
        if before.st_size > max_bytes:
            raise RepositoryError("REPOSITORY_FILE_LIMIT")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
                raise RepositoryError("REPOSITORY_CHANGED")
            data = stream.read(max_bytes + 1)
        kind = "REGULAR"
    elif stat.S_ISDIR(before.st_mode) and (path / ".git").exists():
        return None, "NESTED_REPOSITORY", fingerprint
    else:
        raise RepositoryError("UNSUPPORTED_REPOSITORY_ENTRY")
    if len(data) > max_bytes:
        raise RepositoryError("REPOSITORY_FILE_LIMIT")
    after = path.lstat()
    if fingerprint != (after.st_mode, after.st_size, after.st_mtime_ns, after.st_ino):
        raise RepositoryError("REPOSITORY_CHANGED")
    return data, kind, fingerprint


def _object_bytes(root: Path, oid: str, limit: int) -> bytes:
    size_raw = _git(root, "cat-file", "-s", oid, limit=32)
    if int(size_raw) > limit:
        raise RepositoryError("REPOSITORY_FILE_LIMIT")
    try:
        return _git(root, "cat-file", "blob", oid, limit=limit + 1)
    except RepositoryError as exc:
        if str(exc) == "GIT_OUTPUT_LIMIT":
            raise RepositoryError("REPOSITORY_FILE_LIMIT") from None
        raise


def _head(root: Path) -> tuple[str | None, str | None, bool]:
    oid = _git(root, "rev-parse", "--verify", "HEAD", missing_ok=True).decode("ascii", "strict").strip() or None
    branch_raw = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", missing_ok=True)
    branch = _decode(branch_raw).strip() or None
    return oid, branch, branch is None


@dataclass
class Scan:
    identity: str
    manifest: dict
    files: list[dict]
    blobs: dict[str, bytes]
    fingerprint: tuple
    build_system: str


def _fingerprint(root: Path, include_untracked: bool) -> tuple:
    head = _head(root)
    index = _git(root, "ls-files", "--stage", "-z")
    status, _ = _status(root)
    if not include_untracked:
        status = {k: v for k, v in status.items() if v[0] != "?"}
    stats = []
    for name in sorted(set(_index(root)) | set(status)):
        if not include_untracked and status.get(name, (None,))[0] == "?":
            continue
        path = _safe_work_path(root, name)
        try:
            s = path.lstat()
            stats.append((name, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ino))
        except FileNotFoundError:
            stats.append((name, None))
    git_index = _git(root, "rev-parse", "--git-path", "index").decode().strip()
    index_path = Path(git_index) if Path(git_index).is_absolute() else root / git_index
    index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest() if index_path.exists() else None
    return head, hashlib.sha256(index).hexdigest(), index_hash, tuple(sorted(status.items())), tuple(stats)


def _scan(root: Path, stable_id: str, config: AppConfig, include_untracked: bool) -> Scan:
    before = _fingerprint(root, include_untracked)
    head = _tree(root)
    index = _index(root)
    status, _ = _status(root)
    untracked = {name for name, state in status.items() if state[0] == "?"} if include_untracked else set()
    names = sorted(set(head) | set(index) | untracked)
    if len(names) > config.max_repository_files:
        raise RepositoryError("REPOSITORY_FILE_COUNT_LIMIT")
    blobs: dict[str, bytes] = {}
    files: list[dict] = []
    total = 0
    for name in names:
        h = head.get(name)
        i = index.get(name)
        sx, sy, original = status.get(name, (".", ".", None))
        tracked = name not in untracked
        mode = (i or h or (None, None))[0]
        kind = "SUBMODULE" if mode == "160000" else "SYMLINK" if mode == "120000" else "REGULAR"
        refs = {}
        for label, state in (("head", h), ("index", i)):
            if state and state[0] != "160000":
                data = _object_bytes(root, state[1], config.max_repository_file_bytes)
                if len(data) > config.max_repository_file_bytes:
                    raise RepositoryError("REPOSITORY_FILE_LIMIT")
                digest = hashlib.sha256(data).hexdigest()
                blobs[digest] = data
                total += len(data)
                refs[label] = digest
            else:
                refs[label] = None
        if kind == "SUBMODULE":
            work, work_kind = None, "SUBMODULE"
        else:
            work, work_kind, _ = _work_bytes(root, name, config.max_repository_file_bytes)
            if work_kind == "NESTED_REPOSITORY":
                kind = work_kind
            elif work is not None:
                kind = work_kind
        if work is not None:
            digest = hashlib.sha256(work).hexdigest()
            blobs[digest] = work
            total += len(work)
            refs["working"] = digest
        else:
            refs["working"] = None
        if total > config.max_repository_snapshot_bytes:
            raise RepositoryError("REPOSITORY_SNAPSHOT_LIMIT")
        if name in untracked:
            tracked_state = "UNTRACKED"
        elif not i or work is None and kind != "SUBMODULE":
            tracked_state = "DELETED"
        elif sx != "." or sy != ".":
            tracked_state = "MODIFIED"
        else:
            tracked_state = "TRACKED"
        files.append(dict(path=name, original_path=original, entry_kind=kind, tracked_state=tracked_state,
            status_x=sx, status_y=sy, head_git_oid=h[1] if h else None, index_git_oid=i[1] if i else None,
            head_mode=h[0] if h else None, index_mode=i[0] if i else None,
            working_size_bytes=len(work) if work is not None else None,
            language_hint=LANGUAGES.get(PurePosixPath(name).suffix.lower(), "UNKNOWN"), **refs))
    after = _fingerprint(root, include_untracked)
    if before != after:
        raise RepositoryError("REPOSITORY_CHANGED")
    root_files = {name for name in index if "/" not in name}
    maven = "pom.xml" in root_files
    gradle = bool({"build.gradle", "build.gradle.kts"} & root_files)
    build = "MULTIPLE" if maven and gradle else "MAVEN" if maven else "GRADLE" if gradle else "UNKNOWN"
    head_oid, branch, detached = before[0]
    manifest = dict(repository_stable_id=stable_id, policy_version=POLICY_VERSION,
        include_untracked=include_untracked, head_commit_oid=head_oid, branch_name=branch,
        is_detached=detached, files=[{k: f[k] for k in ("path", "original_path", "entry_kind", "tracked_state",
        "status_x", "status_y", "head_git_oid", "index_git_oid", "head_mode", "index_mode", "head", "index", "working")}
        for f in files])
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return Scan(identity, manifest, files, blobs, after, build)


def _blob_path(config: AppConfig, digest: str) -> Path:
    return config.repository_root / "blobs" / "sha256" / digest[:2] / digest / "content"


def _verify_blob(path: Path, digest: str, data: bytes) -> None:
    if not path.is_file() or path.read_bytes() != data or hashlib.sha256(data).hexdigest() != digest:
        raise RepositoryError("REPOSITORY_BLOB_INTEGRITY")


@dataclass(frozen=True)
class SnapshotOutcome:
    outcome: str
    identity: str
    snapshot_id: str | None
    file_count: int
    blob_count: int
    build_system_hint: str


def snapshot_repository(identifier: str, config: AppConfig, *, dry_run: bool = False,
                        tracked_only: bool = False) -> SnapshotOutcome:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        source = session.scalar(select(RepositorySource).where((RepositorySource.id == identifier) | (RepositorySource.stable_id == identifier)))
        if source is None:
            raise RepositoryError("UNKNOWN_REPOSITORY")
        source_id, stable_id, root = source.id, source.stable_id, Path(source.local_path)
    if _root(root) != root.resolve():
        raise RepositoryError("REPOSITORY_ROOT_CHANGED")
    scan = _scan(root, stable_id, config, not tracked_only)
    if dry_run:
        return SnapshotOutcome("DRY_RUN_READY", scan.identity, None, len(scan.files), len(scan.blobs), scan.build_system)
    created: list[Path] = []
    config.repository_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=config.repository_root) as staging_name:
          staging = Path(staging_name)
          for digest, data in scan.blobs.items():
              staged = staging / digest
              staged.write_bytes(data)
              _verify_blob(staged, digest, data)
          with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            if _fingerprint(root, not tracked_only) != scan.fingerprint:
                raise RepositoryError("REPOSITORY_CHANGED")
            with Session.begin() as session:
                previous = session.scalar(select(RepositorySnapshot).where(
                    RepositorySnapshot.repository_source_id == source_id,
                    RepositorySnapshot.snapshot_identity == scan.identity))
                if previous:
                    return SnapshotOutcome("ALREADY_SNAPSHOTTED", scan.identity, previous.id,
                        previous.tracked_file_count + previous.untracked_file_count, previous.blob_count, previous.build_system_hint)
                blob_rows: dict[str, RepositoryBlob] = {}
                for digest, data in scan.blobs.items():
                    path = _blob_path(config, digest)
                    row = session.scalar(select(RepositoryBlob).where(RepositoryBlob.sha256 == digest))
                    if row is not None and (row.stored_path != str(path) or row.size_bytes != len(data)):
                        raise RepositoryError("REPOSITORY_BLOB_INTEGRITY")
                    if path.exists():
                        _verify_blob(path, digest, data)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.link(staging / digest, path)
                            created.append(path)
                        except FileExistsError:
                            _verify_blob(path, digest, data)
                    if row is None:
                        row = RepositoryBlob(sha256=digest, size_bytes=len(data), stored_path=str(path))
                        session.add(row)
                        session.flush()
                    blob_rows[digest] = row
                head_oid, branch, detached = scan.fingerprint[0]
                snapshot = RepositorySnapshot(repository_source_id=source_id, snapshot_identity=scan.identity,
                    policy_version=POLICY_VERSION, include_untracked=not tracked_only, head_commit_oid=head_oid,
                    branch_name=branch, is_detached=detached,
                    is_dirty=any(f["tracked_state"] != "TRACKED" for f in scan.files),
                    tracked_file_count=sum(f["tracked_state"] != "UNTRACKED" for f in scan.files),
                    untracked_file_count=sum(f["tracked_state"] == "UNTRACKED" for f in scan.files),
                    deleted_file_count=sum(f["tracked_state"] == "DELETED" for f in scan.files),
                    blob_count=len(scan.blobs), build_system_hint=scan.build_system, status="SUCCESS")
                session.add(snapshot)
                session.flush()
                for f in scan.files:
                    session.add(RepositoryFile(snapshot_id=snapshot.id, path=f["path"], original_path=f["original_path"],
                        entry_kind=f["entry_kind"], tracked_state=f["tracked_state"], status_x=f["status_x"],
                        status_y=f["status_y"], head_git_oid=f["head_git_oid"], index_git_oid=f["index_git_oid"],
                        head_mode=f["head_mode"], index_mode=f["index_mode"],
                        head_blob_id=blob_rows[f["head"]].id if f["head"] else None,
                        index_blob_id=blob_rows[f["index"]].id if f["index"] else None,
                        working_blob_id=blob_rows[f["working"]].id if f["working"] else None,
                        working_size_bytes=f["working_size_bytes"], language_hint=f["language_hint"]))
                return SnapshotOutcome("SNAPSHOTTED", scan.identity, snapshot.id,
                    len(scan.files), len(scan.blobs), scan.build_system)
    except Exception:
        # Remove only bytes this call published and only if no committed row refers to them.
        for path in created:
            with Session() as session:
                if not session.scalar(select(RepositoryBlob).where(RepositoryBlob.stored_path == str(path))):
                    path.unlink(missing_ok=True)
        raise


def list_repositories(config: AppConfig) -> list[RepositorySource]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return list(session.scalars(select(RepositorySource).order_by(RepositorySource.stable_id)))


def repository_info(identifier: str, config: AppConfig) -> tuple[RepositorySource, list[RepositoryRemote]]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        source = session.scalar(select(RepositorySource).where((RepositorySource.id == identifier) | (RepositorySource.stable_id == identifier)))
        if not source:
            raise RepositoryError("UNKNOWN_REPOSITORY")
        remotes = list(session.scalars(select(RepositoryRemote).where(RepositoryRemote.repository_source_id == source.id).order_by(RepositoryRemote.name)))
        return source, remotes


def list_snapshots(identifier: str, config: AppConfig) -> list[RepositorySnapshot]:
    source, _ = repository_info(identifier, config)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return list(session.scalars(select(RepositorySnapshot).where(RepositorySnapshot.repository_source_id == source.id).order_by(RepositorySnapshot.captured_at)))


def read_repository_snapshot(identifier: str, config: AppConfig) -> tuple[RepositorySnapshot, list[RepositoryFile]]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        snap = session.get(RepositorySnapshot, identifier)
        if not snap:
            raise RepositoryError("UNKNOWN_SNAPSHOT")
        files = list(session.scalars(select(RepositoryFile).where(RepositoryFile.snapshot_id == snap.id).order_by(RepositoryFile.path)))
        return snap, files


def read_repository_blob(blob_id: str, config: AppConfig) -> bytes:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        blob = session.get(RepositoryBlob, blob_id)
        if not blob:
            raise RepositoryError("UNKNOWN_BLOB")
        path = _blob_path(config, blob.sha256)
        if Path(blob.stored_path) != path:
            raise RepositoryError("REPOSITORY_BLOB_INTEGRITY")
        data = path.read_bytes()
        if len(data) != blob.size_bytes or hashlib.sha256(data).hexdigest() != blob.sha256:
            raise RepositoryError("REPOSITORY_BLOB_INTEGRITY")
        return data
