"""Utilities for working with snapshot JSON files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from flask import current_app

from .constants import STATUS_ORDER
from .utils import (
    display_name,
    display_value,
    extract_kort_id,
    normalize_last_updated,
    normalize_players,
    normalize_status,
    status_label,
)

logger = logging.getLogger(__name__)


def load_snapshots() -> Dict[str, Dict[str, Any]]:
    directory_setting = current_app.config.get("SNAPSHOTS_DIR")
    directory = Path(directory_setting)
    if not directory.exists():
        return {}

    snapshots: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Nie udało się wczytać pliku snapshot %s: %s", path, exc)
            continue

        if isinstance(payload, dict) and isinstance(payload.get("snapshots"), list):
            entries = payload["snapshots"]
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = [payload]

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            fallback = f"{path.stem}-{index}"
            kort_id = extract_kort_id(entry, fallback)
            snapshots[str(kort_id)] = entry

    return snapshots


def normalize_snapshot_entry(kort_id: str, snapshot: Dict[str, Any] | None, link_meta: Dict[str, str] | None = None) -> Dict[str, Any]:
    snapshot = snapshot or {}
    has_snapshot = bool(snapshot)
    available = snapshot.get("available", True) if has_snapshot else False
    status = normalize_status(snapshot.get("status"), available, has_snapshot)
    status_label_text = status_label(status)

    overlay_is_on = bool(available)
    overlay_label = "ON" if overlay_is_on else "OFF"
    last_updated = normalize_last_updated(
        snapshot.get("last_updated")
        or snapshot.get("updated_at")
        or snapshot.get("timestamp")
    )

    kort_label = (
        snapshot.get("court_name")
        or snapshot.get("kort_name")
        or snapshot.get("kort")
        or snapshot.get("court")
        or (link_meta or {}).get("name")
        or (f"Kort {kort_id}" if kort_id else "Kort")
    )

    players = normalize_players(snapshot.get("players"), snapshot.get("serving"))
    row_span = max(len(players), 1)

    score_summary = display_value(
        snapshot.get("game_score")
        or snapshot.get("score_summary")
        or snapshot.get("score"),
    )

    set_summary = display_value(snapshot.get("set_score") or snapshot.get("sets"))

    return {
        "kort_id": str(kort_id),
        "kort_label": display_name(kort_label, fallback=f"Kort {kort_id}" if kort_id else "Kort"),
        "status": status,
        "status_label": status_label_text,
        "available": available,
        "has_snapshot": has_snapshot,
        "overlay_is_on": overlay_is_on,
        "overlay_label": overlay_label,
        "last_updated": last_updated,
        "players": players,
        "row_span": row_span,
        "score_summary": score_summary,
        "set_summary": set_summary,
    }


__all__ = ["load_snapshots", "normalize_snapshot_entry", "STATUS_ORDER"]
