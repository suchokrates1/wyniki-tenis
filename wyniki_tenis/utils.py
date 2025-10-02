"""Utility helpers shared across views and services."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Sequence

from .constants import (
    ACTIVE_STATUSES,
    FINISHED_STATUSES,
    NO_DATA_STATUSES,
    STATUS_LABELS,
    UNAVAILABLE_STATUSES,
)


def as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to ``int`` returning ``default`` when conversion fails."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def as_float(value: Any, default: float) -> float:
    """Convert ``value`` to ``float`` supporting numbers provided as strings."""
    try:
        normalized = value
        if not isinstance(normalized, str):
            normalized = str(normalized)
        normalized = normalized.strip().replace(",", ".")
        return float(normalized)
    except (TypeError, ValueError):
        return default


def kort_id_sort_key(value: Any) -> tuple[int, str | int]:
    if isinstance(value, str) and value.isdigit():
        return (0, int(value))
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        pass
    return (1, str(value))


def extract_kort_id(entry: Dict[str, Any], fallback: str) -> str:
    candidate = (
        (entry or {}).get("kort_id")
        or (entry or {}).get("court_id")
        or (entry or {}).get("id")
        or (entry or {}).get("kort")
        or (entry or {}).get("court")
    )
    if candidate is None:
        return str(fallback)

    if isinstance(candidate, (int, float)):
        return str(int(candidate))

    candidate_str = str(candidate).strip()
    digits = re.findall(r"\d+", candidate_str)
    if digits:
        return digits[0]
    return candidate_str or str(fallback)


def display_value(value: Any, fallback: str = "brak danych") -> str:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return fallback if not value else ", ".join(map(str, value))
    text = str(value).strip()
    return text if text else fallback


def display_name(value: Any, fallback: str = "brak danych") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def normalize_last_updated(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return text
    else:
        return str(value)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).isoformat()


def _is_serving_marker(serving_marker: Any, index: int, name: str | None) -> bool:
    if serving_marker is None:
        return False

    if isinstance(serving_marker, (int, float)):
        try:
            return int(serving_marker) == index
        except (TypeError, ValueError):
            return False

    if isinstance(serving_marker, str):
        marker = serving_marker.strip().lower()
        if marker in {"player1", "p1"}:
            return index == 0
        if marker in {"player2", "p2"}:
            return index == 1
        if name:
            return marker == str(name).strip().lower()
        return False

    if isinstance(serving_marker, dict):
        if "index" in serving_marker:
            try:
                return int(serving_marker["index"]) == index
            except (TypeError, ValueError):
                pass
        marker_name = serving_marker.get("name") or serving_marker.get("player")
        if marker_name and name:
            return str(marker_name).strip().lower() == str(name).strip().lower()
    return False


def normalize_players(players_data: Any, serving_marker: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []

    if isinstance(players_data, dict):
        iterable: Iterable[Any] = list(players_data.values())
    elif isinstance(players_data, Sequence) and not isinstance(players_data, (str, bytes)):
        iterable = players_data
    else:
        iterable = []

    for index, raw_player in enumerate(iterable):
        if isinstance(raw_player, dict):
            name = raw_player.get("name") or raw_player.get("player") or raw_player.get("label")
            sets = (
                raw_player.get("sets")
                or raw_player.get("set_score")
                or raw_player.get("sets_won")
                or raw_player.get("set")
            )
            games = (
                raw_player.get("games")
                or raw_player.get("games_won")
                or raw_player.get("score")
                or raw_player.get("points")
            )
        else:
            name = raw_player
            sets = None
            games = None

        normalized.append(
            {
                "display_name": display_name(name),
                "display_sets": display_value(sets),
                "display_games": display_value(games),
                "is_serving": _is_serving_marker(serving_marker, index, name),
            }
        )

    if not normalized:
        normalized.append(
            {
                "display_name": "brak danych",
                "display_sets": "brak danych",
                "display_games": "brak danych",
                "is_serving": False,
            }
        )

    return normalized


def normalize_status(raw_status: Any, available: bool, has_snapshot: bool) -> str:
    if not has_snapshot:
        return "brak_danych"

    status_text = str(raw_status or "").strip().lower()

    if status_text in NO_DATA_STATUSES:
        return "brak_danych"

    if status_text in FINISHED_STATUSES:
        base_status = "finished"
    elif status_text in ACTIVE_STATUSES or status_text == "ok":
        base_status = "active"
    elif status_text in UNAVAILABLE_STATUSES:
        base_status = "unavailable"
    else:
        base_status = "active"

    if not available and base_status != "brak_danych":
        return "unavailable"

    return base_status


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


__all__ = [
    "as_float",
    "as_int",
    "display_name",
    "display_value",
    "extract_kort_id",
    "kort_id_sort_key",
    "normalize_last_updated",
    "normalize_players",
    "normalize_status",
    "status_label",
]
