"""One-snapshot, zero-write material and source inbox."""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import select

from uniflow.application.academic_classification import rule_fingerprint
from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
from uniflow.infrastructure.database.models import (
    Assessment, ClassificationProposal, Course, Material, MaterialArtifact,
    MaterialExpectationLink, MaterialReleaseRule, ParseRun, RepositoryAcademicLink,
    RepositorySnapshot, RepositorySource, SemanticExtractionRun, SkillProposal,
    SkillProposalEvidence, SourceArtifact,
)
from uniflow.schemas.application_read_detail_v1 import (
    MaterialReadV1, MaterialVersionReadV1, ParseObservationV1, SourceArtifactReadV1,
)
from uniflow.schemas.material_inbox_read_v1 import (
    ClassificationProposalReadV1, ExpectationLinkReadV1, MaterialInboxV1,
    ProjectedExpectationReadV1, RepositoryAcademicLinkReadV1, RepositoryReadV1,
    RepositorySnapshotReadV1, SkillProposalReadV1, UnresolvedRuleReadV1,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _strict_json(raw: str | None, label: str, *, shape=None):
    if raw is None:
        return None
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("DUPLICATE_KEY")
                result[key] = value
            return result
        parsed = json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE")))
        if shape is not None and not isinstance(parsed, shape):
            raise ValueError("INVALID_SHAPE")
        return parsed
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}_INVALID") from exc


def _source_dtos(db, artifacts: list[SourceArtifact]) -> dict[str, SourceArtifactReadV1]:
    ids = [a.id for a in artifacts]
    runs = list(db.scalars(select(ParseRun).where(ParseRun.source_artifact_id.in_(ids)))) if ids else []
    by_source = defaultdict(list)
    for run in runs:
        by_source[run.source_artifact_id].append(run)
    result = {}
    for artifact in artifacts:
        relevant = sorted(by_source[artifact.id], key=lambda r: (
            -_utc(r.completed_at).timestamp(), -_utc(r.started_at).timestamp(),
            -r.attempt_number, r.id))
        observed = None
        if relevant:
            run = relevant[0]
            if run.status not in {"SUCCESS", "FAILED", "OCR_REQUIRED"}:
                raise ValueError("PARSE_STATUS_INVALID")
            warnings = _strict_json(run.warning_json, "PARSE_WARNINGS", shape=list)
            if warnings is not None and (len(warnings) > 100 or any(
                    not isinstance(v, str) or len(v) > 500 for v in warnings)):
                raise ValueError("PARSE_WARNINGS_INVALID")
            message = run.error_message
            observed = ParseObservationV1(source_artifact_id=artifact.id, parse_run_id=run.id,
                parser_name=run.parser_name, parser_version=run.parser_version,
                dependency_version=run.dependency_version, attempt_number=run.attempt_number,
                status=run.status, started_at=_utc(run.started_at), completed_at=_utc(run.completed_at),
                page_count=run.page_count, unit_count=run.unit_count, char_count=run.char_count,
                error_code=run.error_code, error_message_excerpt=message[:500] if message else None,
                error_message_truncated=len(message) > 500 if message else False,
                warnings=warnings, derived_artifact_id=run.derived_artifact_id)
        result[artifact.id] = SourceArtifactReadV1(artifact_id=artifact.id,
            sha256=artifact.sha256, original_filename=artifact.original_filename,
            extension=artifact.extension, mime_type=artifact.mime_type, size_bytes=artifact.size_bytes,
            created_at=_utc(artifact.created_at), parse_observation=observed)
    return result


def _material_dtos(materials: list[Material], links: list[MaterialArtifact],
                   artifacts: dict[str, SourceArtifactReadV1], courses: dict[str, Course]) -> dict[str, MaterialReadV1]:
    by_material = defaultdict(list)
    material_ids = {m.id for m in materials}
    for link in links:
        if link.material_id not in material_ids:
            raise ValueError("MATERIAL_VERSION_MATERIAL_MISSING")
        if link.artifact_id not in artifacts:
            raise ValueError("MATERIAL_ARTIFACT_SOURCE_MISSING")
        by_material[link.material_id].append(MaterialVersionReadV1(
            material_artifact_id=link.id, version_number=link.version_number,
            is_current=link.is_current, linked_at=_utc(link.linked_at),
            deactivated_at=_utc(link.deactivated_at), deactivation_reason=link.deactivation_reason,
            replacement_link_id=link.replacement_link_id,
            classification_proposal_id=link.classification_proposal_id,
            source_reference_id=link.source_id, source_artifact=artifacts[link.artifact_id]))
    result = {}
    for material in materials:
        if material.course_id is not None and material.course_id not in courses:
            raise ValueError("MATERIAL_COURSE_MISSING")
        versions = sorted(by_material[material.id], key=lambda v: (
            not v.is_current, -v.version_number, -v.linked_at.timestamp(), v.material_artifact_id))
        current = [v.material_artifact_id for v in versions if v.is_current]
        if len(current) > 1:
            raise ValueError("MATERIAL_MULTIPLE_CURRENT_VERSIONS")
        result[material.id] = MaterialReadV1(material_id=material.id, stable_id=material.stable_id,
            course_id=material.course_id,
            course_stable_id=courses[material.course_id].stable_id if material.course_id else None,
            title=material.title, material_type=material.material_type,
            lifecycle_state=material.lifecycle_state.value, source_reference_id=material.source_id,
            current_version_id=current[0] if current else None, versions=versions)
    return result


def _classification_dto(row: ClassificationProposal) -> ClassificationProposalReadV1:
    evidence = _strict_json(row.evidence_json, "CLASSIFICATION_EVIDENCE", shape=(dict, list))
    canonical_evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8") if evidence is not None else None
    return ClassificationProposalReadV1(proposal_id=row.id, proposal_kind=row.proposal_kind,
        status=row.status, created_at=_utc(row.created_at),
        subject_source_artifact_id=row.subject_source_artifact_id,
        subject_repository_source_id=row.subject_repository_source_id,
        subject_material_id=row.subject_material_id, proposed_course_id=row.proposed_course_id,
        proposed_assessment_id=row.proposed_assessment_id,
        proposed_material_id=row.proposed_material_id,
        proposed_material_stable_id=row.proposed_material_stable_id,
        proposed_material_title=row.proposed_material_title,
        proposed_material_type=row.proposed_material_type,
        proposed_expectation_rule_id=row.proposed_expectation_rule_id,
        proposed_expectation_date=row.proposed_expectation_date,
        proposed_expectation_identity=row.proposed_expectation_identity,
        classifier_name=row.classifier_name, classifier_version=row.classifier_version,
        classifier_config_sha256=row.classifier_config_sha256, confidence=row.confidence,
        evidence_json_sha256=hashlib.sha256(canonical_evidence).hexdigest() if canonical_evidence else None,
        evidence_json_bytes=len(canonical_evidence) if canonical_evidence else None,
        replaces_link_id=row.replaces_link_id, decided_at=_utc(row.decided_at),
        decision_actor=row.decision_actor, decision_reason=row.decision_reason,
        superseded_by_proposal_id=row.superseded_by_proposal_id)


def _skill_dto(row: SkillProposal, spans: list[str]) -> SkillProposalReadV1:
    return SkillProposalReadV1(proposal_id=row.id, proposal_kind=row.proposal_kind,
        status=row.status, created_at=_utc(row.created_at),
        extraction_run_id=row.extraction_run_id, extraction_attempt_id=row.extraction_attempt_id,
        evidence_span_ids=spans, confidence=row.confidence,
        proposed_skill_id=row.proposed_skill_id, proposed_name=row.proposed_name,
        proposed_description=row.proposed_description, proposed_kind=row.proposed_kind,
        proposed_domain=row.proposed_domain, target_skill_id=row.target_skill_id,
        prerequisite_skill_id=row.prerequisite_skill_id, dependent_skill_id=row.dependent_skill_id,
        course_id=row.course_id, proposed_alias=row.proposed_alias,
        accepted_skill_id=row.accepted_skill_id, accepted_alias_id=row.accepted_alias_id,
        accepted_dependency_id=row.accepted_dependency_id,
        accepted_course_skill_id=row.accepted_course_skill_id, decision_action=row.decision_action,
        decision_actor=row.decision_actor, decided_at=_utc(row.decided_at),
        decision_reason=row.decision_reason,
        superseded_by_proposal_id=row.superseded_by_proposal_id)


def _snapshot_dto(row: RepositorySnapshot) -> RepositorySnapshotReadV1:
    return RepositorySnapshotReadV1(snapshot_id=row.id, captured_at=_utc(row.captured_at),
        snapshot_identity=row.snapshot_identity, policy_version=row.policy_version,
        include_untracked=row.include_untracked, head_commit_oid=row.head_commit_oid,
        branch_name=row.branch_name, is_detached=row.is_detached, is_dirty=row.is_dirty,
        tracked_file_count=row.tracked_file_count, untracked_file_count=row.untracked_file_count,
        deleted_file_count=row.deleted_file_count, blob_count=row.blob_count,
        build_system_hint=row.build_system_hint, status=row.status)


def _repository_dtos(repos: list[RepositorySource], snapshots: list[RepositorySnapshot],
                     links: list[RepositoryAcademicLink], courses: dict[str, Course],
                     assessments: dict[str, Assessment]) -> dict[str, RepositoryReadV1]:
    by_repo_snapshots = defaultdict(list)
    by_repo_links = defaultdict(list)
    repo_ids = {r.id for r in repos}
    for snapshot in snapshots:
        if snapshot.repository_source_id not in repo_ids:
            raise ValueError("REPOSITORY_SNAPSHOT_SOURCE_MISSING")
        by_repo_snapshots[snapshot.repository_source_id].append(snapshot)
    for link in links:
        if link.repository_source_id not in repo_ids:
            raise ValueError("REPOSITORY_LINK_SOURCE_MISSING")
        if link.course_id not in courses:
            raise ValueError("REPOSITORY_LINK_COURSE_MISSING")
        if link.assessment_id and (link.assessment_id not in assessments or
                assessments[link.assessment_id].course_id != link.course_id):
            raise ValueError("REPOSITORY_LINK_ASSESSMENT_INVALID")
        by_repo_links[link.repository_source_id].append(RepositoryAcademicLinkReadV1(
            link_id=link.id, course_id=link.course_id,
            course_stable_id=courses[link.course_id].stable_id,
            assessment_id=link.assessment_id,
            assessment_stable_id=assessments[link.assessment_id].stable_id if link.assessment_id else None,
            is_active=link.is_active,
            created_at=_utc(link.created_at), deactivated_at=_utc(link.deactivated_at),
            deactivation_reason=link.deactivation_reason,
            replacement_link_id=link.replacement_link_id,
            classification_proposal_id=link.classification_proposal_id))
    result = {}
    for repo in repos:
        if repo.course_id is not None and repo.course_id not in courses:
            raise ValueError("REPOSITORY_COURSE_MISSING")
        if repo.assessment_id and (repo.assessment_id not in assessments or
                assessments[repo.assessment_id].course_id != repo.course_id):
            raise ValueError("REPOSITORY_ASSESSMENT_INVALID")
        history = sorted(by_repo_snapshots[repo.id], key=lambda s: (-_utc(s.captured_at).timestamp(), s.id))
        academic = sorted(by_repo_links[repo.id], key=lambda l: (
            not l.is_active, -l.created_at.timestamp(), l.link_id))
        if sum(l.is_active for l in academic) > 1:
            raise ValueError("REPOSITORY_MULTIPLE_ACTIVE_LINKS")
        result[repo.id] = RepositoryReadV1(repository_source_id=repo.id,
            stable_id=repo.stable_id, provider_hint=repo.provider_hint,
            primary_course_id=repo.course_id,
            primary_course_stable_id=courses[repo.course_id].stable_id if repo.course_id else None,
            primary_assessment_id=repo.assessment_id,
            primary_assessment_stable_id=assessments[repo.assessment_id].stable_id if repo.assessment_id else None,
            created_at=_utc(repo.created_at),
            last_seen_at=_utc(repo.last_seen_at),
            latest_snapshot=_snapshot_dto(history[0]) if history else None,
            snapshot_count=len(history), academic_links=academic)
    return result


def get_material_inbox(config, semester_id: str, *, as_of: datetime,
                       from_date: date, through_date: date,
                       course_id: str | None = None) -> MaterialInboxV1:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if type(from_date) is not date or type(through_date) is not date or from_date > through_date:
        raise ValueError("INVALID_MATERIAL_INBOX_RANGE")
    if (through_date - from_date).days >= 366:
        raise ValueError("MATERIAL_INBOX_RANGE_TOO_LARGE")
    with _read_transaction(config) as db:
        semester, _, local, _, summary = _context(db, semester_id, as_of)
        all_courses = {c.id: c for c in db.scalars(select(Course))}
        semester_courses = {cid: c for cid, c in all_courses.items() if c.semester_id == semester.id}
        selected = next((c for c in semester_courses.values() if c.stable_id == course_id), None) if course_id else None
        if course_id is not None and selected is None:
            raise ValueError("UNKNOWN_COURSE_IN_SEMESTER")
        selected_ids = {selected.id} if selected else set(semester_courses)
        # Global rows below are used only for relationship-based scope discovery.
        # Construct strict detail DTOs after selecting the requested Course(s).
        all_materials = list(db.scalars(select(Material)))
        material_by_id = {m.id: m for m in all_materials}
        all_links = list(db.scalars(select(MaterialArtifact)))
        materials = [m for m in all_materials if m.course_id in selected_ids or
            (selected is None and m.course_id is None)]
        material_ids = {m.id for m in materials}
        links = [link for link in all_links if link.material_id in material_ids]
        linked_source_ids = {link.artifact_id for link in all_links}
        needed_source_ids = {link.artifact_id for link in links}
        all_artifacts = list(db.scalars(select(SourceArtifact)))
        if selected is None:
            needed_source_ids.update(a.id for a in all_artifacts if a.id not in linked_source_ids)
        artifacts = [a for a in all_artifacts if a.id in needed_source_ids]
        source_dtos = _source_dtos(db, artifacts)
        material_dtos = _material_dtos(materials, links, source_dtos, all_courses)
        all_repos = list(db.scalars(select(RepositorySource)))
        all_repo_links = list(db.scalars(select(RepositoryAcademicLink)))
        active_repo_ids = {link.repository_source_id for link in all_repo_links if link.is_active}
        selected_repo_ids = {link.repository_source_id for link in all_repo_links
            if link.is_active and link.course_id in selected_ids}
        if selected is None:
            # A repository with historical academic scope is not a project-wide
            # unscoped observation merely because its active link ended.
            historically_linked = {link.repository_source_id for link in all_repo_links}
            selected_repo_ids.update(repo.id for repo in all_repos
                if repo.id not in active_repo_ids and repo.id not in historically_linked
                and repo.course_id is None and repo.assessment_id is None)
        repos = [repo for repo in all_repos if repo.id in selected_repo_ids]
        repo_links = [link for link in all_repo_links if link.repository_source_id in selected_repo_ids]
        assessments = {a.id: a for a in db.scalars(select(Assessment))}
        snapshots = list(db.scalars(select(RepositorySnapshot).where(
            RepositorySnapshot.repository_source_id.in_(selected_repo_ids)))) if selected_repo_ids else []
        repo_dtos = _repository_dtos(repos, snapshots,
            repo_links, all_courses, assessments)
        material_by_course = defaultdict(set)
        source_courses = defaultdict(set)
        for material in all_materials:
            if material.course_id:
                material_by_course[material.id].add(material.course_id)
        for link in all_links:
            source_courses[link.artifact_id].update(material_by_course[link.material_id])
        repo_courses = defaultdict(set)
        for link in all_repo_links:
            repo_courses[link.repository_source_id].add(link.course_id)
        manifest = _generate_material_manifest_in_transaction(db, semester_id,
            from_date, through_date, as_of, course_id)
        expectations = [e for e in manifest.expectations if e.course_id in {
            semester_courses[cid].stable_id for cid in selected_ids}]
        unresolved = [u for u in manifest.unresolved_rules if u.course_id in {
            semester_courses[cid].stable_id for cid in selected_ids}]
        projected_ids = {e.id for e in expectations}
        rules = {r.id: r for r in db.scalars(select(MaterialReleaseRule))}
        stored_expectation_links = list(db.scalars(select(MaterialExpectationLink)))
        expectation_dtos = []
        active_by_expectation = defaultdict(list)
        for link in stored_expectation_links:
            material = material_by_id.get(link.material_id)
            if material is None or material.course_id not in selected_ids:
                continue
            rule = rules.get(link.release_rule_id)
            if rule is None or rule.course_id != material.course_id:
                raise ValueError("EXPECTATION_LINK_PROVENANCE_INVALID")
            if link.expectation_identity != f"{rule.stable_id}@{link.occurrence_date.isoformat()}":
                raise ValueError("EXPECTATION_LINK_IDENTITY_INVALID")
            current_fingerprint = rule_fingerprint(rule)
            dto = ExpectationLinkReadV1(link_id=link.id, material_id=link.material_id,
                release_rule_id=rule.id, occurrence_date=link.occurrence_date,
                expectation_identity=link.expectation_identity,
                stored_rule_fingerprint=link.rule_fingerprint,
                current_rule_fingerprint=current_fingerprint,
                fingerprint_status="CURRENT" if link.rule_fingerprint == current_fingerprint else "STALE_RULE",
                is_active=link.is_active, created_at=_utc(link.created_at),
                deactivated_at=_utc(link.deactivated_at), deactivation_reason=link.deactivation_reason,
                replacement_link_id=link.replacement_link_id,
                audit_revision_id=link.audit_revision_id, rule_source_id=link.rule_source_id,
                classification_proposal_id=link.classification_proposal_id,
                projected_in_range=link.expectation_identity in projected_ids)
            expectation_dtos.append(dto)
            if link.is_active:
                active_by_expectation[link.expectation_identity].append(link.id)
        expectation_dtos.sort(key=lambda x: (x.occurrence_date, x.expectation_identity, x.link_id))
        expectation_items = [ProjectedExpectationReadV1(expectation_id=e.id,
            rule_stable_id=e.rule_id, course_stable_id=e.course_id,
            material_kind=e.material_kind, release_date=e.release_date,
            release_time=e.release_time, information_status=e.information_status,
            manifest_state=e.state,
            explicit_link_state="LINKED" if active_by_expectation[e.id] else "NO_EXPLICIT_UNIFLOW_LINK",
            active_link_ids=sorted(active_by_expectation[e.id])) for e in expectations]
        unresolved_items = [UnresolvedRuleReadV1(rule_stable_id=u.rule_id,
            course_stable_id=u.course_id, material_kind=u.material_kind,
            information_status=u.information_status, reason=u.reason) for u in unresolved]
        classification = list(db.scalars(select(ClassificationProposal)))
        pending_c, decided_c, global_c = [], [], []
        for row in classification:
            cids = set()
            for cid in (row.proposed_course_id,
                        material_by_id[row.subject_material_id].course_id if row.subject_material_id in material_by_id else None,
                        material_by_id[row.proposed_material_id].course_id if row.proposed_material_id in material_by_id else None,
                        rules[row.proposed_expectation_rule_id].course_id if row.proposed_expectation_rule_id in rules else None,
                        assessments[row.proposed_assessment_id].course_id if row.proposed_assessment_id in assessments else None):
                if cid:
                    cids.add(cid)
            if row.subject_source_artifact_id:
                cids.update(source_courses[row.subject_source_artifact_id])
            if row.subject_repository_source_id:
                cids.update(repo_courses[row.subject_repository_source_id])
            if not cids:
                if selected is None:
                    global_c.append(_classification_dto(row))
                continue
            if not cids & selected_ids:
                continue
            (pending_c if row.status == "PENDING" else decided_c).append(_classification_dto(row))
        runs = {r.id: r for r in db.scalars(select(SemanticExtractionRun))}
        academic_links = {r.id: r for r in all_repo_links}
        spans = defaultdict(list)
        for pair in db.scalars(select(SkillProposalEvidence)):
            spans[pair.proposal_id].append(pair.evidence_span_id)
        pending_s, decided_s, global_s = [], [], []
        for row in db.scalars(select(SkillProposal)):
            cids = {row.course_id} if row.course_id else set()
            run = runs.get(row.extraction_run_id)
            if run is not None and run.material_artifact_id:
                ma = next((link for link in all_links if link.id == run.material_artifact_id), None)
                if ma and ma.material_id in material_by_id and material_by_id[ma.material_id].course_id:
                    cids.add(material_by_id[ma.material_id].course_id)
            if run is not None and run.repository_academic_link_id in academic_links:
                cids.add(academic_links[run.repository_academic_link_id].course_id)
            if not cids:
                if selected is None:
                    if run is None:
                        raise ValueError("SKILL_PROPOSAL_RUN_MISSING")
                    global_s.append(_skill_dto(row, sorted(set(spans[row.id]))))
                continue
            if cids & selected_ids:
                if run is None:
                    raise ValueError("SKILL_PROPOSAL_RUN_MISSING")
                (pending_s if row.status == "PENDING" else decided_s).append(
                    _skill_dto(row, sorted(set(spans[row.id]))))
        def proposal_order(p):
            return (_utc(p.decided_at or p.created_at), p.proposal_kind, p.proposal_id)
        for group in (pending_c, decided_c, global_c, pending_s, decided_s, global_s):
            group.sort(key=proposal_order)
        def material_order(m):
            return (m.course_stable_id is None, m.course_stable_id or "",
                    m.stable_id or m.title, m.material_id)
        chosen_materials = sorted((material_dtos[m.id] for m in materials if m.course_id in selected_ids),
            key=material_order)
        chosen_repos = sorted((repo_dtos[r.id] for r in repos if any(
            link.is_active and link.course_id in selected_ids for link in all_repo_links
            if link.repository_source_id == r.id)), key=lambda r: (r.stable_id, r.repository_source_id))
        global_artifacts = sorted((source_dtos[a.id] for a in artifacts if a.id not in linked_source_ids),
            key=lambda a: (a.sha256, a.artifact_id)) if selected is None else []
        global_materials = sorted((material_dtos[m.id] for m in materials if m.course_id is None),
            key=material_order) if selected is None else []
        global_repos = sorted((repo_dtos[r.id] for r in repos if not any(
            link.repository_source_id == r.id for link in all_repo_links)
            and r.course_id is None and r.assessment_id is None),
            key=lambda r: (r.stable_id, r.repository_source_id)) if selected is None else []
        return MaterialInboxV1(as_of=local, semester=summary, course_filter_stable_id=course_id,
            from_date=from_date, through_date=through_date, materials=chosen_materials,
            pending_classification_proposals=pending_c, decided_classification_proposals=decided_c,
            pending_skill_proposals=pending_s, decided_skill_proposals=decided_s,
            projected_expectations=expectation_items, unresolved_rules=unresolved_items,
            expectation_links=expectation_dtos, repositories=chosen_repos,
            project_wide_unassigned_artifacts=global_artifacts,
            project_wide_unscoped_materials=global_materials,
            project_wide_unlinked_repositories=global_repos,
            project_wide_unscoped_classification_proposals=global_c,
            project_wide_unscoped_skill_proposals=global_s)
