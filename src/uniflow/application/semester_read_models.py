"""Small detached Semester discovery boundary for desktop startup."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from uniflow.application.application_read_models import _read_transaction
from uniflow.infrastructure.config.settings import AppConfig
from uniflow.infrastructure.database.models import Semester

SUPPORTED_REVISIONS = frozenset({"0016_weekly_plan_block_execution", "0017_audit_resolution", "0018_scoring_review", "0019_scoring_mastery", "0020_study_blocks", "0021_study_block_changes", "0022_study_block_completion"})


def validate_project_database(config: AppConfig) -> None:
    if not Path(config.database_path).is_file():
        raise ValueError("The project database is missing. Run UniFlow setup before opening the GUI.")
    uri = config.database_path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = row[0] if row else None
    if revision not in SUPPORTED_REVISIONS:
        raise ValueError(f"Database schema {revision} is incompatible; expected a supported UniFlow revision.")


class SemesterChoiceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = "1.0"
    stable_id: str
    name: str
    state: str
    timezone: str


def list_semesters(config: AppConfig) -> tuple[SemesterChoiceV1, ...]:
    with _read_transaction(config) as db:
        rows = db.scalars(select(Semester).order_by(Semester.stable_id)).all()
        return tuple(SemesterChoiceV1(stable_id=row.stable_id, name=row.name,
            state=row.state.value, timezone=row.timezone) for row in rows)
