"""Bounded weekly-plan error presentation regressions."""
import pytest
from sqlalchemy import select

from uniflow.application import planning_workflow
from uniflow.application.weekly_plans import read_weekly_plan_revision
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.gui.presentation import present_error
from uniflow.infrastructure.database.models import WeeklyPlanRevision
from test_planning_workflow_facade import project, create


def test_missing_block_preserves_persisted_revisions(project):
    config, factory = project
    created = create(config)
    before = read_weekly_plan_revision(config, created.plan_id)
    with factory() as db:
        ids_before = set(db.scalars(select(WeeklyPlanRevision.id)))
    with pytest.raises(WorkflowCommandError) as caught:
        planning_workflow.remove_block(config, {
            "plan_id": created.plan_id, "expected_revision_id": created.revision_id,
            "block_id": "missing-block"})
    assert caught.value.code == "UNKNOWN_PLAN_BLOCK"
    presented = present_error(caught.value)
    assert presented.code == "UNKNOWN_PLAN_BLOCK"
    assert not presented.retryable
    with factory() as db:
        assert set(db.scalars(select(WeeklyPlanRevision.id))) == ids_before
    assert read_weekly_plan_revision(config, created.plan_id) == before
    removed = planning_workflow.remove_block(config, {
        "plan_id": created.plan_id, "expected_revision_id": created.revision_id,
        "block_id": before.blocks[0].block_id})
    assert removed.parent_revision_id == created.revision_id
    assert not read_weekly_plan_revision(config, created.plan_id).blocks
    with factory() as db:
        assert set(db.scalars(select(WeeklyPlanRevision.id))) == ids_before | {removed.revision_id}


@pytest.mark.parametrize("code", ["PLAN_ALREADY_EXISTS", "REVISION_CHANGED",
    "ACCEPTED_REVISION_CHANGED", "REVISION_NOT_CURRENT"])
def test_existing_workflow_mappings(code):
    assert present_error(WorkflowCommandError(code)).code == code


@pytest.mark.parametrize("error", [WorkflowCommandError("PRIVATE_INTERNAL_FAILURE"),
    RuntimeError("private internal text"), ValueError("UNKNOWN_PLAN_BLOCK"),
    WorkflowCommandError("UNKNOWN_PLAN_BLOCK_EXTRA")])
def test_unknown_errors_keep_safe_fallback(error):
    shown = present_error(error)
    assert shown.code == "APPLICATION_FAILURE"
    assert shown.retryable
    assert str(error) not in shown.message
    assert str(error) not in shown.title
