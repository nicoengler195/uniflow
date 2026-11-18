"""Strict Phase 4A2B1 material inbox contract."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.application_read_detail_v1 import MaterialReadV1, SourceArtifactReadV1
from uniflow.schemas.application_read_v1 import ReadDiagnosticV1, SemesterSummaryV1, StrictReadModel


class _AwareDetailModel(StrictReadModel):
    @model_validator(mode="after")
    def timestamps_aware(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and value.utcoffset() is None:
                raise ValueError(f"{name.upper()}_MUST_BE_AWARE")
        return self


class ClassificationProposalReadV1(_AwareDetailModel):
    proposal_id: str
    proposal_kind: str
    status: Literal["PENDING", "ACCEPTED", "REJECTED", "SUPERSEDED"]
    created_at: datetime
    subject_source_artifact_id: str | None
    subject_repository_source_id: str | None
    subject_material_id: str | None
    proposed_course_id: str | None
    proposed_assessment_id: str | None
    proposed_material_id: str | None
    proposed_material_stable_id: str | None
    proposed_material_title: str | None
    proposed_material_type: str | None
    proposed_expectation_rule_id: str | None
    proposed_expectation_date: date | None
    proposed_expectation_identity: str | None
    classifier_name: str
    classifier_version: str
    classifier_config_sha256: str | None
    confidence: float | None = Field(ge=0, le=1)
    evidence_json_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_json_bytes: int | None = Field(default=None, ge=0)
    replaces_link_id: str | None
    decided_at: datetime | None
    decision_actor: str | None
    decision_reason: str | None
    superseded_by_proposal_id: str | None

    @model_validator(mode="after")
    def evidence_shape(self):
        if (self.evidence_json_sha256 is None) != (self.evidence_json_bytes is None):
            raise ValueError("CLASSIFICATION_EVIDENCE_SHAPE_INVALID")
        return self


class SkillProposalReadV1(_AwareDetailModel):
    proposal_id: str
    proposal_kind: Literal["NEW_SKILL", "EXISTING_SKILL_EVIDENCE", "SKILL_ALIAS", "SKILL_DEPENDENCY", "SKILL_COURSE_ASSOCIATION"]
    status: Literal["PENDING", "ACCEPTED", "REJECTED", "SUPERSEDED"]
    created_at: datetime
    extraction_run_id: str
    extraction_attempt_id: str | None
    evidence_span_ids: list[str]
    confidence: float | None = Field(ge=0, le=1)
    proposed_skill_id: str | None
    proposed_name: str | None
    proposed_description: str | None
    proposed_kind: str | None
    proposed_domain: str | None
    target_skill_id: str | None
    prerequisite_skill_id: str | None
    dependent_skill_id: str | None
    course_id: str | None
    proposed_alias: str | None
    accepted_skill_id: str | None
    accepted_alias_id: str | None
    accepted_dependency_id: str | None
    accepted_course_skill_id: str | None
    decision_action: str | None
    decision_actor: str | None
    decided_at: datetime | None
    decision_reason: str | None
    superseded_by_proposal_id: str | None


class ProjectedExpectationReadV1(_AwareDetailModel):
    expectation_id: str
    rule_stable_id: str
    course_stable_id: str
    material_kind: str
    release_date: date
    release_time: time | None
    information_status: str
    manifest_state: Literal["UPCOMING", "NOT_LINKED_IN_UNIFLOW"]
    explicit_link_state: Literal["LINKED", "NO_EXPLICIT_UNIFLOW_LINK"]
    active_link_ids: list[str]

    @model_validator(mode="after")
    def coherent(self):
        if self.expectation_id != f"{self.rule_stable_id}@{self.release_date.isoformat()}":
            raise ValueError("EXPECTATION_IDENTITY_INVALID")
        if len(self.active_link_ids) != len(set(self.active_link_ids)) or self.active_link_ids != sorted(self.active_link_ids):
            raise ValueError("EXPECTATION_LINK_IDS_INVALID")
        if (self.explicit_link_state == "LINKED") != bool(self.active_link_ids):
            raise ValueError("EXPECTATION_LINK_STATE_INVALID")
        return self


class UnresolvedRuleReadV1(_AwareDetailModel):
    rule_stable_id: str
    course_stable_id: str
    material_kind: str
    information_status: str
    reason: str
    state: Literal["UNRESOLVED_RULE"] = "UNRESOLVED_RULE"


class ExpectationLinkReadV1(_AwareDetailModel):
    link_id: str
    material_id: str
    release_rule_id: str
    occurrence_date: date
    expectation_identity: str
    stored_rule_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_rule_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint_status: Literal["CURRENT", "STALE_RULE"]
    is_active: bool
    created_at: datetime
    deactivated_at: datetime | None
    deactivation_reason: str | None
    replacement_link_id: str | None
    audit_revision_id: str | None
    rule_source_id: str | None
    classification_proposal_id: str | None
    projected_in_range: bool

    @model_validator(mode="after")
    def coherent(self):
        if (self.stored_rule_fingerprint == self.current_rule_fingerprint) != (self.fingerprint_status == "CURRENT"):
            raise ValueError("EXPECTATION_FINGERPRINT_STATUS_INVALID")
        return self


class RepositorySnapshotReadV1(_AwareDetailModel):
    snapshot_id: str
    captured_at: datetime
    snapshot_identity: str
    policy_version: str
    include_untracked: bool
    head_commit_oid: str | None
    branch_name: str | None
    is_detached: bool
    is_dirty: bool
    tracked_file_count: int = Field(ge=0)
    untracked_file_count: int = Field(ge=0)
    deleted_file_count: int = Field(ge=0)
    blob_count: int = Field(ge=0)
    build_system_hint: str
    status: str


class RepositoryAcademicLinkReadV1(_AwareDetailModel):
    link_id: str
    course_id: str
    course_stable_id: str
    assessment_id: str | None
    assessment_stable_id: str | None
    is_active: bool
    created_at: datetime
    deactivated_at: datetime | None
    deactivation_reason: str | None
    replacement_link_id: str | None
    classification_proposal_id: str | None

    @model_validator(mode="after")
    def coherent(self):
        if self.assessment_id is None and self.assessment_stable_id is not None:
            raise ValueError("REPOSITORY_LINK_ASSESSMENT_SHAPE_INVALID")
        return self


class RepositoryReadV1(_AwareDetailModel):
    repository_source_id: str
    stable_id: str
    provider_hint: str | None
    primary_course_id: str | None
    primary_course_stable_id: str | None
    primary_assessment_id: str | None
    primary_assessment_stable_id: str | None
    created_at: datetime
    last_seen_at: datetime
    latest_snapshot: RepositorySnapshotReadV1 | None
    snapshot_count: int = Field(ge=0)
    academic_links: list[RepositoryAcademicLinkReadV1]

    @model_validator(mode="after")
    def coherent(self):
        if (self.primary_course_id is None) != (self.primary_course_stable_id is None):
            raise ValueError("REPOSITORY_COURSE_SHAPE_INVALID")
        if self.primary_assessment_id is None and self.primary_assessment_stable_id is not None:
            raise ValueError("REPOSITORY_ASSESSMENT_SHAPE_INVALID")
        if self.snapshot_count == 0 and self.latest_snapshot is not None:
            raise ValueError("REPOSITORY_SNAPSHOT_SHAPE_INVALID")
        if self.snapshot_count > 0 and self.latest_snapshot is None:
            raise ValueError("REPOSITORY_SNAPSHOT_SHAPE_INVALID")
        ids = [x.link_id for x in self.academic_links]
        if len(ids) != len(set(ids)) or sum(x.is_active for x in self.academic_links) > 1:
            raise ValueError("REPOSITORY_LINKS_INVALID")
        return self


class MaterialInboxV1(_AwareDetailModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["material-inbox-v1"] = "material-inbox-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    course_filter_stable_id: str | None
    from_date: date
    through_date: date
    materials: list[MaterialReadV1]
    pending_classification_proposals: list[ClassificationProposalReadV1]
    decided_classification_proposals: list[ClassificationProposalReadV1]
    pending_skill_proposals: list[SkillProposalReadV1]
    decided_skill_proposals: list[SkillProposalReadV1]
    projected_expectations: list[ProjectedExpectationReadV1]
    unresolved_rules: list[UnresolvedRuleReadV1]
    expectation_links: list[ExpectationLinkReadV1]
    repositories: list[RepositoryReadV1]
    project_wide_unassigned_artifacts: list[SourceArtifactReadV1]
    project_wide_unscoped_materials: list[MaterialReadV1]
    project_wide_unlinked_repositories: list[RepositoryReadV1]
    project_wide_unscoped_classification_proposals: list[ClassificationProposalReadV1]
    project_wide_unscoped_skill_proposals: list[SkillProposalReadV1]
    diagnostics: list[ReadDiagnosticV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.as_of.utcoffset() is None or type(self.from_date) is not date or type(self.through_date) is not date:
            raise ValueError("MATERIAL_INBOX_TIME_INVALID")
        if self.from_date > self.through_date or (self.through_date - self.from_date).days >= 366:
            raise ValueError("INVALID_MATERIAL_INBOX_RANGE")
        if self.course_filter_stable_id is not None and any((self.project_wide_unassigned_artifacts,
                self.project_wide_unscoped_materials, self.project_wide_unlinked_repositories,
                self.project_wide_unscoped_classification_proposals,
                self.project_wide_unscoped_skill_proposals)):
            raise ValueError("COURSE_FILTER_HAS_PROJECT_WIDE_ITEMS")
        for pending, decided, project_wide in (
                (self.pending_classification_proposals, self.decided_classification_proposals,
                    self.project_wide_unscoped_classification_proposals),
                (self.pending_skill_proposals, self.decided_skill_proposals,
                    self.project_wide_unscoped_skill_proposals)):
            ids = [p.proposal_id for p in pending + decided + project_wide]
            if (len(ids) != len(set(ids)) or any(p.status != "PENDING" for p in pending)
                    or any(p.status == "PENDING" for p in decided)):
                raise ValueError("PROPOSAL_SECTION_INVALID")
        material_ids = [m.material_id for m in self.materials + self.project_wide_unscoped_materials]
        if len(material_ids) != len(set(material_ids)):
            raise ValueError("MATERIAL_SECTION_DUPLICATE")
        if len({x.link_id for x in self.expectation_links}) != len(self.expectation_links):
            raise ValueError("EXPECTATION_LINK_DUPLICATE")
        repo_ids = [r.repository_source_id for r in self.repositories + self.project_wide_unlinked_repositories]
        if len(repo_ids) != len(set(repo_ids)):
            raise ValueError("REPOSITORY_SECTION_DUPLICATE")
        expectation_ids = [e.expectation_id for e in self.projected_expectations]
        if len(expectation_ids) != len(set(expectation_ids)):
            raise ValueError("EXPECTATION_SECTION_DUPLICATE")
        unresolved_ids = [u.rule_stable_id for u in self.unresolved_rules]
        if len(unresolved_ids) != len(set(unresolved_ids)):
            raise ValueError("UNRESOLVED_RULE_DUPLICATE")
        artifact_ids = [a.artifact_id for a in self.project_wide_unassigned_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("PROJECT_ARTIFACT_DUPLICATE")
        if self.course_filter_stable_id is not None and any(
                m.course_stable_id != self.course_filter_stable_id for m in self.materials):
            raise ValueError("MATERIAL_COURSE_FILTER_INVALID")
        for name, rows, key in (
            ("MATERIALS", self.materials, lambda x: (x.course_stable_id is None,
                x.course_stable_id or "", x.stable_id or x.title, x.material_id)),
            ("PROJECT_MATERIALS", self.project_wide_unscoped_materials,
                lambda x: (x.course_stable_id is None, x.course_stable_id or "", x.stable_id or x.title, x.material_id)),
            ("CLASSIFICATION_PENDING", self.pending_classification_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("CLASSIFICATION_DECIDED", self.decided_classification_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("SKILL_PENDING", self.pending_skill_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("SKILL_DECIDED", self.decided_skill_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("PROJECT_CLASSIFICATION", self.project_wide_unscoped_classification_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("PROJECT_SKILL", self.project_wide_unscoped_skill_proposals,
                lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id)),
            ("EXPECTATIONS", self.projected_expectations,
                lambda x: (x.release_date, x.release_time or time.min, x.expectation_id)),
            ("UNRESOLVED_RULES", self.unresolved_rules,
                lambda x: x.rule_stable_id),
            ("LINKS", self.expectation_links,
                lambda x: (x.occurrence_date, x.expectation_identity, x.link_id)),
            ("REPOSITORIES", self.repositories, lambda x: (x.stable_id, x.repository_source_id)),
            ("PROJECT_REPOSITORIES", self.project_wide_unlinked_repositories,
                lambda x: (x.stable_id, x.repository_source_id)),
            ("PROJECT_ARTIFACTS", self.project_wide_unassigned_artifacts,
                lambda x: (x.sha256, x.artifact_id)),
        ):
            if rows != sorted(rows, key=key):
                raise ValueError(f"{name}_ORDER_INVALID")
        return self


def canonical_material_inbox_json(value: MaterialInboxV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")
