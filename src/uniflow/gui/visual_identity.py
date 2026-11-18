"""Deterministic, cosmetic identity derived from detached presentation hints.

These families are never academic classification or persisted domain facts.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


DomainFamily = Literal["MATHEMATICS", "PHYSICS", "COMPUTER_SCIENCE", "DIGITAL_SYSTEMS",
    "BIOLOGY", "NEUROSCIENCE", "CHEMISTRY", "DATA_ANALYTICS", "LANGUAGE", "HUMANITIES", "ECONOMICS",
    "ALGORITHMS", "THEORETICAL_CS", "SOFTWARE_ENGINEERING", "DATABASES", "OPERATING_SYSTEMS",
    "DISTRIBUTED_SYSTEMS", "COGNITION", "LANGUAGE_INTERACTION", "RESEARCH",
    "ELECTRICAL_ENGINEERING", "CONTROL_SYSTEMS", "ROBOTICS",
    "GENERIC_ACADEMIC"]
AccentFamily = Literal["CYAN", "BLUE", "VIOLET", "MAGENTA", "TEAL", "AMBER", "NEUTRAL", "YELLOW"]
ArtworkFamily = Literal["MATH_CURVES", "PHYSICS_ORBITS", "NEURAL_NETWORK",
    "DIGITAL_GRID", "DATA_CONSTELLATION", "BIO_ORGANIC", "CHEM_MOLECULAR", "ACADEMIC_ABSTRACT", "COASTAL_RETRO"]

ICON_KEYS = frozenset({"sigma", "atom", "code", "chip", "neural", "leaf", "molecule",
    "speech", "book", "chart", "academic", "electrical_engineering", "control_systems", "robotics"})
ARTWORK_KEYS = frozenset({"MATH_CURVES", "PHYSICS_ORBITS", "NEURAL_NETWORK",
    "DIGITAL_GRID", "DATA_CONSTELLATION", "BIO_ORGANIC", "CHEM_MOLECULAR", "ACADEMIC_ABSTRACT", "COASTAL_RETRO"})
ACCENT_KEYS = frozenset({"CYAN", "BLUE", "VIOLET", "MAGENTA", "TEAL", "AMBER", "NEUTRAL", "YELLOW"})


@dataclass(frozen=True)
class CourseVisualIdentity:
    domain_family: DomainFamily
    icon_key: str
    accent_family: AccentFamily
    artwork_family: ArtworkFamily


@dataclass(frozen=True)
class SkillVisualIdentity:
    family: str
    icon_key: str
    accent_family: AccentFamily


_COURSE_RULES = (
    ("ELECTRICAL_ENGINEERING", ("elektrotechnik", "schaltungstechnik", "electrical engineering"),
        "electrical_engineering", "DIGITAL_GRID", ("BLUE",)),
    ("CONTROL_SYSTEMS", ("regelungstechnik", "control engineering", "control systems"),
        "control_systems", "DATA_CONSTELLATION", ("AMBER",)),
    ("ROBOTICS", ("robotik", "robotics", "autonome systeme", "autonomous systems"),
        "robotics", "DIGITAL_GRID", ("MAGENTA",)),
    ("ALGORITHMS", ("data structures", "datenstrukturen", "algorithms and data", "algorithmen und daten"), "code", "DIGITAL_GRID", ("CYAN", "BLUE", "VIOLET")),
    ("THEORETICAL_CS", ("theoretical computer science", "theoretische informatik", "automata theory", "automatentheorie"), "sigma", "MATH_CURVES", ("VIOLET", "BLUE", "CYAN")),
    ("SOFTWARE_ENGINEERING", ("software engineering", "softwaretechnik", "software development", "softwareentwicklung"), "code", "DIGITAL_GRID", ("CYAN", "VIOLET", "BLUE")),
    ("DATABASES", ("database", "datenbank", "information systems", "informationssystem"), "chip", "DIGITAL_GRID", ("BLUE", "TEAL", "CYAN")),
    ("OPERATING_SYSTEMS", ("operating systems", "operating system", "betriebssystem"), "chip", "DIGITAL_GRID", ("BLUE", "VIOLET", "CYAN")),
    ("DISTRIBUTED_SYSTEMS", ("distributed systems", "distributed system", "verteilte systeme", "verteilte system", "parallel computing", "parallelverarbeitung"), "chip", "DIGITAL_GRID", ("VIOLET", "BLUE", "CYAN")),
    ("COGNITION", ("cognitive psychology", "kognitive psychologie", "cognition", "kognition", "psychology", "psychologie"), "neural", "NEURAL_NETWORK", ("MAGENTA", "VIOLET", "BLUE")),
    ("LANGUAGE_INTERACTION", ("natural language processing", "sprachverarbeitung", "human computer interaction", "mensch computer interaktion", "nlp"), "speech", "NEURAL_NETWORK", ("MAGENTA", "VIOLET", "CYAN")),
    ("RESEARCH", ("research methods", "forschungsmethoden", "thesis", "abschlussarbeit", "elective", "wahlpflicht"), "book", "ACADEMIC_ABSTRACT", ("VIOLET", "TEAL", "BLUE")),
    ("NEUROSCIENCE", ("neuro", "neuroscience", "neurowissenschaft"), "neural", "NEURAL_NETWORK", ("VIOLET", "MAGENTA", "BLUE")),
    ("DIGITAL_SYSTEMS", ("gti", "digital systems", "digitale system", "computer architecture", "rechnerarchitektur", "logic circuit"), "chip", "DIGITAL_GRID", ("BLUE", "CYAN", "VIOLET")),
    ("DATA_ANALYTICS", ("data science", "data analytics", "datenanalyse", "statistics", "statistik", "machine learning"), "chart", "DATA_CONSTELLATION", ("BLUE", "TEAL", "VIOLET")),
    ("COMPUTER_SCIENCE", ("informatik", "computer science", "programming", "programmieren", "programmierung", "functional programming", "funktionale programmierung", "object oriented", "objektorientiert", "linux", "software", "algorithm"), "code", "DIGITAL_GRID", ("CYAN", "BLUE", "VIOLET")),
    ("MATHEMATICS", ("mathematik", "mathematics", "math", "analysis", "algebra", "calculus"), "sigma", "MATH_CURVES", ("CYAN", "VIOLET", "BLUE")),
    ("PHYSICS", ("physik", "physics", "mechanics", "mechanik", "quantum"), "atom", "PHYSICS_ORBITS", ("BLUE", "CYAN", "MAGENTA")),
    ("CHEMISTRY", ("chemie", "chemistry", "chemical", "molecular"), "molecule", "CHEM_MOLECULAR", ("TEAL", "CYAN", "AMBER")),
    ("BIOLOGY", ("biologie", "biology", "bio", "cell biology"), "leaf", "BIO_ORGANIC", ("TEAL", "CYAN", "AMBER")),
    ("LANGUAGE", ("sprache", "language", "linguistics", "linguistik", "english", "deutsch"), "speech", "COASTAL_RETRO", ("MAGENTA", "VIOLET", "AMBER")),
    ("ECONOMICS", ("economics", "economy", "wirtschaft", "finance", "finanz"), "chart", "ACADEMIC_ABSTRACT", ("AMBER", "TEAL", "BLUE")),
    ("HUMANITIES", ("history", "geschichte", "philosophy", "philosophie", "literature", "literatur"), "book", "COASTAL_RETRO", ("MAGENTA", "AMBER", "VIOLET")),
)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _matches(text: str, hint: str) -> bool:
    if hint in {"neuro", "bio"}:
        return any(token.startswith(hint) for token in text.split())
    return f" {hint} " in f" {text} " or any(token.startswith(hint) for token in text.split() if len(hint) >= 5)


def _stable_choice(stable_id: str, choices: tuple[str, ...]) -> str:
    digest = hashlib.sha256(stable_id.encode("utf-8")).digest()
    return choices[int.from_bytes(digest[:4], "big") % len(choices)]


def _course_accent(text: str, stable_id: str, choices: tuple[str, ...]) -> str:
    """Distinct presentation accents for the known study subjects."""
    specific = (
        (("funktionale programmierung", "functional programming"), "VIOLET"),
        (("objektorientierte programmierung", "object oriented programming", "oop"), "AMBER"),
        (("linux praktikum", "linux practical", "linux lab"), "TEAL"),
        (("mathematik", "mathematics", "math"), "CYAN"),
        (("physik", "physics"), "BLUE"),
        (("neuro", "neuroscience"), "MAGENTA"),
    )
    for hints, accent in specific:
        if any(_matches(text, hint) for hint in hints):
            return accent
    return _stable_choice(stable_id, choices)


def resolve_course_visual(stable_id: str, name: str) -> CourseVisualIdentity:
    text = _normalize(f"{name} {stable_id}")
    if any(_matches(text, hint) for hint in ("mathematik", "mathematics", "math")):
        return CourseVisualIdentity("MATHEMATICS", "sigma",
            _course_accent(text, stable_id, ("CYAN", "VIOLET", "BLUE")), "MATH_CURVES")
    for family, hints, icon, artwork, accents in _COURSE_RULES:
        if any(_matches(text, hint) for hint in hints):
            return CourseVisualIdentity(family, icon, _course_accent(text, stable_id, accents), artwork)
    return CourseVisualIdentity("GENERIC_ACADEMIC", "academic",
        _stable_choice(stable_id, ("CYAN", "BLUE", "VIOLET", "MAGENTA", "TEAL", "AMBER")),
        "ACADEMIC_ABSTRACT")


_COURSE_ARTWORK = {
    "ELECTRICAL_ENGINEERING": "electrical_engineering.png",
    "CONTROL_SYSTEMS": "control_systems.png",
    "ROBOTICS": "robotics.png",
    "MATHEMATICS": "mathematics.png", "PHYSICS": "physics.png",
    "NEUROSCIENCE": "neuroscience.png", "COMPUTER_SCIENCE": "programming.png",
    "DIGITAL_SYSTEMS": "digital_systems.png", "CHEMISTRY": "chemistry_lab.png",
    "ALGORITHMS": "algorithms_data_structures.png",
    "THEORETICAL_CS": "theoretical_computer_science.png",
    "SOFTWARE_ENGINEERING": "software_engineering.png",
    "DATABASES": "databases_information_systems.png",
    "OPERATING_SYSTEMS": "operating_systems.png",
    "DISTRIBUTED_SYSTEMS": "parallel_distributed_systems.png",
    "COGNITION": "cognition_psychology.png",
    "LANGUAGE_INTERACTION": "language_interaction_nlp.png",
    "RESEARCH": "research_thesis_electives.png",
}


def course_artwork_asset(identity: CourseVisualIdentity) -> str:
    """Package-relative art key; never a persisted academic classification."""
    return "courses/" + _COURSE_ARTWORK.get(identity.domain_family, "generic_academic.png")


_SKILL_RULES = (
    ("PROOF_LOGIC", ("proof", "beweis", "logic", "logik"), "sigma"),
    ("CALCULATION", ("calculate", "calculation", "berechnung", "rechnen"), "sigma"),
    ("PROGRAMMING", ("programming", "programmieren", "coding", "implement"), "code"),
    ("ALGORITHM", ("algorithm", "algorithmus"), "chip"),
    ("MODELING", ("modeling", "modellierung", "simulation"), "chart"),
    ("EXPERIMENT", ("experiment", "laboratory", "labor"), "molecule"),
    ("MEMORY_RECALL", ("recall", "remember", "erinnern", "memorize"), "book"),
    ("APPLICATION", ("apply", "application", "anwenden", "anwendung"), "academic"),
)


def resolve_skill_visual(name: str, kind: str) -> SkillVisualIdentity:
    text = _normalize(name)
    for family, hints, icon in _SKILL_RULES:
        if any(_matches(text, hint) for hint in hints):
            return SkillVisualIdentity(family, icon, "VIOLET" if kind == "CONCEPT" else "TEAL")
    if kind == "CONCEPT":
        return SkillVisualIdentity("CONCEPT", "book", "VIOLET")
    if kind == "ABILITY":
        return SkillVisualIdentity("ABILITY", "academic", "TEAL")
    return SkillVisualIdentity("GENERIC", "academic", "BLUE")
