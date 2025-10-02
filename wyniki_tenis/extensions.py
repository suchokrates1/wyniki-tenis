"""Flask extensions used by the application."""

from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

# The SQLAlchemy extension is initialized without an app and configured later
# in :func:`wyniki_tenis.app.create_app`.
db = SQLAlchemy()

__all__ = ["db"]
