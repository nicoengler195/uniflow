# Data Model

Phase 3E2B3A adds `WeeklyPlan` (unique Semester/local Monday week), `WeeklyPlanRevision` (immutable numbered lineage and four artifact references), and `WeeklyPlanAcceptance` (append-only exact-revision user acceptance). Alembic `0015_weekly_plan_persistence` adds exactly these three tables. Current revision is the highest number; the root's accepted revision may differ. Blocks and unscheduled actions live in strict `WeeklyPlanRevisionV1` bytes, not normalized tables. See [weekly plans](weekly_plans.md).

Phase 3E2B3B adds no table or migration. `MANUAL_EDIT` revisions use the same schema `1.0` and parent-linked artifact model. They inherit the exact three source artifacts, append only a revision artifact, and leave the accepted pointer unchanged. `USER_REMOVED` retains an action in the revision's unscheduled list; its original scheduler block identity can be recovered from the source WeeklySchedule. No execution or LearningSession row is created. See [weekly plans](weekly_plans.md).

Phase 3E2B2 adds **no table or migration**. `WeeklyScheduleV1` is a strict, derived response with policy `weekly-scheduler-v1`, captured NBA/calendar payload hashes, a scheduler context fingerprint, deterministic proposed block IDs, and unscheduled reasons. It is not a `WeeklyPlan`, `WeeklyBlock`, `LearningSession`, or `DerivedArtifact`. See [weekly scheduler](weekly_scheduler.md). Persisted plan history belongs to 3E2B3.

Phase 3E2B1 adds `PlanningProfile` (unique per Semester, revisioned optional user duration and daily capacity), `AvailabilityWindow` (recurring explicit legal study windows), and `CalendarConstraint` (dated BLOCKED or AVAILABLE_ADDITION facts). Child rows retain creation and deactivation revisions. Existing ScheduleEntry and ScheduleException audit tables are projected without duplication. Alembic revision is `0014_planning_calendar_foundation`; see [planning calendar](planning_calendar.md).

Phase 3E2A adds no table or migration. `PlanningRecommendationV1` is a derived, strict response with policy `next-best-action-v1`, structured factors, stable action IDs, and a scoped input fingerprint. It is not persisted. Later Phase 3E2B completed availability and weekly blocks; see [planning](planning.md) and [weekly plans](weekly_plans.md).

Phase 3F1 adds `LearningResult` metadata with a unique result ID and unique handoff FK, an immutable `LEARNING_RESULT_JSON` DerivedArtifact, raw input SHA-256, completion/receipt times, outcome, and `VALIDATED` processing status. Nullable `Evidence.learning_result_id` preserves legacy rows while giving imported evidence exact result provenance; `(learning_result_id, skill_id)` is unique. No MasteryState schema changes occur. Alembic revision is `0012_learning_result_import`. See [learning results](learning_results.md).

Phase 3E1 adds `LearningSessionTargetSkill` (ordered, unique explicit targets) and `LearningHandoff` (one immutable artifact reference and context fingerprint per session revision). `LearningSession.current_handoff_id` points to the current revision; `handoff_path` remains legacy. The body is stored as a SHA-256 addressed `DerivedArtifact` of kind `LEARNING_HANDOFF_JSON`, not as database JSON. Alembic revision is `0011_learning_handoff_generation`. See [learning handoffs](learning_handoffs.md).

Phase 3D2C2 reuses the existing attempt provider/model, prompt/config/package/output hashes, authorization actor/time/policy/item/byte fields, and request start time. Normalized `SEMANTIC_EXTRACTION_OUTPUT_JSON`, repository evidence spans, and PENDING proposals remain the persistence boundary. Raw provider requests, quotes, and responses are not stored. Alembic remains `0010_external_semantic_adapter`.

## Phase 3D2C1 repository extraction provenance

No schema migration is required. `SemanticExtractionRun` fixes the confirmed repository link, snapshot, role, and state. Each `SemanticExtractionAttempt` stores a metadata-only selected-file manifest and package hash. Existing repository `SemanticEvidenceSpan` columns store the selected file/blob/state, original byte range, SHA-256, and creator attempt. The normalized output remains a `SEMANTIC_EXTRACTION_OUTPUT_JSON` derived artifact; candidates create only PENDING `NEW_SKILL` proposals.

## Phase 3D2B2 disclosure provenance

`SemanticExtractionAttempt` now records actor category, authorization time and policy version, selected item/UTF-8 byte counts, and network-request start time. `external_disclosure_allowed` means the particular attempt passed both authorization gates; it does not assert successful provider delivery. Provider/model, prompt version, nonsecret effective configuration, input/output hashes, and immutable normalized output remain on existing attempt and artifact records. No source text, key, raw prompt, or provider response is stored in these fields.

## Phase 3D2B1 attempts

`SemanticExtractionRun` continues to identify immutable academic source
context. `SemanticExtractionAttempt` records each execution, effective limits,
input manifest hash, adapter identity, status, controlled failure, and optional
successful derived output. Automatic spans and proposals carry nullable
attempt provenance; older manual rows retain NULL. See
[semantic extraction runtime](semantic_extraction_runtime.md).

## Phase 3D2A graph and provenance

Skill remains the canonical, semester-independent node, with `kind` (`UNKNOWN`, `CONCEPT`, `ABILITY`) and future merge redirect fields. SemanticExtractionRun, SemanticEvidenceSpan, SkillProposal, and SkillProposalEvidence separate source observations from reviewed interpretations. SkillAlias and CourseSkill hold reviewed relations. SkillDependency uses a surrogate ID and active/history fields so corrections retain old edges. Exact document unit-relative character or repository byte ranges and SHA-256 values preserve immutable source provenance. Learning Evidence and MasteryState are separate and untouched. See [skill extraction foundation](skill_extraction_foundation.md).

The database is relational and designed for local SQLite first, with SQLAlchemy keeping a future migration path open.

## Core Entities

- `Semester`: a container with a lifecycle state and timezone. It does not own global skills.
- `Course`: a module or component inside a semester; `parent_course_id` expresses hierarchy.
- `SourceArtifact`: one physical stored byte sequence per SHA-256, with original filename/path, size, type hint, and immutable storage path.
- `Material`: logical academic object with stable ID, optional course, title, type, and lifecycle state.
- `MaterialArtifact`: versioned many-to-many link. Each Material has at most one current artifact; historical links remain.
- `Assessment` and `AssessmentAttempt`: flexible assessments and attempts without hard-coded courses. Imported assessments have stable IDs.
- `Deadline`: exact datetime or date-only deadline, linked optionally to a course, assessment, or material. Imported deadlines have stable IDs.
- `ScheduleEntry`, `ScheduleException`, `MaterialReleaseRule`, `AssessmentRequirement`: stable audit-derived records with validated JSON payloads, confidence, source, and creating revision.
- `AuditFactRecord`: current reconciled fact snapshot by stable ID. It supports deterministic planning without rewriting older audit revisions.
- `Skill`: global persistent skill identified by a stable code such as `MATH_CALC_CHAIN_RULE`.
- `SkillDependency`: prerequisite graph edges between skills.
- `Evidence`: historical observations from learning sessions, assessments, manual entries, or official sources.
- `MasteryState`: history of calculated mastery values. UniFlow does not store mastery as a single untraceable number.
- `OfficialResult`: academic result, grade, status, and credits, intentionally separate from skill mastery.
- `SemesterAudit`: immutable revision record with audit ID, raw and canonical hashes, archived original path, import time, and provenance.
- `SourceReference`: provenance for facts, rules, materials, results, and relationships.
- `DropJob`: one observed path/size/mtime event with stability, attempt, outcome, and error history. It may reference a stored `SourceArtifact`.
- `DerivedArtifact`: immutable generated bytes, SHA-256 addressed and separate from original `SourceArtifact` bytes.
- `ParseRun`: parser identity, version, normalized config hash, attempt, status, counts, and controlled failure information for one SourceArtifact.
- `ParsedDocument`: format and normalized text metadata for a successful or OCR-required run; full text stays outside SQLite.
- `ParsedUnit`: ordered PAGE or DOCUMENT provenance with Unicode character offsets and unit text hash.
- `RepositorySource`: local changing Git working tree with optional explicit Course and Assessment association and sanitized remotes.
- `RepositoryRemote`: remote name and sanitized public identity only.
- `RepositorySnapshot`: a deterministic observed repository state with policy version and HEAD/branch metadata.
- `RepositoryFile`: per-path HEAD, index, and working-tree state, Git object IDs, and links to immutable bytes.
- `RepositoryBlob`: SHA-256 addressed exact repository bytes, separate from document source and derived artifacts.

## Information Status

Facts carry structured status where appropriate:

- `CONFIRMED`
- `OBSERVED`
- `INFERRED`
- `UNKNOWN`

Unknown facts stay unknown. They are not inferred from another course.

`Material.content_hash` remains as a deprecated, globally unique legacy column so older rows retain their values. New intake never writes it. A historical hash alone does not prove that artifact bytes are available; no migration invents `SourceArtifact` rows for those hashes.

Material Manifest expectations are computed from current release rules for a requested date range. They are not stored as database rows. Phase 3D1 persists only explicitly confirmed `MaterialExpectationLink` occurrences, with rule fingerprint and provenance. `ClassificationProposal` stores pending interpretations separately from confirmed academic links. `RepositoryAcademicLink` preserves repository association history; RepositorySource fields mirror its active link. See `academic_classification.md`.

Phase 3F2 adds nullable `MasteryState.previous_mastery_state_id` and a partial unique index for the v1 result Evidence/method pair in Alembic `0013_mastery_calculation`. No table or redundant calculation input is added. The append-only provenance chain supports exact replay; see [mastery calculation](mastery_calculation.md).



## Phase 3E2B3C accepted-block materialization

B3B was committed at `0caa6d2`, and B3C was committed at `9ac54e2`; Phase 3 is complete. An explicitly selected block of the accepted revision materializes to a canonical LearningSession under one writer lock and transaction. START creates a PLANNED session with a user goal; RESUME links an existing resumable session. Exact revision/block identity is idempotent. Current block Calendar, deadline, relational, and week-end checks apply without requiring whole-plan NBA freshness. The operation generates no Handoff, starts no session, and writes no Result, Evidence, or Mastery. See [weekly block materialization](weekly_block_materialization.md).
