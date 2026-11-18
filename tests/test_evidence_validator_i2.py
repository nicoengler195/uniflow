"""Isolated I2 acceptance checks. All new input is SYNTHETIC; no runtime config."""
from copy import deepcopy
from decimal import localcontext
import json
from pathlib import Path
import subprocess
import sys

import pytest

from uniflow.application.evidence_artifacts_i2 import (
    ArtifactError, ArtifactStore, MAX_BYTES, MAX_TOTAL_BYTES, load_json_bytes, select, sha256,
)
from uniflow.application.evidence_validator_i2 import validate_evidence_bytes
from uniflow.schemas.evidence_sidecar_v1 import Selector
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1

FIX = Path(__file__).parent / "fixtures" / "evidence_validator_i2"


def raw(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")


def inputs():
    sidecar = json.loads((FIX / "sidecar.json").read_bytes())
    artifacts = {"handoff": (FIX / "handoff.json").read_bytes(),
                 "result": (FIX / "result.json").read_bytes(),
                 "SYNTHETIC-SOURCE": (FIX / "source.txt").read_bytes()}
    return sidecar, artifacts


def check(s=None, a=None, **kwargs):
    base, artifacts = inputs()
    return validate_evidence_bytes(raw(base if s is None else s),
        artifacts=artifacts if a is None else a, handoff_artifact_id="handoff",
        result_artifact_id="result", handoff_revision=kwargs.pop("handoff_revision", 1),
        score_assessment_ids=kwargs.pop("score_assessment_ids", {"TASK-1": "ASSESSMENT-A1"}), **kwargs)


def has(report, code, status=None):
    return any(f.code == code and (status is None or f.status == status) for f in report.findings)


def rebind(s, a, role, obj):
    a[role] = raw(obj)
    if role == "result":
        s["result_binding"]["result_sha256"] = sha256(a[role])
    else:
        s["handoff_artifact_sha256"] = sha256(a[role])


def test_t11_t18_valid_reproducible_report_keeps_trust_gaps_and_inputs():
    s, a = inputs()
    before = deepcopy((s, a))
    r = check(s, a)
    assert not [f for f in r.findings if f.status in {"ERROR", "CONFLICT", "MISSING"}]
    assert has(r, "RESULT_SHA256", "PASS")
    assert has(r, "RESULT_SCORE", "PASS")
    assert has(r, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert has(r, "RUBRIC_PREDECLARATION", "UNVERIFIED")
    assert r.human_review_required
    assert r == check(s, a)
    assert (s, a) == before
    assert all(len(x.sha256) == 64 for x in r.checked_artifacts)


@pytest.mark.parametrize("field,value,code", [
    ("session_id", "foreign", "HANDOFF_SESSION"),
    ("handoff_id", "foreign", "HANDOFF_ID"),
    ("handoff_revision", 2, "HANDOFF_REVISION"),
    ("handoff_artifact_sha256", "0" * 64, "HANDOFF_SHA256"),
])
def test_t05_foreign_sidecar_identity(field, value, code):
    s, a = inputs(); s[field] = value
    assert has(check(s, a), code, "CONFLICT")


@pytest.mark.parametrize("field,code", [("session_id", "RESULT_SESSION"), ("handoff_id", "RESULT_HANDOFF_ID"),
                                       ("handoff_artifact_sha256", "RESULT_HANDOFF_SHA256")])
def test_t05_foreign_valid_result(field, code):
    s, a = inputs(); r = json.loads(a["result"])
    r[field] = "1" * 64 if field.endswith("sha256") else "foreign"
    rebind(s, a, "result", r)
    assert has(check(s, a), code, "CONFLICT")


@pytest.mark.parametrize("target", ["rubric", "result", "extra", "error"])
def test_t05_foreign_skill_never_gets_scope_pass(target):
    s, a = inputs(); r = json.loads(a["result"])
    if target == "rubric":
        s["rubrics"][0]["skill_criteria"][0]["skill_id"] = "foreign"
        s["rubrics"][0]["dimension_totals"][1]["skill_id"] = "foreign"
    elif target == "result":
        r["tasks"][0]["skill_marks"][0]["skill_id"] = "foreign"
    elif target == "extra":
        r["tasks"][0]["additional_skills"] = [{"skill_id": "foreign", "observation": "SYNTHETIC"}]
    else:
        r["errors"] = [{"task_id": "TASK-1", "skill_id": "foreign", "description": "SYNTHETIC", "context": "SYNTHETIC", "status": "OBSERVED"}]
    rebind(s, a, "result", r)
    report = check(s, a)
    assert has(report, "SKILL_SCOPE", "CONFLICT")
    assert not any(f.code == "SKILL_SCOPE" and f.status == "PASS" and ("declared_skill_id", "foreign") in f.values for f in report.findings)


@pytest.mark.parametrize("where", ["task", "phase", "attempt", "rubric"])
def test_t05_task_and_phase_scope(where):
    s, a = inputs()
    if where == "task":
        s["tasks"][0]["task_id"] = "foreign"
    elif where == "phase":
        s["tasks"][0]["phase"] = "FINAL_EXAM"
    elif where == "attempt":
        s["attempts"][0]["task_id"] = "foreign"
    else:
        s["rubrics"][0]["phase"] = "FINAL_EXAM"
    assert any(f.status == "CONFLICT" for f in check(s, a).findings)


@pytest.mark.parametrize("change", [lambda b: b + b" ", lambda b: b.replace(b"\n", b"\r\n"),
                                    lambda b: b.replace(b"SYNTHETIC", b"SYNTHETIX", 1)])
def test_t06_t18_same_name_different_result_bytes(change):
    s, a = inputs(); a["result"] = change(a["result"])
    assert has(check(s, a), "RESULT_SHA256", "CONFLICT")


def test_t06_unicode_normalization_changes_hash():
    s, a = inputs(); a["SYNTHETIC-SOURCE"] = a["SYNTHETIC-SOURCE"].replace("e\u0301".encode(), "é".encode())
    assert has(check(s, a), "ARTIFACT_SHA256", "CONFLICT")


@pytest.mark.parametrize("kind", ["missing", "quote", "length", "range", "hash", "undeclared", "partial"])
def test_t07_invalid_evidence(kind):
    s, a = inputs(); ref = s["tasks"][0]["task_statement_ref"]
    if kind == "missing":
        del a["SYNTHETIC-SOURCE"]
    elif kind == "quote":
        ref["quote"] += " "
    elif kind == "length":
        s["source_artifacts"][0]["byte_length"] += 1
    elif kind == "range":
        ref["selector"]["end"] += 1
    elif kind == "hash":
        ref["sha256"] = "0" * 64
    elif kind == "undeclared":
        ref["artifact_id"] = "not-approved"
    else:
        s["source_artifacts"].append({**s["source_artifacts"][0], "artifact_id": "absent"})
    report = check(s, a)
    assert any(f.status in {"ERROR", "CONFLICT", "MISSING"} for f in report.findings)
    if kind == "partial":
        assert has(report, "REF_RESOLVED", "PASS")


@pytest.mark.parametrize("start,end,ok", [(0, 2, True), (1, 2, False), (0, 1, False), (0, 3, True), (2, 4, False)])
def test_utf8_boundaries(start, end, ok):
    sel = Selector.model_validate(dict(kind="UTF8_BYTE_RANGE", pointer=None, start=start, end=end))
    if ok:
        assert select("ä!".encode(), sel).text
    else:
        with pytest.raises(ArtifactError):
            select("ä!".encode(), sel)


@pytest.mark.parametrize("pointer,expected", [("/a~1b/~0key/0", "ä"), ("/0", "object zero"), ("/a~1b/~0key/1", None)])
def test_rfc6901_escape_object_and_array(pointer, expected):
    sel = Selector.model_validate(dict(kind="JSON_POINTER", pointer=pointer, start=None, end=None))
    assert select(raw({"a/b": {"~key": ["ä", None]}, "0": "object zero"}), sel).value == expected


@pytest.mark.parametrize("pointer", ["/x/00", "/x/-", "/x/-1", "/x/3", "/x/99999999999999999999999", "/x/0/no", "/missing"])
def test_bad_pointer_targets(pointer):
    sel = Selector.model_validate(dict(kind="JSON_POINTER", pointer=pointer, start=None, end=None))
    with pytest.raises(ArtifactError):
        select(b'{"x":[1]}', sel)


def test_pointer_message_identity_and_quote_semantics():
    s, a = inputs(); ref = s["tasks"][0]["task_statement_ref"]
    a["json-source"] = b'{"message":{"source_message_id":"m1","text":"hello"}}'
    s["source_artifacts"].append(dict(s["source_artifacts"][0], artifact_id="json-source",
        sha256=sha256(a["json-source"]), byte_length=len(a["json-source"]), media_type="application/json"))
    ref.update(artifact_id="json-source", sha256=sha256(a["json-source"]), quote=None, source_message_id="m2",
               selector=dict(kind="JSON_POINTER", pointer="/message", start=None, end=None))
    assert has(check(s, a), "REF_MESSAGE_ID", "CONFLICT")
    ref["quote"] = '{"text":"hello"}'
    assert has(check(s, a), "REF_NONSTRING_QUOTE", "UNKNOWN")
    ref.update(quote="hello", source_message_id="m1")
    ref["selector"]["pointer"] = "/message/text"
    assert has(check(s, a), "REF_MESSAGE_ID_UNVERIFIED", "UNVERIFIED")


@pytest.mark.parametrize("bad,code", [(b'{"x":1,"x":2}', "DUPLICATE_JSON_KEY"), (b'{"x":NaN}', "NONFINITE_JSON_NUMBER"),
    (b'{"x":Infinity}', "NONFINITE_JSON_NUMBER"), (b'{"x":-Infinity}', "NONFINITE_JSON_NUMBER"),
    (b'{"x":1e999}', "NONFINITE_JSON_NUMBER"), (b'\xff', "INVALID_UTF8"), (b'{', "INVALID_JSON"),
    (b'"\\ud800"', "INVALID_UNICODE_SCALAR"), (b'1' * 129, "NUMBER_LIMIT"),
    (b'[' * 49 + b'0' + b']' * 49, "DEPTH_LIMIT"), (b'"' + b'x' * 65537 + b'"', "STRING_LIMIT"),
    (b'[' + b'0,' * 50000 + b'0]', "NODE_LIMIT"), (b' ' * (MAX_BYTES + 1), "BYTE_LIMIT")], ids=lambda value: value if isinstance(value, str) else "synthetic-bytes")
def test_s03_bounded_strict_loader(bad, code):
    with pytest.raises(ArtifactError, match=code):
        load_json_bytes(bad)
    report = validate_evidence_bytes(bad, artifacts={})
    assert has(report, code, "ERROR")


@pytest.mark.parametrize("name", ["../data", "..", "D:\\data", "C:/data", "\\\\server\\share", "/data", "https://example.org", "a/b", "a\\b"])
def test_s04_paths_urls_unc_are_never_authority(name):
    with pytest.raises(ArtifactError, match="INVALID_ARTIFACT_ID"):
        ArtifactStore({name: b"text"})


def test_s04_nonbytes_cannot_read_symlink_or_junction(tmp_path):
    target = tmp_path / "target"; target.write_bytes(b"SYNTHETIC")
    for value in (target, str(target), bytearray(b"text")):
        with pytest.raises(ArtifactError, match="BYTES_REQUIRED"):
            ArtifactStore({"explicit": value})
    # No path conversion or opening exists, regardless of symlink privileges.
    assert ArtifactStore({"explicit": b"SYNTHETIC"}).get("explicit") == b"SYNTHETIC"


def test_store_count_total_snapshot_and_missing():
    with pytest.raises(ArtifactError):
        ArtifactStore({f"a{i}": b"" for i in range(65)})
    with pytest.raises(ArtifactError, match="TOTAL_BYTE_LIMIT"):
        ArtifactStore({f"a{i}": b"x" * MAX_BYTES for i in range(MAX_TOTAL_BYTES // MAX_BYTES + 1)})
    original = {"a": b"old"}; store = ArtifactStore(original); original["a"] = b"new"
    assert store.get("a") == b"old"
    with pytest.raises(ArtifactError, match="ARTIFACT_MISSING"):
        store.get("absent")


@pytest.mark.parametrize("mutation,code", [
    ("cycle", "EVENT_GRAPH_CYCLE"), ("missing", "EVENT_PARENT_MISSING"),
    ("sequence", "EVENT_SEQUENCE_DUPLICATE"), ("reverse_sequence", "EVENT_PARENT_SEQUENCE"),
    ("foreign_attempt", "EVENT_ATTEMPT_REFERENCE"), ("correction", "CORRECTION_TARGET_MISSING"),
    ("order", "CORRECTION_ORDER"), ("assessment", "ATTEMPT_ASSESSMENT_MISSING"),
    ("revision", "ASSESSMENT_PREVIOUS_MISSING"), ("feedback", "ASSISTANCE_FEEDBACK_EVENT_TYPE")])
def test_t08_cross_object_graphs(mutation, code):
    s, a = inputs()
    if mutation == "cycle":
        s["events"][0]["parent_event_ids"] = [s["events"][1]["event_id"]]
        s["events"][1]["parent_event_ids"] = [s["events"][0]["event_id"]]
    elif mutation == "missing":
        s["events"][0]["parent_event_ids"] = ["absent"]
    elif mutation == "sequence":
        s["events"][0]["collector_sequence"] = s["events"][1]["collector_sequence"] = 1
    elif mutation == "reverse_sequence":
        s["events"][0]["collector_sequence"] = 2
        s["events"][1].update(collector_sequence=1, parent_event_ids=["E-TASK"])
    elif mutation == "foreign_attempt":
        s["events"][2]["attempt_id"] = "foreign"
    elif mutation == "correction":
        s["attempts"][1]["correction_attempt"]["corrects_attempt_id"] = "absent"
    elif mutation == "order":
        s["attempts"][0]["attempt_order"] = 3
    elif mutation == "assessment":
        s["attempts"][0]["assessment"] = ["absent"]
    elif mutation == "revision":
        s["assessments"][0]["supersedes_assessment_id"] = "absent"
    else:
        s["attempts"][0]["feedback_after_attempt"] = ["E-TASK"]
    assert has(check(s, a), code, "CONFLICT")


def test_t08_duplicate_order_self_parent_and_pointer_syntax_rejected_by_i1_gate():
    for kind in ("order", "self", "pointer"):
        s, a = inputs()
        if kind == "order":
            s["attempts"][1]["attempt_order"] = 1
        elif kind == "self":
            s["events"][0]["parent_event_ids"] = [s["events"][0]["event_id"]]
        else:
            s["events"][0]["source_ref"]["selector"] = dict(kind="JSON_POINTER", pointer="/a~2", start=None, end=None)
        assert has(check(s, a), "SIDECAR_SCHEMA_INVALID", "ERROR")


def set_time(claim, value, start=None, end=None):
    claim.update(asserted_at=value, interval_start=start, interval_end=end,
                 timezone_or_offset="SYNTHETIC offset per timestamp")


def test_t09_equal_overlapping_and_received_only_times_are_unknown():
    for mode in ("equal", "overlap", "received"):
        s, a = inputs(); first, second = [x["answer_time_claim"] for x in s["attempts"]]
        set_time(first, "2026-10-25T02:30:00+02:00")
        set_time(second, "2026-10-25T02:30:00+02:00")
        if mode == "overlap":
            first.update(interval_start="2026-10-25T02:00:00+02:00", interval_end="2026-10-25T03:00:00+01:00")
        elif mode == "received":
            first.update(asserted_at=None, received_at="2026-10-25T03:00:00+01:00")
        assert has(check(s, a), "ATTEMPT_ORDER_TIME", "UNKNOWN")


def test_t20_dst_offsets_compare_instants_not_wallclock():
    s, a = inputs()
    set_time(s["attempts"][0]["answer_time_claim"], "2026-10-25T02:30:00+02:00")
    set_time(s["attempts"][1]["answer_time_claim"], "2026-10-25T02:30:00+01:00")
    assert has(check(s, a), "ATTEMPT_ORDER_TIME", "UNVERIFIED")
    s["attempts"][0]["answer_time_claim"], s["attempts"][1]["answer_time_claim"] = s["attempts"][1]["answer_time_claim"], s["attempts"][0]["answer_time_claim"]
    assert has(check(s, a), "ATTEMPT_ORDER_TIME", "CONFLICT")


@pytest.mark.parametrize("value", ["2026-10-25T02:30:00", "2026-10-25T02:30:00+99:00", "not-a-time"])
def test_t20_bad_offsets_and_times(value):
    s, a = inputs(); s["attempts"][0]["answer_time_claim"]["asserted_at"] = value
    assert has(check(s, a), "SIDECAR_SCHEMA_INVALID", "ERROR")


def test_offset_label_disagreement():
    s, a = inputs(); s["attempts"][0]["answer_time_claim"]["timezone_or_offset"] = "+02:00"
    assert has(check(s, a), "TIME_OFFSET_LABEL", "CONFLICT")


def test_h02_feedback_after_a1_is_assistance_before_a2_only():
    r = check()
    assert has(r, "FEEDBACK_AFTER_TIME", "UNVERIFIED")
    assert has(r, "ASSISTANCE_BEFORE_TIME", "UNVERIFIED")
    s, a = inputs()
    s["attempts"][0]["assistance_before_attempt"] = deepcopy(s["attempts"][1]["assistance_before_attempt"])
    s["attempts"][0]["independence_status"] = "GUIDED"
    assert has(check(s, a), "ASSISTANCE_BEFORE_TIME", "CONFLICT")


@pytest.mark.parametrize("kind,code", [("hash", "RUBRIC_DECLARATION_HASH"), ("time", "RUBRIC_BEFORE_ANSWER_TIME"),
                                     ("revision", "TASK_RUBRIC_REVISION"), ("event", "RUBRIC_DECLARATION_EVENT")])
def test_t10_rubric_conflicts(kind, code):
    s, a = inputs()
    if kind == "hash": s["rubrics"][0]["content_sha256"] = "0" * 64
    elif kind == "time": s["events"][1]["time_claim"]["asserted_at"] = "2026-10-26T12:00:00+01:00"
    elif kind == "revision": s["tasks"][0]["rubric_ref"]["revision"] = 2
    else: s["rubrics"][0]["declaration_event_ref"] = "absent"
    assert has(check(s, a), code, "MISSING" if kind == "event" else "CONFLICT")


def test_t11_joint_replacement_still_unverified_and_review_required():
    s, a = inputs()
    s["rubrics"][0]["domain"] = "SYNTHETIC changed rule"
    # All event/rubric hashes already agree: this cannot prove these changed fields.
    r = check(s, a)
    assert has(r, "RUBRIC_HASH_REPRESENTATION", "UNVERIFIED")
    assert has(r, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert r.human_review_required


def test_s07_replay_new_result_and_revision_detected():
    s, a = inputs(); r = json.loads(a["result"]); r["tasks"][0]["fraction"] = 1.0
    a["result"] = raw(r)
    report = check(s, a, handoff_revision=2)
    assert has(report, "RESULT_SHA256", "CONFLICT")
    assert has(report, "HANDOFF_REVISION", "CONFLICT")


def test_missing_revision_never_claims_checked():
    report = check(handoff_revision=None)
    assert has(report, "HANDOFF_REVISION_UNAVAILABLE", "UNKNOWN")
    assert not has(report, "HANDOFF_REVISION", "PASS")


def test_t16_score_conflict_keeps_original_values_evidence_and_decimal_context():
    s, a = inputs(); r = json.loads(a["result"]); r["tasks"][0]["fraction"] = 1.0
    rebind(s, a, "result", r); before = deepcopy((s, a))
    with localcontext() as ctx:
        ctx.prec = 2
        report = check(s, a)
    conflict = next(f for f in report.findings if f.code == "RESULT_SCORE" and f.status == "CONFLICT")
    assert ("sidecar_awarded", "80") in conflict.values
    assert ("v1_fraction", "1.0") in conflict.values
    assert any(k == "sidecar_evidence_path" for k, v in conflict.values)
    assert has(report, "RESULT_SCORE", "PASS")  # Separate 100/100 Skill.
    assert (s, a) == before


def test_no_automatic_best_latest_projection_or_pending_zero():
    assert has(check(score_assessment_ids={}), "SCORE_ATTEMPT_NOT_SELECTED", "UNKNOWN")
    s, a = inputs(); s["assessments"][0]["criterion_awards"][0].update(status="PENDING", awarded_points=None)
    assert has(check(s, a), "SCORE_PENDING", "UNKNOWN")
    assert has(check(score_assessment_ids={"TASK-1": "absent"}), "SCORE_PROJECTION_SCOPE", "CONFLICT")


def test_draft_missing_time_no_fabrication():
    base = Path(__file__).parent / "fixtures" / "evidence_sidecar_i1" / "f2_missing_draft.json"
    s = json.loads(base.read_bytes()); before = deepcopy(s)
    r = validate_evidence_bytes(raw(s), artifacts={})
    assert has(r, "UNBOUND_DRAFT", "UNKNOWN")
    assert not has(r, "RESULT_MISSING")
    assert s == before and r.human_review_required


def test_t14_t15_t17_legacy_v1_survives_broken_optional_sidecar():
    fix = Path(__file__).parent / "fixtures" / "scoring_c1"
    result_bytes = (fix / "historical_result.json").read_bytes()
    handoff_bytes = (fix / "historical_handoff.json").read_bytes()
    assert sha256(result_bytes) == "1b545e372484c256c1ecaccc50c6118fa6408b79a9a7735d0935732ba27f70d0"
    assert sha256(handoff_bytes) == "535c1e148ff4d4941e3e677e64f1cd2523ec396c6fc646934f503ec39e5a3ff7"
    original = ScoringResultV1.model_validate(load_json_bytes(result_bytes))
    report = validate_evidence_bytes(b'{"broken":', artifacts={"result": result_bytes})
    assert has(report, "INVALID_JSON", "ERROR")
    assert original == ScoringResultV1.model_validate(load_json_bytes(result_bytes))


def test_s05_no_database_import_filesystem_or_network_on_validator_import():
    # Fresh interpreter catches hidden runtime imports without production configuration.
    code = "import sys; from uniflow.application.evidence_validator_i2 import validate_evidence_bytes; assert not any(x.startswith(('uniflow.infrastructure', 'sqlalchemy', 'socket')) for x in sys.modules)"
    run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_s05_untrusted_source_is_inert_and_hash_checked(tmp_path):
    s, a = inputs(); payload = b"__import__('os').system('echo UNTRUSTED'); <script>alert(1)</script>"
    a["SYNTHETIC-SOURCE"] = payload
    r = check(s, a)
    assert has(r, "ARTIFACT_SHA256", "CONFLICT")
    assert list(tmp_path.iterdir()) == []


def test_s05_validator_cannot_open_files_or_network(monkeypatch):
    import builtins
    import socket
    s, a = inputs(); encoded = raw(s)
    def deny(*args, **kwargs):
        raise AssertionError("Unexpected I/O")
    monkeypatch.setattr(builtins, "open", deny)
    monkeypatch.setattr(Path, "open", deny)
    monkeypatch.setattr(socket, "socket", deny)
    report = validate_evidence_bytes(encoded, artifacts=a, handoff_artifact_id="handoff", result_artifact_id="result")
    assert has(report, "RESULT_SHA256", "PASS")


def test_t08_correction_and_assessment_revision_cycles():
    s, a = inputs()
    s["attempts"][0].update(attempt_order=3, independence_status="GUIDED",
        correction_attempt={"corrects_attempt_id": "A2", "kind": "GUIDED_CORRECTION"})
    assert has(check(s, a), "CORRECTION_CYCLE", "CONFLICT")
    s, a = inputs()
    second = deepcopy(s["assessments"][0]); second.update(assessment_id="AS-R2", revision=2,
        supersedes_assessment_id="ASSESSMENT-A1")
    s["assessments"][0]["supersedes_assessment_id"] = "AS-R2"
    s["assessments"].append(second); s["attempts"][0]["assessment"].append("AS-R2")
    assert has(check(s, a), "ASSESSMENT_REVISION_CYCLE", "CONFLICT")


def test_t08_causal_help_contradiction_without_precise_times():
    s, a = inputs()
    s["attempts"][0]["assistance_before_attempt"] = deepcopy(s["attempts"][1]["assistance_before_attempt"])
    s["attempts"][0]["independence_status"] = "GUIDED"
    # Parent E-A1 -> E-EXPLANATION contradicts help before A1 even with equal times.
    s["events"][4]["time_claim"] = deepcopy(s["events"][2]["time_claim"])
    assert has(check(s, a), "ASSISTANCE_FEEDBACK_CAUSAL_ORDER", "CONFLICT")


def test_t08_foreign_event_parent_task():
    s, a = inputs()
    t = deepcopy(s["tasks"][0]); t["task_id"] = "TASK-2"; s["tasks"].append(t)
    s["events"][0]["task_id"] = "TASK-2"
    s["events"][1]["parent_event_ids"] = ["E-TASK"]
    assert has(check(s, a), "EVENT_PARENT_TASK", "CONFLICT")


def test_t08_missing_and_wrong_assessment_ownership():
    s, a = inputs(); s["assessments"][0]["attempt_id"] = "absent"
    assert has(check(s, a), "ASSESSMENT_ATTEMPT_MISSING", "CONFLICT")
    s, a = inputs(); s["attempts"][0]["assessment"] = []
    assert has(check(s, a), "ASSESSMENT_BACKREFERENCE", "CONFLICT")


def test_missing_and_malformed_originals_and_result_version():
    for role in ("result", "handoff"):
        s, a = inputs(); del a[role]
        assert has(check(s, a), role.upper() + "_ARTIFACT_MISSING", "MISSING")
        s, a = inputs(); a[role] = b'{"x":1,"x":2}'
        assert has(check(s, a), role.upper() + "_DUPLICATE_JSON_KEY", "ERROR")
    s, a = inputs(); r = json.loads(a["result"]); r["contract_version"] = "unknown"
    rebind(s, a, "result", r)
    assert has(check(s, a), "RESULT_SCHEMA_INVALID", "ERROR")


def test_incoherent_handoff_does_not_confirm_skill_scope():
    s, a = inputs(); h = json.loads(a["handoff"])
    h["return_contract"]["allowed_skill_ids"] = ["foreign"]
    rebind(s, a, "handoff", h)
    report = check(s, a)
    assert has(report, "HANDOFF_SKILL_INVENTORY", "CONFLICT")
    assert not has(report, "SKILL_SCOPE", "PASS")


def test_json_source_is_strict_even_without_pointer_and_event_hash_is_independent():
    s, a = inputs(); s["source_artifacts"][0]["media_type"] = "application/json"
    assert has(check(s, a), "INVALID_JSON", "ERROR")
    s, a = inputs(); s["events"][0]["content_sha256"] = "0" * 64
    assert has(check(s, a), "EVENT_CONTENT_SHA256", "CONFLICT")


def test_frozen_report_and_declared_approval_cannot_approve_validator():
    s, a = inputs()
    # I1 already carries an explicit review record in some variants; no decision
    # can change the report's mandatory review flag.
    report = check(s, a)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        report.human_review_required = False


def test_unknown_revision_invalid_projection_and_schema_root():
    assert has(check(handoff_revision=True), "HANDOFF_REVISION_INVALID", "ERROR")
    assert has(check(score_assessment_ids=[]), "SCORE_PROJECTION_INVALID", "ERROR")
    assert has(validate_evidence_bytes(b'[]', artifacts={}), "SIDECAR_SCHEMA_INVALID", "ERROR")
    assert load_json_bytes(b'{"text":"[\\\"{quoted}\\\"]"}') == {"text": '["{quoted}"]'}


def test_extreme_aware_dates_do_not_overflow_utc_conversion():
    s, a = inputs()
    set_time(s["attempts"][0]["answer_time_claim"], "0001-01-01T00:00:00+23:59")
    set_time(s["attempts"][1]["answer_time_claim"], "9999-12-31T23:59:59-23:59")
    assert has(check(s, a), "ATTEMPT_ORDER_TIME", "UNVERIFIED")


def test_explicit_context_has_limits_and_bad_roles_are_structured():
    s, a = inputs()
    for role in ([], Path("D:/not-read"), "../no-access"):
        report = validate_evidence_bytes(raw(s), artifacts=a, handoff_artifact_id=role)
        assert has(report, "ROLE_ARTIFACT_ID_INVALID", "ERROR")
    assert has(check(score_assessment_ids={str(i): "x" for i in range(201)}), "SCORE_PROJECTION_INVALID", "ERROR")


@pytest.mark.parametrize("missing", ["rubric_id", "revision"])
def test_review_r_i2_01_real_i1_guarantees_assessment_rubric(missing):
    from pydantic import ValidationError
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    s, a = inputs()
    ref = s["assessments"][0]["rubric_revision_ref"]
    ref[missing] = "SYNTHETIC-ABSENT" if missing == "rubric_id" else 999
    before = deepcopy((s, a))
    with pytest.raises(ValidationError, match="assessment requires a locally declared rubric revision for point checks"):
        EvidenceSidecarV1.model_validate(s)
    report = check(s, a)
    assert has(report, "SIDECAR_SCHEMA_INVALID", "ERROR")
    assert not any(f.code == "RESULT_SCORE" for f in report.findings)
    assert report == check(s, a) and report.human_review_required
    assert (s, a) == before


@pytest.mark.parametrize("dimension,index", [("TASK", 0), ("SKILL", 2)])
def test_review_r_i2_02_missing_awards_rejected_by_real_i1(dimension, index):
    from pydantic import ValidationError
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    s, a = inputs()
    removed = s["assessments"][0]["criterion_awards"].pop(index)
    rubric = s["rubrics"][0]
    definition = next(c for c in rubric["task_criteria"] + rubric["skill_criteria"]
                      if c["criterion_id"] == removed["criterion_id"])
    assert definition["dimension"] == dimension
    with pytest.raises(ValidationError, match="each criterion needs exactly one award, including explicit PENDING"):
        EvidenceSidecarV1.model_validate(s)
    report = check(s, a)
    assert has(report, "SIDECAR_SCHEMA_INVALID", "ERROR")
    assert not any(f.code in {"RESULT_SCORE", "SCORE_PENDING"} for f in report.findings)
    assert report == check(s, a) and report.human_review_required


@pytest.mark.parametrize("dimension,index", [("TASK", 0), ("SKILL", 2)])
@pytest.mark.parametrize("kind", ["unknown", "duplicate"])
def test_review_r_i2_03_unknown_and_duplicate_awards_rejected(dimension, index, kind):
    from pydantic import ValidationError
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    s, a = inputs()
    rubric = s["rubrics"][0]
    award = deepcopy(s["assessments"][0]["criterion_awards"][index])
    assert next(c for c in rubric["task_criteria"] + rubric["skill_criteria"]
                if c["criterion_id"] == award["criterion_id"])["dimension"] == dimension
    if kind == "unknown":
        award["criterion_id"] = "SYNTHETIC-UNKNOWN"
    s["assessments"][0]["criterion_awards"].append(award)
    message = "each criterion needs exactly one award" if kind == "unknown" else "duplicate criterion award"
    with pytest.raises(ValidationError, match=message):
        EvidenceSidecarV1.model_validate(s)
    report = check(s, a)
    assert has(report, "SIDECAR_SCHEMA_INVALID", "ERROR")
    assert not any(f.code == "RESULT_SCORE" for f in report.findings)
    assert report == check(s, a) and report.human_review_required


def test_review_complete_criterion_binding_in_both_dimensions():
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    s, a = inputs(); model = EvidenceSidecarV1.model_validate(s)
    assessment = model.assessments[0]
    ref = assessment.rubric_revision_ref
    rubric = next(r for r in model.rubrics if (r.rubric_id, r.revision) == (ref.rubric_id, ref.revision))
    criteria = rubric.task_criteria + rubric.skill_criteria
    awards = assessment.criterion_awards
    assert {c.criterion_id for c in criteria} == {w.criterion_id for w in awards}
    assert len(awards) == len({w.criterion_id for w in awards})
    for dimension in ("TASK", "SKILL"):
        ids = {c.criterion_id for c in criteria if c.dimension == dimension}
        assert ids and ids <= {w.criterion_id for w in awards}
    scores = [f for f in check(s, a).findings if f.code == "RESULT_SCORE"]
    assert len(scores) == 2 and all(f.status == "PASS" for f in scores)


@pytest.mark.parametrize("dimension,index", [("TASK", 0), ("SKILL", 2)])
def test_review_pending_award_remains_unknown_in_its_dimension(dimension, index):
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    s, a = inputs()
    s["assessments"][0]["criterion_awards"][index].update(status="PENDING", awarded_points=None)
    model = EvidenceSidecarV1.model_validate(s)
    assert model.assessments[0].criterion_awards[index].awarded_points is None
    report = check(s, a)
    assert has(report, "SCORE_PENDING", "UNKNOWN")
    scores = [f for f in report.findings if f.code == "RESULT_SCORE"]
    assert len(scores) == 1 and scores[0].status == "PASS"
    assert (len(scores[0].object_ids) == 3) == (dimension == "TASK")
    assert report == check(s, a) and report.human_review_required


def test_review_r_i2_04_foreign_session_preserves_only_local_passes():
    # Approved binding policy: local diagnostics survive blocked confirmation.
    from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1
    from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
    s, a = inputs()
    LearningHandoffV2.model_validate_json(a["handoff"])
    ScoringResultV1.model_validate_json(a["result"])
    s["session_id"] = "SYNTHETIC-FOREIGN-SESSION"
    EvidenceSidecarV1.model_validate(s)
    before = deepcopy((s, a)); report = check(s, a)
    assert has(report, "HANDOFF_SESSION", "CONFLICT")
    assert has(report, "RESULT_SESSION", "CONFLICT")
    assert has(report, "LOCAL_SKILL_MEMBERSHIP", "PASS")
    assert has(report, "LOCAL_RESULT_SCORE", "PASS")
    assert has(report, "SKILL_SCOPE", "UNKNOWN")
    assert has(report, "RESULT_SCORE", "UNKNOWN")
    assert not has(report, "SKILL_SCOPE", "PASS")
    assert not has(report, "RESULT_SCORE", "PASS")
    assert has(report, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert report.human_review_required and report == check(s, a)
    assert (s, a) == before


def test_review_bound_draft_and_missing_statuses_stay_separate():
    s, a = inputs(); bound = check(s, a)
    assert has(bound, "RESULT_SHA256", "PASS")
    s["result_binding"] = dict(status="UNBOUND_DRAFT", result_sha256=None,
        contract_version=None, reason="SYNTHETIC review draft")
    draft = check(s, a)
    assert has(draft, "UNBOUND_DRAFT", "UNKNOWN")
    assert not any(f.code in {"RESULT_SHA256", "RESULT_SCORE"} for f in draft.findings)
    del a["SYNTHETIC-SOURCE"]
    missing = check(s, a)
    assert has(missing, "ARTIFACT_MISSING", "MISSING")
    assert has(missing, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert bound.human_review_required and draft.human_review_required and missing.human_review_required
    assert missing == check(s, a)


@pytest.mark.parametrize("mutation,root_code", [
    ("session", "HANDOFF_SESSION"), ("handoff_id", "HANDOFF_ID"),
    ("handoff_hash", "HANDOFF_SHA256"), ("result_hash", "RESULT_SHA256"),
    ("revision", "HANDOFF_REVISION"),
])
def test_r04_binding_conflicts_block_confirmation_but_keep_local_numbers(mutation, root_code):
    s, a = inputs()
    if mutation == "session": s["session_id"] = "SYNTHETIC-FOREIGN"
    elif mutation == "handoff_id": s["handoff_id"] = "SYNTHETIC-FOREIGN"
    elif mutation == "handoff_hash": s["handoff_artifact_sha256"] = "0" * 64
    elif mutation == "result_hash": s["result_binding"]["result_sha256"] = "0" * 64
    report = check(s, a, handoff_revision=2 if mutation == "revision" else 1)
    assert has(report, root_code, "CONFLICT")
    assert has(report, "LOCAL_RESULT_SCORE", "PASS")
    assert not has(report, "RESULT_SCORE", "PASS")
    assert has(report, "RESULT_SCORE", "UNKNOWN")
    if mutation != "result_hash":
        assert not has(report, "SKILL_SCOPE", "PASS")
    else:
        # Sidecar-to-handoff membership has no dependency on the result bytes.
        assert any(f.code == "SKILL_SCOPE" and f.status == "PASS" and f.location == "/rubrics" for f in report.findings)
        assert not any(f.code == "SKILL_SCOPE" and f.status == "PASS" and f.location.startswith("/result") for f in report.findings)
    assert report.human_review_required
    assert report == check(s, a, handoff_revision=2 if mutation == "revision" else 1)


def test_r04_complete_binding_dependencies_and_trust_are_separate():
    report = check()
    assert report.validator_version == "uniflow-evidence-validator-i2.1"
    for code in ("HANDOFF_BINDING", "RESULT_BINDING", "SKILL_SCOPE", "RESULT_SCORE"):
        fs = [f for f in report.findings if f.code == code]
        assert fs and all(f.status == "PASS" for f in fs)
        assert all(any(k.startswith("requires.") for k, v in f.values) for f in fs)
        assert all(v == "PASS" for f in fs for k, v in f.values if k.startswith("requires."))
    assert has(report, "REVISION_CONTEXT_UNAUTHENTICATED", "UNVERIFIED")
    assert has(report, "NO_EXTERNAL_TRUST_ANCHOR", "UNVERIFIED")
    assert report.human_review_required


def test_r04_missing_revision_is_not_fabricated_or_treated_as_conflict():
    report = check(handoff_revision=None)
    assert has(report, "HANDOFF_REVISION_UNAVAILABLE", "UNKNOWN")
    assert has(report, "LOCAL_RESULT_SCORE", "PASS")
    assert has(report, "RESULT_SCORE", "UNKNOWN")
    assert not has(report, "SKILL_SCOPE", "PASS")
    assert not any(f.status == "CONFLICT" for f in report.findings)
    handoff = next(f for f in report.findings if f.code == "HANDOFF_BINDING")
    assert ("requires.HANDOFF_REVISION", "UNKNOWN") in handoff.values


@pytest.mark.parametrize("role", ["handoff", "result"])
def test_r04_missing_original_has_no_confirmed_score(role):
    s, a = inputs(); del a[role]
    report = check(s, a)
    assert has(report, role.upper() + "_ARTIFACT_MISSING", "MISSING")
    assert not has(report, "RESULT_SCORE", "PASS")
    if role == "handoff":
        assert has(report, "LOCAL_RESULT_SCORE", "PASS")
        assert has(report, "RESULT_SCORE", "UNKNOWN")
    else:
        assert has(report, "SCORE_RESULT_UNAVAILABLE", "UNKNOWN")
        assert not has(report, "LOCAL_RESULT_SCORE")


@pytest.mark.parametrize("selection", [{}, {"TASK-1": "absent"}, []])
def test_r04_missing_invalid_selection_never_confirms_score(selection):
    report = check(score_assessment_ids=selection)
    assert not has(report, "RESULT_SCORE", "PASS")
    assert report.human_review_required


def test_r04_draft_keeps_handoff_scope_without_assuming_final_result():
    s, a = inputs()
    s["result_binding"] = dict(status="UNBOUND_DRAFT", result_sha256=None,
        contract_version=None, reason="SYNTHETIC draft")
    report = check(s, a)
    assert has(report, "UNBOUND_DRAFT", "UNKNOWN")
    assert has(report, "HANDOFF_BINDING", "PASS")
    assert has(report, "SKILL_SCOPE", "PASS")  # Only handoff membership.
    assert not has(report, "RESULT_BINDING", "PASS")
    assert not has(report, "RESULT_SCORE")
    assert not has(report, "LOCAL_RESULT_SCORE")


def test_r04_score_and_hash_conflicts_both_survive_blocked_binding():
    s, a = inputs(); result = json.loads(a["result"])
    result["tasks"][0]["fraction"] = 1.0
    a["result"] = raw(result)  # Keep the stale declared hash deliberately.
    report = check(s, a)
    assert has(report, "RESULT_SHA256", "CONFLICT")
    assert has(report, "LOCAL_RESULT_SCORE", "CONFLICT")
    assert has(report, "RESULT_SCORE", "CONFLICT")
    assert has(report, "RESULT_SCORE", "UNKNOWN")  # Equal skill, blocked binding.
    assert not has(report, "RESULT_SCORE", "PASS")


@pytest.mark.parametrize("mismatch", ["task_phase", "rubric_revision", "backreference", "difficulty"])
def test_r04_projection_context_is_per_task_and_required(mismatch):
    s, a = inputs()
    if mismatch == "task_phase": s["tasks"][0]["phase"] = "FINAL_EXAM"
    elif mismatch == "rubric_revision": s["tasks"][0]["rubric_ref"]["revision"] = 999
    elif mismatch == "backreference": s["attempts"][0]["assessment"] = []
    else: s["rubrics"][0]["difficulty"] = "HARD"
    report = check(s, a)
    assert has(report, "LOCAL_RESULT_SCORE", "PASS")
    assert has(report, "RESULT_SCORE", "UNKNOWN")
    assert not has(report, "RESULT_SCORE", "PASS")
    assert any(f.status == "CONFLICT" for f in report.findings)


def test_r04_foreign_skill_conflict_is_not_hidden_by_session_conflict():
    s, a = inputs(); s["session_id"] = "SYNTHETIC-FOREIGN"
    result = json.loads(a["result"]); result["tasks"][0]["skill_marks"][0]["skill_id"] = "foreign"
    rebind(s, a, "result", result)
    report = check(s, a)
    assert has(report, "HANDOFF_SESSION", "CONFLICT")
    assert has(report, "LOCAL_SKILL_MEMBERSHIP", "CONFLICT")
    assert has(report, "SKILL_SCOPE", "CONFLICT")
    assert not has(report, "SKILL_SCOPE", "PASS")


def test_r04_selected_assessment_revision_chain_is_required():
    s, a = inputs(); s["assessments"][0]["supersedes_assessment_id"] = "SYNTHETIC-ABSENT"
    report = check(s, a)
    assert has(report, "ASSESSMENT_PREVIOUS_MISSING", "CONFLICT")
    assert has(report, "LOCAL_RESULT_SCORE", "PASS")
    assert not has(report, "RESULT_SCORE", "PASS")
    assert has(report, "SCORE_CONTEXT_ASSESSMENT_REVISION_CHAIN", "CONFLICT")


def test_r04_context_of_other_task_does_not_poison_valid_projection():
    s, a = inputs()
    task = deepcopy(s["tasks"][0]); task["task_id"] = "TASK-2"
    rubric = deepcopy(s["rubrics"][0]); rubric.update(rubric_id="R2", task_id="TASK-2", declaration_event_ref=None)
    task["rubric_ref"]["rubric_id"] = "R2"
    attempt = deepcopy(s["attempts"][0]); attempt.update(task_id="TASK-2", attempt_id="B1", assessment=["ASSESSMENT-B1"], feedback_after_attempt=[])
    assessment = deepcopy(s["assessments"][0]); assessment.update(assessment_id="ASSESSMENT-B1", attempt_id="B1")
    assessment["rubric_revision_ref"]["rubric_id"] = "R2"
    s["tasks"].append(task); s["rubrics"].append(rubric); s["attempts"].append(attempt); s["assessments"].append(assessment)
    result = json.loads(a["result"]); rt = deepcopy(result["tasks"][0]); rt["task_id"] = "TASK-2"; result["tasks"].append(rt)
    rebind(s, a, "result", result)
    s["tasks"][0]["rubric_ref"]["revision"] = 999
    report = check(s, a, score_assessment_ids={"TASK-1": "ASSESSMENT-A1", "TASK-2": "ASSESSMENT-B1"})
    first = [f for f in report.findings if f.code == "RESULT_SCORE" and f.object_ids[0] == "TASK-1"]
    second = [f for f in report.findings if f.code == "RESULT_SCORE" and f.object_ids[0] == "TASK-2"]
    assert first and all(f.status == "UNKNOWN" for f in first)
    assert second and all(f.status == "PASS" for f in second)


def test_r04_report_reader_keeps_legacy_version_without_reinterpretation():
    from uniflow.schemas.evidence_validation_i2 import EvidenceValidationReport
    report = check()
    legacy = report.model_dump(mode="json")
    legacy["validator_version"] = "uniflow-evidence-validator-i2.0"
    restored = EvidenceValidationReport.model_validate_json(json.dumps(legacy))
    assert restored.validator_version == "uniflow-evidence-validator-i2.0"
    assert restored.findings == report.findings  # Reading does not rewrite statuses.
    assert report.validator_version == "uniflow-evidence-validator-i2.1"
