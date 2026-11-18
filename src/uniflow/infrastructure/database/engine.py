from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config


def get_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    _enable_sqlite_foreign_keys(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def initialize_database(database_path: Path) -> None:
    engine = get_engine(database_path)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    if "semesters" in tables and "alembic_version" not in tables:
        command.stamp(cfg, "0001_initial_foundation")
    command.upgrade(cfg, "head")


def database_info(database_path: Path) -> dict[str, Any]:
    engine = get_engine(database_path)
    with engine.connect() as connection:
        tables = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ).scalars().all()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
    return {"database_path": str(database_path), "tables": tables, "foreign_keys": bool(foreign_keys)}


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
