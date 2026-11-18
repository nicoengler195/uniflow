"""Pure bounded review projection; all strings are inert PlainText/JSON data."""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from uniflow.application.evidence_artifacts_i2 import (
    ArtifactError, ArtifactStore, ID_PATTERN, MAX_ARTIFACTS, MAX_BYTES,
    MAX_STRING, MAX_TOTAL_BYTES, load_json_bytes, select, sha256,
)
from uniflow.application.evidence_validator_i2 import validate_evidence_bytes
from uniflow.schemas.evidence_sidecar_v1 import EvidenceSidecarV1, EvidenceRef
from uniflow.schemas.evidence_validation_i2 import EvidenceValidationReport
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1
from uniflow.schemas.review_projection_i3 import (
    ArtifactIdentity, ArtifactProjection, AssessmentProjection, AttemptProjection,
    AwardProjection, EntityKey, EvidenceProjection, EventProjection, FindingProjection,
    ModelSnapshot, ProjectionDiagnostic, ProjectionInputContext, ReviewProjectionV1,
    ReviewQuestion, RubricProjection, SessionProjection, SourceLocator, TaskProjection,
)

MAX_REPORT_BYTES = MAX_BYTES
MAX_FINDINGS = 20000
MAX_QUESTIONS = 1000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
NOTICE = "Administrative Sortierung; deklarierte Reihenfolge ist keine authentifizierte Chronologie. Human Review bleibt erforderlich."
QUESTIONS = (
    ("I3Q_RUBRIC_QUALITY", "Ist die deklarierte Rubrik fachlich angemessen und ihre Vorabexistenz ausreichend belegt?"),
    ("I3Q_FINAL_ANSWER", "Welche Antwort ist maßgeblich, und ist sie mathematisch korrekt?"),
    ("I3Q_ASSISTANCE", "Sind Hilfe, Feedback und die nur deklarierte Eigenständigkeit zutreffend berücksichtigt?"),
    ("I3Q_EVIDENCE", "Sind Originalbelege und Provenienz für eine menschliche Prüfung ausreichend?"),
    ("I3Q_ASSESSMENT_SELECTION", "Ist die ausdrücklich angegebene Assessmentauswahl nachvollziehbar beziehungsweise noch offen?"),
    ("I3Q_CONFLICTS", "Welche Konflikte und Unsicherheiten müssen menschlich geklärt werden?"),
)
TRUST = frozenset({"NO_EXTERNAL_TRUST_ANCHOR", "REVISION_CONTEXT_UNAUTHENTICATED", "RUBRIC_PREDECLARATION",
                   "RUBRIC_HASH_REPRESENTATION", "COLLECTOR_SEQUENCE_UNAUTHENTICATED", "REVIEW_DECLARATION_UNAUTHENTICATED"})


class _Limit(Exception):
    pass


class _InputChanged(Exception):
    pass


def _same_entries(original, captured):
    """Bounded stability observation; never call arbitrary mapping protocols."""
    if len(original) != len(captured):
        return False
    try:
        count = 0
        for key, value in original.items():
            count += 1
            if count > len(captured) or type(key) is not str or key not in captured or captured[key] is not value:
                return False
        return count == len(captured)
    except RuntimeError:
        return False


def _capture_inputs(artifacts, selection):
    """Capture immutable values before I2, with fixed bounds before allocation.

    The owned dictionaries never escape to callers or to the validator. I2 gets
    bounded copies of the same captured values, and I3 keeps the original capture.
    """
    if type(artifacts) is not dict:
        raise ArtifactError("ARTIFACT_COUNT_OR_MAPPING")
    if len(artifacts) > MAX_ARTIFACTS:
        raise _Limit
    if selection is not None and type(selection) is not dict:
        raise ArtifactError("SCORE_PROJECTION_INVALID")
    if selection is not None and len(selection) > 200:
        raise _Limit
    captured, chosen, total = {}, None if selection is None else {}, 0
    try:
        for i, (key, value) in enumerate(artifacts.items()):
            if i >= MAX_ARTIFACTS:
                raise _Limit
            if type(key) is not str or not ID_PATTERN.fullmatch(key) or ".." in key:
                raise ArtifactError("INVALID_ARTIFACT_ID")
            if type(value) is not bytes:
                raise ArtifactError("BYTES_REQUIRED")
            total += len(value)
            if len(value) > MAX_BYTES or total > MAX_TOTAL_BYTES:
                raise _Limit
            captured[key] = value
        if selection is not None:
            for i, (key, value) in enumerate(selection.items()):
                if i >= 200:
                    raise _Limit
                if type(key) is not str or type(value) is not str:
                    raise ArtifactError("SCORE_PROJECTION_INVALID")
                if len(key) > MAX_STRING or len(value) > MAX_STRING:
                    raise _Limit
                chosen[key] = value
    except RuntimeError as exc:
        raise _InputChanged from exc
    # UTF-8 and existing I2 store policy validated on the owned bounded capture.
    store = ArtifactStore(captured)
    if not _same_entries(artifacts, captured) or (selection is not None and not _same_entries(selection, chosen)):
        raise _InputChanged
    return captured, chosen, store


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _walk(value, path=""):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _walk(child, path + "/" + token)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _walk(child, path + "/" + str(i))


def _key(kind, value):
    return EntityKey(kind=kind, entity_id=value[kind.lower() + "_id"],
                     revision=value.get("revision") if kind in {"RUBRIC", "ASSESSMENT"} else None)


class _Builder:
    def __init__(self, sidecar_bytes, artifacts, handoff_id, result_id, revision, selection, supplied):
        self.raw = sidecar_bytes
        self.artifacts = artifacts
        self.handoff_id, self.result_id, self.revision = handoff_id, result_id, revision
        self.selection = selection
        self.supplied = supplied
        self.report = None
        self.store = None
        self.used = 0
        self.diagnostics = []
        self.findings = []
        self.evidence = []
        self.ref_indices = {}
        self.handling = "ABSENT" if supplied is None else "REJECTED"
        self.report_hash = None
        self.entities = []
        self.archives = []
        self.used_entities = set()
        self.not_checked = ["Keine Mathematikprüfung, fachliche Freigabe, Score-Neuberechnung, Masterywirkung oder authentifizierte Historie."]

    def charge(self, text):
        # Conservative allowance for JSON escaping and model metadata before expansion.
        self.used += len(text.encode("utf-8")) * 2 + 256
        if self.used > MAX_OUTPUT_BYTES:
            raise _Limit

    def diagnostic(self, code, reason, status="ERROR"):
        self.diagnostics.append(ProjectionDiagnostic(code="I3P_" + code, status=status, reason=reason))

    def locator(self, origin, pointer, index=None):
        raw = self.raw if origin == "SIDECAR" else self.supplied if origin == "SUPPLIED_I2" else None
        if origin == "CURRENT_I2":
            digest = self.report_hash
        elif origin in {"HANDOFF", "RESULT"}:
            raw = self.store.get(self.handoff_id if origin == "HANDOFF" else self.result_id)
            digest = sha256(raw)
        else:
            digest = sha256(raw)
        return SourceLocator(origin=origin, source_sha256=digest, pointer=pointer, original_array_index=index)

    def snap(self, value, pointer, origin="SIDECAR", index=None):
        payload = _json(value)
        self.charge(payload)
        return ModelSnapshot(source=self.locator(origin, pointer, index), payload_json=payload)

    def context(self):
        inventory = () if self.store is None else tuple(ArtifactIdentity(artifact_id=i, sha256=h, byte_length=n) for i, h, n in self.store.inventory())
        selection = self.selection if type(self.selection) is dict else {}
        pairs = tuple(sorted(selection.items()))
        valid_pairs = all(type(k) is str and type(v) is str for k, v in pairs)
        if not valid_pairs:
            pairs = ()
        descriptor = dict(projection_version="uniflow-review-projection-i3.0", policy_version="i3-projection-policy-1",
            sidecar_sha256=sha256(self.raw) if type(self.raw) is bytes else None,
            original_artifacts=[x.model_dump() for x in inventory], handoff_artifact_id=self.handoff_id,
            result_artifact_id=self.result_id, caller_handoff_revision=self.revision,
            assessment_selection_supplied=self.selection is not None, assessment_selection=pairs,
            supplied_report_sha256=sha256(self.supplied) if type(self.supplied) is bytes else None,
            validator_version=self.report.validator_version if self.report else None)
        return ProjectionInputContext(**{k: v for k, v in descriptor.items() if k not in {"projection_version", "policy_version", "original_artifacts"}},
            original_artifacts=inventory,
            current_report_sha256=self.report_hash, supplied_report_handling=self.handling,
            context_sha256=sha256(_json(descriptor).encode("utf-8")))

    def load(self, role_id, model, role):
        if self.store is None or role_id is None:
            return None
        try:
            value = load_json_bytes(self.store.get(role_id))
            model.model_validate(value)
            return value
        except ArtifactError as exc:
            if exc.code.endswith("_LIMIT"):
                raise _Limit from exc
            self.diagnostic("INVALID_" + role, "Original konnte nicht unabhängig schema-gültig gelesen werden.")
            return None
        except ValidationError:
            self.diagnostic("INVALID_" + role, "Original konnte nicht unabhängig schema-gültig gelesen werden.")
            return None

    def attach_report(self, report, digest, legacy=False):
        if len(self.findings) + len(report.findings) > MAX_FINDINGS:
            raise _Limit
        for i, finding in enumerate(report.findings):
            targets = []
            # Source-index paths uniquely identify their enclosing entity.
            match = re.match(r"^/(tasks|attempts|rubrics|assessments|events|source_artifacts|reviews)/(\d+)(?:/|$)", finding.location)
            if match:
                targets = [k for path, k in self.entities if path == f"/{match[1]}/{match[2]}"]
            if not targets:
                # IDs are untyped; collisions and rubric revisions stay ambiguous.
                targets = [k for _, k in self.entities if k.entity_id in finding.object_ids]
            targets = tuple(dict.fromkeys(targets))
            association = "EXACT" if len(targets) == 1 else "AMBIGUOUS" if targets else "GLOBAL"
            if not legacy and (finding.code in {"LOCAL_RESULT_SCORE", "RESULT_SCORE"} or finding.code.startswith("SCORE_CONTEXT_")) and len(finding.object_ids) >= 2:
                typed = [k for _, k in self.entities if (k.kind == "TASK" and k.entity_id == finding.object_ids[0])
                         or (k.kind == "ASSESSMENT" and k.entity_id == finding.object_ids[1])]
                if len(typed) == 2:
                    targets, association = tuple(typed), "EXACT"
            if legacy:
                targets, association = (), "GLOBAL"
                category = "LEGACY_REPORT"
            elif finding.code.startswith("LOCAL_"):
                category = "LOCAL_DIAGNOSTIC"
            elif finding.code in {"SKILL_SCOPE", "RESULT_SCORE", "HANDOFF_BINDING", "RESULT_BINDING"}:
                category = "BINDING_CONFIRMATION"
            elif finding.code in TRUST:
                category = "TRUST_LIMIT"
            elif finding.code.startswith(("REF_", "ARTIFACT_", "HANDOFF_", "RESULT_", "TASK_", "EVENT_", "ATTEMPT_", "ASSESSMENT_", "RUBRIC_", "CORRECTION_", "CRITERION_", "SCORE_", "SIDECAR_", "TIME_", "TRANSFER_", "ASSISTANCE_", "FEEDBACK_", "COLLECTOR_", "DECLARED_", "UNBOUND_", "INVALID_", "BYTE_", "DEPTH_", "NODE_", "STRING_", "NUMBER_", "DUPLICATE_", "NONFINITE_", "TOTAL_", "ROLE_", "BYTES_")):
                category = "TECHNICAL_CHECK"
            else:
                category = "UNKNOWN_CODE"
            self.findings.append(FindingProjection(source_report_version=report.validator_version,
                source_report_sha256=digest, original_finding_index=i, finding=finding,
                display_category=category, targets=targets, association=association))
        self.not_checked.extend(report.not_checked)

    def supplied_report(self):
        if self.supplied is None:
            return
        if type(self.supplied) is not bytes:
            self.diagnostic("REPORT_REJECTED", "Berichtseingabe muss explizite Bytes enthalten.")
            return
        if len(self.supplied) > MAX_REPORT_BYTES:
            raise _Limit
        try:
            source = load_json_bytes(self.supplied)
            if type(source) is not dict or set(source) != set(EvidenceValidationReport.model_fields):
                self.diagnostic("REPORT_REJECTED", "Gelieferter Bericht muss alle Envelopefelder einschließlich expliziter Version enthalten.")
                return
            report = EvidenceValidationReport.model_validate_json(self.supplied)
        except ArtifactError as exc:
            if exc.code.endswith("_LIMIT"):
                raise _Limit from exc
            self.diagnostic("REPORT_REJECTED", "Gelieferter Bericht verletzt den strikten JSON-Vertrag.")
            return
        except ValidationError:
            self.diagnostic("REPORT_REJECTED", "Gelieferter Bericht verletzt den strikten JSON-/Versions-/Reportvertrag.")
            return
        if report.validator_version == "uniflow-evidence-validator-i2.0":
            self.handling = "ARCHIVED_LEGACY"
            self.attach_report(report, sha256(self.supplied), legacy=True)
            self.archives.append(self.snap(source, "", "SUPPLIED_I2"))
            self.diagnostic("LEGACY_REPORT_ONLY", "I2.0 ist ausschließlich historische Dokumentation, keine aktuelle Bindungsbestätigung.", "UNVERIFIED")
        elif self.report is not None and report == self.report:
            self.handling = "MATCHED_CURRENT"
        else:
            self.diagnostic("REPORT_REJECTED", "Vollvergleich mit frisch erzeugtem I2.1-Bericht fehlgeschlagen; gelieferte positive Befunde werden nicht übernommen.", "CONFLICT")

    def indices(self, key):
        return tuple(i for i, f in enumerate(self.findings) if key in f.targets and f.association == "EXACT")

    def refs_under(self, prefix):
        return tuple(i for path, i in self.ref_indices.items() if path.startswith(prefix + "/"))

    def prepare_evidence(self, sidecar):
        by_path = {}
        for i, f in enumerate(self.findings):
            if f.display_category != "LEGACY_REPORT":
                by_path.setdefault(f.finding.location, []).append(i)
        for path, value in _walk(sidecar):
            if not {"artifact_id", "selector", "sha256", "provenance_status"} <= value.keys():
                continue
            loc = self.locator("SIDECAR", path)
            related = by_path.get(path, [])
            chosen = None
            if any(self.findings[i].finding.code == "REF_RESOLVED" and self.findings[i].finding.status == "PASS" for i in related):
                ref = EvidenceRef.model_validate(value)
                chosen = select(self.store.get(ref.artifact_id), ref.selector).text
                if chosen is not None:
                    self.charge(chosen)
            self.ref_indices[path] = len(self.evidence)
            self.evidence.append(EvidenceProjection(ref_source=loc, declaration=self.snap(value, path),
                artifact_id=value["artifact_id"], selected_text=chosen,
                selected_text_source=loc if chosen is not None else None,
                selection_finding_indices=tuple(i for i in related if not self.findings[i].finding.code.startswith("REF_MESSAGE")),
                message_finding_indices=tuple(i for i in related if self.findings[i].finding.code.startswith("REF_MESSAGE"))))

    def finish(self, **kwargs):
        result = ReviewProjectionV1(input_context=self.context(), findings=tuple(self.findings),
            diagnostics=tuple(self.diagnostics), evidence=tuple(self.evidence), not_checked=tuple(self.not_checked), **kwargs)
        if len(result.model_dump_json().encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise _Limit
        return result

    def build(self, *, legacy=False):
        sidecar = None
        handoff = self.load(self.handoff_id, LearningHandoffV2, "HANDOFF")
        if not legacy:
            try:
                sidecar = load_json_bytes(self.raw)
                EvidenceSidecarV1.model_validate(sidecar)
            except (ArtifactError, ValidationError):
                sidecar = None
                self.diagnostic("INVALID_SIDECAR", "Sidecar nicht schema-gültig; keine Teilrettung von Tasks oder Attempts.")
        result = None if sidecar and sidecar["result_binding"]["status"] == "UNBOUND_DRAFT" else self.load(self.result_id, ScoringResultV1, "RESULT")
        if sidecar is not None:
            for collection, kind in (("tasks", "TASK"), ("attempts", "ATTEMPT"), ("rubrics", "RUBRIC"),
                                     ("assessments", "ASSESSMENT"), ("events", "EVENT"), ("source_artifacts", "ARTIFACT"), ("reviews", "REVIEW")):
                self.entities.extend((f"/{collection}/{i}", _key(kind, obj)) for i, obj in enumerate(sidecar[collection]))
        if self.report:
            self.report_hash = sha256(_json(self.report.model_dump(mode="json")).encode("utf-8"))
            self.attach_report(self.report, self.report_hash)
        self.supplied_report()
        if sidecar is not None:
            self.prepare_evidence(sidecar)
        session = SessionProjection(
            # Whole source roots keep every locator invertible, including gaps
            # and metadata not repeated in the navigation views.
            sidecar_declaration=self.snap(sidecar, "") if sidecar else None,
            handoff_context=self.snap(handoff, "", "HANDOFF") if handoff else None,
            result_context=self.snap(result, "", "RESULT") if result else None,
            declared_handoff_revision=sidecar["handoff_revision"] if sidecar else None,
            caller_handoff_revision=self.revision,
            declared_result_binding=self.snap(sidecar["result_binding"], "/result_binding") if sidecar else None,
            binding_finding_indices=tuple(i for i, f in enumerate(self.findings) if f.display_category == "BINDING_CONFIRMATION"),
            provenance_notice=NOTICE)
        common = dict(session=session,
            phase_states=tuple(self.snap(v, f"/phase_states/{i}", "RESULT", i) for i, v in enumerate(result["phase_states"])) if result else (),
            final_exam_decision=self.snap(result["final_exam_decision"], "/final_exam_decision", "RESULT") if result and result.get("final_exam_decision") else None)
        source_archives = list(self.archives)
        if self.report:
            source_archives.append(self.snap(self.report.model_dump(mode="json"), "", "CURRENT_I2"))
        if sidecar is None:
            detached = tuple(self.snap(v, f"/tasks/{i}", "RESULT", i) for i, v in enumerate(result["tasks"])) if result else ()
            detached += tuple(source_archives)
            questions = (ReviewQuestion(question_code="I3Q_EVIDENCE", text=QUESTIONS[3][1]),)
            return self.finish(mode="LEGACY_V1" if result else "DIAGNOSTIC_ONLY", detached_entities=detached, questions=questions, **common)
        # A draft never borrows a caller-supplied final result for its review views.
        if sidecar["result_binding"]["status"] == "UNBOUND_DRAFT":
            result = None
            common.update(phase_states=(), final_exam_decision=None,
                          session=session.model_copy(update={"result_context": None}))
        rmap = {(v["rubric_id"], v["revision"]): (i, v) for i, v in enumerate(sidecar["rubrics"])}
        taskmap = {v["task_id"]: v for v in sidecar["tasks"]}
        valid_attempts = {v["attempt_id"] for v in sidecar["attempts"] if v["task_id"] in taskmap and taskmap[v["task_id"]]["phase"] == v["phase"]}
        assessment_views = {}
        for i, obj in enumerate(sidecar["assessments"]):
            ref = obj["rubric_revision_ref"]
            ri, rubric = rmap[(ref["rubric_id"], ref["revision"])]
            attempt = next((a for a in sidecar["attempts"] if a["attempt_id"] == obj["attempt_id"]), None)
            if attempt is None or attempt["attempt_id"] not in valid_attempts or rubric["task_id"] != attempt["task_id"] or rubric["phase"] != attempt["phase"]:
                continue
            path = f"/assessments/{i}"
            criteria = {c["criterion_id"]: (c, f"/rubrics/{ri}/{group}/{j}") for group in ("task_criteria", "skill_criteria") for j, c in enumerate(rubric[group])}
            awards = []
            for j, award in enumerate(obj["criterion_awards"]):
                definition, definition_path = criteria[award["criterion_id"]]
                ap = path + f"/criterion_awards/{j}"
                awards.append(AwardProjection(criterion_id=award["criterion_id"], dimension=definition["dimension"], skill_id=definition["skill_id"],
                    criterion=self.snap(definition, definition_path), award=self.snap(award, ap, index=j),
                    awarded_points=award["awarded_points"], status=award["status"], correctness_declaration=award["correctness"],
                    evidence_indices=self.refs_under(ap), assessed_components=tuple(self.snap(c, ap + f"/assessed_components/{k}", index=k) for k, c in enumerate(award["assessed_components"]))))
            key = _key("ASSESSMENT", obj)
            fi = self.indices(key)
            assessment_views[obj["assessment_id"]] = AssessmentProjection(assessment_id=obj["assessment_id"], revision=obj["revision"], attempt_id=obj["attempt_id"],
                rubric_key=_key("RUBRIC", rubric), declaration=self.snap(obj, path, index=i),
                awards=tuple(sorted(awards, key=lambda x: (x.dimension, x.skill_id or "", x.criterion_id))),
                qualitative_observations=tuple(self.snap(q, path + f"/qualitative_observations/{j}", index=j) for j, q in enumerate(obj["qualitative_observations"])),
                supersedes_assessment_id=obj["supersedes_assessment_id"], selected_for_v1=(self.selection or {}).get(attempt["task_id"]) == obj["assessment_id"],
                comparison_finding_indices=tuple(j for j in fi if self.findings[j].finding.code in {"LOCAL_RESULT_SCORE", "RESULT_SCORE"}), finding_indices=fi)
            self.used_entities.add(path)
        attempts = {}
        for i, obj in enumerate(sidecar["attempts"]):
            if obj["attempt_id"] not in valid_attempts:
                continue
            path = f"/attempts/{i}"
            attempts[obj["attempt_id"]] = AttemptProjection(attempt_id=obj["attempt_id"], task_id=obj["task_id"], phase=obj["phase"], declared_attempt_order=obj["attempt_order"],
                declaration=self.snap(obj, path, index=i), answer_evidence_indices=tuple(self.ref_indices[path + f"/answer_evidence/{j}"] for j in range(len(obj["answer_evidence"]))),
                answer_time=self.snap(obj["answer_time_claim"], path + "/answer_time_claim"), assistance_before=self.snap(obj["assistance_before_attempt"], path + "/assistance_before_attempt"),
                feedback_event_ids=tuple(obj["feedback_after_attempt"]), correction=self.snap(obj["correction_attempt"], path + "/correction_attempt") if obj["correction_attempt"] else None,
                assessments=tuple(sorted((a for a in assessment_views.values() if a.attempt_id == obj["attempt_id"]), key=lambda a: (a.revision, a.assessment_id))), finding_indices=self.indices(_key("ATTEMPT", obj)))
            self.used_entities.add(path)
        questions = [ReviewQuestion(question_code="I3Q_REVIEW_REQUIRED", text=NOTICE)]
        tasks = []
        result_tasks = {v["task_id"]: (i, v) for i, v in enumerate(result["tasks"])} if result else {}
        used_result_tasks = set()
        for i, obj in sorted(enumerate(sidecar["tasks"]), key=lambda pair: (0 if pair[1]["phase"] == "LEARNING" else 1, pair[1]["task_id"])):
            if len(questions) + len(QUESTIONS) > MAX_QUESTIONS:
                raise _Limit
            key, path = _key("TASK", obj), f"/tasks/{i}"
            fi = self.indices(key)
            qi = tuple(range(len(questions), len(questions) + len(QUESTIONS)))
            questions.extend(ReviewQuestion(question_code=code, scope=key, text=text, supporting_finding_indices=fi, supporting_sources=(self.locator("SIDECAR", path, i),)) for code, text in QUESTIONS)
            rt = result_tasks.get(obj["task_id"])
            # Never merge a V1 bonus or a different phase into a sidecar task.
            if rt and (rt[1]["task_type"] != "SCORED" or rt[1]["phase"] != obj["phase"]):
                rt = None
            if rt:
                used_result_tasks.add(rt[0])
            tasks.append(TaskProjection(task_id=obj["task_id"], phase=obj["phase"], declaration=self.snap(obj, path, index=i),
                statement_evidence_index=self.ref_indices[path + "/task_statement_ref"],
                rubric_key=EntityKey(kind="RUBRIC", entity_id=obj["rubric_ref"]["rubric_id"], revision=obj["rubric_ref"]["revision"]),
                attempts=tuple(sorted((a for a in attempts.values() if a.task_id == obj["task_id"]), key=lambda a: (a.declared_attempt_order, a.attempt_id))),
                v1_task=self.snap(rt[1], f"/tasks/{rt[0]}", "RESULT", rt[0]) if rt else None,
                selected_assessment_id=(self.selection or {}).get(obj["task_id"]), finding_indices=fi, question_indices=qi))
            self.used_entities.add(path)
        rubrics = []
        for i, obj in sorted(enumerate(sidecar["rubrics"]), key=lambda x: (x[1]["rubric_id"], x[1]["revision"])):
            path = f"/rubrics/{i}"
            rubrics.append(RubricProjection(rubric_id=obj["rubric_id"], revision=obj["revision"], task_id=obj["task_id"], phase=obj["phase"], declaration=self.snap(obj, path, index=i),
                task_criteria=tuple(self.snap(c, path + f"/task_criteria/{j}", index=j) for j, c in sorted(enumerate(obj["task_criteria"]), key=lambda x: x[1]["criterion_id"])),
                skill_criteria=tuple(self.snap(c, path + f"/skill_criteria/{j}", index=j) for j, c in sorted(enumerate(obj["skill_criteria"]), key=lambda x: (x[1]["skill_id"], x[1]["criterion_id"]))),
                dimension_totals=tuple(self.snap(c, path + f"/dimension_totals/{j}", index=j) for j, c in enumerate(obj["dimension_totals"])), declaration_event_id=obj["declaration_event_ref"], finding_indices=self.indices(_key("RUBRIC", obj))))
            self.used_entities.add(path)
        events = []
        for i, obj in sorted(enumerate(sidecar["events"]), key=lambda x: x[1]["event_id"]):
            path = f"/events/{i}"
            events.append(EventProjection(event_id=obj["event_id"], event_type=obj["event_type"], task_id=obj["task_id"], attempt_id=obj["attempt_id"],
                declaration=self.snap(obj, path, index=i), parent_event_ids=tuple(obj["parent_event_ids"]), declared_collector_sequence=obj["collector_sequence"],
                time_claim=self.snap(obj["time_claim"], path + "/time_claim"), source_evidence_index=self.ref_indices[path + "/source_ref"], finding_indices=self.indices(_key("EVENT", obj))))
            self.used_entities.add(path)
        actual = {v.artifact_id: v for v in self.context().original_artifacts}
        sources = {v["artifact_id"]: (i, v) for i, v in enumerate(sidecar["source_artifacts"])}
        artifact_views = []
        for aid in sorted(actual.keys() | sources.keys()):
            declared = sources.get(aid)
            artifact_views.append(ArtifactProjection(artifact_id=aid, declaration=self.snap(declared[1], f"/source_artifacts/{declared[0]}", index=declared[0]) if declared else None,
                actual=actual.get(aid), finding_indices=self.indices(EntityKey(kind="ARTIFACT", entity_id=aid))))
        detached = [self.snap(sidecar[group][int(path.rsplit("/", 1)[1])], path) for path, key in self.entities
                    if (group := path.split("/")[1]) in {"attempts", "assessments"} and path not in self.used_entities]
        detached.extend(source_archives)
        if result:
            detached.extend(self.snap(v, f"/tasks/{i}", "RESULT", i) for i, v in enumerate(result["tasks"]) if i not in used_result_tasks)
        return self.finish(mode="SIDECAR_EVIDENCE", tasks=tuple(tasks), rubrics=tuple(rubrics), events=tuple(events),
            source_artifacts=tuple(artifact_views), detached_entities=tuple(detached), questions=tuple(questions),
            declared_reviews=tuple(self.snap(v, f"/reviews/{i}", index=i) for i, v in enumerate(sidecar["reviews"])), **common)


def _failure(code, reason, *, report_supplied=False):
    # Fixed small result: no misleading truncated views or untrusted error echo.
    descriptor = {"failure_code": code, "projection_version": "uniflow-review-projection-i3.0", "policy_version": "i3-projection-policy-1"}
    context = ProjectionInputContext(sidecar_sha256=None, original_artifacts=(), handoff_artifact_id=None,
        result_artifact_id=None, caller_handoff_revision=None, assessment_selection_supplied=False,
        assessment_selection=(), supplied_report_sha256=None, current_report_sha256=None,
        supplied_report_handling="REJECTED" if report_supplied else "ABSENT", validator_version=None, context_sha256=sha256(_json(descriptor).encode()))
    return ReviewProjectionV1(mode="DIAGNOSTIC_ONLY", input_context=context, session=SessionProjection(),
        diagnostics=(ProjectionDiagnostic(code="I3P_" + code, status="ERROR", reason=reason),),
        questions=(ReviewQuestion(question_code="I3Q_REVIEW_REQUIRED", text=NOTICE),),
        not_checked=("Projektion abgebrochen; keine vollständige Reviewansicht oder vollständige Befundübernahme.",))


def project_review_bytes(sidecar_bytes, *, artifacts, handoff_artifact_id=None, result_artifact_id=None,
                         handoff_revision=None, score_assessment_ids=None, supplied_i2_report_bytes=None) -> ReviewProjectionV1:
    """Exactly one current I2.1 call; supplied reports are never authoritative."""
    def abort(code, reason):
        return _failure(code, reason, report_supplied=supplied_i2_report_bytes is not None)
    try:
        captured, selection, store = _capture_inputs(artifacts, score_assessment_ids)
        report = validate_evidence_bytes(sidecar_bytes, artifacts=captured.copy(), handoff_artifact_id=handoff_artifact_id,
            result_artifact_id=result_artifact_id, handoff_revision=handoff_revision,
            score_assessment_ids=None if selection is None else selection.copy())
        if report.validator_version != "uniflow-evidence-validator-i2.1":
            return abort("VALIDATION_UNAVAILABLE", "Aktuelle I2.1-Validierung nicht verfügbar.")
        if any(f.status == "ERROR" and (f.code.endswith("_LIMIT") or f.code in {"BYTE_LIMIT", "DEPTH_LIMIT", "NODE_LIMIT", "STRING_LIMIT", "NUMBER_LIMIT"}) for f in report.findings):
            raise _Limit
        # Reject malformed caller context before any typed navigation is built.
        if (handoff_revision is not None and (type(handoff_revision) is not int or handoff_revision < 1)) or any(f.code in {"ROLE_ARTIFACT_ID_INVALID", "SCORE_PROJECTION_INVALID"} for f in report.findings):
            return abort("INVALID_CONTEXT", "Caller-Kontext ist kein gültiger expliziter I2-Kontext.")
        builder = _Builder(sidecar_bytes, captured, handoff_artifact_id, result_artifact_id, handoff_revision, selection, supplied_i2_report_bytes)
        builder.report = report
        builder.store = store
        return builder.build()
    except _InputChanged:
        return abort("INPUT_SNAPSHOT_CHANGED", "Callerzuordnungen änderten sich während der Eingangserfassung; keine konsistente Projektion behauptet.")
    except _Limit:
        return abort("RESOURCE_LIMIT", "Ressourcenlimit überschritten; vollständige Projektion ausdrücklich abgebrochen.")
    except ArtifactError as exc:
        return abort("RESOURCE_LIMIT" if exc.code.endswith("_LIMIT") else "INVALID_CONTEXT" if exc.code == "SCORE_PROJECTION_INVALID" else "VALIDATION_UNAVAILABLE", "Eingangskontext konnte nicht innerhalb der sicheren Typ- und Ressourcengrenzen erfasst werden.")
    except (ValidationError, TypeError, ValueError, RecursionError, OverflowError):
        return abort("VALIDATION_UNAVAILABLE", "Eingaben konnten nicht sicher und vollständig projiziert werden.")


def project_legacy_scoring_bytes(result_bytes, *, handoff_bytes=None) -> ReviewProjectionV1:
    """Independent V1 view; no sidecar, I2 report or attempts are invented."""
    try:
        artifacts = {"result": result_bytes}
        if handoff_bytes is not None:
            artifacts["handoff"] = handoff_bytes
        builder = _Builder(None, artifacts, "handoff" if handoff_bytes is not None else None, "result", None, None, None)
        builder.store = ArtifactStore(artifacts)
        return builder.build(legacy=True)
    except _Limit:
        return _failure("RESOURCE_LIMIT", "Legacy-Projektion wegen Ressourcenlimit abgebrochen.")
    except ArtifactError as exc:
        return _failure("RESOURCE_LIMIT" if exc.code.endswith("_LIMIT") else "INVALID_RESULT", "Legacy-V1-Eingabe konnte nicht innerhalb sicherer Grenzen gelesen werden.")
    except (ValidationError, TypeError, ValueError, RecursionError, OverflowError):
        return _failure("INVALID_RESULT", "Legacy-V1 konnte nicht sicher gelesen werden.")
