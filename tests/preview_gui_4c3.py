"""Render actual 4C3 widgets from a deterministic temporary canonical project.

Run with the project virtual environment from the repository root.
"""
from __future__ import annotations

import gc
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtWidgets import QApplication

from uniflow.application import learning_workflow
from uniflow.application.initializer import initialize_project
from uniflow.application.semester_read_models import list_semesters
from uniflow.domain.enums import SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, CourseSkill, Semester, Skill

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class ImmediateRunner:
    def submit(self, operation, token, fn, callback):
        callback(operation, token, fn())


def _result(config, export_parent, session_id, *, apply):
    learning_workflow.generate_handoff(config, {"session_id": session_id})
    from uniflow.application.session_detail_read_models import get_session_detail
    current = get_session_detail(config, "SYN", session_id, as_of=NOW).session.current_handoff
    exported = learning_workflow.export_exchange(config, {"handoff_id": current.handoff_id,
        "destination_directory": export_parent})
    learning_workflow.start_session(config, {"session_id": session_id})
    child = export_parent / exported.directory_name
    handoff = json.loads((child / "handoff.json").read_bytes())
    envelope = json.loads((child / "handoff-envelope.json").read_bytes())
    payload = {"schema_version": "2.0", "protocol_version": envelope["protocol_version"],
        "result_id": envelope["result_id"], "session_id": session_id,
        "handoff_id": current.handoff_id, "course_id": handoff["course"]["id"],
        "handoff_artifact_sha256": envelope["handoff_artifact_sha256"],
        "completed_at": "2026-10-15T13:00:00+00:00", "session_outcome": "PARTIAL",
        "skill_evidence": [{"skill_id": "A", "recall": 0.0, "application": 0.8},
            {"skill_id": "B", "independent_solution": False}]}
    file = export_parent / envelope["result_filename"]
    file.write_text(json.dumps(payload), encoding="utf-8")
    imported = learning_workflow.import_result_file(config, {"path": file})
    if apply:
        learning_workflow.apply_mastery(config, {"result_id": imported.result_id})


def main():
    app = QApplication.instance() or QApplication([])
    from uniflow.gui.theme.stylesheet import stylesheet
    app.setStyleSheet(stylesheet())
    output = Path("runtime/gui_preview")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uniflow-4c3-preview-") as temporary:
        root = Path(temporary)
        config = load_config(project_root=root / "project")
        initialize_project(config)
        factory = create_session_factory(get_engine(config.database_path))
        with factory.begin() as db:
            semester = Semester(stable_id="SYN", name="Synthetic Semester", state=SemesterState.ACTIVE,
                timezone="UTC")
            math = Course(stable_id="MATH", name="Mathematics", semester=semester)
            neuro = Course(stable_id="NEURO", name="Neuroscience", semester=semester)
            db.add_all([math, neuro])
            db.flush()
            db.add_all([Skill(id="A", name="Algebra", kind="CONCEPT"),
                Skill(id="B", name="Proof", kind="ABILITY")])
            db.flush()
            for course in (math, neuro):
                db.add_all([CourseSkill(course_id=course.id, skill_id="A"),
                    CourseSkill(course_id=course.id, skill_id="B")])
            math_id, neuro_id = math.id, neuro.id
        ids = {}
        for state, course_id in (("planned", neuro_id), ("validated", neuro_id), ("completed", math_id)):
            ids[state] = learning_workflow.create_session(config, {"course_id": course_id,
                "target_skill_ids": ["A", "B"], "goal": f"Study {state.replace('_', ' ')} concepts"}).session_id
        export_parent = root / "exports"
        export_parent.mkdir()
        _result(config, export_parent, ids["validated"], apply=False)
        _result(config, export_parent, ids["completed"], apply=True)
        controller = GuiController(config, list_semesters(config)[0], runner=ImmediateRunner(), clock=lambda: NOW)
        window = MainWindow(controller)
        window.show()
        app.processEvents()
        from uniflow.gui.routes import Route
        controller.navigate(Route.SESSIONS)
        for width, filename in ((1600, "sessions_1600.png"), (1440, "sessions_1440.png")):
            window.resize(width, 900)
            for _ in range(4):
                app.processEvents()
            window.grab().save(str(output / filename))
        ids["in_progress"] = learning_workflow.create_session(config, {"course_id": math_id,
            "target_skill_ids": ["A", "B"], "goal": "Study in progress concepts"}).session_id
        learning_workflow.generate_handoff(config, {"session_id": ids["in_progress"]})
        learning_workflow.start_session(config, {"session_id": ids["in_progress"]})
        for state in ("planned", "in_progress", "completed"):
            controller.select_session(ids[state])
            window.resize(1600, 900)
            for _ in range(4):
                app.processEvents()
            window.grab().save(str(output / f"session_detail_{state}.png"))
            if state == "completed":
                window.scroll.verticalScrollBar().setValue(window.scroll.verticalScrollBar().maximum())
                for _ in range(3):
                    app.processEvents()
                window.grab().save(str(output / "session_detail_completed_lower.png"))
        for route, filename in ((Route.HOME, "home_4c3_smoke.png"),
                                (Route.WEEK, "week_4c3_smoke.png")):
            controller.navigate(route)
            window.resize(1600, 900)
            for _ in range(4):
                app.processEvents()
            window.grab().save(str(output / filename))
        window.close()
        window.deleteLater()
        app.processEvents()
        factory.kw["bind"].dispose()
        gc.collect()


if __name__ == "__main__":
    main()
