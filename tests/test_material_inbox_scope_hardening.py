"""Unrelated canonical rows cannot poison a scoped material read."""
from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from test_material_inbox_read_models import AT, DAY, _parse, _populate, _source, project
from uniflow.application.material_inbox_read_models import get_material_inbox
from uniflow.domain.enums import InformationStatus, MaterialState, SemesterState
from uniflow.infrastructure.database.models import (
    Assessment, Course, Material, MaterialArtifact, MaterialExpectationLink,
    MaterialReleaseRule, ParseRun, RepositoryAcademicLink, RepositorySnapshot,
    RepositorySource, Semester, SourceArtifact,
)
from uniflow.schemas.material_inbox_read_v1 import MaterialInboxV1, canonical_material_inbox_json


def inbox(config, course_id=None):
    return get_material_inbox(config, "S", as_of=AT, from_date=DAY,
        through_date=DAY, course_id=course_id)


def _material(db, mid, course, source, *, bad=False):
    db.add(Material(id=mid, course_id=course, stable_id=mid.upper(), title=mid,
        lifecycle_state=MaterialState.DOWNLOADED))
    db.add(MaterialArtifact(id=f"{mid}-link", material_id=mid, artifact_id=source,
        version_number=1, is_current=True, linked_at=AT,
        deactivated_at=AT if bad else None))


def test_course_and_project_wide_source_scope(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([_source("a", "a"), _source("b", "b"), _source("global", "c")])
        _material(db, "ma", "c1", "a")
        _material(db, "mb", "c2", "b")
        db.add(_parse("b", "bad-b", "FAILED", minute=1, error="PDF_ENCRYPTED",
            warnings="{broken"))
        db.add(_parse("global", "bad-global", "FAILED", minute=2,
            error="PDF_ENCRYPTED", warnings="{broken"))
    assert [m.material_id for m in inbox(config, "C1").materials] == ["ma"]
    with pytest.raises(ValueError, match="PARSE_WARNINGS_INVALID"):
        inbox(config)
    with factory.begin() as db:
        db.get(ParseRun, "bad-b").warning_json = None
    assert [m.material_id for m in inbox(config, "C1").materials] == ["ma"]
    with pytest.raises(ValueError, match="PARSE_WARNINGS_INVALID"):
        inbox(config)  # Project-wide never-assigned artifact is now in scope.
    with factory.begin() as db:
        db.get(ParseRun, "bad-global").warning_json = None
        db.add(MaterialArtifact(id="shared", material_id="ma", artifact_id="b",
            version_number=2, is_current=False, linked_at=AT))
    with pytest.raises(ValueError, match="PARSE_WARNINGS_INVALID"):
        with factory.begin() as db:
            db.get(ParseRun, "bad-b").warning_json = "{broken"
        inbox(config, "C1")  # Shared physical bytes are genuinely in scope.


def test_foreign_semester_artifact_isolation(project):
    config, factory = project
    with factory.begin() as db:
        db.add(Semester(id="foreign-sem", stable_id="FOREIGN", name="Foreign",
            state=SemesterState.ACTIVE, timezone="Europe/Berlin"))
        db.add(Course(id="foreign-course", semester_id="foreign-sem",
            stable_id="FOREIGN_C", name="Foreign Course"))
        db.add(_source("foreign-source", "d"))
        _material(db, "foreign-material", "foreign-course", "foreign-source")
        db.add(_parse("foreign-source", "foreign-parse", "FAILED", minute=1,
            error="PDF_ENCRYPTED", warnings="{broken"))
    assert not inbox(config).materials
    assert not inbox(config).project_wide_unassigned_artifacts
    with pytest.raises(ValueError, match="PARSE_WARNINGS_INVALID"):
        get_material_inbox(config, "FOREIGN", as_of=AT, from_date=DAY, through_date=DAY)


def test_material_detail_isolation(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([_source("a", "a"), _source("b", "b")])
        _material(db, "ma", "c1", "a")
        _material(db, "mb", "c2", "b", bad=True)
    assert len(inbox(config, "C1").materials) == 1
    with pytest.raises(ValueError, match="CURRENT_VERSION_DEACTIVATED"):
        inbox(config, "C2")


def test_repository_detail_isolation(project):
    config, factory = project
    with factory.begin() as db:
        db.add(Assessment(id="ass-a", stable_id="ASS_A", course_id="c1", name="Assessment"))
        db.add_all([
            RepositorySource(id="repo-a", stable_id="REPO_A", local_path="C:/repo/a",
                course_id="c1", created_at=AT, last_seen_at=AT),
            RepositorySource(id="repo-b", stable_id="REPO_B", local_path="C:/repo/b",
                course_id="c2", assessment_id="ass-a", created_at=AT, last_seen_at=AT),
        ])
        db.flush()
        db.add_all([
            RepositoryAcademicLink(id="link-a", repository_source_id="repo-a", course_id="c1",
                is_active=True, created_at=AT),
            RepositoryAcademicLink(id="link-b", repository_source_id="repo-b", course_id="c2",
                is_active=True, created_at=AT),
        ])
    assert [r.stable_id for r in inbox(config, "C1").repositories] == ["REPO_A"]
    with pytest.raises(ValueError, match="REPOSITORY_ASSESSMENT_INVALID"):
        inbox(config, "C2")


def test_expectation_link_scope_before_integrity(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([
            Material(id="ma", course_id="c1", stable_id="MA", title="A",
                lifecycle_state=MaterialState.EXPECTED),
            Material(id="mb", course_id="c2", stable_id="MB", title="B",
                lifecycle_state=MaterialState.EXPECTED),
            MaterialReleaseRule(id="rule-a", stable_id="RULE_A", course_id="c1",
                payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","material_kind":"A"}',
                information_status=InformationStatus.CONFIRMED),
        ])
        db.flush()
        db.add(MaterialExpectationLink(id="bad-link", material_id="mb", release_rule_id="rule-a",
            occurrence_date=DAY, expectation_identity="WRONG", rule_fingerprint="a" * 64,
            is_active=True, created_at=AT))
    assert not inbox(config, "C1").expectation_links
    with pytest.raises(ValueError, match="EXPECTATION_LINK_PROVENANCE_INVALID"):
        inbox(config, "C2")


def test_skill_proposal_scope_before_missing_run(project):
    config, factory = project
    # Test an FK-corrupt row without relaxing normal application connections.
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("INSERT INTO skill_proposals "
            "(id, created_at, extraction_run_id, proposal_kind, status, identity_sha256, course_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("broken-skill", AT.isoformat(), "missing-run", "NEW_SKILL", "PENDING", "a" * 64, "c2"))
    assert not inbox(config, "C1").pending_skill_proposals
    with pytest.raises(ValueError, match="SKILL_PROPOSAL_RUN_MISSING"):
        inbox(config, "C2")


def test_strict_top_level_uniqueness_and_order(project):
    config, factory = project
    _populate(config, factory)
    base = inbox(config)
    raw = canonical_material_inbox_json(base)
    assert canonical_material_inbox_json(MaterialInboxV1.model_validate(base.model_dump())) == raw

    def rejected(change):
        value = copy.deepcopy(base.model_dump())
        change(value)
        with pytest.raises(ValueError):
            MaterialInboxV1.model_validate(value)

    rejected(lambda v: v["projected_expectations"].append(copy.deepcopy(v["projected_expectations"][0])))
    rejected(lambda v: v["project_wide_unlinked_repositories"].append(
        copy.deepcopy(v["project_wide_unlinked_repositories"][0])))
    rejected(lambda v: v["project_wide_unassigned_artifacts"].append(
        copy.deepcopy(v["project_wide_unassigned_artifacts"][0])))
    rejected(lambda v: v["project_wide_unscoped_materials"].append(copy.deepcopy(v["materials"][0])))
    rejected(lambda v: v["project_wide_unlinked_repositories"].append(copy.deepcopy(v["repositories"][0])))
    rejected(lambda v: v["project_wide_unscoped_classification_proposals"].append(
        copy.deepcopy(v["pending_classification_proposals"][0])))
    rejected(lambda v: v["project_wide_unscoped_skill_proposals"].append(
        copy.deepcopy(v["pending_skill_proposals"][0])))

    def wrong_unresolved_order(v):
        extra = copy.deepcopy(v["unresolved_rules"][0])
        extra["rule_stable_id"] = "AAA"
        v["unresolved_rules"].append(extra)
    rejected(wrong_unresolved_order)

    def wrong_global_repo_order(v):
        extra = copy.deepcopy(v["project_wide_unlinked_repositories"][0])
        extra["repository_source_id"] = "another"
        extra["stable_id"] = "AAA"
        v["project_wide_unlinked_repositories"].append(extra)
    rejected(wrong_global_repo_order)

    def wrong_global_proposal_order(v):
        extra = copy.deepcopy(v["project_wide_unscoped_classification_proposals"][0])
        extra["proposal_id"] = "another"
        extra["proposal_kind"] = "AAA"
        v["project_wide_unscoped_classification_proposals"].append(extra)
    rejected(wrong_global_proposal_order)

    def wrong_global_skill_order(v):
        first = copy.deepcopy(v["pending_skill_proposals"][0])
        first["proposal_id"] = "z-global"
        second = copy.deepcopy(first)
        second["proposal_id"] = "a-global"
        v["project_wide_unscoped_skill_proposals"] = [first, second]
    rejected(wrong_global_skill_order)
