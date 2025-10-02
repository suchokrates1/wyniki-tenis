"""Public package interface for the tennis overlay application."""

from __future__ import annotations

from .app import BASE_DIR, configure_logging, create_app
from .config_storage import load_config, save_config
from .constants import (
    CORNER_LABELS,
    CORNER_POSITION_STYLES,
    CORNERS,
    LINKS_PATH,
)
from .extensions import db
from .links import overlay_links_by_kort_id
from .models import OverlayConfig, OverlayLink
from .utils import as_float
from .views import render_config

app = create_app()

__all__ = [
    "BASE_DIR",
    "CORNERS",
    "CORNER_LABELS",
    "CORNER_POSITION_STYLES",
    "OverlayConfig",
    "OverlayLink",
    "app",
    "as_float",
    "configure_logging",
    "create_app",
    "db",
    "load_config",
    "LINKS_PATH",
    "overlay_links_by_kort_id",
    "render_config",
    "save_config",
]
