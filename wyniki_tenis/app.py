"""Application factory and setup helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS

from .config_storage import set_fallback_app
from .extensions import db
from .links import overlay_links_by_kort_id
from .views import LEGACY_ENDPOINTS, bp as views_bp

BASE_DIR = Path(__file__).resolve().parent.parent


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)


def create_app() -> Flask:
    load_dotenv()
    load_dotenv(BASE_DIR / ".env", override=False)
    configure_logging()

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=None,
    )
    CORS(app)

    app.config.setdefault(
        "SQLALCHEMY_DATABASE_URI",
        os.environ.get("DATABASE_URL", "sqlite:///overlay.db"),
    )
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SNAPSHOTS_DIR", BASE_DIR / "snapshots")

    db.init_app(app)

    with app.app_context():
        db.create_all()

    app.register_blueprint(views_bp)

    for endpoint, view_name, rule, methods in LEGACY_ENDPOINTS:
        view_func = app.view_functions[f"{views_bp.name}.{view_name}"]
        app.add_url_rule(rule, endpoint=endpoint, view_func=view_func, methods=methods)

    set_fallback_app(app)

    from results import snapshots, start_background_updater

    start_background_updater(app, overlay_links_by_kort_id)

    app.extensions.setdefault("snapshots", snapshots)

    return app


__all__ = ["BASE_DIR", "configure_logging", "create_app"]
