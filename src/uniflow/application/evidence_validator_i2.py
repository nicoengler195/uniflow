"""Pure, opt-in I2 reference validator. No import of runtime/config/database code."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
import re

from pydantic import ValidationError

from uniflow.application.evidence_artifacts_i2 import (
    ArtifactError, ArtifactStore, MAX_STRING, ID_PATTERN, load_json_bytes, select, sha256,
)
from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1, EvidenceRef
from uniflow.schemas.evidence_validation_i2 import CheckedArtifact, EvidenceValidationReport, Finding
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
from uniflow.schemas.scoring_contract_v1 import ScoredTaskV1, ScoringResultV1

NOT_CHECKED = (
    "Mathematical truth, rubric quality and mastery",
    "Authenticated authorship, observer identity, pre-answer existence and complete assistance history",
    "Filesystem/Windows path loading (bytes-only API); product integration and review UI",
    "Global latest revision or deletion of history without an external trusted inventory",
    "JSON-pointer original lexical byte spans and free-text timezone/precision semantics",
)


def _walk(value, path=""):
    """Bounded I1 model traversal retaining each locator's exact report path."""
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk(child, path + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + "/" + str(index))


def _cycle_nodes(graph):
    # Iterative DFS avoids Python recursion for long, shallowly encoded graphs.
    color, active, found = {}, [], set()
    for root in graph:
        if color.get(root):
            continue
        stack = [(root, iter(graph[root]))]
        color[root] = 1
        active.append(root)
        while stack:
            node, edges = stack[-1]
            target = next(edges, None)
            if target is None:
                color[node] = 2
                active.pop()
                stack.pop()
            elif target in graph:
                if color.get(target) == 1:
                    found.update(active[active.index(target):])
                elif not color.get(target):
                    color[target] = 1
                    active.append(target)
                    stack.append((target, iter(graph[target])))
    return tuple(sorted(found))


def _window(claim):
    # received_at is a transport time, never an event time.
    def instant(s):
        # Aware datetime comparisons account for offsets without converting to
        # an out-of-range year at the datetime.min/max boundaries.
        return datetime.fromisoformat(s)
    if claim.interval_start is not None:
        return instant(claim.interval_start), instant(claim.interval_end)
    if claim.observed_at is not None:
        t = instant(claim.observed_at)
        return t, t
    if claim.asserted_at is not None:
        t = instant(claim.asserted_at)
        return t, t
    return None


def validate_evidence_bytes(
    sidecar_bytes: bytes, *, artifacts: dict[str, bytes],
    handoff_artifact_id: str | None = None,
    result_artifact_id: str | None = None,
    handoff_revision: int | None = None,
    score_assessment_ids: dict[str, str] | None = None,
) -> EvidenceValidationReport:
    """Check explicit originals. Revision and score projection are caller declarations.

    score_assessment_ids maps V1 task ID -> selected sidecar assessment ID; no
    first/latest/best attempt is inferred. No mutable object or original is changed.
    Invalid byte input raises ArtifactError only for non-bytes sidecar input;
    ordinary loader/schema failures are returned as structured ERROR findings.
    """
    if type(sidecar_bytes) is not bytes:
        raise ArtifactError("BYTES_REQUIRED")
    findings = []
    checks = {}
    checked = ()
    sidecar = None

    def emit(status, code, ids=(), location="", reason="", values=()):
        checks[code] = status
        findings.append(Finding(status=status, code=code, object_ids=tuple(ids),
                                location=location, reason=reason, values=tuple(values)))

    def compare(code, actual, expected, ids=(), location=""):
        ok = actual == expected
        emit("PASS" if ok else "CONFLICT", code, ids, location,
             "Exact equality." if ok else "Explicit values disagree.",
             (("actual", str(actual)), ("expected", str(expected))))
        return ok

    def confirm(code, local_status, requirements, ids=(), location="", values=()):
        # Only explicit prerequisites of this finding matter. No global green flag.
        # A proven local contradiction is never hidden behind a failed binding.
        requirements = tuple(requirements)
        status = local_status
        if local_status == "PASS" and any(state != "PASS" for _, state in requirements):
            status = "UNKNOWN"
        emit(status, code, ids, location,
             "Binding prerequisites and local comparison passed; no authenticity or human approval."
             if status == "PASS" else "Local contradiction retained."
             if status == "CONFLICT" else "Required binding or local comparison not confirmed; see prerequisites and source findings.",
             tuple(values) + (("local_status", local_status),) +
             tuple(("requires." + name, state) for name, state in requirements))

    def required(*codes):
        return tuple((code, checks.get(code, "UNKNOWN")) for code in codes)

    def report():
        binding = () if sidecar is None else tuple(sidecar.result_binding.model_dump().items())
        return EvidenceValidationReport(
            sidecar_id=sidecar.sidecar_id if sidecar else None,
            sidecar_version=sidecar.sidecar_version if sidecar else None,
            sidecar_sha256=sha256(sidecar_bytes), declared_result_binding=binding,
            checked_artifacts=checked, findings=tuple(findings), not_checked=NOT_CHECKED)

    emit("UNVERIFIED", "NO_EXTERNAL_TRUST_ANCHOR", reason=
         "Hashes and claims may be jointly replaced. No authenticated author, collector, prior rubric or independence is established.")
    try:
        store = ArtifactStore(artifacts)
        checked = tuple(CheckedArtifact(artifact_id=i, sha256=h, byte_length=n)
                        for i, h, n in store.inventory())
        sidecar = EvidenceSidecarV1.model_validate(load_json_bytes(sidecar_bytes))
    except ArtifactError as exc:
        emit("ERROR", exc.code, reason="Artifact/sidecar loader rejected input.")
        return report()
    except ValidationError as exc:
        # Do not echo arbitrary input, assertion exception objects, or sensitive text.
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:32]:
            emit("ERROR", "SIDECAR_SCHEMA_INVALID", location="/" + "/".join(map(str, error["loc"])),
                 reason="I1 contract rejected input: " + error["type"])
        return report()
    emit("PASS", "SIDECAR_SCHEMA", (sidecar.sidecar_id,), reason="Unmodified I1 schema accepted.")
    for role_id in (handoff_artifact_id, result_artifact_id):
        if role_id is not None and (type(role_id) is not str or not ID_PATTERN.fullmatch(role_id) or ".." in role_id):
            emit("ERROR", "ROLE_ARTIFACT_ID_INVALID", reason="Role must name an explicit opaque store ID.")
            return report()

    def original(artifact_id, model, role):
        if artifact_id is None:
            emit("MISSING", role + "_MISSING", reason="No explicit original supplied.")
            return None, None
        try:
            raw = store.get(artifact_id)
            parsed = model.model_validate(load_json_bytes(raw))
            emit("PASS", role + "_SCHEMA", (artifact_id,), reason="Original contract accepted.")
            return raw, parsed
        except ArtifactError as exc:
            emit("MISSING" if exc.code == "ARTIFACT_MISSING" else "ERROR", role + "_" + exc.code,
                 (artifact_id,), reason="Original could not be validated.")
        except ValidationError:
            emit("ERROR", role + "_SCHEMA_INVALID", (artifact_id,), reason="Original contract rejected.")
        return None, None

    handoff_raw, handoff = original(handoff_artifact_id, LearningHandoffV2, "HANDOFF")
    result_raw, result = (None, None)
    if sidecar.result_binding.status == "BOUND":
        emit("PASS", "RESULT_BINDING_DECLARATION", reason="BOUND declared; original verification remains separate.")
        result_raw, result = original(result_artifact_id, ScoringResultV1, "RESULT")
        # Even malformed JSON must expose byte mismatch when original bytes exist.
        if result_artifact_id is not None:
            try:
                compare("RESULT_SHA256", sha256(store.get(result_artifact_id)),
                        sidecar.result_binding.result_sha256, (sidecar.sidecar_id, result_artifact_id))
            except ArtifactError:
                pass
    else:
        emit("UNKNOWN", "RESULT_BINDING_DECLARATION", reason="Draft has no bound original result.")
        emit("UNKNOWN", "UNBOUND_DRAFT", (sidecar.sidecar_id,), reason=
             "No final result is assumed or generated. Supplied result role is ignored for this draft.")
    if handoff_artifact_id is not None:
        try:
            compare("HANDOFF_SHA256", sha256(store.get(handoff_artifact_id)),
                    sidecar.handoff_artifact_sha256, (sidecar.sidecar_id, handoff_artifact_id))
        except ArtifactError:
            pass
    if handoff_revision is None:
        emit("UNKNOWN", "HANDOFF_REVISION_UNAVAILABLE", reason=
             "Neither LearningHandoffV2 nor ScoringResultV1 has a revision field.")
    elif type(handoff_revision) is not int or handoff_revision < 1:
        emit("ERROR", "HANDOFF_REVISION_INVALID", reason="Explicit positive integer required.")
    else:
        compare("HANDOFF_REVISION", sidecar.handoff_revision, handoff_revision)
        emit("UNVERIFIED", "REVISION_CONTEXT_UNAUTHENTICATED", reason="Revision context is caller supplied, not an authenticated receipt.")
    allowed = None
    if handoff is not None:
        compare("HANDOFF_SESSION", sidecar.session_id, handoff.session_id)
        compare("HANDOFF_ID", sidecar.handoff_id, handoff.handoff_id)
        rc = handoff.return_contract
        scope_ok = all([
            compare("HANDOFF_RETURN_SESSION", rc.session_id, handoff.session_id),
            compare("HANDOFF_RETURN_ID", rc.handoff_id, handoff.handoff_id),
            compare("HANDOFF_RETURN_COURSE", rc.course_id, handoff.course.id),
            compare("HANDOFF_SKILL_INVENTORY", rc.allowed_skill_ids,
                    [s.skill_id for s in handoff.target_skills + handoff.prerequisites]),
        ])
        if not rc.allowed_skill_ids or len(set(rc.allowed_skill_ids)) != len(rc.allowed_skill_ids):
            scope_ok = False
            emit("CONFLICT", "HANDOFF_SKILL_INVENTORY_INVALID", reason="Skill inventory must be nonempty and unique.")
        if scope_ok:
            allowed = frozenset(rc.allowed_skill_ids)
    if result is not None:
        compare("RESULT_CONTRACT", result.contract_version, sidecar.result_binding.contract_version)
        compare("RESULT_SESSION", result.session_id, sidecar.session_id)
        compare("RESULT_HANDOFF_ID", result.handoff_id, sidecar.handoff_id)
        compare("RESULT_HANDOFF_SHA256", result.handoff_artifact_sha256, sidecar.handoff_artifact_sha256)
        if handoff_raw is not None:
            compare("RESULT_ORIGINAL_HANDOFF_SHA256", result.handoff_artifact_sha256, sha256(handoff_raw))
        if handoff is not None:
            compare("RESULT_ORIGINAL_SESSION", result.session_id, handoff.session_id)
            compare("RESULT_ORIGINAL_HANDOFF_ID", result.handoff_id, handoff.handoff_id)

    handoff_requirements = required(
        "HANDOFF_SCHEMA", "HANDOFF_SHA256", "HANDOFF_SESSION", "HANDOFF_ID",
        "HANDOFF_RETURN_SESSION", "HANDOFF_RETURN_ID", "HANDOFF_RETURN_COURSE",
        "HANDOFF_SKILL_INVENTORY", "HANDOFF_REVISION",
    ) + (("NONEMPTY_UNIQUE_SKILL_INVENTORY", "PASS" if allowed is not None else "UNKNOWN"),)
    confirm("HANDOFF_BINDING", "PASS", handoff_requirements)
    result_requirements = required(
        "HANDOFF_BINDING", "RESULT_BINDING_DECLARATION", "RESULT_SCHEMA", "RESULT_SHA256",
        "RESULT_CONTRACT", "RESULT_SESSION", "RESULT_HANDOFF_ID", "RESULT_HANDOFF_SHA256",
        "RESULT_ORIGINAL_HANDOFF_SHA256", "RESULT_ORIGINAL_SESSION", "RESULT_ORIGINAL_HANDOFF_ID",
    )
    confirm("RESULT_BINDING", "PASS", result_requirements)

    def skill(sid, ids, path, *, result_source=False):
        local_status = "UNKNOWN" if allowed is None else "PASS" if sid in allowed else "CONFLICT"
        values = (("declared_skill_id", sid), ("handoff_artifact_id", handoff_artifact_id or ""))
        emit(local_status, "LOCAL_SKILL_MEMBERSHIP", ids, path,
             "Local membership in the supplied internally consistent handoff inventory only; no session binding.", values)
        confirm("SKILL_SCOPE", local_status,
                required("HANDOFF_BINDING", "RESULT_BINDING") if result_source else required("HANDOFF_BINDING"),
                ids, path, values)

    tasks = {t.task_id: t for t in sidecar.tasks}
    attempts = {a.attempt_id: a for a in sidecar.attempts}
    events = {e.event_id: e for e in sidecar.events}
    assessments = {a.assessment_id: a for a in sidecar.assessments}
    rubrics = {(r.rubric_id, r.revision): r for r in sidecar.rubrics}
    result_tasks = {t.task_id: t for t in result.tasks if isinstance(t, ScoredTaskV1)} if result else {}
    sources = {s.artifact_id: s for s in sidecar.source_artifacts}
    for src in sidecar.source_artifacts:
        try:
            raw = store.get(src.artifact_id)
            compare("ARTIFACT_SHA256", sha256(raw), src.sha256, (src.artifact_id,))
            compare("ARTIFACT_LENGTH", len(raw), src.byte_length, (src.artifact_id,))
            if src.media_type.split(";", 1)[0].strip() == "application/json":
                load_json_bytes(raw)
        except ArtifactError as exc:
            emit("MISSING" if exc.code == "ARTIFACT_MISSING" else "ERROR", exc.code,
                 (src.artifact_id,), reason="Declared source unavailable or rejected.")

    def resolve(ref, path):
        if ref.artifact_id not in sources:
            emit("MISSING", "REF_ARTIFACT_UNDECLARED", (ref.artifact_id,), path, "No SourceArtifact declaration.")
            return None
        try:
            raw = store.get(ref.artifact_id)
            src = sources[ref.artifact_id]
            if (sha256(raw) != ref.sha256 or ref.sha256 != src.sha256 or len(raw) != src.byte_length):
                emit("CONFLICT", "REF_ARTIFACT_BINDING", (ref.artifact_id,), path,
                     "Reference hash/declared hash/actual bytes or length disagree.")
                return None
            selection = select(raw, ref.selector)
            if ref.quote is not None:
                if selection.text is None:
                    emit("UNKNOWN", "REF_NONSTRING_QUOTE", (ref.artifact_id,), path,
                         "JSON object/array/number has no contract-defined textual quotation.")
                    return None
                if selection.text != ref.quote:
                    emit("CONFLICT", "REF_QUOTE", (ref.artifact_id,), path,
                         "Exact selected text differs; no trimming or Unicode normalization.")
                    return None
            emit("PASS", "REF_RESOLVED", (ref.artifact_id,), path, "Selector and optional exact quotation verified.")
            if ref.source_message_id is not None:
                # Only a selected JSON object has an unambiguous message-ID field.
                if isinstance(selection.value, dict) and "source_message_id" in selection.value:
                    compare("REF_MESSAGE_ID", selection.value["source_message_id"], ref.source_message_id,
                            (ref.artifact_id,), path)
                else:
                    emit("UNVERIFIED", "REF_MESSAGE_ID_UNVERIFIED", (ref.artifact_id,), path,
                         "No contract-defined message-ID binding at selected target.")
            else:
                emit("UNKNOWN", "REF_MESSAGE_ID_ABSENT", (ref.artifact_id,), path,
                     "Message identity unavailable; locator does not authenticate provenance.")
            return selection
        except ArtifactError as exc:
            emit("MISSING" if exc.code in {"ARTIFACT_MISSING", "POINTER_TARGET_MISSING"} else "ERROR",
                 "REF_" + exc.code, (ref.artifact_id,), path, "Evidence resolution rejected input.")
            return None

    selections = {}
    for path, obj in _walk(sidecar.model_dump()):
        if {"artifact_id", "sha256", "selector", "provenance_status"} <= obj.keys():
            selections[path] = resolve(EvidenceRef.model_validate(obj), path)
        if "gap_id" in obj:
            emit("MISSING", "DECLARED_GAP", (obj["gap_id"],), path, obj["reason"])
        if {"time_status", "timezone_or_offset"} <= obj.keys():
            # Only numeric offset labels have defined, machine-checkable semantics.
            label = obj["timezone_or_offset"]
            if label and re.fullmatch(r"[+-]\d\d:\d\d", label):
                for field in ("asserted_at", "observed_at"):
                    if obj[field] is not None:
                        offset = datetime.fromisoformat(obj[field]).strftime("%z")
                        compare("TIME_OFFSET_LABEL", label.replace(":", ""), offset, location=path + "/" + field)

    def task_scope(task_id, phase, ids):
        task = tasks.get(task_id)
        if task is None:
            emit("CONFLICT", "TASK_REFERENCE", ids, reason="Task absent from local sidecar.")
        elif phase is not None:
            compare("TASK_PHASE", phase, task.phase, ids)
        return task

    for t in sidecar.tasks:
        if result is not None:
            rt = result_tasks.get(t.task_id)
            if rt is None:
                emit("CONFLICT", "RESULT_TASK_SCOPE", (t.task_id,), reason="No matching scored V1 task.")
            else:
                compare("RESULT_TASK_PHASE", t.phase, rt.phase, (t.task_id,))
        else:
            emit("UNKNOWN", "EXTERNAL_TASK_SCOPE_UNAVAILABLE", (t.task_id,), reason="Handoff does not enumerate tasks; no bound valid result.")
        r = rubrics.get((t.rubric_ref.rubric_id, t.rubric_ref.revision))
        if r is None:
            emit("CONFLICT", "TASK_RUBRIC_REVISION", (t.task_id,), reason="Task rubric revision absent.")
        else:
            compare("TASK_RUBRIC_TASK", r.task_id, t.task_id, (t.task_id, r.rubric_id))
            compare("TASK_RUBRIC_DIFFICULTY", r.difficulty, t.difficulty, (t.task_id, r.rubric_id))
        if t.transfer_of_task_id is not None:
            if t.transfer_of_task_id == t.task_id or t.transfer_of_task_id not in tasks:
                emit("CONFLICT", "TRANSFER_TASK_REFERENCE", (t.task_id,), reason="Transfer target missing or self-referential.")
    for r in sidecar.rubrics:
        task_scope(r.task_id, r.phase, (r.rubric_id,))
        for c in r.skill_criteria:
            skill(c.skill_id, (r.rubric_id, c.criterion_id), "/rubrics")
        defs = {c.criterion_id: c for c in r.task_criteria + r.skill_criteria}
        graph = {c.criterion_id: c.prerequisite_criteria for c in defs.values()}
        for c in defs.values():
            for parent in c.prerequisite_criteria:
                if parent not in defs:
                    emit("CONFLICT", "CRITERION_PREREQUISITE_MISSING", (r.rubric_id, c.criterion_id, parent), reason="Prerequisite criterion absent.")
        if _cycle_nodes(graph):
            emit("CONFLICT", "CRITERION_CYCLE", (r.rubric_id,) + _cycle_nodes(graph), reason="Cyclic prerequisite declarations.")
        e = events.get(r.declaration_event_ref)
        if e is None:
            emit("MISSING", "RUBRIC_DECLARATION_EVENT", (r.rubric_id,), reason="No declaration event available.")
        else:
            compare("RUBRIC_EVENT_TYPE", e.event_type, "RUBRIC_DECLARED", (r.rubric_id, e.event_id))
            compare("RUBRIC_EVENT_TASK", e.task_id, r.task_id, (r.rubric_id, e.event_id))
            compare("RUBRIC_DECLARATION_HASH", r.content_sha256, e.content_sha256, (r.rubric_id, e.event_id))
            emit("UNVERIFIED", "RUBRIC_HASH_REPRESENTATION", (r.rubric_id,), reason=
                 "I1 specifies no serialized rubric-body hashing convention. Event source hash alone cannot verify every rubric field.")
        emit("UNVERIFIED", "RUBRIC_PREDECLARATION", (r.rubric_id,), reason="Local hash and revision are not an external pre-answer receipt.")
    if result is not None:
        for i, rt in enumerate(result.tasks):
            for mark in rt.additional_skills:
                if mark.skill_id is not None:
                    skill(mark.skill_id, (rt.task_id,), f"/result/tasks/{i}/additional_skills", result_source=True)
            if isinstance(rt, ScoredTaskV1):
                for mark in rt.skill_marks:
                    skill(mark.skill_id, (rt.task_id,), f"/result/tasks/{i}/skill_marks", result_source=True)
        for error in result.errors:
            if error.skill_id is not None:
                skill(error.skill_id, (error.task_id,), "/result/errors", result_source=True)

    def chronology(before, after, ids, code):
        left, right = _window(before), _window(after)
        if left is None or right is None or not (left[1] < right[0] or left[0] > right[1]):
            emit("UNKNOWN", code, ids, reason="Missing event time or overlapping/equal uncertainty windows; no strict order established.")
        elif left[0] > right[1]:
            emit("CONFLICT", code, ids, reason="Declared event instants/intervals contradict the required order (UTC comparison).")
        else:
            emit("UNVERIFIED", code, ids, reason="Declared windows support order; no independent historical authentication.")

    event_graph = {e.event_id: e.parent_event_ids for e in sidecar.events}

    def ancestor(parent, child):
        pending, seen = list(event_graph.get(child, ())), set()
        while pending:
            current = pending.pop()
            if current == parent:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(event_graph.get(current, ()))
        return False

    cycles = _cycle_nodes(event_graph)
    emit("CONFLICT" if cycles else "PASS", "EVENT_GRAPH_CYCLE", cycles,
         reason="Parent cycle found." if cycles else "No parent cycle in supplied graph.")
    sequences = {}
    for index, e in enumerate(sidecar.events):
        if e.task_id is not None:
            task_scope(e.task_id, None, (e.event_id,))
        a = attempts.get(e.attempt_id)
        if e.attempt_id is not None:
            if a is None:
                emit("CONFLICT", "EVENT_ATTEMPT_REFERENCE", (e.event_id,), reason="Attempt absent.")
            else:
                compare("EVENT_ATTEMPT_TASK", e.task_id, a.task_id, (e.event_id, a.attempt_id))
                if e.event_type == "CORRECTION_SUBMITTED" and a.correction_attempt is None:
                    emit("CONFLICT", "EVENT_CORRECTION_TYPE", (e.event_id, a.attempt_id), reason="Correction event references non-correction attempt.")
                if e.event_type == "ANSWER_SUBMITTED" and a.correction_attempt is not None:
                    emit("CONFLICT", "EVENT_ANSWER_TYPE", (e.event_id, a.attempt_id), reason="Correction attempt declared as initial submission.")
                if e.event_type in {"ANSWER_SUBMITTED", "CORRECTION_SUBMITTED"}:
                    ew, aw = _window(e.time_claim), _window(a.answer_time_claim)
                    if ew and aw and (ew[1] < aw[0] or aw[1] < ew[0]):
                        emit("CONFLICT", "EVENT_ANSWER_TIME_DISJOINT", (e.event_id, a.attempt_id), reason="Submission and attempt describe disjoint times for the same answer.")
                elif e.event_type in {"ASSESSMENT_RECORDED", "FEEDBACK_DELIVERED"}:
                    chronology(a.answer_time_claim, e.time_claim, (a.attempt_id, e.event_id), "EVENT_AFTER_ANSWER_TIME")
        selection = selections.get(f"/events/{index}/source_ref")
        if selection is not None and selection.raw is not None:
            compare("EVENT_CONTENT_SHA256", sha256(selection.raw), e.content_sha256, (e.event_id,))
        else:
            emit("UNKNOWN", "EVENT_CONTENT_BYTES_UNAVAILABLE", (e.event_id,), reason="No verified original byte slice; JSON-pointer serialization is not original bytes.")
        if e.collector_sequence is not None:
            # I1 has no collector identity; use sidecar session as declared scope.
            if e.collector_sequence in sequences:
                emit("CONFLICT", "EVENT_SEQUENCE_DUPLICATE", (sequences[e.collector_sequence], e.event_id), reason="Duplicate position in declared session sequence.")
            sequences[e.collector_sequence] = e.event_id
        else:
            emit("UNKNOWN", "EVENT_SEQUENCE_ABSENT", (e.event_id,), reason="No collector position supplied; list order is not chronology.")
        for pid in e.parent_event_ids:
            p = events.get(pid)
            if p is None:
                emit("CONFLICT", "EVENT_PARENT_MISSING", (e.event_id, pid), reason="Parent event absent.")
                continue
            if p.task_id is not None and e.task_id is not None and p.task_id != e.task_id:
                emit("CONFLICT", "EVENT_PARENT_TASK", (pid, e.event_id), reason="Cross-task causal parent not permitted by I2 local graph policy.")
            if p.collector_sequence is not None and e.collector_sequence is not None and p.collector_sequence >= e.collector_sequence:
                emit("CONFLICT", "EVENT_PARENT_SEQUENCE", (pid, e.event_id), reason="Parent does not precede child in declared sequence.")
            chronology(p.time_claim, e.time_claim, (pid, e.event_id), "EVENT_PARENT_TIME")
    sequence_events = [events[sequences[n]] for n in sorted(sequences)]
    for prev, nxt in zip(sequence_events, sequence_events[1:]):
        chronology(prev.time_claim, nxt.time_claim, (prev.event_id, nxt.event_id), "EVENT_SEQUENCE_TIME")
    emit("UNVERIFIED", "COLLECTOR_SEQUENCE_UNAUTHENTICATED", reason="I1 has no collector identity or protected sequence receipt.")

    corrections = {}
    for a in sidecar.attempts:
        t = task_scope(a.task_id, a.phase, (a.attempt_id,))
        if t is not None:
            chronology(t.issued_time_claim, a.answer_time_claim, (t.task_id, a.attempt_id), "TASK_ANSWER_TIME")
            r = rubrics.get((t.rubric_ref.rubric_id, t.rubric_ref.revision))
            declaration = events.get(r.declaration_event_ref) if r else None
            if declaration:
                chronology(declaration.time_claim, a.answer_time_claim,
                           (r.rubric_id, a.attempt_id), "RUBRIC_BEFORE_ANSWER_TIME")
        emit("UNVERIFIED", "ATTEMPT_ORDER_DECLARED", (a.attempt_id,), reason="Order and independence are local claims, not authenticated history.")
        for aid in a.assessment:
            assessment = assessments.get(aid)
            if assessment is None:
                emit("CONFLICT", "ATTEMPT_ASSESSMENT_MISSING", (a.attempt_id, aid), reason="Referenced assessment absent.")
            else:
                compare("ATTEMPT_ASSESSMENT_OWNER", assessment.attempt_id, a.attempt_id, (a.attempt_id, aid))
        for eid in a.assistance_before_attempt.event_refs + a.feedback_after_attempt:
            e = events.get(eid)
            if e is None:
                emit("CONFLICT", "ATTEMPT_EVENT_MISSING", (a.attempt_id, eid), reason="Referenced assistance/feedback absent.")
                continue
            compare("ATTEMPT_EVENT_TASK", e.task_id, a.task_id, (a.attempt_id, eid))
            before = eid in a.assistance_before_attempt.event_refs
            allowed_types = {"HINT_AVAILABLE", "EXPLANATION_AVAILABLE", "FEEDBACK_DELIVERED"}
            if e.event_type not in allowed_types:
                emit("CONFLICT", "ASSISTANCE_FEEDBACK_EVENT_TYPE", (a.attempt_id, eid), reason="Event cannot represent assistance or feedback.")
            if not before and e.attempt_id is not None:
                compare("FEEDBACK_ATTEMPT_OWNER", e.attempt_id, a.attempt_id, (a.attempt_id, eid))
            submissions = [submission for submission in sidecar.events
                           if submission.attempt_id == a.attempt_id and submission.event_type in {"ANSWER_SUBMITTED", "CORRECTION_SUBMITTED"}]
            for submission in submissions:
                if (before and ancestor(submission.event_id, eid)) or (not before and ancestor(eid, submission.event_id)):
                    emit("CONFLICT", "ASSISTANCE_FEEDBACK_CAUSAL_ORDER", (a.attempt_id, eid, submission.event_id),
                         reason="Declared event parents contradict assistance-before or feedback-after relation.")
            chronology(e.time_claim if before else a.answer_time_claim,
                       a.answer_time_claim if before else e.time_claim,
                       (a.attempt_id, eid), "ASSISTANCE_BEFORE_TIME" if before else "FEEDBACK_AFTER_TIME")
        if a.correction_attempt is not None:
            prev_id = a.correction_attempt.corrects_attempt_id
            corrections[a.attempt_id] = [prev_id]
            prev = attempts.get(prev_id)
            if prev is None:
                emit("CONFLICT", "CORRECTION_TARGET_MISSING", (a.attempt_id, prev_id), reason="Earlier attempt absent.")
            else:
                compare("CORRECTION_TASK", a.task_id, prev.task_id, (a.attempt_id, prev_id))
                if prev.attempt_order >= a.attempt_order:
                    emit("CONFLICT", "CORRECTION_ORDER", (a.attempt_id, prev_id), reason="Correction target must have smaller declared attempt_order.")
                chronology(prev.answer_time_claim, a.answer_time_claim, (prev_id, a.attempt_id), "CORRECTION_TIME")
    cycles = _cycle_nodes(corrections)
    emit("CONFLICT" if cycles else "PASS", "CORRECTION_CYCLE", cycles, reason="Correction graph cycle check.")
    for t in sidecar.tasks:
        ordered = sorted((a for a in sidecar.attempts if a.task_id == t.task_id), key=lambda a: a.attempt_order)
        for prev, nxt in zip(ordered, ordered[1:]):
            chronology(prev.answer_time_claim, nxt.answer_time_claim, (prev.attempt_id, nxt.attempt_id), "ATTEMPT_ORDER_TIME")

    supersedes = {}
    for assessment in sidecar.assessments:
        a = attempts.get(assessment.attempt_id)
        if a is None:
            emit("CONFLICT", "ASSESSMENT_ATTEMPT_MISSING", (assessment.assessment_id,), reason="Attempt absent.")
        else:
            if assessment.assessment_id not in a.assessment:
                emit("CONFLICT", "ASSESSMENT_BACKREFERENCE", (assessment.assessment_id, a.attempt_id), reason="Attempt does not reference its assessment.")
            ref = assessment.rubric_revision_ref
            r = rubrics[(ref.rubric_id, ref.revision)]
            compare("ASSESSMENT_RUBRIC_TASK", r.task_id, a.task_id, (assessment.assessment_id, r.rubric_id))
            compare("ASSESSMENT_RUBRIC_PHASE", r.phase, a.phase, (assessment.assessment_id, r.rubric_id))
            declaration = events.get(r.declaration_event_ref)
            if declaration:
                chronology(declaration.time_claim, a.answer_time_claim,
                           (assessment.assessment_id, r.rubric_id), "ASSESSMENT_RUBRIC_BEFORE_ANSWER_TIME")
        if assessment.supersedes_assessment_id is not None:
            pid = assessment.supersedes_assessment_id
            supersedes[assessment.assessment_id] = [pid]
            prev = assessments.get(pid)
            if prev is None:
                emit("CONFLICT", "ASSESSMENT_PREVIOUS_MISSING", (assessment.assessment_id, pid), reason="Previous revision absent.")
            elif prev.attempt_id != assessment.attempt_id or prev.revision >= assessment.revision:
                emit("CONFLICT", "ASSESSMENT_REVISION_ORDER", (assessment.assessment_id, pid), reason="Superseded revision belongs to different attempt or is not earlier.")
    cycles = _cycle_nodes(supersedes)
    if cycles:
        emit("CONFLICT", "ASSESSMENT_REVISION_CYCLE", cycles, reason="Cyclic supersession.")
    for review in sidecar.reviews:
        emit("UNVERIFIED", "REVIEW_DECLARATION_UNAUTHENTICATED", (review.review_id,), reason=
             "Stored reviewer/decision/hashes are declarations; they do not approve this validator report or authenticate reviewed bytes.")

    projection = score_assessment_ids if score_assessment_ids is not None else {}
    if type(projection) is not dict or len(projection) > 200 or any(
        type(k) is not str or type(v) is not str or len(k) > MAX_STRING or len(v) > MAX_STRING
        for k, v in projection.items()
    ):
        emit("ERROR", "SCORE_PROJECTION_INVALID", reason="Explicit task -> assessment string mapping required.")
        return report()
    for tid, aid in sorted(projection.items()):
        if result is None:
            emit("UNKNOWN", "SCORE_RESULT_UNAVAILABLE", (tid, aid), reason="No validated BOUND result is available for this explicit projection.")
            continue
        assessment, rt = assessments.get(aid), result_tasks.get(tid)
        a = attempts.get(assessment.attempt_id) if assessment else None
        if not assessment or not rt or not a or a.task_id != tid or a.phase != rt.phase:
            emit("CONFLICT", "SCORE_PROJECTION_SCOPE", (tid, aid), reason="Projection requires matching V1 task, attempt, assessment and phase.")
            continue
        rref = assessment.rubric_revision_ref
        r = rubrics[(rref.rubric_id, rref.revision)]
        awards = {w.criterion_id: w for w in assessment.criterion_awards}
        task = tasks.get(tid)
        revision_chain_ok = True
        revision, seen_revisions = assessment, set()
        while revision.supersedes_assessment_id is not None:
            seen_revisions.add(revision.assessment_id)
            previous = assessments.get(revision.supersedes_assessment_id)
            if (previous is None or previous.assessment_id in seen_revisions
                    or previous.attempt_id != revision.attempt_id or previous.revision >= revision.revision):
                revision_chain_ok = False
                break
            revision = previous
        # Per-projection prerequisites are carried with each dimension, never
        # taken from the last similarly named finding of a different task.
        context_checks = [
            ("sidecar_task", task is not None),
            ("task_phase", task is not None and task.phase == rt.phase),
            ("rubric_task", r.task_id == tid),
            ("rubric_phase", r.phase == a.phase),
            ("task_rubric_revision", task is not None and task.rubric_ref == rref),
            ("assessment_backreference", aid in a.assessment),
            ("assessment_revision_chain", revision_chain_ok),
            ("criterion_binding", set(awards) == {c.criterion_id for c in r.task_criteria + r.skill_criteria}),
        ]
        for name, ok in context_checks:
            emit("PASS" if ok else "CONFLICT", "SCORE_CONTEXT_" + name.upper(), (tid, aid),
                 reason="Explicit selected assessment context check.")
        difficulty_ok = compare("RESULT_TASK_DIFFICULTY", rt.difficulty, r.difficulty, (tid, aid))
        projection_requirements = required("RESULT_BINDING") + tuple(
            (name, "PASS" if ok else "CONFLICT") for name, ok in context_checks
        ) + (("task_difficulty", "PASS" if difficulty_ok else "CONFLICT"),
             ("selected_task_attempt_assessment_phase", "PASS"))
        for total in r.dimension_totals:
            dimension_requirements = projection_requirements
            if total.skill_id is not None and (allowed is None or total.skill_id not in allowed):
                emit("UNKNOWN", "SCORE_SKILL_SCOPE_UNCONFIRMED", (tid, aid), reason="No confirmed skill scope; no skill-score PASS emitted.")
            if total.skill_id is not None:
                dimension_requirements += (("skill_membership", "UNKNOWN" if allowed is None else
                                             "PASS" if total.skill_id in allowed else "CONFLICT"),)
            criteria = [c for c in r.task_criteria + r.skill_criteria
                        if c.dimension == total.dimension and c.skill_id == total.skill_id]
            values = [awards[c.criterion_id].awarded_points for c in criteria]
            location = "/assessments/" + str(sidecar.assessments.index(assessment)) + "/criterion_awards"
            if any(v is None for v in values):
                emit("UNKNOWN", "SCORE_PENDING", (tid, aid), location, "Pending points are never converted to zero.")
                continue
            fraction = rt.fraction
            if total.skill_id is not None:
                mark = next((m for m in rt.skill_marks if m.skill_id == total.skill_id), None)
                if mark is None:
                    emit("CONFLICT", "RESULT_SKILL_MARK_MISSING", (tid, aid, total.skill_id), reason="Rubric skill has no V1 mark.")
                    continue
                fraction = mark.fraction
                override = next((o for o in r.skill_difficulty_overrides if o.skill_id == total.skill_id), None)
                mark_ok = compare("RESULT_SKILL_DIFFICULTY", mark.difficulty or rt.difficulty,
                                  override.difficulty if override else r.difficulty, (tid, aid, total.skill_id))
                dimension_requirements += (("skill_mark_difficulty", "PASS" if mark_ok else "CONFLICT"),)
            # Cross multiplication is exact even for repeating rational fractions.
            with localcontext() as context:
                context.prec = 160
                numerator = sum((Decimal(v) for v in values), Decimal(0))
                maximum = Decimal(total.max_points)
                matches = numerator == Decimal(str(fraction)) * maximum
            ids = (tid, aid) + ((total.skill_id,) if total.skill_id else ())
            values = (("sidecar_awarded", str(numerator)), ("sidecar_maximum", str(maximum)),
                  ("v1_fraction", str(fraction)), ("v1_artifact_id", result_artifact_id or ""),
                  ("sidecar_evidence_path", location), ("v1_evidence_path", f"/tasks/{next(i for i, t in enumerate(result.tasks) if t.task_id == tid)}/evidence"))
            local_status = "PASS" if matches else "CONFLICT"
            emit(local_status, "LOCAL_RESULT_SCORE", ids, location,
                 "Local exact Decimal dimension comparison only; no external session/byte binding.", values)
            confirm("RESULT_SCORE", local_status, dimension_requirements, ids, location, values)
    for tid in sorted(tasks.keys() - projection.keys()):
        emit("UNKNOWN", "SCORE_ATTEMPT_NOT_SELECTED", (tid,), reason="V1 names no attempt; explicit assessment projection required.")
    return report()
