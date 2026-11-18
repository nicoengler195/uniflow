Phase 4C4B adds the [Human Review GUI](gui_review.md) over the committed Phase 4B3 [review reads and workflow commands](review_workflow_facade.md). Qt review widgets use detached DTOs and facade-only mutations. Phase 4C4A added the read-only [Materials GUI](gui_materials.md). Earlier phase notes below retain their checkpoint context.

Phase 4A is complete at `5b74a22`; Phase 4B1 is committed at `85fb920`. Phase 4B2 adds the GUI-independent [planning workflow facade](planning_workflow.md). Existing services retain locks, transactions, artifact publication, and legality checks.

# Architecture

Phase 4C3 adds a narrow `get_session_detail` read that reuses the verified Session Inbox projector in one query-only transaction. The desktop controller calls only the Phase 4B1 `learning_workflow` facade for learning mutations and keeps Qt dialogs on the GUI thread. See [Sessions GUI](gui_sessions.md).

Phase 4C1 adds a PySide6 desktop boundary. See [GUI architecture](gui_architecture.md) and [design system](gui_design_system.md). Widgets consume detached Phase 4A DTOs; the controller invokes Phase 4B commands via workers and never exposes an ORM Session.

Phase 4A1, committed at `edf6771`, adds a GUI-independent application read boundary for Home and Today. Phase 4A2A, committed at `9dd359c`, extends it with independent Week and ChangeFeed projections. Phase 4A2B1 adds reusable Material detail and [MaterialInbox](material_inbox.md), committed at `72b5a92`. Phase 4A2B2 adds reusable Session detail and [SessionInbox](session_inbox.md), committed at `c971390`. Each top-level projection uses one caller-time-aware, read-only transaction and returns a detached strict DTO. Phase 4A2B3 adds [CourseOverview](course_overview.md), committed at `5b74a22`. Phase 4B1 adds learning workflow commands and errors; Phase 4B2 will add planning mutation commands; Phase 4C will add the PySide6 shell.

Phase 3E2B3A persists generated weekly proposals behind the user-intent boundary and was committed at `74d85da`. One `WeeklyPlan` per Semester/week owns immutable revisions; four content-addressed artifacts retain the exact NBA, calendar, B2 schedule, and plan revision bodies. Phase 3E2B3B adds pure manual revision transforms, stored-Calendar layout validation, and one-edit lineage verification; source artifacts and acceptance remain separate. See [weekly plans](weekly_plans.md). Phase 3E2B3C adds explicit accepted-block materialization.

Phase 3E2B2 adds a pure `schedule_captured_week` interval engine over strict captured NBA and calendar projections, plus a thin read-only `schedule_week` wrapper. The wrapper calls each producer once; the core has no database/configuration import, writer lock, clock read, or persistence. The two producer read sessions are not an atomic snapshot, so the weekly proposal records both payload hashes and context fingerprints. See [weekly scheduler](weekly_scheduler.md). B3A now owns persisted generated revisions and acceptance; B3C owns explicit accepted-block materialization.

Phase 3E2B1 separates user-owned planning calendar state from audit-owned academic timetable facts. The application layer holds writer-locked personal mutations and pure read-only week projection; CLI remains a thin wrapper. It creates no learning sessions or scheduled blocks. See [planning calendar](planning_calendar.md).

Phase 3E2A adds a dedicated read-only planning application service over canonical Skill, CourseSkill, mastery, session, source, and deadline state. It returns strict deterministic JSON; CLI is a thin wrapper. It creates no rows or handoffs and does not call a model or network. 3E2B weekly scheduling is deferred; see [planning](planning.md).

Phase 3F1 adds an explicit local inbound boundary for untrusted `LearningResultV2`. Session start freezes the current handoff. Import validates the stored artifact and its return contract, then atomically stores a canonical result and exact per-Skill Evidence under the project writer lock. Sessions stop at `VALIDATED`; Phase 3F2 owns deterministic mastery calculation and completion. See [learning results](learning_results.md).

Phase 3E1 adds explicit Skill-target learning sessions and immutable, locally generated handoff revisions. The application boundary owns deterministic graph traversal, read-only mastery snapshots, reviewed source selection, and academic context; CLI commands only call that boundary. The body is a `LEARNING_HANDOFF_JSON` DerivedArtifact, while `LearningHandoff` records revision metadata and `LearningSession.current_handoff_id` points to the active package. See [learning handoffs](learning_handoffs.md).

Phase 3D2C2 adds a thin authorized repository OpenAI adapter beside the document adapter. Both share transport and provider parsing helpers; C1 owns repository selection, exact state/blob verification, byte provenance, and finalization. Provider results create PENDING proposals for review, never mastery evidence. No migration was required.

## Phase 3D2C1 offline repository runtime

Repository extraction reads explicitly selected immutable snapshot blobs for one run state. A repository-specific bounded input builder maps Unicode evidence to exact blob byte offsets; the existing canonical result, output artifact, attempt history, writer lock, and PENDING review boundary remain in use. The fixture adapter is the only C1 adapter. See [repository semantic extraction](repository_semantic_extraction.md).

## Phase 3D2B2 authorized external adapter

The OpenAI Responses adapter is the only external network boundary. Local policy and per-attempt authorization gate it before credential loading or transport. Its provider-specific exact quote anchors are resolved locally into B1 ranges; the existing B1 validation and human proposal review remain authoritative. See [external semantic adapter](external_semantic_adapter.md).

## Phase 3D2B1 offline document runtime

The document-only runtime builds bounded verified ParsedUnit chunks, passes
them as data to a provider-neutral adapter, strictly validates its result,
and atomically stores output, exact evidence, and PENDING Skill proposals.
The sole adapter is an offline fixture. Execution attempts have their own
history; inference runs outside the writer lock. External adapters fail
closed before content disclosure. See [semantic extraction runtime](semantic_extraction_runtime.md).

## Phase 3D2A semantic curation

Verified ParsedUnit or RepositoryBlob content is an observation. A manual SkillProposal is an interpretation. Only reviewed acceptance changes the semester-independent canonical Skill graph. SemanticExtractionRun selects confirmed academic scope and source role; SemanticEvidenceSpan records exact ranges and hashes. Proposal persistence and acceptance use the project writer lock. See [skill extraction foundation](skill_extraction_foundation.md).

UniFlow separates deterministic core logic from future UI and AI features.

## Layers

- `domain`: GUI-independent enums and concepts.
- `schemas`: strict versioned Pydantic models for external interchange.
- `infrastructure`: SQLite, SQLAlchemy models, configuration, and logging.
- `application`: orchestration services such as initialization, doctor checks, and mastery recording.
- `cli`: thin Typer command wrappers around application services.

Dependency direction is inward: CLI depends on application services; application services depend on domain and infrastructure; future GUI code should call application services instead of embedding business logic.

## Current And Future Flow

1. YAML or JSON semester audits are validated, planned, and imported with explicit conflicts.
2. UniFlow stores source/provenance, audit revisions, and deterministic facts in SQLite.
3. UniFlow creates a strict outgoing learning handoff for a subject learning chat.
4. The learning chat returns strict result JSON.
5. UniFlow stores evidence and updates mastery history through deterministic services.
6. Dashboards and GUI views can later read from the same application layer.

Alembic is the canonical schema upgrade path. Audit parsing, planning, and persistence live in the application layer; Typer only presents their results.

Phase 3A adds `application.material_intake.import_material` as the single boundary for local file intake. The CLI calls this service; a future file watcher should call the same service. It hashes and copies bytes into managed storage, then registers physical artifacts and optional logical Materials. No parsing or classification occurs here.

Phase 3B adds a project-scoped OS writer lock around mutation services, a polling Drop scanner that calls `import_material`, and a read-only manifest projection from `MaterialReleaseRule`. DropJob history records observations and outcomes. The scanner does not classify artifacts or connect them to expectations. See `drop_watcher.md` and `material_manifest.md`.

Phase 3C1 adds `application.document_parsing.parse_source_artifact`. Its explicit registry parses only PDF, plain text, and Markdown from managed SourceArtifact bytes. Extraction runs without the writer lock; final identity check, derived publication, and metadata persistence use it. Normalized output is a separate DerivedArtifact. See `document_parsing.md`. Git repository sources are reserved for Phase 3C2.

Phase 3C2 adds a separate `RepositorySource → RepositorySnapshot → RepositoryFile → RepositoryBlob` lineage. The application service observes Git and captures exact bytes without changing the repository. See `repository_snapshots.md`.

Phase 3D1 adds `ClassificationProposal` as a manual interpretation layer. Acceptance creates confirmed MaterialArtifact, RepositoryAcademicLink, or MaterialExpectationLink associations with provenance and correction history. A proposal does not alter academic truth until accepted. All mutation uses the project writer lock. See `academic_classification.md`.

Phase 3F2 adds a dedicated local mastery application boundary. It revalidates the immutable result artifact and exact imported Evidence, applies the versioned observed-dimensions mean, appends MasteryState history, and atomically completes result/session processing. See [mastery calculation](mastery_calculation.md).



## Phase 3E2B3C accepted-block materialization

B3B was committed at `0caa6d2`, and B3C was committed at `9ac54e2`; Phase 3 is complete. An explicitly selected block of the accepted revision materializes to a canonical LearningSession under one writer lock and transaction. START creates a PLANNED session with a user goal; RESUME links an existing resumable session. Exact revision/block identity is idempotent. Current block Calendar, deadline, relational, and week-end checks apply without requiring whole-plan NBA freshness. The operation generates no Handoff, starts no session, and writes no Result, Evidence, or Mastery. See [weekly block materialization](weekly_block_materialization.md).


Phase 4C2 is implemented and uncommitted from `04b17c0`. The GUI Week mutation boundary uses the strict Phase 4B2 planning facade, worker-owned application calls, exact revision tokens, and fresh Week/Home reads. See [Week interactions](gui_week_interactions.md). Earlier phase notes remain below as historical context.
Phase 4D1 extends the semester audit import planner with exact, human-authored `ACCEPT_REPLACEMENT` decisions. A resolution manifest binds the candidate canonical hash and each current/proposed fact hash plus current revision. `RESOLVED_UPDATE` is distinct from automatic `UPDATE`. Accepted decisions and manifest hashes are stored in `audit_resolution_records`; exact manifest bytes are archived beside the accepted audit. The writer lock and import transaction revalidate decisions before persistence. See [Semester Audit Import](semester_audit_import.md).
