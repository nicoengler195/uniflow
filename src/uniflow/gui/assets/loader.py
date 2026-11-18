"""Package-relative Qt artwork loading with a small pixmap cache."""
from __future__ import annotations

import logging
from functools import lru_cache
from importlib.resources import files

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap

logger = logging.getLogger(__name__)


@lru_cache(maxsize=48)
def pixmap(relative_path: str) -> QPixmap:
    """Return a decoded packaged image, or a null pixmap on failure."""
    if not relative_path.startswith(("courses/", "hero/", "brand/")) or ".." in relative_path:
        logger.warning("Rejected GUI artwork key")
        return QPixmap()
    try:
        data = files("uniflow.gui.assets").joinpath(relative_path).read_bytes()
        result = QPixmap()
        if result.loadFromData(data, "PNG"):
            return result
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        pass
    logger.warning("GUI artwork unavailable: %s", relative_path)
    return QPixmap()


@lru_cache(maxsize=96)
def scaled_pixmap(relative_path: str, width: int, height: int, *, contain: bool = False) -> QPixmap:
    source = pixmap(relative_path)
    if source.isNull() or width <= 0 or height <= 0:
        return QPixmap()
    return source.scaled(QSize(width, height),
        Qt.AspectRatioMode.KeepAspectRatio if contain else Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation)
