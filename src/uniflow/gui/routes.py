from enum import StrEnum


class Route(StrEnum):
    LEARNING_PRIORITIES = "learning_priorities"
    CALENDAR = "calendar"
    HOME = "home"
    WEEK = "week"
    SESSIONS = "sessions"
    SESSION_DETAIL = "session_detail"
    MATERIALS = "materials"
    MATERIAL_DETAIL = "material_detail"
    PROPOSAL_REVIEW = "proposal_review"
