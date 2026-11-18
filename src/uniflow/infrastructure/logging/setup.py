from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(logs_root: Path) -> None:
    logs_root.mkdir(parents=True, exist_ok=True)
    log_path = logs_root / "uniflow.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
