from __future__ import annotations

import logging

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database import initialize_database
from uniflow.infrastructure.logging import configure_logging
from uniflow.infrastructure.write_lock import project_write_lock

logger = logging.getLogger(__name__)


def initialize_project(config: AppConfig) -> None:
    for directory in config.required_directories:
        directory.mkdir(parents=True, exist_ok=True)
    configure_logging(config.logs_root)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        initialize_database(config.database_path)
    logger.info("Initialized UniFlow project at %s", config.project_root)
