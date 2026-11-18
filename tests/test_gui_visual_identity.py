"""Presentation identities never touch canonical academic data."""
import inspect
import uniflow.gui.visual_identity as visuals

from uniflow.gui.visual_identity import (ACCENT_KEYS, ARTWORK_KEYS, ICON_KEYS,
    course_artwork_asset, resolve_course_visual, resolve_skill_visual)


def test_common_course_visual_families_and_fallback():
    examples = {
        ("MATH_1", "Mathematik I: Analysis"): "MATHEMATICS",
        ("PHY_1", "Physik und Mechanik"): "PHYSICS",
        ("NEURO", "Neuroscience basics"): "NEUROSCIENCE",
        ("INFO", "Informatik und Programmieren"): "COMPUTER_SCIENCE",
        ("PDP", "Prinzipien der Programmierung"): "COMPUTER_SCIENCE",
        ("LINUX", "Linux Programming"): "COMPUTER_SCIENCE",
        ("FP", "Functional Programming"): "COMPUTER_SCIENCE",
        ("OOP", "Object-Oriented Programming"): "COMPUTER_SCIENCE",
        ("GTI", "Digital Systems"): "DIGITAL_SYSTEMS",
        ("DATA", "Data Science and Statistics"): "DATA_ANALYTICS",
        ("BIO", "Biologie"): "BIOLOGY",
        ("CHEM", "Chemistry"): "CHEMISTRY",
        ("LANG", "Language and Linguistics"): "LANGUAGE",
        ("HIST", "History"): "HUMANITIES",
        ("ECON", "Economics"): "ECONOMICS",
        ("FUTURE_X", "Novel Interdisciplinary Seminar"): "GENERIC_ACADEMIC",
    }
    for (stable_id, name), expected in examples.items():
        value = resolve_course_visual(stable_id, name)
        assert value.domain_family == expected
        assert value == resolve_course_visual(stable_id, name)
        assert value.icon_key in ICON_KEYS
        assert value.accent_family in ACCENT_KEYS
        assert value.artwork_family in ARTWORK_KEYS
    assert resolve_course_visual("FUTURE_X", "Novel Interdisciplinary Seminar").artwork_family == "ACADEMIC_ABSTRACT"


def test_course_variation_and_skill_fallbacks():
    values = {resolve_course_visual(f"MATH_{index}", "Mathematics").accent_family for index in range(20)}
    assert values == {"CYAN"}
    assert resolve_skill_visual("Unfamiliar topic", "CONCEPT").family == "CONCEPT"
    assert resolve_skill_visual("Unfamiliar task", "ABILITY").family == "ABILITY"
    assert resolve_skill_visual("Write a proof", "CONCEPT").family == "PROOF_LOGIC"
    assert resolve_skill_visual("Implement algorithm", "ABILITY").family in {"PROGRAMMING", "ALGORITHM"}
    for kind in ("CONCEPT", "ABILITY", "UNKNOWN"):
        value = resolve_skill_visual("Unknown", kind)
        assert value.icon_key in ICON_KEYS and value.accent_family in ACCENT_KEYS
    source = inspect.getsource(visuals)
    assert "sqlalchemy" not in source and "sqlite" not in source


def test_packaged_course_artwork_mapping_and_fallback():
    cases = {
        "Mathematics": "mathematics.png",
        "Physics": "physics.png",
        "Neuroscience": "neuroscience.png",
        "Functional Programming": "programming.png",
        "Prinzipien der Programmierung": "programming.png",
        "Digital Systems": "digital_systems.png",
        "Algorithms and Data Structures": "algorithms_data_structures.png",
        "Theoretical Computer Science": "theoretical_computer_science.png",
        "Software Engineering": "software_engineering.png",
        "Database Systems": "databases_information_systems.png",
        "Operating Systems": "operating_systems.png",
        "Distributed Systems": "parallel_distributed_systems.png",
        "Cognitive Psychology": "cognition_psychology.png",
        "Natural Language Processing": "language_interaction_nlp.png",
        "Research Methods": "research_thesis_electives.png",
        "Chemistry": "chemistry_lab.png",
        "Unexpected course": "generic_academic.png",
    }
    for name, filename in cases.items():
        assert course_artwork_asset(resolve_course_visual("COURSE", name)) == "courses/" + filename


def test_packaged_artwork_loads_independently_of_cwd(qtbot, monkeypatch, tmp_path):
    from uniflow.gui.assets.loader import pixmap, scaled_pixmap
    from uniflow.gui.components.artwork import PackagedArtwork
    monkeypatch.chdir(tmp_path)
    assert not pixmap("hero/next_best_action_study_sunset.png").isNull()
    assert not scaled_pixmap("courses/mathematics.png", 240, 100).isNull()
    assert pixmap("courses/does_not_exist.png").isNull()
    assert pixmap("../not_allowed.png").isNull()
    widget = PackagedArtwork("courses/does_not_exist.png")
    qtbot.addWidget(widget)
    widget.resize(240, 100)
    assert not widget.grab().isNull()  # generic asset or painter fallback is safe


def test_library_artwork_fills_banner_with_subject_focus(qtbot):
    from uniflow.gui.assets.loader import scaled_pixmap
    from uniflow.gui.components.artwork import PackagedArtwork
    for key in ("mathematics", "physics", "neuroscience", "programming"):
        asset = "courses/" + key + ".png"
        widget = PackagedArtwork(asset, mode="library")
        qtbot.addWidget(widget)
        widget.resize(280, 130)
        image = scaled_pixmap(asset, 280, 130)
        assert image.width() >= 280 and image.height() >= 130
        assert asset in PackagedArtwork.FOCUS
        assert not widget.grab().isNull()


def test_small_icons_are_local_and_deterministic(qtbot):
    from PySide6.QtCore import QBuffer, QIODevice
    from uniflow.gui.components.nav_icons import content_icon, route_icon
    from uniflow.gui.routes import Route

    def encoded(icon):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert icon.pixmap(20, 20).save(buffer, "PNG")
        return bytes(buffer.data())

    for kind in ("today", "calendar", "deadline", "plan", "progress", "action",
                 "session", "pdf", "warning"):
        assert encoded(content_icon(kind)) == encoded(content_icon(kind))
    for route in (Route.HOME, Route.WEEK, Route.SESSIONS, Route.MATERIALS):
        assert encoded(route_icon(route)) == encoded(route_icon(route))


def test_known_subject_accents_are_distinct_without_changing_identity():
    subjects = (
        ("MATH", "Mathematik"), ("PHY", "Physik"),
        ("NEURO", "Neuro- und Verhaltensbiologie"),
        ("FP", "Funktionale Programmierung"),
        ("OOP", "Objektorientierte Programmierung"),
        ("LINUX", "Linux-Praktikum"),
    )
    accents = [resolve_course_visual(stable_id, name).accent_family
               for stable_id, name in subjects]
    assert len(set(accents)) == len(subjects)
    assert accents == [resolve_course_visual(stable_id, name).accent_family
                       for stable_id, name in subjects]
    assert resolve_course_visual("UNKNOWN", "Unbekanntes Fach").domain_family == "GENERIC_ACADEMIC"


def test_event_metadata_uses_only_known_type_and_location(qtbot):
    from PySide6.QtWidgets import QLabel
    from uniflow.gui.components import AcademicEventButton
    known = AcademicEventButton("Physik", accent_family="BLUE",
        activity_type="LECTURE", location="R 1")
    unknown = AcademicEventButton("Physik", accent_family="BLUE",
        activity_type=None, location=None)
    qtbot.addWidget(known)
    qtbot.addWidget(unknown)
    assert "Vorlesung" in [label.text() for label in known.findChildren(QLabel)]
    assert "R 1" in [label.text() for label in known.findChildren(QLabel)]
    assert [label.text() for label in unknown.findChildren(QLabel)] == ["Physik"]
