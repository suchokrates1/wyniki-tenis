"""Compatibility wrapper exposing the legacy public interface."""

from __future__ import annotations

import os

from wyniki_tenis import (
    CORNER_LABELS,
    CORNER_POSITION_STYLES,
    CORNERS,
    LINKS_PATH,
    OverlayConfig,
    app,
    as_float,
    db,
    load_config,
    overlay_links_by_kort_id,
    render_config,
    save_config,
)
from wyniki_tenis.models import OverlayLink

__all__ = [
    "CORNERS",
    "CORNER_LABELS",
    "CORNER_POSITION_STYLES",
    "LINKS_PATH",
    "OverlayConfig",
    "OverlayLink",
    "app",
    "as_float",
    "db",
    "load_config",
    "overlay_links_by_kort_id",
    "render_config",
    "save_config",
]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
