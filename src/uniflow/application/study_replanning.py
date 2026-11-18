"""C2 nonpersistent advice; all reads share one SQLite read transaction."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import inspect, select
from uniflow.application.application_read_models import _read_transaction
from uniflow.application.learning_needs import project_learning_needs, PRIORITIES
from uniflow.application.planning_calendar import _semester, _project_calendar_week_in_transaction
from uniflow.application.study_blocks import rows, digest, utc, local_interval
from uniflow.application.study_block_changes import effective_blocks


@dataclass(frozen=True)
class StudyReplanningPolicyV1:
    version: str = 'study-replanning-v1'
    urgent_priorities: tuple[str, ...] = ('CRITICAL', 'HIGH')


POLICY = StudyReplanningPolicyV1()


@dataclass(frozen=True)
class StudyReplanningAdviceV1:
    advice_id: str
    policy_version: str
    semester_id: str
    course_id: str | None
    advice_type: str
    need_id: str | None
    block_id: str | None
    block_revision: int | None
    priority: str
    reasons: tuple[str, ...]
    sources: tuple[str, ...]
    evidence_quality: str
    missing_information: tuple[str, ...]
    user_action: str
    as_of: datetime
    target_period: tuple[str, str] | None = None


@dataclass(frozen=True)
class StudyPlanHealthProjectionV1:
    semester_id: str
    as_of: datetime
    horizon_days: int
    advice: tuple[StudyReplanningAdviceV1, ...]
    confirmed_blocks: int
    completed_blocks: int
    cancelled_blocks: int
    covered_need_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    policy_version: str = POLICY.version


def project_study_plan_health(config, semester_id, as_of, course_id=None, horizon_days=30):
    utc(as_of)
    with _read_transaction(config) as db:
        needs = project_learning_needs(config, semester_id, as_of, course_id, horizon_days, _session=db)
        semester = _semester(db, semester_id)
        zone = ZoneInfo(semester.timezone)
        instant = utc(as_of)
        tables = set(inspect(db.connection()).get_table_names())
        blocks = effective_blocks(db, semester.id, include_cancelled=True) if 'confirmed_study_blocks' in tables else []
        selected = [b for b in blocks if course_id is None or b['course_id'] == course_id]
        windows = rows(db, 'SELECT * FROM study_availability_windows WHERE semester_id=:sid', sid=semester.id) if 'study_availability_windows' in tables else []
        valid_windows = []
        for w in windows:
            try:
                start, end = datetime.fromisoformat(w['starts_at']), datetime.fromisoformat(w['ends_at'])
                if w['timezone'] != semester.timezone: continue
                local_interval(semester, start.astimezone(zone).date(), start.astimezone(zone).time(), end.astimezone(zone).time())
                if utc(end) > instant: valid_windows.append(w)
            except ValueError:
                continue
        advice = []
        def add(kind, priority, reasons, sources=(), missing=(), action='Lernprioritäten öffnen', need=None, block=None, quality='Aktueller gespeicherter Zustand'):
            identity = dict(policy=POLICY.version, semester=semester_id, kind=kind,
                            need=need.recommendation_id if need else None, block=block['id'] if block else None)
            advice.append(StudyReplanningAdviceV1('c2:'+digest(identity), POLICY.version, semester_id,
                need.course_id if need else block['course_id'] if block else course_id, kind,
                need.recommendation_id if need else None, block['id'] if block else None,
                block['revision'] if block else None, priority, tuple(reasons), tuple(sorted(set(sources))),
                quality, tuple(sorted(set(missing))), action, instant))
        if not valid_windows:
            add('NEEDS_CLARIFICATION', 'NEEDS_CLARIFICATION', ('Keine bestätigte zukünftige Lernverfügbarkeit; keine konkreten Zeitvorschläge.',),
                missing=('Bestätigte Lernverfügbarkeit',), action='Bestehende B1-Planung öffnen')
        covered = set()
        for need in needs.needs:
            linked = [b for b in selected if b.get('need_id') == need.recommendation_id and b.get('need_policy') == need.policy_version and b['course_id'] == need.course_id]
            active = [b for b in linked if b['status'] == 'CONFIRMED' and utc(datetime.fromisoformat(b['ends_at'])) > instant]
            if active: covered.add(need.recommendation_id)
            # An unrelated legacy block could cover the need, but does not prove it.
            uncertain = [b for b in selected if b['course_id'] == need.course_id and b['status'] == 'CONFIRMED'
                         and (not b.get('need_id') or b.get('need_policy') != need.policy_version)]
            if not active and need.priority != 'NEEDS_CLARIFICATION':
                add('NEEDS_CLARIFICATION' if uncertain else 'UNCOVERED_LEARNING_NEED', need.priority,
                    ('Zuordnung zwischen Lernbedarf und Lernblock nicht belegbar.' if uncertain else 'Für diesen Lernbedarf ist noch keine zugeordnete Lernzeit erkennbar.',),
                    need.sources, ('Belastbare Lernbedarfszuordnung',) if uncertain else (), 'Bestehende B1-Planung öffnen', need)
            if need.priority in POLICY.urgent_priorities:
                add('TIME_CRITICAL_LEARNING_NEED', need.priority,
                    ('Dieser Lernbedarf sollte bei der nächsten Planung berücksichtigt werden.', *need.reasons),
                    need.sources, need.missing_information, need=need, quality=need.evidence_quality)
            if need.missing_information or need.priority == 'NEEDS_CLARIFICATION':
                add('NEEDS_CLARIFICATION', 'NEEDS_CLARIFICATION', need.reasons, need.sources,
                    need.missing_information, need=need, quality=need.evidence_quality)
        week_cache = {}
        weekly_cache = {}
        from uniflow.infrastructure.database.models import WeeklyPlan, WeeklyPlanRevision
        from uniflow.application.weekly_plans import _load_verified
        for b in selected:
            refs = [f"confirmed_study_blocks/{b['id']}"]
            if b['revision'] > 1: refs.append(f"study_block_revisions/{b['id']}/revision/{b['revision']}")
            if b['status'] == 'CANCELLED': continue
            if b['status'] == 'COMPLETED':
                completions = rows(db, 'SELECT * FROM study_block_completions WHERE block_id=:bid', bid=b['id'])
                add('COMPLETED_ACTIVITY_REVIEW', 'OPTIONAL', ('Lernaktivität ausdrücklich abgeschlossen; USER_REPORTED ist kein nachgewiesener Skillgewinn. Lernpriorität bleibt unverändert.',),
                    refs+[f"study_block_completions/{c['id']}" for c in completions], action='Lernprioritäten öffnen', block=b, quality='USER_REPORTED')
                continue
            start, end = utc(datetime.fromisoformat(b['starts_at'])), utc(datetime.fromisoformat(b['ends_at']))
            if end <= instant:
                add('NEEDS_CLARIFICATION', 'NEEDS_CLARIFICATION', ('Vergangener Lernblock ist weiterhin offen; kein Abschluss nachgewiesen.',), refs,
                    ('Expliziter Abschlussstatus',), 'Semesterkalender öffnen', block=b)
                continue
            conflicts, missing = [], []
            day = start.astimezone(zone).date()
            while day <= end.astimezone(zone).date():
                monday = day-timedelta(days=day.weekday())
                if monday not in week_cache:
                    try: week_cache[monday] = _project_calendar_week_in_transaction(db, semester_id, monday)
                    except ValueError as exc: week_cache[monday] = str(exc)
                week = week_cache[monday]
                if isinstance(week, str): missing.append(week)
                else:
                    for a in week.academic_occurrences:
                        if a.hard_block and start < utc(a.ends_at) and utc(a.starts_at) < end:
                            source = f'schedule_exceptions/{a.schedule_exception_id}' if a.schedule_exception_id else f'schedule_entries/{a.schedule_entry_id}'
                            conflicts.append((source, a.starts_at.isoformat(), a.ends_at.isoformat()))
                    for c in week.constraint_occurrences:
                        if c.constraint_type == 'BLOCKED' and start < utc(c.ends_at) and utc(c.starts_at) < end:
                            conflicts.append((f'calendar_constraints/{c.calendar_constraint_id}', c.starts_at.isoformat(), c.ends_at.isoformat()))
                    if week.diagnostics: missing.append('Ungeklärte Kalenderdaten')
                    for a in week.academic_occurrences:
                        if a.diagnostics or a.participation == 'UNKNOWN' or a.information_status != 'CONFIRMED':
                            missing.append('Ungeklärte Veranstaltung: ' + a.occurrence_id)
                            refs.extend([f'schedule_entries/{a.schedule_entry_id}'] if a.schedule_entry_id else [])
                            refs.extend([f'schedule_exceptions/{a.schedule_exception_id}'] if a.schedule_exception_id else [])
                if monday not in weekly_cache:
                    reserved, errors = [], []
                    for plan in db.scalars(select(WeeklyPlan).where(WeeklyPlan.semester_id == semester.id, WeeklyPlan.week_start == monday)):
                        if plan.accepted_revision_id is None: continue
                        revision = db.get(WeeklyPlanRevision, plan.accepted_revision_id)
                        try:
                            if revision is None or revision.weekly_plan_id != plan.id: raise ValueError('SOURCE_ARTIFACT_INVALID')
                            body = _load_verified(config, db, plan, revision)[3]
                            reserved.extend((f'weekly_plan_revisions/{revision.id}', x.starts_at, x.ends_at) for x in body.blocks)
                        except ValueError as exc: errors.append(str(exc))
                    weekly_cache[monday] = reserved, errors
                reserved, errors = weekly_cache[monday]
                missing.extend(errors)
                for source, a, z in reserved:
                    if start < utc(z) and utc(a) < end: conflicts.append((source, a.isoformat(), z.isoformat()))
                day += timedelta(days=1)
            for other in blocks:
                if other['id'] != b['id'] and other['status'] in ('CONFIRMED','COMPLETED') and start < utc(datetime.fromisoformat(other['ends_at'])) and utc(datetime.fromisoformat(other['starts_at'])) < end:
                    conflicts.append((f"confirmed_study_blocks/{other['id']}", other['starts_at'], other['ends_at']))
            if conflicts:
                add('CONFIRMED_BLOCK_CONFLICT', 'HIGH', ('Dieser Lernblock benötigt eine Terminprüfung.', *[f'{s}: {a} bis {z}' for s,a,z in sorted(set(conflicts))]),
                    refs+[s for s,_,_ in conflicts], action='Semesterkalender öffnen', block=b)
            if missing:
                add('NEEDS_CLARIFICATION', 'NEEDS_CLARIFICATION', ('Kalenderprüfung unvollständig.',), refs, missing, 'Semesterkalender öffnen', block=b)
        merged = {}
        for item in advice:
            previous = merged.get(item.advice_id)
            if previous:
                item = replace(item, priority=min((previous.priority, item.priority), key=PRIORITIES.index),
                    reasons=tuple(sorted(set(previous.reasons + item.reasons))),
                    sources=tuple(sorted(set(previous.sources + item.sources))),
                    missing_information=tuple(sorted(set(previous.missing_information + item.missing_information))),
                    user_action=previous.user_action)
            merged[item.advice_id] = item
        ordered = tuple(sorted(merged.values(), key=lambda a: (PRIORITIES.index(a.priority), a.course_id or '', a.advice_type, a.advice_id)))
        return StudyPlanHealthProjectionV1(semester_id, instant, horizon_days, ordered,
            sum(b['status']=='CONFIRMED' for b in selected), sum(b['status']=='COMPLETED' for b in selected),
            sum(b['status']=='CANCELLED' for b in selected), tuple(sorted(covered)),
            ('Nur aktuelle Befunde; keine nachgewiesene historische Kalenderänderung.', 'Keine Zeitvorschläge ohne belegte Dauer; keine Schreibcommands.'))
