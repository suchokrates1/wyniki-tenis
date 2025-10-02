"""Database models used by the application."""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from .config_schema import ensure_config_structure
from .extensions import db


class OverlayConfig(db.Model):
    __tablename__ = "overlay_config"

    id = db.Column(db.Integer, primary_key=True)
    view_width = db.Column(db.Integer, nullable=False)
    view_height = db.Column(db.Integer, nullable=False)
    display_scale = db.Column(db.Float, nullable=False)
    left_offset = db.Column(db.Integer, nullable=False)
    label_position = db.Column(db.String(64), nullable=False)
    kort_all = db.Column(db.Text, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "view_width": self.view_width,
            "view_height": self.view_height,
            "display_scale": self.display_scale,
            "left_offset": self.left_offset,
            "label_position": self.label_position,
            "kort_all": json.loads(self.kort_all or "{}"),
        }
        return ensure_config_structure(payload)


def serialize_overlay_config(data: Dict[str, Any], instance: OverlayConfig | None = None) -> Tuple[OverlayConfig, Dict[str, Any]]:
    ensured = ensure_config_structure(data)
    target = instance or OverlayConfig()
    target.view_width = ensured["view_width"]
    target.view_height = ensured["view_height"]
    target.display_scale = ensured["display_scale"]
    target.left_offset = ensured["left_offset"]
    target.label_position = ensured["label_position"]
    target.kort_all = json.dumps(ensured["kort_all"])
    return target, ensured


class OverlayLink(db.Model):
    __tablename__ = "overlay_links"

    id = db.Column(db.Integer, primary_key=True)
    kort_id = db.Column(db.String(128), unique=True, nullable=False)
    overlay_url = db.Column(db.String(1024), nullable=False)
    control_url = db.Column(db.String(1024), nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kort_id": self.kort_id,
            "overlay": self.overlay_url,
            "control": self.control_url,
        }


__all__ = [
    "OverlayConfig",
    "OverlayLink",
    "serialize_overlay_config",
]
