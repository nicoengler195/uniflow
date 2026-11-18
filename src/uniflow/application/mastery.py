from __future__ import annotations

from sqlalchemy.orm import Session

from uniflow.infrastructure.database.models import Evidence, MasteryState


class DeterministicMasteryService:
    """Small placeholder service; richer scoring can replace this strategy later."""

    def record_mastery(
        self,
        session: Session,
        *,
        skill_id: str,
        value: float,
        calculation_method: str = "manual_phase1_placeholder",
        evidence: Evidence | None = None,
        learning_session_id: str | None = None,
    ) -> MasteryState:
        if not 0.0 <= value <= 1.0:
            raise ValueError("mastery value must be between 0.0 and 1.0")
        mastery = MasteryState(
            skill_id=skill_id,
            value=value,
            calculation_method=calculation_method,
            evidence=evidence,
            learning_session_id=learning_session_id,
        )
        session.add(mastery)
        return mastery
