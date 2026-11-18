"""Non-persistent, deterministic learning needs. No planning or write service calls."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from uniflow.application.application_read_models import _read_transaction, _context, _courses
from uniflow.infrastructure.database.models import (
    Assessment, Material, Deadline, Skill, CourseSkill, SkillDependency,
    MasteryState, Evidence, ScoringMasteryApplication,
)


@dataclass(frozen=True)
class LearningPriorityPolicyV1:
    version: str = 'learning-priority-v1'
    critical_days: int = 2
    high_days: int = 7
    low_mastery_below: float = 0.6

    def deadline(self, days, *, known_open):
        if not known_open or days is None or days < 0:
            return 'NEEDS_CLARIFICATION'
        return 'CRITICAL' if days <= self.critical_days else 'HIGH' if days <= self.high_days else 'NORMAL'


POLICY = LearningPriorityPolicyV1()
PRIORITIES = ('CRITICAL', 'HIGH', 'NORMAL', 'OPTIONAL', 'NEEDS_CLARIFICATION')


@dataclass(frozen=True)
class LearningNeedV1:
    recommendation_id: str
    semester_id: str
    course_id: str
    course_name: str
    skill_ids: tuple[str, ...]
    action: str
    priority: str
    reasons: tuple[str, ...]
    sources: tuple[str, ...]
    evidence_quality: str
    missing_information: tuple[str, ...]
    known_minutes: int | None = None
    policy_version: str = POLICY.version


@dataclass(frozen=True)
class LearningNeedsProjectionV1:
    semester_id: str
    as_of: datetime
    horizon_days: int
    courses: tuple[tuple[str, str], ...]
    needs: tuple[LearningNeedV1, ...]
    diagnostics: tuple[str, ...]
    policy_version: str = POLICY.version


def project_learning_needs(config, semester_id, as_of, course_id=None, horizon_days=30, *, _session=None):
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError('AS_OF_MUST_BE_TIMEZONE_AWARE')
    if type(horizon_days) is not int or not 1 <= horizon_days <= 90:
        raise ValueError('INVALID_LEARNING_HORIZON')
    from contextlib import nullcontext
    with (nullcontext(_session) if _session is not None else _read_transaction(config)) as db:
        semester, zone, local, _, _ = _context(db, semester_id, as_of)
        courses = _courses(db, semester)
        names = {c.id: c.name for c in courses if course_id is None or c.id == course_id}
        if course_id is not None and course_id not in names:
            raise ValueError('UNKNOWN_COURSE_IN_SEMESTER')
        needs = []
        diagnostics = ['Keine belastbare Aufwandsschätzung; keine automatische Lernplanung.',
                       'S09 = NOT_VERIFIED. Lokale Bewertungen sind kein unabhängiger wissenschaftlicher Nachweis.']
        tables = inspect(db.connection()).get_table_names()
        if 'scoring_mastery_applications' not in tables:
            diagnostics.append('Schema 0017: Herkunft aus reviewed scoring (0019) nicht verfügbar; keine Ergänzung aus Annahmen.')
        assessment_courses = dict(db.execute(select(Assessment.id, Assessment.course_id)).all())
        material_courses = dict(db.execute(select(Material.id, Material.course_id)).all())
        for row in db.scalars(select(Deadline).order_by(Deadline.id)):
            refs = [('deadlines', row.id)]
            if row.source_id: refs.append(('source_references', row.source_id))
            links = [row.course_id] if row.course_id else []
            broken = False
            for table, identity, mapping in [('assessments', row.assessment_id, assessment_courses),
                                             ('materials', row.material_id, material_courses)]:
                if identity:
                    refs.append((table, identity))
                    target = mapping.get(identity)
                    broken |= target is None
                    if target: links.append(target)
            linked = set(links)
            selected = sorted(linked.intersection(names))
            if not selected:
                if not linked: diagnostics.append(f'Frist ohne Kurszuordnung ausgelassen: deadlines/{row.id}')
                continue
            if row.status.value in {'MET', 'CANCELLED'}:
                continue
            day = row.due_at.date() if row.due_at and row.is_date_only else None
            stamp = None
            uncertain_time = False
            if row.due_at and not row.is_date_only:
                stamp = row.due_at.replace(tzinfo=zone) if row.due_at.tzinfo is None else row.due_at.astimezone(zone)
                if row.due_at.tzinfo is None:
                    wall = row.due_at
                    uncertain_time = (wall.replace(tzinfo=zone, fold=0).utcoffset() != wall.replace(tzinfo=zone, fold=1).utcoffset()
                        or stamp.astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None) != wall)
                day = stamp.date()
            if day and day > local.date() + timedelta(days=horizon_days):
                continue
            missing = ['Zeitbedarf', 'Fristspezifische Skillzuordnung']
            reasons = [f'Frist: {row.title}; Status {row.status.value}; Information {row.information_status.value}.']
            if row.is_date_only: missing.append('Fristuhrzeit'); reasons.append('Datum ohne Uhrzeit; keine Mitternachtsfrist.')
            if day is None: missing.append('Fristdatum')
            else: reasons.append(f'Lokales Fristdatum: {day.isoformat()}.')
            if stamp: reasons.append(f'Gespeicherte Fristuhrzeit im Semester: {stamp.isoformat()}.')
            if uncertain_time: missing.append('Eindeutige lokale Fristuhrzeit (Zeitumstellung)')
            ambiguous = broken or len(linked) != 1
            if ambiguous: missing.append('Eindeutige Kurszuordnung')
            known = row.status.value == 'OPEN' and row.information_status.value in {'CONFIRMED', 'OBSERVED'} and not ambiguous and not uncertain_time
            if not known: missing.append('Bestätigter offener Friststatus und eindeutige Zuordnung')
            past = day is not None and (day < local.date() or stamp is not None and stamp < local)
            if past: reasons.append('Frist bereits vergangen; Ausgang prüfen, keine neue Dringlichkeit ableiten.')
            days = (day-local.date()).days if day else None
            priority = POLICY.deadline(days, known_open=known and not past)
            for cid in selected:
                needs.append(LearningNeedV1(f'v1:deadline:{row.id}:{cid}', semester_id, cid, names[cid], (),
                    f'Frist prüfen: {row.title}' if priority == 'NEEDS_CLARIFICATION' else f'Kursinhalte für Frist vorbereiten: {row.title}',
                    priority, tuple(reasons), tuple(f'{t}/{i}' for t,i in refs),
                    'Bestätigte Fristdaten' if known else 'Unvollständige oder widersprüchliche Fristdaten', tuple(missing)))
        # Only active canonical skills with a real active course link establish scope.
        skills = {s.id: s for s in db.scalars(select(Skill).where(Skill.status == 'ACTIVE', Skill.canonical_skill_id.is_(None)))}
        links = list(db.scalars(select(CourseSkill).where(CourseSkill.course_id.in_(names), CourseSkill.is_active.is_(True)).order_by(CourseSkill.id)))
        dependencies = list(db.scalars(select(SkillDependency).where(SkillDependency.is_active.is_(True)).order_by(SkillDependency.id)))
        def cyclic(edge):
            pending, visited = [edge.prerequisite_skill_id], set()
            while pending:
                current = pending.pop()
                if current == edge.dependent_skill_id: return True
                if current in visited: continue
                visited.add(current)
                pending.extend(e.prerequisite_skill_id for e in dependencies if e.dependent_skill_id == current)
            return False
        invalid = {e.id for e in dependencies if cyclic(e)}
        if invalid: diagnostics.append('Zyklische Skillabhängigkeiten ausgelassen: ' + ', '.join(sorted(invalid)))
        dependencies = [e for e in dependencies if e.id not in invalid]
        origins = {}
        if 'scoring_mastery_applications' in tables:
            origins = {a.new_state_id: a for a in db.scalars(select(ScoringMasteryApplication))}
        states = {}
        for state in db.scalars(select(MasteryState).order_by(MasteryState.calculated_at.desc(), MasteryState.id.desc())):
            calculated = state.calculated_at.replace(tzinfo=ZoneInfo('UTC')) if state.calculated_at.tzinfo is None else state.calculated_at
            if calculated <= local and state.skill_id not in states: states[state.skill_id] = state
        for link in links:
            if link.skill_id not in skills: continue
            candidates = {link.skill_id: [f'course_skills/{link.id}']}
            for edge in dependencies:
                if edge.dependent_skill_id == link.skill_id and edge.prerequisite_skill_id in skills and edge.prerequisite_skill_id != link.skill_id:
                    candidates.setdefault(edge.prerequisite_skill_id, []).extend([f'course_skills/{link.id}', f'skill_dependencies/{edge.id}'])
            for sid, origin in sorted(candidates.items()):
                state = states.get(sid)
                valid = state is not None and math.isfinite(state.value) and 0 <= state.value <= 1
                if valid and state.value >= POLICY.low_mastery_below: continue
                sources = [*origin, f'skills/{sid}']
                missing = ['Zeitbedarf']
                if valid:
                    sources.append(f'mastery_states/{state.id}')
                    reason = f'Dokumentierter Mastery-Wert {state.value:g} unter Policygrenze {POLICY.low_mastery_below:g}; Methode {state.calculation_method}; Zeitpunkt {state.calculated_at.isoformat()}.'
                    if state.evidence_id:
                        sources.append(f'evidence/{state.evidence_id}')
                        evidence = db.get(Evidence, state.evidence_id)
                        if evidence:
                            reason += f' Evidenztyp {evidence.evidence_type.value}; Herkunft {evidence.value_json or "nicht angegeben"}.'
                            if evidence.source_id: sources.append(f'source_references/{evidence.source_id}')
                        else: missing.append('Bewertungsevidenz')
                    else: missing.append('Bewertungsevidenz')
                    origin = origins.get(state.id)
                    if origin:
                        sources.append(f'scoring_mastery_applications/{origin.id}')
                        reason += f' Herkunftspolicy {origin.policy_id} Version {origin.policy_version}; Ursprung {origin.origin}; Ergebnis {origin.result_sha256}.'
                    quality = f'Lokale Bewertung ({state.calculation_method}); S09 = NOT_VERIFIED'
                    priority, action = 'NORMAL', 'Wissenslücke bearbeiten'
                else:
                    if state: sources.append(f'mastery_states/{state.id}')
                    reason = 'Mastery unbekannt oder ungültig; fehlende Bewertung ist nicht 0.'
                    quality = 'Keine gültige Bewertung'
                    missing.append('Gültige Mastery-Bewertung')
                    priority, action = 'NEEDS_CLARIFICATION', 'Kenntnisstand prüfen'
                if sid != link.skill_id: reason += f' Gespeicherte Voraussetzung für {link.skill_id}; keine transitive Ableitung.'
                needs.append(LearningNeedV1(f'v1:skill:{link.course_id}:{sid}', semester_id, link.course_id, names[link.course_id], (sid,),
                    f'{action}: {skills[sid].name}', priority, (reason,), tuple(sources), quality, tuple(missing)))
        # Merge identical course/skill needs reached through direct and prerequisite paths.
        merged = {}
        from dataclasses import replace
        for need in needs:
            previous = merged.get(need.recommendation_id)
            if previous:
                need = replace(need, sources=tuple(sorted(set(previous.sources + need.sources))),
                               reasons=tuple(sorted(set(previous.reasons + need.reasons))))
            merged[need.recommendation_id] = need
        ordered = tuple(sorted(merged.values(), key=lambda n: (PRIORITIES.index(n.priority), n.course_name, n.course_id, n.recommendation_id)))
        return LearningNeedsProjectionV1(semester_id, local, horizon_days, tuple((c.id,c.name) for c in courses), ordered, tuple(sorted(set(diagnostics))))
