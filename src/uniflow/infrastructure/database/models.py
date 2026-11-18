from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import LargeBinary, BigInteger, Boolean, CheckConstraint, Date, DateTime, Time, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from uniflow.domain.enums import (
    AssessmentAttemptStatus,
    AssessmentType,
    DeadlineStatus,
    EvidenceType,
    InformationStatus,
    LearningSessionState,
    MaterialState,
    SemesterState,
)


def new_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SourceReference(Base, TimestampMixin):
    __tablename__ = "source_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    uri: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    information_status: Mapped[InformationStatus] = mapped_column(
        Enum(InformationStatus), default=InformationStatus.UNKNOWN, nullable=False
    )


class Semester(Base, TimestampMixin):
    __tablename__ = "semesters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stable_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[SemesterState] = mapped_column(Enum(SemesterState), default=SemesterState.PLANNING, nullable=False)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Berlin", nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    source: Mapped[SourceReference | None] = relationship()
    courses: Mapped[list[Course]] = relationship(back_populates="semester", cascade="all, delete-orphan")


class Course(Base, TimestampMixin):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    semester_id: Mapped[str] = mapped_column(ForeignKey("semesters.id"), nullable=False)
    stable_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="MODULE", nullable=False)
    parent_course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    participation: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    participation_reason: Mapped[str | None] = mapped_column(Text)
    unknowns_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))
    parent: Mapped[Course | None] = relationship(remote_side="Course.id")
    information_status: Mapped[InformationStatus] = mapped_column(
        Enum(InformationStatus), default=InformationStatus.UNKNOWN, nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    semester: Mapped[Semester] = relationship(back_populates="courses")
    source: Mapped[SourceReference | None] = relationship()
    materials: Mapped[list[Material]] = relationship(back_populates="course")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="course")
    deadlines: Mapped[list[Deadline]] = relationship(back_populates="course")
    learning_sessions: Mapped[list[LearningSession]] = relationship(back_populates="course")
    official_results: Mapped[list[OfficialResult]] = relationship(back_populates="course")


class Material(Base, TimestampMixin):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_materials_content_hash"),
        UniqueConstraint("stable_id", name="uq_materials_stable_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    stable_id: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    material_type: Mapped[str | None] = mapped_column(String(80))
    lifecycle_state: Mapped[MaterialState] = mapped_column(
        Enum(MaterialState), default=MaterialState.EXPECTED, nullable=False
    )
    file_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    course: Mapped[Course | None] = relationship(back_populates="materials")
    source: Mapped[SourceReference | None] = relationship()
    artifacts: Mapped[list[MaterialArtifact]] = relationship(back_populates="material")


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_source_path: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str | None] = mapped_column(String(40))
    mime_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    materials: Mapped[list[MaterialArtifact]] = relationship(back_populates="artifact")


class RepositorySource(Base):
    __tablename__ = "repository_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stable_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    local_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    provider_hint: Mapped[str | None] = mapped_column(String(80))
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    remotes: Mapped[list[RepositoryRemote]] = relationship(back_populates="source", order_by="RepositoryRemote.name")
    snapshots: Mapped[list[RepositorySnapshot]] = relationship(back_populates="source", order_by="RepositorySnapshot.captured_at")


class RepositoryRemote(Base):
    __tablename__ = "repository_remotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    repository_source_id: Mapped[str] = mapped_column(ForeignKey("repository_sources.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sanitized_identity: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("repository_source_id", "name", name="uq_repository_remote_name"),)
    source: Mapped[RepositorySource] = relationship(back_populates="remotes")


class RepositoryBlob(Base):
    __tablename__ = "repository_blobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (UniqueConstraint("repository_source_id", "snapshot_identity", name="uq_repository_snapshot_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    repository_source_id: Mapped[str] = mapped_column(ForeignKey("repository_sources.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    snapshot_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    include_untracked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    head_commit_oid: Mapped[str | None] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(Text)
    is_detached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tracked_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    untracked_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_count: Mapped[int] = mapped_column(Integer, nullable=False)
    build_system_hint: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[RepositorySource] = relationship(back_populates="snapshots")
    files: Mapped[list[RepositoryFile]] = relationship(back_populates="snapshot", order_by="RepositoryFile.path")


class RepositoryFile(Base):
    __tablename__ = "repository_files"
    __table_args__ = (UniqueConstraint("snapshot_id", "path", name="uq_repository_file_path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    original_path: Mapped[str | None] = mapped_column(Text)
    entry_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    tracked_state: Mapped[str] = mapped_column(String(30), nullable=False)
    status_x: Mapped[str | None] = mapped_column(String(2))
    status_y: Mapped[str | None] = mapped_column(String(2))
    head_git_oid: Mapped[str | None] = mapped_column(String(64))
    index_git_oid: Mapped[str | None] = mapped_column(String(64))
    head_blob_id: Mapped[str | None] = mapped_column(ForeignKey("repository_blobs.id"))
    index_blob_id: Mapped[str | None] = mapped_column(ForeignKey("repository_blobs.id"))
    working_blob_id: Mapped[str | None] = mapped_column(ForeignKey("repository_blobs.id"))
    head_mode: Mapped[str | None] = mapped_column(String(10))
    index_mode: Mapped[str | None] = mapped_column(String(10))
    working_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    language_hint: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot: Mapped[RepositorySnapshot] = relationship(back_populates="files")


class DerivedArtifact(Base):
    __tablename__ = "derived_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ParseRun(Base):
    __tablename__ = "parse_runs"
    __table_args__ = (UniqueConstraint("source_artifact_id", "parser_name", "parser_version",
        "dependency_version", "config_sha256", "attempt_number", name="uq_parse_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(80), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    dependency_version: Mapped[str] = mapped_column(String(80), nullable=False, default="stdlib")
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    derived_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("derived_artifacts.id"))
    page_count: Mapped[int | None] = mapped_column(Integer)
    unit_count: Mapped[int | None] = mapped_column(Integer)
    char_count: Mapped[int | None] = mapped_column(Integer)
    warning_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)

    source_artifact: Mapped[SourceArtifact] = relationship()
    derived_artifact: Mapped[DerivedArtifact | None] = relationship()
    document: Mapped[ParsedDocument | None] = relationship(back_populates="run", uselist=False)
    units: Mapped[list[ParsedUnit]] = relationship(back_populates="run", order_by="ParsedUnit.ordinal")


class ParsedDocument(Base):
    __tablename__ = "parsed_documents"

    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), primary_key=True)
    format: Mapped[str] = mapped_column(String(20), nullable=False)
    derived_artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    normalized_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_whitespace_char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False)

    run: Mapped[ParseRun] = relationship(back_populates="document")
    derived_artifact: Mapped[DerivedArtifact] = relationship()


class ParsedUnit(Base):
    __tablename__ = "parsed_units"
    __table_args__ = (UniqueConstraint("parse_run_id", "ordinal", name="uq_parsed_unit_ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_label: Mapped[str | None] = mapped_column(String(80))
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_whitespace_char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    run: Mapped[ParseRun] = relationship(back_populates="units")


class MaterialArtifact(Base):
    __tablename__ = "material_artifacts"
    __table_args__ = (
        UniqueConstraint("material_id", "version_number", name="uq_material_version"),
        UniqueConstraint("material_id", "artifact_id", name="uq_material_artifact"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    classification_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("classification_proposals.id"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_link_id: Mapped[str | None] = mapped_column(ForeignKey("material_artifacts.id"))

    material: Mapped[Material] = relationship(back_populates="artifacts")
    artifact: Mapped[SourceArtifact] = relationship(back_populates="materials")
    source: Mapped[SourceReference | None] = relationship()


class DropJob(Base):
    __tablename__ = "drop_jobs"
    __table_args__ = (UniqueConstraint("source_path", "size_bytes", "mtime_ns", name="uq_drop_observation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    observed_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(80))
    source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("source_artifacts.id"))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)


class Assessment(Base, TimestampMixin):
    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stable_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    assessment_type: Mapped[AssessmentType] = mapped_column(
        Enum(AssessmentType), default=AssessmentType.UNKNOWN, nullable=False
    )
    information_status: Mapped[InformationStatus] = mapped_column(
        Enum(InformationStatus), default=InformationStatus.UNKNOWN, nullable=False
    )
    rules_summary: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    course: Mapped[Course] = relationship(back_populates="assessments")
    source: Mapped[SourceReference | None] = relationship()
    attempts: Mapped[list[AssessmentAttempt]] = relationship(back_populates="assessment", cascade="all, delete-orphan")


class AssessmentAttempt(Base, TimestampMixin):
    __tablename__ = "assessment_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("assessments.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AssessmentAttemptStatus] = mapped_column(
        Enum(AssessmentAttemptStatus), default=AssessmentAttemptStatus.UNKNOWN, nullable=False
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(40))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    assessment: Mapped[Assessment] = relationship(back_populates="attempts")
    source: Mapped[SourceReference | None] = relationship()


class Deadline(Base, TimestampMixin):
    __tablename__ = "deadlines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stable_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"))
    material_id: Mapped[str | None] = mapped_column(ForeignKey("materials.id"))
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_date_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[DeadlineStatus] = mapped_column(Enum(DeadlineStatus), default=DeadlineStatus.UNKNOWN, nullable=False)
    information_status: Mapped[InformationStatus] = mapped_column(
        Enum(InformationStatus), default=InformationStatus.UNKNOWN, nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    course: Mapped[Course | None] = relationship(back_populates="deadlines")
    assessment: Mapped[Assessment | None] = relationship()
    material: Mapped[Material | None] = relationship()
    source: Mapped[SourceReference | None] = relationship()


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(120))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    canonical_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    __table_args__ = (CheckConstraint("kind IN ('UNKNOWN','CONCEPT','ABILITY')", name="ck_skill_kind"),
                      CheckConstraint("(status = 'ACTIVE' AND canonical_skill_id IS NULL) OR (status = 'MERGED' AND canonical_skill_id IS NOT NULL AND canonical_skill_id != id)", name="ck_skill_merge"))


class SkillDependency(Base, TimestampMixin):
    __tablename__ = "skill_dependencies"

    __table_args__ = (Index("ix_skill_dependency_active", "prerequisite_skill_id", "dependent_skill_id", unique=True, sqlite_where=text("is_active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    prerequisite_skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    dependent_skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    skill_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("skill_proposals.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_dependency_id: Mapped[str | None] = mapped_column(ForeignKey("skill_dependencies.id"))

    prerequisite: Mapped[Skill] = relationship(foreign_keys=[prerequisite_skill_id])
    dependent: Mapped[Skill] = relationship(foreign_keys=[dependent_skill_id])
    source: Mapped[SourceReference | None] = relationship()


class SemanticExtractionRun(Base):
    __tablename__ = "semantic_extraction_runs"
    __table_args__ = (
        CheckConstraint("source_family IN ('DOCUMENT','REPOSITORY','MANUAL')", name="ck_semantic_family"),
        CheckConstraint("source_role IN ('UNKNOWN','INSTRUCTION','LECTURE_MATERIAL','EXERCISE','SOLUTION','STUDENT_CODE','TEST','BUILD_METADATA')", name="ck_semantic_role"),
        CheckConstraint("(source_family = 'DOCUMENT' AND material_artifact_id IS NOT NULL AND parse_run_id IS NOT NULL AND repository_academic_link_id IS NULL AND repository_snapshot_id IS NULL AND repository_state IS NULL) OR (source_family = 'REPOSITORY' AND material_artifact_id IS NULL AND parse_run_id IS NULL AND repository_academic_link_id IS NOT NULL AND repository_snapshot_id IS NOT NULL AND repository_state IN ('HEAD','INDEX','WORKTREE')) OR (source_family = 'MANUAL' AND material_artifact_id IS NULL AND parse_run_id IS NULL AND repository_academic_link_id IS NULL AND repository_snapshot_id IS NULL AND repository_state IS NULL)", name="ck_semantic_run_sources"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    source_family: Mapped[str] = mapped_column(String(20), nullable=False)
    source_role: Mapped[str] = mapped_column(String(30), nullable=False, default="UNKNOWN")
    extractor_name: Mapped[str] = mapped_column(String(80), nullable=False, default="MANUAL")
    extractor_version: Mapped[str] = mapped_column(String(40), nullable=False, default="1")
    extractor_config_sha256: Mapped[str | None] = mapped_column(String(64))
    input_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    material_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("material_artifacts.id"))
    parse_run_id: Mapped[str | None] = mapped_column(ForeignKey("parse_runs.id"))
    repository_academic_link_id: Mapped[str | None] = mapped_column(ForeignKey("repository_academic_links.id"))
    repository_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("repository_snapshots.id"))
    repository_state: Mapped[str | None] = mapped_column(String(20))
    provider_name: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(120))
    prompt_schema_version: Mapped[str | None] = mapped_column(String(80))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    seed: Mapped[int | None] = mapped_column(Integer)


class SemanticExtractionAttempt(Base):
    __tablename__ = "semantic_extraction_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_semantic_attempt_number"),
        CheckConstraint("status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="ck_semantic_attempt_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("semantic_extraction_runs.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_name: Mapped[str] = mapped_column(String(80), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    effective_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_schema_version: Mapped[str | None] = mapped_column(String(80))
    provider_name: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(120))
    input_package_sha256: Mapped[str | None] = mapped_column(String(64))
    input_manifest_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    output_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("derived_artifacts.id"))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    external_disclosure_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    authorization_actor: Mapped[str | None] = mapped_column(String(40))
    authorization_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorization_policy_version: Mapped[str | None] = mapped_column(String(40))
    authorized_item_count: Mapped[int | None] = mapped_column(Integer)
    authorized_utf8_bytes: Mapped[int | None] = mapped_column(Integer)
    external_request_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SemanticEvidenceSpan(Base):
    __tablename__ = "semantic_evidence_spans"
    __table_args__ = (
        CheckConstraint("(material_artifact_id IS NOT NULL AND parsed_unit_id IS NOT NULL AND char_start IS NOT NULL AND char_end IS NOT NULL AND repository_academic_link_id IS NULL AND repository_file_id IS NULL AND repository_blob_id IS NULL AND repository_state IS NULL AND byte_start IS NULL AND byte_end IS NULL) OR (material_artifact_id IS NULL AND parsed_unit_id IS NULL AND char_start IS NULL AND char_end IS NULL AND repository_academic_link_id IS NOT NULL AND repository_file_id IS NOT NULL AND repository_blob_id IS NOT NULL AND repository_state IN ('HEAD','INDEX','WORKTREE') AND byte_start IS NOT NULL AND byte_end IS NOT NULL)", name="ck_semantic_span_source"),
        CheckConstraint("char_start IS NULL OR (char_start >= 0 AND char_end > char_start)", name="ck_semantic_char_range"),
        CheckConstraint("byte_start IS NULL OR (byte_start >= 0 AND byte_end >= byte_start)", name="ck_semantic_byte_range"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    extraction_run_id: Mapped[str] = mapped_column(ForeignKey("semantic_extraction_runs.id"), nullable=False)
    extraction_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("semantic_extraction_attempts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    material_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("material_artifacts.id"))
    parsed_unit_id: Mapped[str | None] = mapped_column(ForeignKey("parsed_units.id"))
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    repository_academic_link_id: Mapped[str | None] = mapped_column(ForeignKey("repository_academic_links.id"))
    repository_file_id: Mapped[str | None] = mapped_column(ForeignKey("repository_files.id"))
    repository_blob_id: Mapped[str | None] = mapped_column(ForeignKey("repository_blobs.id"))
    repository_state: Mapped[str | None] = mapped_column(String(20))
    byte_start: Mapped[int | None] = mapped_column(BigInteger)
    byte_end: Mapped[int | None] = mapped_column(BigInteger)
    span_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class SkillProposal(Base):
    __tablename__ = "skill_proposals"
    __table_args__ = (
        Index("ix_skill_proposal_pending_identity", "identity_sha256", unique=True, sqlite_where=text("status = 'PENDING'")),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_skill_proposal_confidence"),
        CheckConstraint("proposal_kind IN ('NEW_SKILL','EXISTING_SKILL_EVIDENCE','SKILL_ALIAS','SKILL_DEPENDENCY','SKILL_COURSE_ASSOCIATION')", name="ck_skill_proposal_kind"),
        CheckConstraint("status IN ('PENDING','ACCEPTED','REJECTED','SUPERSEDED')", name="ck_skill_proposal_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    extraction_run_id: Mapped[str] = mapped_column(ForeignKey("semantic_extraction_runs.id"), nullable=False)
    extraction_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("semantic_extraction_attempts.id"))
    proposal_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    proposed_skill_id: Mapped[str | None] = mapped_column(String(120))
    proposed_name: Mapped[str | None] = mapped_column(String(240))
    proposed_description: Mapped[str | None] = mapped_column(Text)
    proposed_kind: Mapped[str | None] = mapped_column(String(20))
    proposed_domain: Mapped[str | None] = mapped_column(String(120))
    target_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    prerequisite_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    dependent_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    proposed_alias: Mapped[str | None] = mapped_column(String(240))
    accepted_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    accepted_alias_id: Mapped[str | None] = mapped_column(ForeignKey("skill_aliases.id"))
    accepted_dependency_id: Mapped[str | None] = mapped_column(ForeignKey("skill_dependencies.id"))
    accepted_course_skill_id: Mapped[str | None] = mapped_column(ForeignKey("course_skills.id"))
    decision_action: Mapped[str | None] = mapped_column(String(50))
    decision_actor: Mapped[str | None] = mapped_column(String(40))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("skill_proposals.id"))


class SkillProposalEvidence(Base):
    __tablename__ = "skill_proposal_evidence"
    proposal_id: Mapped[str] = mapped_column(ForeignKey("skill_proposals.id"), primary_key=True)
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("semantic_evidence_spans.id"), primary_key=True)


class SkillAlias(Base):
    __tablename__ = "skill_aliases"
    __table_args__ = (Index("ix_skill_alias_active", "skill_id", "normalized_alias", unique=True, sqlite_where=text("is_active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(240), nullable=False)
    skill_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("skill_proposals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_alias_id: Mapped[str | None] = mapped_column(ForeignKey("skill_aliases.id"))


class CourseSkill(Base):
    __tablename__ = "course_skills"
    __table_args__ = (Index("ix_course_skill_active", "course_id", "skill_id", unique=True, sqlite_where=text("is_active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    skill_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("skill_proposals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_link_id: Mapped[str | None] = mapped_column(ForeignKey("course_skills.id"))


class LearningSession(Base, TimestampMixin):
    __tablename__ = "learning_sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[LearningSessionState] = mapped_column(
        Enum(LearningSessionState), default=LearningSessionState.PLANNED, nullable=False
    )
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_path: Mapped[str | None] = mapped_column(Text)
    current_handoff_id: Mapped[str | None] = mapped_column(ForeignKey("learning_handoffs.id"))

    course: Mapped[Course | None] = relationship(back_populates="learning_sessions")
    evidence_items: Mapped[list[Evidence]] = relationship(back_populates="learning_session")
    target_skills: Mapped[list[LearningSessionTargetSkill]] = relationship(back_populates="learning_session", order_by="LearningSessionTargetSkill.ordinal")
    handoffs: Mapped[list[LearningHandoff]] = relationship(back_populates="learning_session", foreign_keys="LearningHandoff.learning_session_id", order_by="LearningHandoff.revision")
    current_handoff: Mapped[LearningHandoff | None] = relationship(foreign_keys=[current_handoff_id], post_update=True)


class LearningSessionTargetSkill(Base):
    __tablename__ = "learning_session_target_skills"
    __table_args__ = (UniqueConstraint("learning_session_id", "skill_id", name="uq_session_target_skill"),
                      UniqueConstraint("learning_session_id", "ordinal", name="uq_session_target_ordinal"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    learning_session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    learning_session: Mapped[LearningSession] = relationship(back_populates="target_skills")
    skill: Mapped[Skill] = relationship()


class LearningHandoff(Base):
    __tablename__ = "learning_handoffs"
    __table_args__ = (UniqueConstraint("learning_session_id", "revision", name="uq_learning_handoff_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    learning_session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(40), nullable=False)
    context_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_handoff_id: Mapped[str | None] = mapped_column(ForeignKey("learning_handoffs.id"))
    learning_session: Mapped[LearningSession] = relationship(back_populates="handoffs", foreign_keys=[learning_session_id])
    artifact: Mapped[DerivedArtifact] = relationship()


class LearningResult(Base):
    __tablename__ = "learning_results"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    learning_session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id"), nullable=False)
    learning_handoff_id: Mapped[str] = mapped_column(ForeignKey("learning_handoffs.id"), nullable=False, unique=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(30), nullable=False)
    artifact: Mapped[DerivedArtifact] = relationship()


class Evidence(Base, TimestampMixin):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("learning_result_id", "skill_id", name="uq_evidence_result_skill"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    learning_session_id: Mapped[str | None] = mapped_column(ForeignKey("learning_sessions.id"))
    learning_result_id: Mapped[str | None] = mapped_column(ForeignKey("learning_results.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    evidence_type: Mapped[EvidenceType] = mapped_column(Enum(EvidenceType), nullable=False)
    value_json: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    skill: Mapped[Skill] = relationship()
    learning_session: Mapped[LearningSession | None] = relationship(back_populates="evidence_items")
    source: Mapped[SourceReference | None] = relationship()


class MasteryState(Base, TimestampMixin):
    __tablename__ = "mastery_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    calculation_method: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence.id"))
    learning_session_id: Mapped[str | None] = mapped_column(ForeignKey("learning_sessions.id"))
    previous_mastery_state_id: Mapped[str | None] = mapped_column(ForeignKey("mastery_states.id"))

    skill: Mapped[Skill] = relationship()
    evidence: Mapped[Evidence | None] = relationship()
    learning_session: Mapped[LearningSession | None] = relationship()
    previous_mastery_state: Mapped[MasteryState | None] = relationship(remote_side="MasteryState.id")


class OfficialResult(Base, TimestampMixin):
    __tablename__ = "official_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"))
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    grade: Mapped[str | None] = mapped_column(String(40))
    credits: Mapped[float | None] = mapped_column(Float)
    issued_on: Mapped[date | None] = mapped_column(Date)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    course: Mapped[Course] = relationship(back_populates="official_results")
    assessment: Mapped[Assessment | None] = relationship()
    source: Mapped[SourceReference | None] = relationship()


class SemesterAudit(Base, TimestampMixin):
    __tablename__ = "semester_audits"
    __table_args__ = (
        UniqueConstraint("semester_id", "revision_number", name="uq_audit_semester_revision"),
        UniqueConstraint("canonical_content_sha256", name="uq_audit_canonical_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    semester_id: Mapped[str | None] = mapped_column(ForeignKey("semesters.id"))
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    audit_id: Mapped[str | None] = mapped_column(String(160))
    revision_number: Mapped[int | None] = mapped_column(Integer)
    original_file_sha256: Mapped[str | None] = mapped_column(String(64))
    canonical_content_sha256: Mapped[str | None] = mapped_column(String(64))
    stored_file_path: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))

    semester: Mapped[Semester | None] = relationship()
    source: Mapped[SourceReference | None] = relationship()


class AuditDerivedMixin(TimestampMixin):
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stable_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    information_status: Mapped[InformationStatus] = mapped_column(Enum(InformationStatus), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))


class ScheduleEntry(AuditDerivedMixin, Base):
    __tablename__ = "schedule_entries"


class ScheduleException(AuditDerivedMixin, Base):
    __tablename__ = "schedule_exceptions"


class PlanningProfile(Base, TimestampMixin):
    __tablename__ = "planning_profiles"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_profile_revision"),
        CheckConstraint("default_action_duration_minutes IS NULL OR default_action_duration_minutes > 0", name="ck_planning_default_duration"),
        CheckConstraint("max_daily_study_minutes IS NULL OR max_daily_study_minutes > 0", name="ck_planning_daily_limit"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    semester_id: Mapped[str] = mapped_column(ForeignKey("semesters.id"), unique=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    default_action_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    max_daily_study_minutes: Mapped[int | None] = mapped_column(Integer)


class WeeklyPlan(Base, TimestampMixin):
    __tablename__ = "weekly_plans"
    __table_args__ = (UniqueConstraint("semester_id", "week_start", name="uq_weekly_plan_semester_week"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    semester_id: Mapped[str] = mapped_column(ForeignKey("semesters.id"), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    accepted_revision_id: Mapped[str | None] = mapped_column(ForeignKey("weekly_plan_revisions.id"))


class WeeklyPlanRevision(Base):
    __tablename__ = "weekly_plan_revisions"
    __table_args__ = (
        UniqueConstraint("weekly_plan_id", "revision_number", name="uq_weekly_plan_revision_number"),
        CheckConstraint("revision_number >= 1", name="ck_weekly_plan_revision_positive"),
        CheckConstraint("revision_kind IN ('GENERATED','MANUAL_EDIT')", name="ck_weekly_plan_revision_kind"),
        CheckConstraint("source_limit BETWEEN 1 AND 100", name="ck_weekly_plan_source_limit"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    weekly_plan_id: Mapped[str] = mapped_column(ForeignKey("weekly_plans.id"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(ForeignKey("weekly_plan_revisions.id"))
    revision_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    nba_artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    calendar_artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    weekly_schedule_artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    revision_artifact_id: Mapped[str] = mapped_column(ForeignKey("derived_artifacts.id"), nullable=False)
    nba_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    weekly_schedule_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    nba_context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduler_context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_course_filter_id: Mapped[str | None] = mapped_column(String(120))
    source_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_overrides_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WeeklyPlanAcceptance(Base):
    __tablename__ = "weekly_plan_acceptances"
    __table_args__ = (CheckConstraint("actor = 'MANUAL_USER'", name="ck_weekly_plan_acceptance_actor"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    weekly_plan_id: Mapped[str] = mapped_column(ForeignKey("weekly_plans.id"), nullable=False)
    weekly_plan_revision_id: Mapped[str] = mapped_column(ForeignKey("weekly_plan_revisions.id"), unique=True, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(20), nullable=False)


class WeeklyPlanBlockExecution(Base):
    __tablename__ = "weekly_plan_block_executions"
    __table_args__ = (
        UniqueConstraint("weekly_plan_revision_id", "block_id", name="uq_weekly_block_execution_revision_block"),
        CheckConstraint("execution_kind IN ('CREATED_SESSION','RESUMED_SESSION')",
                        name="ck_weekly_block_execution_kind"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    weekly_plan_revision_id: Mapped[str] = mapped_column(ForeignKey("weekly_plan_revisions.id", ondelete="RESTRICT"), nullable=False)
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    action_id: Mapped[str] = mapped_column(Text, nullable=False)
    execution_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    learning_session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id", ondelete="RESTRICT"), nullable=False)
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AvailabilityWindow(Base):
    __tablename__ = "availability_windows"
    __table_args__ = (
        CheckConstraint("start_local_time < end_local_time", name="ck_availability_order"),
        CheckConstraint("valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until", name="ck_availability_validity"),
        CheckConstraint("start_fold IS NULL OR start_fold IN (0,1)", name="ck_availability_start_fold"),
        CheckConstraint("end_fold IS NULL OR end_fold IN (0,1)", name="ck_availability_end_fold"),
        CheckConstraint("created_revision >= 2", name="ck_availability_created_revision"),
        CheckConstraint("deactivated_revision IS NULL OR deactivated_revision > created_revision", name="ck_availability_deactivated_revision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    planning_profile_id: Mapped[str] = mapped_column(ForeignKey("planning_profiles.id"), nullable=False)
    weekday: Mapped[str] = mapped_column(String(9), nullable=False)
    start_local_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_local_time: Mapped[time] = mapped_column(Time, nullable=False)
    start_fold: Mapped[int | None] = mapped_column(Integer)
    end_fold: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    created_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    deactivated_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarConstraint(Base):
    __tablename__ = "calendar_constraints"
    __table_args__ = (
        CheckConstraint("constraint_type IN ('BLOCKED','AVAILABLE_ADDITION')", name="ck_calendar_constraint_type"),
        CheckConstraint("(starts_at IS NOT NULL AND ends_at IS NOT NULL AND local_date IS NULL) OR (starts_at IS NULL AND ends_at IS NULL AND local_date IS NOT NULL)", name="ck_calendar_constraint_shape"),
        CheckConstraint("starts_at IS NULL OR starts_at < ends_at", name="ck_calendar_constraint_order"),
        CheckConstraint("created_revision >= 2", name="ck_calendar_constraint_created_revision"),
        CheckConstraint("deactivated_revision IS NULL OR deactivated_revision > created_revision", name="ck_calendar_constraint_deactivated_revision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    planning_profile_id: Mapped[str] = mapped_column(ForeignKey("planning_profiles.id"), nullable=False)
    constraint_type: Mapped[str] = mapped_column(String(24), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[date | None] = mapped_column(Date)
    label: Mapped[str | None] = mapped_column(Text)
    created_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    deactivated_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaterialReleaseRule(AuditDerivedMixin, Base):
    __tablename__ = "material_release_rules"


class ClassificationProposal(Base):
    __tablename__ = "classification_proposals"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_classification_confidence"),
        CheckConstraint("(subject_source_artifact_id IS NOT NULL) + (subject_repository_source_id IS NOT NULL) + (subject_material_id IS NOT NULL) = 1", name="ck_classification_one_subject"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    proposal_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    subject_source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("source_artifacts.id"))
    subject_repository_source_id: Mapped[str | None] = mapped_column(ForeignKey("repository_sources.id"))
    subject_material_id: Mapped[str | None] = mapped_column(ForeignKey("materials.id"))
    classifier_name: Mapped[str] = mapped_column(String(80), nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(40), nullable=False)
    classifier_config_sha256: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    proposed_course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"))
    proposed_assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"))
    proposed_material_id: Mapped[str | None] = mapped_column(ForeignKey("materials.id"))
    proposed_material_stable_id: Mapped[str | None] = mapped_column(String(160))
    proposed_material_title: Mapped[str | None] = mapped_column(String(240))
    proposed_material_type: Mapped[str | None] = mapped_column(String(80))
    proposed_expectation_rule_id: Mapped[str | None] = mapped_column(ForeignKey("material_release_rules.id"))
    proposed_expectation_date: Mapped[date | None] = mapped_column(Date)
    proposed_expectation_identity: Mapped[str | None] = mapped_column(String(192))
    replaces_link_id: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_actor: Mapped[str | None] = mapped_column(String(40))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("classification_proposals.id"))


class RepositoryAcademicLink(Base):
    __tablename__ = "repository_academic_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    repository_source_id: Mapped[str] = mapped_column(ForeignKey("repository_sources.id"), nullable=False)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"))
    classification_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("classification_proposals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_link_id: Mapped[str | None] = mapped_column(ForeignKey("repository_academic_links.id"))


class MaterialExpectationLink(Base):
    __tablename__ = "material_expectation_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), nullable=False)
    release_rule_id: Mapped[str] = mapped_column(ForeignKey("material_release_rules.id"), nullable=False)
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    expectation_identity: Mapped[str] = mapped_column(String(192), nullable=False)
    rule_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))
    rule_source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    classification_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("classification_proposals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
    replacement_link_id: Mapped[str | None] = mapped_column(ForeignKey("material_expectation_links.id"))


class AssessmentRequirement(AuditDerivedMixin, Base):
    __tablename__ = "assessment_requirements"


class AuditFactRecord(Base, TimestampMixin):
    __tablename__ = "audit_fact_records"
    stable_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    information_status: Mapped[InformationStatus] = mapped_column(Enum(InformationStatus), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_references.id"))
    audit_revision_id: Mapped[str | None] = mapped_column(ForeignKey("semester_audits.id"))


class AuditResolutionRecord(Base):
    __tablename__ = "audit_resolution_records"
    __table_args__ = (UniqueConstraint("audit_revision_id", "entity_type", "stable_id",
                                      name="uq_audit_resolution_revision_fact"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    audit_revision_id: Mapped[str] = mapped_column(ForeignKey("semester_audits.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    stable_id: Mapped[str] = mapped_column(String(160), nullable=False)
    old_fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    new_fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    old_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    new_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    old_information_status: Mapped[InformationStatus] = mapped_column(Enum(InformationStatus), nullable=False)
    new_information_status: Mapped[InformationStatus] = mapped_column(Enum(InformationStatus), nullable=False)
    previous_audit_revision_id: Mapped[str] = mapped_column(ForeignKey("semester_audits.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_stored_file_path: Mapped[str] = mapped_column(Text, nullable=False)



class ScoringResultSnapshot(Base):
    """Immutable exact ScoringResultV1 input; deliberately not a LearningResultV2."""
    __tablename__ = "scoring_result_snapshots"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id"), nullable=False)
    handoff_id: Mapped[str] = mapped_column(ForeignKey("learning_handoffs.id"), nullable=False)
    handoff_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    handoff_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    allowed_skill_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ScoringReviewEvent(Base):
    __tablename__ = "scoring_review_events"
    __table_args__ = (UniqueConstraint("result_sha256", "version", name="uq_scoring_review_version"),
                      CheckConstraint("version > 0", name="ck_scoring_review_version"),
                      CheckConstraint("origin = 'LOCAL_GUI_CONFIRMATION'", name="ck_scoring_review_origin"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    result_sha256: Mapped[str] = mapped_column(ForeignKey("scoring_result_snapshots.sha256"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    decisions_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

class ScoringMasteryApplication(Base):
    __tablename__ = "scoring_mastery_applications"
    __table_args__ = (UniqueConstraint("result_sha256", "skill_id", name="uq_scoring_mastery_origin_skill"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    result_sha256: Mapped[str] = mapped_column(ForeignKey("scoring_result_snapshots.sha256"), nullable=False)
    review_event_id: Mapped[str] = mapped_column(ForeignKey("scoring_review_events.id"), nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("learning_sessions.id"), nullable=False)
    handoff_id: Mapped[str] = mapped_column(ForeignKey("learning_handoffs.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    score_points: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    previous_state_id: Mapped[str | None] = mapped_column(ForeignKey("mastery_states.id"))
    new_state_id: Mapped[str] = mapped_column(ForeignKey("mastery_states.id"), nullable=False, unique=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    origin: Mapped[str] = mapped_column(String(40), nullable=False)
