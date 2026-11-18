"""Pure presentation of validated scoring data; no scoring or persistence."""
import json

AI_NOTICE = "Von der KI gemeldet – fachlich nicht unabhängig bestätigt."


def show(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def task_text(task, preview):
    lines = [f"Aufgabe: {task.task_id}", "Titel: Im Vertrag nicht enthalten", AI_NOTICE]
    if task.task_type == "SCORED":
        lines += [f"Phase: {task.phase}", f"Schwierigkeitsgrad: {task.difficulty}",
                  f"Originalbewertung (Anteil 0–1): {task.fraction}",
                  f"Rubrik: {task.rubric}", f"Begründung: {task.rationale}",
                  f"Belegtext der KI: {task.evidence}",
                  "Separate Originalantwort / Musterlösung: Im Vertrag nicht enthalten",
                  f"Ausgegeben: {task.issued_at.isoformat()}",
                  f"Beantwortet: {task.answered_at.isoformat()}",
                  f"Hilfestellung: {task.assistance}",
                  "Hinweise: " + ("\n".join(task.hints) or "Keine gemeldet"),
                  f"Eigenständige Lösung laut KI: {task.independent_solution}",
                  "Skillbewertungen (Originalangaben, keine Mastery):"]
        for mark in task.skill_marks:
            lines += [f"Skill-ID: {mark.skill_id} · Anteil: {mark.fraction}",
                      f"Schwierigkeit: {mark.difficulty or task.difficulty}",
                      f"Kriterium: {mark.criterion}", f"Begründung: {mark.rationale}"]
    else:
        lines += ["Phase / Schwierigkeitsgrad: Im Vertrag nicht enthalten",
                  "Originalbewertung: Nicht bewertet", "Rubrik: Im Ergebnis nicht enthalten",
                  "Kein Antwortbeleg im Ergebnis enthalten.",
                  "Hilfestellungen: Im Vertrag für Bonusaufgaben nicht enthalten",
                  f"Beobachtung: {task.observation}",
                  f"Empfohlene nächste Aktion: {task.recommended_next_action or 'Nicht angegeben'}"]
    lines += ["Zusätzliche qualitative Skillbezüge:"]
    for item in task.additional_skills:
        lines.append(show(item.model_dump(mode="json")))
    if not task.additional_skills:
        lines.append("Keine gemeldet")
    lines += ["Fehlerbeobachtungen (keine bestätigten Wissenslücken):"]
    errors = [e for e in preview.errors if e["task_id"] == task.task_id]
    lines += [show(e) for e in errors] or ["Keine gemeldet"]
    lines += ["Prüfbedarf: Fachliche Bewertung, Belegvollständigkeit, Chronologie und Eigenständigkeit prüfen."]
    return "\n\n".join(lines)


def observations_text(preview):
    lines = [AI_NOTICE, "Fehlerbeobachtungen sind keine bestätigten Wissenslücken."]
    for error in preview.errors:
        lines += [f"Aufgabe: {error['task_id']} · Skill-ID: {error['skill_id'] or 'Nicht zugeordnet'}",
                  f"Status laut KI: {error['status']}", error['description'], error['context']]
    if not preview.errors:
        lines.append("Keine Fehlerbeobachtungen gemeldet.")
    lines += ["Hilfestellungen nach expliziter Aufgaben-ID", show(preview.assistance),
              "Qualitative Skillbezüge (vorläufige Namen sind keine kanonischen Skills)",
              show(preview.qualitative_observations),
              "Skillbezogene Punkte aus bestehender Vorschau (keine Mastery)"]
    from uniflow.application.scoring_contract_v1 import display_points
    def points(value):
        return "Nicht bewertet" if value is None else str(display_points(value))
    for sid, phases in preview.score.skill_phase_points.items():
        lines.append(f"{sid}: LEARNING {points(phases['LEARNING'])} / 50; "
                     f"FINAL_EXAM {points(phases['FINAL_EXAM'])} / 50; "
                     f"Gesamt {points(preview.score.skill_session_scores[sid])} / 100")
    if not preview.score.skill_phase_points:
        lines.append("Nicht bewertet")
    lines += ["Nicht eindeutig zugeordnete Beobachtungen",
              "Keine bezüglich Aufgaben: Der validierte Vertrag verlangt bekannte Aufgaben-IDs. "
              "Fehlende Skillzuordnungen bleiben oben als Nicht zugeordnet offen."]
    return "\n\n".join(lines)


def review_text(preview):
    lines = [AI_NOTICE, "PENDING_HUMAN_REVIEW", "S09 = NOT_VERIFIED",
             "Für alle Aufgaben: Kein unabhängiger fachlicher Nachweis im Vertrag. "
             "KI-Belegtexte ersetzen keine verifizierte Originalantwort oder Musterlösung."]
    for task in preview.result.tasks:
        if task.task_type == "PREVIEW_BONUS":
            lines.append(f"{task.task_id}: Nicht bewertet; Rubrik fehlt. Kein Antwortbeleg im Ergebnis enthalten. "
                         "Phase und Schwierigkeit nicht enthalten (qualitative Bonusaufgabe).")
        else:
            lines.append(f"{task.task_id}: Separate Originalantwort und Musterlösung nicht enthalten; "
                         "Rubrik und KI-Belegtext fachlich prüfen.")
    for error in preview.errors:
        if error['skill_id'] is None:
            lines.append(f"{error['task_id']}: Fehler ohne Skillzuordnung: {error['description']}")
    for phase, value in preview.score.phase_points.items():
        if value is None:
            lines.append(f"{phase}: Nicht bewertet")
    if preview.score.session_score is None:
        lines.append("Gesamtwert: Nicht bewertet – Angaben für vollständigen Gesamtwert fehlen.")
    lines += ["Offene Angaben aus bestehender Vorschau:",
              *preview.open_information,
              "Prüfhinweise sind deterministische Vollständigkeitshinweise für eine spätere fachliche Prüfung."]
    return "\n\n".join(lines)
