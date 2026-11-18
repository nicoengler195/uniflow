"""I3-P01..P35 acceptance oracles using actual I1/I2/V1 models and synthetic bytes."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from uniflow.application import review_projection_i3 as module
from uniflow.application.evidence_artifacts_i2 import sha256
from uniflow.application.evidence_validator_i2 import validate_evidence_bytes
from uniflow.schemas.review_projection_i3 import ReviewProjectionV1

FIX = Path(__file__).parent / "fixtures" / "review_projection_i3"


def raw(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode()


def inputs():
    return json.loads((FIX / "sidecar.json").read_bytes()), {
        "handoff": (FIX / "handoff.json").read_bytes(), "result": (FIX / "result.json").read_bytes(),
        "SYNTHETIC-SOURCE": (FIX / "source.txt").read_bytes()}


def options(**kwargs):
    return dict(handoff_artifact_id="handoff", result_artifact_id="result", handoff_revision=kwargs.pop("revision", 1),
                score_assessment_ids=kwargs.pop("selection", {"TASK-1": "ASSESSMENT-A1"}), **kwargs)


def project(s=None, a=None, **kwargs):
    base, artifacts = inputs()
    return module.project_review_bytes(raw(base if s is None else s), artifacts=artifacts if a is None else a, **options(**kwargs))


def current(s=None, a=None, **kwargs):
    base, artifacts = inputs()
    return validate_evidence_bytes(raw(base if s is None else s), artifacts=artifacts if a is None else a, **options(**kwargs))


def has(p, code, status=None):
    return any(f.finding.code == code and (status is None or f.finding.status == status) for f in p.findings)


def payload(snapshot):
    return json.loads(snapshot.payload_json)


def rebind(s, a, result):
    a["result"] = raw(result); s["result_binding"]["result_sha256"] = sha256(a["result"])


def add_task(s, a, tid="TASK-2", phase="LEARNING"):
    t, rubric = deepcopy(s["tasks"][0]), deepcopy(s["rubrics"][0])
    t.update(task_id=tid, phase=phase); rubric.update(task_id=tid, phase=phase, rubric_id="R-" + tid, declaration_event_ref=None)
    t["rubric_ref"]["rubric_id"] = rubric["rubric_id"]
    s["tasks"].append(t); s["rubrics"].append(rubric)
    result = json.loads(a["result"]); rt = deepcopy(result["tasks"][0]); rt.update(task_id=tid, phase=phase)
    if phase == "FINAL_EXAM":
        rt.update(issued_at="2026-10-25T13:00:00+01:00", answered_at="2026-10-25T13:05:00+01:00")
        result["phase_states"][1].update(status="COMPLETED", difficulty_rationale="SYNTHETIC")
        result["final_exam_decision"] = dict(offered_at="2026-10-25T12:29:00+01:00", decided_at="2026-10-25T12:30:00+01:00", decision="START")
    result["tasks"].append(rt); rebind(s, a, result)


def test_i3_p01_consistent_and_exactly_one_current_validation(monkeypatch):
    calls = []
    original = module.validate_evidence_bytes
    def counted(*args, **kwargs):
        calls.append(1); return original(*args, **kwargs)
    monkeypatch.setattr(module, "validate_evidence_bytes", counted)
    p = project()
    assert calls == [1] and p.mode == "SIDECAR_EVIDENCE"
    assert p.human_review_required and has(p, "RESULT_SCORE", "PASS")
    assert has(p, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert len(p.findings) == len(current().findings)
    assert tuple(f.finding for f in p.findings) == current().findings
    assert tuple(f.original_finding_index for f in p.findings) == tuple(range(len(p.findings)))
    assert set(current().not_checked) <= set(p.not_checked)
    assert ReviewProjectionV1.model_validate_json(p.model_dump_json()) == p


def test_i3_p02_phases_and_exam_decision():
    s, a = inputs(); add_task(s, a, phase="FINAL_EXAM")
    p = project(s, a)
    assert [t.phase for t in p.tasks] == ["LEARNING", "FINAL_EXAM"]
    assert payload(p.final_exam_decision)["decision"] == "START"
    assert len(p.phase_states) == 2


def test_i3_p03_attempts_corrections_assessments_remain_separate():
    p = project(); a1, a2 = p.tasks[0].attempts
    assert (a1.attempt_id, a2.attempt_id) == ("A1", "A2")
    assert payload(a2.correction)["corrects_attempt_id"] == "A1"
    assert a1.assessments[0].assessment_id != a2.assessments[0].assessment_id
    assert a1.assessments[0].selected_for_v1 and not a2.assessments[0].selected_for_v1
    assert a1.assessments[0].comparison_finding_indices


def test_i3_p04_feedback_after_a1_is_not_help_before_a1():
    s, a = inputs(); p = project(s, a)
    first, second = p.tasks[0].attempts
    assert first.feedback_event_ids == tuple(payload(second.assistance_before)["event_refs"])
    s["attempts"][0]["assistance_before_attempt"] = deepcopy(s["attempts"][1]["assistance_before_attempt"])
    s["attempts"][0]["independence_status"] = "GUIDED"
    p = project(s, a)
    assert has(p, "ASSISTANCE_FEEDBACK_CAUSAL_ORDER", "CONFLICT")


def test_i3_p05_earlier_help_context_survives():
    p = project(); second = p.tasks[0].attempts[1]
    assert payload(second.assistance_before)["event_refs"]
    assert payload(second.declaration)["independence_status"] == "GUIDED"
    assert any(q.question_code == "I3Q_ASSISTANCE" for q in p.questions)


def test_i3_p06_empty_unknown_help_is_not_independence():
    s, a = inputs(); help_ = s["attempts"][0]["assistance_before_attempt"]
    help_.update(event_refs=[], coverage="UNKNOWN", scope=None, classification="UNKNOWN", availability="UNKNOWN")
    p = project(s, a)
    assert payload(p.tasks[0].attempts[0].assistance_before)["coverage"] == "UNKNOWN"
    assert "independent_first_attempt" not in p.model_dump_json()


def test_i3_p07_transfer_is_its_own_task():
    s, a = inputs(); add_task(s, a)
    s["tasks"][1]["transfer_of_task_id"] = "TASK-1"
    p = project(s, a)
    assert len(p.tasks) == 2 and payload(p.tasks[1].declaration)["transfer_of_task_id"] == "TASK-1"
    assert not p.tasks[1].attempts and p.tasks[0].attempts[0].attempt_id == "A1"


def test_i3_p08_task_skill_awards_and_override_are_not_averaged():
    s, a = inputs(); s["rubrics"][0]["skill_difficulty_overrides"] = [dict(skill_id="SYNTHETIC-POWER", difficulty="HARD", rationale="SYNTHETIC")]
    p = project(s, a); awards = p.tasks[0].attempts[0].assessments[0].awards
    assert {(w.dimension, w.awarded_points) for w in awards} == {("TASK", "80"), ("TASK", "0"), ("SKILL", "100")}
    assert payload(p.rubrics[0].declaration)["skill_difficulty_overrides"][0]["difficulty"] == "HARD"
    assert payload(p.tasks[0].v1_task)["fraction"] == 0.8


@pytest.mark.parametrize("mutation", ["missing", "quote", "range"])
def test_i3_p09_bad_evidence_never_gets_reconstructed_text(mutation):
    s, a = inputs()
    if mutation == "missing": del a["SYNTHETIC-SOURCE"]
    elif mutation == "quote": s["tasks"][0]["task_statement_ref"]["quote"] = "wrong"
    else: s["tasks"][0]["task_statement_ref"]["selector"]["end"] = 99999
    s["gaps"].append(dict(gap_id="G-NEW", missing_information="SYNTHETIC", reason="not captured"))
    p = project(s, a)
    assert p.mode == "SIDECAR_EVIDENCE"
    assert p.evidence[p.tasks[0].statement_evidence_index].selected_text is None
    assert has(p, "DECLARED_GAP", "MISSING")


def test_i3_p10_missing_draft_does_not_borrow_supplied_result():
    s, a = inputs(); s["result_binding"] = dict(status="UNBOUND_DRAFT", result_sha256=None, contract_version=None, reason="SYNTHETIC")
    missing = dict(asserted_at=None, observed_at=None, received_at=None, source_clock=None, timezone_or_offset=None,
                   precision=None, interval_start=None, interval_end=None, time_status="MISSING",
                   provenance=dict(provenance_status="MISSING", evidence_refs=[], asserted_by=None, observer_id=None,
                       observation_method=None, reconstruction_reason=None, gaps=[dict(gap_id="G", missing_information="time", reason="not recorded")]))
    s["attempts"][0]["answer_time_claim"] = missing
    p = project(s, a)
    assert p.session.result_context is None and p.tasks[0].v1_task is None
    assert payload(p.tasks[0].attempts[0].answer_time)["asserted_at"] is None
    assert has(p, "UNBOUND_DRAFT", "UNKNOWN")


def test_i3_p11_pending_is_not_zero():
    s, a = inputs(); s["assessments"][0]["criterion_awards"][0].update(status="PENDING", awarded_points=None)
    p = project(s, a)
    pending = next(w for w in p.tasks[0].attempts[0].assessments[0].awards if w.status == "PENDING")
    assert pending.awarded_points is None and has(p, "SCORE_PENDING", "UNKNOWN")


def test_i3_p12_r04_local_and_bound_findings_are_distinct():
    s, a = inputs(); s["session_id"] = "SYNTHETIC-FOREIGN"
    p = project(s, a)
    for code in ("HANDOFF_SESSION", "RESULT_SESSION"): assert has(p, code, "CONFLICT")
    for f in p.findings:
        if f.finding.code in {"LOCAL_RESULT_SCORE", "LOCAL_SKILL_MEMBERSHIP"}:
            assert f.display_category == "LOCAL_DIAGNOSTIC" and f.finding.status == "PASS"
        elif f.finding.code in {"RESULT_SCORE", "SKILL_SCOPE"}:
            assert f.display_category == "BINDING_CONFIRMATION" and f.finding.status == "UNKNOWN"


@pytest.mark.parametrize("change", ["handoff", "result", "crlf", "unicode"])
def test_i3_p13_original_byte_conflicts_and_context_hash(change):
    s, a = inputs(); baseline = project(s, a)
    if change == "handoff": s["handoff_artifact_sha256"] = "0" * 64
    elif change == "result": s["result_binding"]["result_sha256"] = "0" * 64
    elif change == "crlf": a["result"] = a["result"].replace(b"\n", b"\r\n")
    else: a["SYNTHETIC-SOURCE"] = a["SYNTHETIC-SOURCE"].replace("e\u0301".encode(), "é".encode())
    p = project(s, a)
    assert p.input_context.context_sha256 != baseline.input_context.context_sha256
    assert any(f.finding.status == "CONFLICT" for f in p.findings)


@pytest.mark.parametrize("revision,expected", [(None, "UNKNOWN"), (2, "CONFLICT"), (1, "UNVERIFIED")])
def test_i3_p14_revision_context_is_not_authenticated(revision, expected):
    p = project(revision=revision)
    assert p.input_context.caller_handoff_revision == revision
    assert has(p, "HANDOFF_REVISION_UNAVAILABLE" if revision is None else "HANDOFF_REVISION" if revision == 2 else "REVISION_CONTEXT_UNAUTHENTICATED", expected)


@pytest.mark.parametrize("selection", [{}, {"TASK-1": "absent"}, {"TASK-1": "ASSESSMENT-A2"}])
def test_i3_p15_no_automatic_assessment_selection(selection):
    p = project(selection=selection)
    assert p.tasks[0].selected_assessment_id == selection.get("TASK-1")
    assert [a.selected_for_v1 for t in p.tasks for attempt in t.attempts for a in attempt.assessments] == [selection.get("TASK-1") == "ASSESSMENT-A1", selection.get("TASK-1") == "ASSESSMENT-A2"]


@pytest.mark.parametrize("mutation", ["task_rubric", "attempt", "award_missing", "award_unknown", "award_duplicate"])
def test_i3_p16_reference_errors_are_not_repaired(mutation):
    s, a = inputs()
    if mutation == "task_rubric": s["tasks"][0]["rubric_ref"]["revision"] = 999
    elif mutation == "attempt": s["assessments"][0]["attempt_id"] = "absent"
    elif mutation == "award_missing": s["assessments"][0]["criterion_awards"].pop()
    else:
        w = deepcopy(s["assessments"][0]["criterion_awards"][0])
        if mutation == "award_unknown": w["criterion_id"] = "unknown"
        s["assessments"][0]["criterion_awards"].append(w)
    p = project(s, a)
    assert any(f.finding.status in {"ERROR", "CONFLICT"} for f in p.findings)
    if mutation.startswith("award"):
        assert p.mode == "LEGACY_V1" and not p.tasks
    elif mutation == "attempt":
        assert any(x.source.pointer == "/assessments/0" for x in p.detached_entities)


def test_i3_p17_cycles_sequences_and_detached_entities_survive():
    s, a = inputs(); s["events"][0]["parent_event_ids"] = [s["events"][1]["event_id"]]
    s["events"][1]["parent_event_ids"] = [s["events"][0]["event_id"]]
    s["events"][0]["collector_sequence"] = s["events"][1]["collector_sequence"] = 1
    s["attempts"][0]["task_id"] = "foreign"
    p = project(s, a)
    assert has(p, "EVENT_GRAPH_CYCLE", "CONFLICT") and has(p, "EVENT_SEQUENCE_DUPLICATE", "CONFLICT")
    assert any(x.source.pointer == "/attempts/0" for x in p.detached_entities)
    assert [e.event_id for e in p.events] == sorted(e.event_id for e in p.events)


def test_i3_p18_dst_uncertainty_and_original_time_strings():
    s, a = inputs(); first, second = [x["answer_time_claim"] for x in s["attempts"]]
    first.update(asserted_at="2026-10-25T02:30:00+02:00", timezone_or_offset="SYNTHETIC variable offset",
                 interval_start="2026-10-25T02:00:00+02:00", interval_end="2026-10-25T03:00:00+01:00")
    second.update(asserted_at="2026-10-25T02:30:00+01:00", timezone_or_offset="SYNTHETIC variable offset")
    p = project(s, a)
    assert payload(p.tasks[0].attempts[0].answer_time) == first
    assert payload(p.tasks[0].attempts[1].answer_time) == second
    assert has(p, "ATTEMPT_ORDER_TIME", "UNKNOWN")


def test_i3_p19_hashes_do_not_authenticate_rubric_or_author():
    s, a = inputs(); s["rubrics"][0]["domain"] = "SYNTHETIC changed rule with consistent local hashes"
    p = project(s, a)
    for code in ("RUBRIC_PREDECLARATION", "RUBRIC_HASH_REPRESENTATION", "NO_EXTERNAL_TRUST_ANCHOR"):
        assert has(p, code, "UNVERIFIED")


def test_i3_p20_score_conflict_values_and_open_math_question():
    s, a = inputs(); result = json.loads(a["result"]); result["tasks"][0]["fraction"] = 1.0; rebind(s, a, result)
    p = project(s, a)
    conflict = next(f.finding for f in p.findings if f.finding.code == "RESULT_SCORE" and f.finding.status == "CONFLICT")
    assert ("sidecar_awarded", "80") in conflict.values and ("v1_fraction", "1.0") in conflict.values
    assert any(q.question_code == "I3Q_FINAL_ANSWER" for q in p.questions)


def test_i3_p21_declared_correctness_does_not_answer_review_questions():
    s, a = inputs(); s["assessments"][0]["criterion_awards"][0]["correctness"] = "INCORRECT"
    p = project(s, a)
    assert has(p, "RESULT_SCORE", "PASS")
    assert any(w.correctness_declaration == "INCORRECT" for w in p.tasks[0].attempts[0].assessments[0].awards)
    assert any(q.question_code == "I3Q_FINAL_ANSWER" for q in p.questions)


def test_i3_p22_historical_and_new_rubric_revision_values_stay_distinct():
    s, a = inputs(); newer = deepcopy(s["rubrics"][0]); newer.update(revision=2, domain="NEW SYNTHETIC T5 Chain .5")
    s["rubrics"][0]["domain"] = "HISTORICAL RECONSTRUCTION Chain .3"; s["rubrics"].append(newer)
    result = json.loads(a["result"]); result["tasks"][0]["skill_marks"][0]["fraction"] = 0.3; rebind(s, a, result)
    p = project(s, a)
    assert [r.revision for r in p.rubrics] == [1, 2]
    assert payload(p.tasks[0].v1_task)["skill_marks"][0]["fraction"] == 0.3
    assert ".5" in payload(p.rubrics[1].declaration)["domain"]


def test_i3_p23_determinism_deep_immutability_and_no_aliases():
    s, a = inputs(); before = deepcopy((s, a)); p = project(s, a)
    assert p == project(s, dict(reversed(list(a.items())))) and (s, a) == before
    original = p.model_dump_json(); s["tasks"].clear(); a.clear()
    assert p.model_dump_json() == original
    for obj, field, value in [(p, "human_review_required", False), (p.tasks[0], "task_id", "changed"),
                              (p.findings[0].finding, "values", ())]:
        with pytest.raises(ValidationError): setattr(obj, field, value)
    assert isinstance(p.tasks, tuple) and isinstance(p.findings[0].finding.values, tuple)


@pytest.mark.parametrize("field", ["findings", "checked_artifacts", "not_checked", "sidecar_id"])
def test_i3_p24_supplied_report_is_fully_compared(field):
    value = current().model_dump(mode="json")
    if field == "findings": value[field][0]["reason"] = "SYNTHETIC forged"
    elif field == "checked_artifacts": value[field][0]["byte_length"] += 1
    elif field == "not_checked": value[field] = []
    else: value[field] = "foreign"
    p = project(supplied_i2_report_bytes=raw(value))
    assert p.input_context.supplied_report_handling == "REJECTED"
    assert any(d.code == "I3P_REPORT_REJECTED" for d in p.diagnostics)
    assert tuple(f.finding for f in p.findings) == current().findings
    assert "SYNTHETIC forged" not in p.model_dump_json()
    assert project(supplied_i2_report_bytes=current().model_dump_json().encode()).input_context.supplied_report_handling == "MATCHED_CURRENT"


def test_i3_p25_caller_context_changes_report_binding_and_digest():
    provided = current().model_dump_json().encode(); baseline = project(supplied_i2_report_bytes=provided)
    for kwargs in (dict(revision=2), dict(selection={}), dict(selection={"TASK-1": "ASSESSMENT-A2"})):
        changed = project(supplied_i2_report_bytes=provided, **kwargs)
        assert changed.input_context.supplied_report_handling == "REJECTED"
        assert changed.input_context.context_sha256 != baseline.input_context.context_sha256
    s, a = inputs(); a["result-alias"] = a["result"]
    provided = current(s, a).model_dump_json().encode()
    p = module.project_review_bytes(raw(s), artifacts=a, **dict(options(), result_artifact_id="result-alias"), supplied_i2_report_bytes=provided)
    assert p.input_context.supplied_report_handling == "REJECTED"


@pytest.mark.parametrize("bad", [b'{"validator_version":"unknown"}', b'{"x":NaN}', b'{"x":1,"x":2}', b'\xff'])
def test_i3_p26_malformed_report_is_rejected(bad):
    p = project(supplied_i2_report_bytes=bad)
    assert p.mode == "SIDECAR_EVIDENCE" and p.input_context.supplied_report_handling == "REJECTED"
    assert any(d.code == "I3P_REPORT_REJECTED" for d in p.diagnostics)


def test_i3_p27_legacy_i20_report_never_becomes_new_confirmation():
    legacy = (FIX / "legacy_i2_report.json").read_bytes()
    p = project(supplied_i2_report_bytes=legacy)
    archived = [f for f in p.findings if f.source_report_version.endswith("i2.0")]
    assert archived and all(f.display_category == "LEGACY_REPORT" for f in archived)
    assert any(f.finding.code == "RESULT_SCORE" and f.finding.status == "PASS" for f in archived)
    no_originals = module.project_review_bytes(b'{}', artifacts={}, supplied_i2_report_bytes=legacy)
    assert no_originals.mode == "DIAGNOSTIC_ONLY" and no_originals.input_context.supplied_report_handling == "ARCHIVED_LEGACY"


def test_i3_p28_valid_legacy_v1_survives_bad_optional_sidecar():
    s, a = inputs(); p = module.project_legacy_scoring_bytes(a["result"])
    assert p.mode == "LEGACY_V1" and not p.tasks and not p.findings
    assert payload(p.detached_entities[0])["fraction"] == 0.8
    p = module.project_review_bytes(b'{"bad":', artifacts=a, **options())
    assert p.mode == "LEGACY_V1" and not p.tasks and has(p, "INVALID_JSON", "ERROR")


def test_i3_p29_declared_review_approve_does_not_approve_projection():
    s, a = inputs(); s["reviews"].append(dict(review_id="REVIEW-SYNTHETIC", reviewer_ref="SYNTHETIC",
        reviewed_object_hashes=["0" * 64], decision="APPROVE", reasons=["SYNTHETIC declaration"],
        recorded_time_claim=deepcopy(s["events"][0]["time_claim"]), provenance=deepcopy(s["attempts"][0]["provenance"])))
    p = project(s, a)
    assert payload(p.declared_reviews[0])["decision"] == "APPROVE" and p.human_review_required
    assert has(p, "REVIEW_DECLARATION_UNAUTHENTICATED", "UNVERIFIED")
    assert p.questions


def test_i3_p30_colliding_ids_revisions_and_duplicate_findings():
    s, a = inputs(); s["events"][0]["event_id"] = "A1"
    rubric = deepcopy(s["rubrics"][0]); rubric["revision"] = 2; s["rubrics"].append(rubric)
    p = project(s, a); expected = current(s, a)
    assert tuple(f.finding for f in p.findings) == expected.findings
    assert any(f.association == "AMBIGUOUS" for f in p.findings)
    assert len({(f.source_report_sha256, f.original_finding_index) for f in p.findings}) == len(p.findings)


def test_i3_p31_text_resolution_and_message_identity_are_separate():
    s, a = inputs(); a["json-source"] = b'{"message":{"source_message_id":"m1","text":"hello"}}'
    s["source_artifacts"].append(dict(s["source_artifacts"][0], artifact_id="json-source", sha256=sha256(a["json-source"]), byte_length=len(a["json-source"]), media_type="application/json"))
    ref = s["tasks"][0]["task_statement_ref"]
    ref.update(artifact_id="json-source", sha256=sha256(a["json-source"]), quote=None, source_message_id="wrong", selector=dict(kind="JSON_POINTER", pointer="/message", start=None, end=None))
    p = project(s, a)
    assert has(p, "REF_RESOLVED", "PASS") and has(p, "REF_MESSAGE_ID", "CONFLICT")
    evidence = p.evidence[p.tasks[0].statement_evidence_index]
    assert evidence.message_finding_indices and evidence.selection_finding_indices
    ref["quote"] = "not a string target"
    p = project(s, a)
    assert has(p, "REF_NONSTRING_QUOTE", "UNKNOWN") and p.evidence[p.tasks[0].statement_evidence_index].selected_text is None


def test_i3_p32_inert_text_no_io_or_runtime_imports(monkeypatch):
    import builtins
    import socket
    s, a = inputs(); s["rubrics"][0]["domain"] = "__import__('os'); <script>alert(1)</script> [link](https://example.invalid)"
    encoded = raw(s)
    def deny(*args, **kwargs): raise AssertionError("Unexpected I/O")
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", deny); m.setattr(Path, "open", deny); m.setattr(socket, "socket", deny)
        p = module.project_review_bytes(encoded, artifacts=a, **options())
    assert payload(p.rubrics[0].declaration)["domain"] == s["rubrics"][0]["domain"]
    code = "import sys; import uniflow.application.review_projection_i3; assert not any(x.startswith(('uniflow.infrastructure','uniflow.runtime','PySide6','sqlalchemy','socket')) for x in sys.modules)"
    run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("limit", ["report", "findings", "questions", "output", "depth"])
def test_i3_p33_limits_explicitly_abort_without_partial_view(monkeypatch, limit):
    kwargs = {}
    if limit == "report": kwargs["supplied_i2_report_bytes"] = b' ' * (2 * 1024 * 1024 + 1)
    elif limit == "findings": monkeypatch.setattr(module, "MAX_FINDINGS", 1)
    elif limit == "questions": monkeypatch.setattr(module, "MAX_QUESTIONS", 1)
    elif limit == "output": monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 4096)
    if limit == "depth":
        p = module.project_review_bytes(b'[' * 49 + b'0' + b']' * 49, artifacts={})
        assert p.mode == "DIAGNOSTIC_ONLY"
    else:
        p = project(**kwargs)
        assert any(d.code == "I3P_RESOURCE_LIMIT" for d in p.diagnostics)
        assert p.mode == "DIAGNOSTIC_ONLY" and not p.tasks and not p.findings
        assert len(p.model_dump_json().encode()) < 4096
    assert p.human_review_required and p.not_checked


def test_i3_p34_sorted_view_keeps_original_source_indices():
    s, a = inputs(); s["attempts"].reverse(); s["events"].reverse()
    p = project(s, a)
    assert [v.attempt_id for v in p.tasks[0].attempts] == ["A1", "A2"]
    assert p.tasks[0].attempts[0].declaration.source.pointer == "/attempts/1"
    assert payload(p.tasks[0].attempts[0].declaration) == s["attempts"][1]
    assert "keine authentifizierte Chronologie" in p.session.provenance_notice


def test_i3_p35_bonus_and_foreign_result_contract_are_not_cast():
    s, a = inputs(); result = json.loads(a["result"])
    result["tasks"].append(dict(task_id="BONUS", task_type="PREVIEW_BONUS", observation="SYNTHETIC", additional_skills=[dict(provisional_skill_name="SYNTHETIC candidate", observation="qualitative only")]))
    rebind(s, a, result); p = project(s, a)
    bonus = next(payload(v) for v in p.detached_entities if v.source.origin == "RESULT")
    assert bonus["task_type"] == "PREVIEW_BONUS" and "fraction" not in bonus and "phase" not in bonus
    foreign = dict(schema_version="2.0", protocol_version="subject-learning-v1", skill_evidence=[])
    rejected = module.project_legacy_scoring_bytes(raw(foreign))
    assert rejected.mode == "DIAGNOSTIC_ONLY" and not rejected.tasks


def test_snapshot_locators_reconstruct_exact_source_values():
    from pydantic import BaseModel
    from uniflow.schemas.review_projection_i3 import ModelSnapshot
    s, a = inputs(); provided = (FIX / "legacy_i2_report.json").read_bytes()
    p = project(s, a, supplied_i2_report_bytes=provided)
    sources = {"SIDECAR": s, "HANDOFF": json.loads(a["handoff"]), "RESULT": json.loads(a["result"]),
               "CURRENT_I2": current(s, a).model_dump(mode="json"), "SUPPLIED_I2": json.loads(provided)}
    stack = [p]
    seen = 0
    while stack:
        node = stack.pop()
        if isinstance(node, ModelSnapshot):
            value = sources[node.source.origin]
            if node.source.pointer:
                for token in node.source.pointer[1:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
            assert payload(node) == value
            seen += 1
        elif isinstance(node, BaseModel):
            stack.extend(getattr(node, name) for name in type(node).model_fields)
        elif isinstance(node, tuple):
            stack.extend(node)
        else:
            assert not isinstance(node, (list, dict))
    assert seen > 50
    legacy_root = next(x for x in p.detached_entities if x.source.origin == "SUPPLIED_I2")
    assert payload(legacy_root) == json.loads(provided)


def test_real_question_limit_and_output_expansion_limit():
    s, a = inputs()
    for i in range(166):
        task = deepcopy(s["tasks"][0]); task["task_id"] = f"SYNTHETIC-{i}"
        s["tasks"].append(task)
    p = project(s, a)
    assert p.mode == "DIAGNOSTIC_ONLY" and any(d.code == "I3P_RESOURCE_LIMIT" for d in p.diagnostics)
    s, a = inputs(); text = b'SYNTHETIC inert text ' * 7500
    a["SYNTHETIC-SOURCE"] = text
    s["source_artifacts"][0].update(sha256=sha256(text), byte_length=len(text))
    for i in range(34):
        task = deepcopy(s["tasks"][0]); task["task_id"] = f"SYNTHETIC-{i}"
        s["tasks"].append(task)
    def replace_refs(value):
        if isinstance(value, dict):
            if "selector" in value and "artifact_id" in value:
                value.update(sha256=sha256(text), quote=None); value["selector"].update(start=0, end=len(text))
            for child in value.values(): replace_refs(child)
        elif isinstance(value, list):
            for child in value: replace_refs(child)
    replace_refs(s)
    p = project(s, a)
    assert p.mode == "DIAGNOSTIC_ONLY" and any(d.code == "I3P_RESOURCE_LIMIT" for d in p.diagnostics)
    assert len(p.model_dump_json().encode()) < 4096


def test_real_finding_limit_fault_injection_uses_actual_report(monkeypatch):
    actual = module.validate_evidence_bytes
    calls = []
    def too_many(*args, **kwargs):
        calls.append(1)
        report = actual(*args, **kwargs)
        return report.model_copy(update={"findings": (report.findings[0],) * 20001})
    monkeypatch.setattr(module, "validate_evidence_bytes", too_many)
    p = project()
    assert calls == [1] and p.mode == "DIAGNOSTIC_ONLY"
    assert p.diagnostics[0].code == "I3P_RESOURCE_LIMIT" and not p.findings


@pytest.mark.parametrize("kind", ["role", "revision", "selection", "artifact", "nonbytes"])
def test_malformed_api_inputs_are_bounded_diagnostics(kind):
    s, a = inputs(); kwargs = options(); encoded = raw(s)
    if kind == "role": kwargs["handoff_artifact_id"] = []
    elif kind == "revision": kwargs["handoff_revision"] = True
    elif kind == "selection": kwargs["score_assessment_ids"] = []
    elif kind == "artifact": a["unexpected"] = Path("D:/never-open")
    else: encoded = {"not": "bytes"}
    p = module.project_review_bytes(encoded, artifacts=a, **kwargs)
    assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics and p.human_review_required


@pytest.mark.parametrize("field", ["validator_version", "human_review_required"])
def test_external_report_defaulted_fields_must_be_explicit(field):
    report = current().model_dump(mode="json"); del report[field]
    p = project(supplied_i2_report_bytes=raw(report))
    assert p.input_context.supplied_report_handling == "REJECTED"
    assert tuple(f.finding for f in p.findings) == current().findings


def test_all_entry_points_classify_input_resource_abort():
    for raw_input in (b' ' * (2 * 1024 * 1024 + 1), b'[' * 49 + b'0' + b']' * 49):
        p = module.project_legacy_scoring_bytes(raw_input)
        assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics[0].code == "I3P_RESOURCE_LIMIT"
    p = project(supplied_i2_report_bytes=b'[' * 49 + b'0' + b']' * 49)
    assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics[0].code == "I3P_RESOURCE_LIMIT"


@pytest.mark.parametrize("supplied", [None, b"invalid"])
def test_diagnostic_abort_does_not_invent_a_supplied_report(supplied):
    p = module.project_review_bytes(None, artifacts={}, supplied_i2_report_bytes=supplied)
    assert p.mode == "DIAGNOSTIC_ONLY"
    assert p.input_context.supplied_report_handling == ("ABSENT" if supplied is None else "REJECTED")


@pytest.mark.parametrize("target", ["caller", "validator_arguments"])
def test_r01_artifact_mutation_after_real_validation_cannot_change_projection(monkeypatch, target):
    s, caller = inputs(); original_inputs = dict(caller)
    expected = project(s, caller)
    changed = json.loads(caller["result"])
    changed["tasks"][0].update(fraction=1.0, evidence="SYNTHETIC swapped result")
    from uniflow.schemas.scoring_contract_v1 import ScoringResultV1
    replacement = raw(changed); ScoringResultV1.model_validate_json(replacement)
    real = module.validate_evidence_bytes
    reports = []
    def wrapped(*args, **kwargs):
        report = real(*args, **kwargs)  # Full actual I1/I2.1/V1 validation first.
        reports.append(report)
        mapping = caller if target == "caller" else kwargs["artifacts"]
        mapping["result"] = replacement
        mapping["SYNTHETIC-SOURCE"] = original_inputs["SYNTHETIC-SOURCE"].replace(b"SYNTHETIC", b"ALTERED!!")
        return report
    monkeypatch.setattr(module, "validate_evidence_bytes", wrapped)
    p = project(s, caller)
    assert len(reports) == 1 and p == expected
    assert {a.artifact_id: a.sha256 for a in p.input_context.original_artifacts} == {a.artifact_id: a.sha256 for a in reports[0].checked_artifacts}
    assert payload(p.tasks[0].v1_task)["fraction"] == 0.8
    evidence = p.evidence[p.tasks[0].statement_evidence_index]
    assert evidence.selected_text == original_inputs["SYNTHETIC-SOURCE"].decode()
    assert evidence.selected_text == payload(evidence.declaration)["quote"]
    assert has(p, "RESULT_SCORE", "PASS")


@pytest.mark.parametrize("target", ["caller", "validator_arguments"])
def test_r01_selection_mutation_after_real_validation_uses_captured_choice(monkeypatch, target):
    s, a = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    expected = project(s, a, selection=selection)
    real = module.validate_evidence_bytes; reports = []
    def wrapped(*args, **kwargs):
        report = real(*args, **kwargs); reports.append(report)
        mapping = selection if target == "caller" else kwargs["score_assessment_ids"]
        mapping.clear(); mapping.update({"TASK-1": "ASSESSMENT-A2", "foreign-task": "ASSESSMENT-A1"})
        return report
    monkeypatch.setattr(module, "validate_evidence_bytes", wrapped)
    p = project(s, a, selection=selection)
    assert len(reports) == 1 and p == expected
    assert p.input_context.assessment_selection == (("TASK-1", "ASSESSMENT-A1"),)
    assert p.tasks[0].selected_assessment_id == "ASSESSMENT-A1"
    first, second = p.tasks[0].attempts
    assert first.assessments[0].selected_for_v1 and not second.assessments[0].selected_for_v1
    assert tuple(f.finding for f in p.findings) == reports[0].findings


def test_r01_distinct_valid_input_versions_repeat_without_mutation():
    s, a = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    before = deepcopy((s, a, selection)); first = project(s, a, selection=selection)
    assert (s, a, selection) == before
    changed = json.loads(a["result"]); changed["tasks"][0]["fraction"] = 1.0
    rebind(s, a, changed); selection["TASK-1"] = "ASSESSMENT-A2"
    second = project(s, a, selection=selection)
    assert second.mode == first.mode == "SIDECAR_EVIDENCE"
    assert first.input_context.context_sha256 != second.input_context.context_sha256
    assert first.input_context.original_artifacts != second.input_context.original_artifacts
    assert has(second, "RESULT_SCORE", "PASS")
    assert second == project(s, dict(reversed(list(a.items()))), selection=dict(selection))
    encoded = second.model_dump_json(); a.clear(); selection.clear(); s.clear()
    assert second.model_dump_json() == encoded


@pytest.mark.parametrize("which", ["artifacts", "selection"])
def test_r01_change_observed_during_capture_aborts_before_i2(monkeypatch, which):
    s, caller = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    store_type = module.ArtifactStore; calls = []
    def capture_store(values):
        store = store_type(values)
        if which == "artifacts": caller["result"] += b" "
        else: selection["TASK-1"] = "ASSESSMENT-A2"
        return store
    def unexpected(*args, **kwargs):
        calls.append(1); raise AssertionError("I2 must not run for unstable capture")
    monkeypatch.setattr(module, "ArtifactStore", capture_store)
    monkeypatch.setattr(module, "validate_evidence_bytes", unexpected)
    p = project(s, caller, selection=selection)
    assert calls == [] and p.mode == "DIAGNOSTIC_ONLY"
    assert p.diagnostics[0].code == "I3P_INPUT_SNAPSHOT_CHANGED"
    assert not p.tasks and not p.findings and p.human_review_required


@pytest.mark.parametrize("which", ["mapping", "subclass", "path", "bytearray", "utf8", "selection_mapping", "selection_list", "selection_value"])
def test_r01_capture_rejects_types_without_mapping_conversion(monkeypatch, which):
    from types import MappingProxyType
    s, a = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    class HostileDict(dict):
        def items(self): raise AssertionError("Arbitrary dict subclass protocol")
        def copy(self): raise AssertionError("Arbitrary copy protocol")
    if which == "mapping": a = MappingProxyType(a)
    elif which == "subclass": a = HostileDict(a)
    elif which == "path": a["result"] = Path("D:/must-not-open")
    elif which == "bytearray": a["result"] = bytearray(a["result"])
    elif which == "utf8": a["result"] = b'\xff'
    elif which == "selection_mapping": selection = MappingProxyType(selection)
    elif which == "selection_list": selection = []
    else: selection["TASK-1"] = object()
    def deny(*args, **kwargs): raise AssertionError("Invalid capture reached I2")
    monkeypatch.setattr(module, "validate_evidence_bytes", deny)
    p = module.project_review_bytes(raw(s), artifacts=a, **dict(options(), score_assessment_ids=selection))
    assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics and p.human_review_required


@pytest.mark.parametrize("limit", ["count", "bytes", "total", "selection_count", "selection_text"])
def test_r01_capture_bounds_before_validation_or_large_copy(monkeypatch, limit):
    s, a = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    if limit == "count": a = {str(i): object() for i in range(65)}
    elif limit == "bytes": a["result"] = b'x' * (2 * 1024 * 1024 + 1)
    elif limit == "total": a = {str(i): b'x' * (2 * 1024 * 1024) for i in range(9)}
    elif limit == "selection_count": selection = {str(i): object() for i in range(201)}
    else: selection["TASK-1"] = 'x' * 65537
    def deny(*args, **kwargs): raise AssertionError("Over-limit capture reached I2")
    monkeypatch.setattr(module, "validate_evidence_bytes", deny)
    p = project(s, a, selection=selection)
    assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics[0].code == "I3P_RESOURCE_LIMIT"
    assert len(p.model_dump_json().encode()) < 4096


def test_r01_manipulated_supplied_report_with_post_i2_caller_mutation(monkeypatch):
    s, a = inputs(); reference = current(s, a)
    forged = reference.model_dump(mode="json"); forged["findings"][0]["reason"] = "SYNTHETIC forged"
    real = module.validate_evidence_bytes; calls = []
    def wrapped(*args, **kwargs):
        report = real(*args, **kwargs); calls.append(1)
        a["result"] += b" "
        return report
    monkeypatch.setattr(module, "validate_evidence_bytes", wrapped)
    p = project(s, a, supplied_i2_report_bytes=raw(forged))
    assert calls == [1] and p.input_context.supplied_report_handling == "REJECTED"
    assert tuple(f.finding for f in p.findings) == reference.findings
    assert {x.artifact_id: x.sha256 for x in p.input_context.original_artifacts} == {x.artifact_id: x.sha256 for x in reference.checked_artifacts}
    assert "SYNTHETIC forged" not in p.model_dump_json()


def test_r01_draft_and_legacy_modes_keep_their_original_semantics(monkeypatch):
    s, a = inputs(); original_result = a["result"]
    s["result_binding"] = dict(status="UNBOUND_DRAFT", result_sha256=None, contract_version=None, reason="SYNTHETIC draft")
    expected = project(s, a)
    real = module.validate_evidence_bytes; calls = []
    def wrapped(*args, **kwargs):
        report = real(*args, **kwargs); calls.append(1); a.clear(); return report
    monkeypatch.setattr(module, "validate_evidence_bytes", wrapped)
    p = project(s, a)
    assert calls == [1] and p == expected and p.tasks[0].v1_task is None
    legacy = module.project_legacy_scoring_bytes(original_result)
    assert legacy.mode == "LEGACY_V1" and not legacy.tasks and not legacy.findings
    assert calls == [1] and p.human_review_required and legacy.human_review_required


@pytest.mark.parametrize("which", ["artifacts", "selection"])
def test_r01_real_dict_iterator_error_during_capture_is_controlled(monkeypatch, which):
    s, a = inputs(); selection = {"TASK-1": "ASSESSMENT-A1"}
    original_enumerate = enumerate; iterations = []
    def mutate_during_iteration(iterable):
        iterations.append(1)
        target_call = 1 if which == "artifacts" else 2
        iterator = original_enumerate(iterable)
        for item in iterator:
            yield item
            if len(iterations) == target_call:
                target = a if which == "artifacts" else selection
                target["SYNTHETIC-added-during-iteration"] = b"data" if which == "artifacts" else "ASSESSMENT-A2"
                # The next real dict-items iteration raises RuntimeError.
    def deny(*args, **kwargs): raise AssertionError("Unstable capture reached I2")
    monkeypatch.setattr(module, "enumerate", mutate_during_iteration, raising=False)
    monkeypatch.setattr(module, "validate_evidence_bytes", deny)
    p = project(s, a, selection=selection)
    assert p.mode == "DIAGNOSTIC_ONLY" and p.diagnostics[0].code == "I3P_INPUT_SNAPSHOT_CHANGED"


def test_r01_none_and_explicit_empty_selection_are_not_repaired():
    missing = project(selection=None); empty = project(selection={})
    assert missing.input_context.assessment_selection == empty.input_context.assessment_selection == ()
    assert not missing.input_context.assessment_selection_supplied
    assert empty.input_context.assessment_selection_supplied
    assert missing.input_context.context_sha256 != empty.input_context.context_sha256
    assert not missing.tasks[0].selected_assessment_id and not empty.tasks[0].selected_assessment_id
