"""Material calendar projection in the caller's read-only snapshot.

Only explicit current expectation links constitute evidence. Artifact creation is
an acquisition/registration timestamp, never an original publication timestamp.
"""
from datetime import timezone, time

from sqlalchemy import select

from uniflow.application.academic_classification import rule_fingerprint
from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
from uniflow.infrastructure.database.models import (
    Material, MaterialArtifact, MaterialExpectationLink, MaterialReleaseRule, SourceArtifact,
)


def project_material_events(db, semester, courses, start, end, as_of):
    from uniflow.application.semester_calendar import CalendarEventV1

    names = {c.id: c.name for c in courses}
    by_stable = {c.stable_id: c for c in courses}
    events, diagnostics = [], []
    rules = {r.stable_id: r for r in db.scalars(select(MaterialReleaseRule).where(
        MaterialReleaseRule.course_id.in_(names)))}
    materials = {m.id: m for m in db.scalars(select(Material).where(Material.course_id.in_(names)))}
    artifacts = list(db.execute(select(MaterialArtifact, SourceArtifact).join(
        SourceArtifact, SourceArtifact.id == MaterialArtifact.artifact_id).where(
        MaterialArtifact.material_id.in_(materials), MaterialArtifact.deactivated_at.is_(None))))
    imported = {v.material_id for v, _ in artifacts}
    links = list(db.scalars(select(MaterialExpectationLink).where(
        MaterialExpectationLink.release_rule_id.in_([r.id for r in rules.values()]),
        MaterialExpectationLink.is_active.is_(True))))
    # Reuse the authoritative recurrence/identity contract, including semester bounds.
    manifest = _generate_material_manifest_in_transaction(db, semester.stable_id, start, end, as_of)
    valid_by_material = {}
    for item in manifest.expectations:
        if item.course_id not in by_stable:
            continue
        course = by_stable[item.course_id]
        rule = rules[item.rule_id]
        active = [link for link in links if link.release_rule_id == rule.id
            and link.expectation_identity == item.id]
        valid = [link for link in active if link.occurrence_date == item.release_date
            and link.rule_fingerprint == rule_fingerprint(rule)
            and link.material_id in imported
            and materials[link.material_id].course_id == course.id]
        mids = tuple(sorted({link.material_id for link in valid}))
        for mid in mids:
            valid_by_material.setdefault(mid, []).append(item.id)
        state = ('AMBIGUOUS' if len(mids) > 1 else 'CONFIRMED_PRESENT' if mids
            else 'UNCONFIRMED' if (item.release_date < as_of.date() or (item.release_time is not None
                and item.state == 'NOT_LINKED_IN_UNIFLOW')) else 'EXPECTED')
        explanation = ('Mehrere explizit zugeordnete Materialien; Zuordnung mehrdeutig.' if len(mids) > 1
            else 'Expliziter aktueller Erwartungslink mit Artefaktnachweis.' if mids
            else 'Kein belastbarer Materialnachweis; nicht bestätigt. Keine Aussage über Nichtveröffentlichung.')
        if active and not valid:
            explanation += ' Vorhandene Links sind veraltet, inkonsistent oder ohne aktives Artefakt.'
        events.append(CalendarEventV1('material-expected:' + item.id, 'MATERIAL_EXPECTED',
            rule.id, 'material_release_rules', semester.stable_id, course.id, course.name,
            item.material_kind, item.release_date, None, None, state, item.information_status,
            notes=explanation + f' Regel: {rule.stable_id}; Quelle: {rule.source_id or "unbekannt"}; '
                + f'Erwartete Uhrzeit: {item.release_time or "unbekannt"}; Links: {", ".join(l.id for l in active) or "keine"}',
            material_kind=item.material_kind, material_ids=mids, date_basis='EXPECTED_RELEASE',
            expectation_ids=(item.id,),
            expected_time=time.fromisoformat(str(item.release_time)) if item.release_time else None))
    for rule in manifest.unresolved_rules:
        if rule.course_id in by_stable:
            diagnostics.append(f'Materialregel nicht datierbar: {by_stable[rule.course_id].name} · '
                f'{rule.material_kind} · {rule.rule_id}: {rule.reason}')
    # Resolve links outside the displayed range too, without relocating acquisition events.
    visible_materials = set()
    for version, artifact in artifacts:
        stamp = artifact.created_at
        if stamp is not None:
            stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(as_of.tzinfo)
            if start <= stamp.date() <= end:
                visible_materials.add(version.material_id)
    occurrence_cache = {}
    for link in links:
        mid = link.material_id
        if mid not in visible_materials or start <= link.occurrence_date <= end:
            continue
        rule = next((r for r in rules.values() if r.id == link.release_rule_id), None)
        if mid not in imported or rule is None or materials[mid].course_id != rule.course_id:
            continue
        if link.rule_fingerprint != rule_fingerprint(rule):
            continue
        if link.occurrence_date not in occurrence_cache:
            occurrence_cache[link.occurrence_date] = _generate_material_manifest_in_transaction(
                db, semester.stable_id, link.occurrence_date, link.occurrence_date, as_of)
        occurrence = occurrence_cache[link.occurrence_date]
        if any(e.id == link.expectation_identity and e.rule_id == rule.stable_id for e in occurrence.expectations):
            valid_by_material.setdefault(mid, []).append(link.expectation_identity)
    for version, artifact in artifacts:
        material = materials[version.material_id]
        stamp = artifact.created_at
        if stamp is None:
            diagnostics.append(f'Material ohne belastbares Erfassungsdatum: {material.id} / {artifact.id}')
            continue
        # utc_now is the storage contract; SQLite strips the UTC offset.
        stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(as_of.tzinfo)
        if not start <= stamp.date() <= end:
            continue
        expectations = tuple(sorted(set(valid_by_material.get(material.id, []))))
        events.append(CalendarEventV1(f'material-observed:{version.id}', 'MATERIAL_OBSERVED',
            version.id, 'material_artifacts', semester.stable_id, material.course_id, names[material.course_id],
            material.title, stamp.date(), stamp, None, 'RECORDED', 'OBSERVED',
            notes=f'Erfasst/importiert, kein ursprüngliches Veröffentlichungsdatum. '
                f'Zuordnung: {", ".join(expectations) or "ungeklärt (kein belastbarer Erwartungsbezug)"}. '
                f'Artefakt: {artifact.id}; SHA-256: {artifact.sha256}; '
                f'Datei: {artifact.original_filename}; Herkunft: {artifact.original_source_path}; '
                f'Version: {version.version_number}; Quelle: {version.source_id or material.source_id or "unbekannt"}',
            material_kind=material.material_type, material_ids=(material.id,), artifact_id=artifact.id,
            date_basis='ARTIFACT_RECORDED_AT', expectation_ids=expectations))
    for material in materials.values():
        if material.id not in imported:
            diagnostics.append(f'Material ohne aktiven Artefaktnachweis, kein Kalendereintrag: {material.title} ({material.id})')
    return events, diagnostics
