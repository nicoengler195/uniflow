from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import get_engine


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def run_doctor(config: AppConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("configuration", True, f"loaded for {config.project_root}"))

    for directory in config.required_directories:
        checks.append(_check_directory(directory))

    try:
        engine = get_engine(config.database_path)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            table_count = connection.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
            ).scalar_one()
        checks.append(DoctorCheck("database", True, f"accessible with {table_count} tables"))
    except Exception as exc:  # pragma: no cover - exact driver errors vary
        checks.append(DoctorCheck("database", False, str(exc)))

    if config.external_drop_folder is None:
        checks.append(DoctorCheck("external drop folder", True, "not configured"))
    else:
        checks.append(_check_directory(config.external_drop_folder, must_exist=True))

    return checks


def _check_directory(path: Path, *, must_exist: bool = False) -> DoctorCheck:
    if must_exist and not path.exists():
        return DoctorCheck(str(path), False, "configured path does not exist")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".uniflow_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return DoctorCheck(str(path), True, "writable")
    except Exception as exc:  # pragma: no cover - OS-specific
        return DoctorCheck(str(path), False, str(exc))
