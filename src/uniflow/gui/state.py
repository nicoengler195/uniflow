from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

from uniflow.gui.routes import Route


class Busy(StrEnum):
    IDLE = "idle"
    READING = "reading"
    MUTATING = "mutating"


class ReviewFamily(StrEnum):
    CLASSIFICATION = "CLASSIFICATION"
    SKILL = "SKILL"


@dataclass
class AppState:
    project_root: Path
    semester_stable_id: str
    route: Route = Route.HOME
    selected_course_stable_id: str | None = None
    selected_session_id: str | None = None
    materials_course_stable_id: str | None = None
    selected_material_id: str | None = None
    selected_review_family: ReviewFamily | None = None
    selected_proposal_id: str | None = None
    review_return_route: Route = Route.MATERIALS
    materials_from_date: date | None = None
    materials_through_date: date | None = None
    selected_week_start: date | None = None
    busy: dict[Route, Busy] = field(default_factory=lambda: {route: Busy.IDLE for route in Route})
    last_presentation_error: object | None = None
