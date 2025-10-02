"""Persistence helpers for overlay configuration."""

from __future__ import annotations

from typing import Any, Dict

from flask import Flask, current_app, has_app_context

from .constants import DEFAULT_BASE_CONFIG
from .extensions import db
from .models import OverlayConfig, serialize_overlay_config


_fallback_app: Flask | None = None


def set_fallback_app(app: Flask) -> None:
    global _fallback_app
    _fallback_app = app


def _get_app() -> Flask:
    if has_app_context():
        return current_app
    if _fallback_app is None:
        raise RuntimeError("Application not initialised")
    return _fallback_app


def load_config() -> Dict[str, Any]:
    app = _get_app()
    with app.app_context():
        db.create_all()
        record = OverlayConfig.query.first()
        if not record:
            record, ensured = serialize_overlay_config(dict(DEFAULT_BASE_CONFIG))
            db.session.add(record)
            db.session.commit()
            return ensured
        return record.to_dict()


def save_config(config: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    with app.app_context():
        db.create_all()
        record = OverlayConfig.query.first()
        record, ensured = serialize_overlay_config(config, instance=record)
        if record.id is None:
            db.session.add(record)
        db.session.commit()
        return ensured


__all__ = ["load_config", "save_config", "set_fallback_app"]
