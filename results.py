import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

SNAPSHOT_STATUS_NO_DATA = "brak danych"
SNAPSHOT_STATUS_UNAVAILABLE = "niedostępny"
SNAPSHOT_STATUS_OK = "ok"

UPDATE_INTERVAL_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 5

snapshots_lock = threading.Lock()
snapshots: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_output_url(control_url: str) -> str:
    if not control_url:
        return control_url

    parsed = urlparse(control_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        control_index = segments.index("control")
        identifier = segments[control_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(
            "Nie można wyodrębnić identyfikatora aplikacji kontrolnej z adresu"
        ) from exc

    return f"https://app.overlays.uno/apiv2/controlapps/{identifier}/api"


def ensure_snapshot_entry(kort_id: str) -> Dict[str, Any]:
    with snapshots_lock:
        entry = snapshots.setdefault(
            str(kort_id),
            {
                "kort_id": str(kort_id),
                "status": SNAPSHOT_STATUS_NO_DATA,
                "last_updated": None,
                "players": {},
                "raw": {},
                "serving": None,
                "error": None,
            },
        )
    return entry


def _flatten_overlay_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}

    def _normalize(value: Any) -> Any:
        if isinstance(value, dict):
            if "value" in value:
                return _normalize(value["value"])
            if "Value" in value:
                return _normalize(value["Value"])
        return value

    def _walk(obj: Any) -> None:
        if not isinstance(obj, dict):
            return

        for key, value in obj.items():
            normalized = _normalize(value)
            if isinstance(normalized, dict):
                _walk(normalized)
            else:
                flat[key] = normalized

            if isinstance(value, dict):
                _walk(value)

    _walk(payload)
    return flat


def parse_overlay_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Niepoprawna struktura JSON – oczekiwano obiektu")

    normalized = _flatten_overlay_payload(payload)

    if "PlayerA" not in normalized or "PlayerB" not in normalized:
        raise ValueError("Brak wymaganych danych graczy w źródle JSON")

    players = _extract_players(normalized)
    serving = _detect_server(normalized)

    return {
        "players": players,
        "serving": serving,
        "raw": normalized,
    }


def _extract_players(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}
    for suffix in ("A", "B"):
        name_key = f"Player{suffix}"
        player_payload: Dict[str, Any] = {
            "name": data.get(name_key),
            "points": data.get(f"PointsPlayer{suffix}"),
            "sets": {
                key: value
                for key, value in data.items()
                if key.startswith("Set") and key.endswith(f"Player{suffix}")
            },
        }
        players[suffix] = player_payload
    return players


def _detect_server(data: Dict[str, Any]) -> Optional[str]:
    for suffix in ("A", "B"):
        value = data.get(f"ServePlayer{suffix}")
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return suffix
    return None


def update_snapshot_for_kort(
    kort_id: str,
    control_url: str,
    *,
    session: Optional[requests.sessions.Session] = None,
) -> Dict[str, Any]:
    ensure_snapshot_entry(kort_id)
    try:
        output_url = build_output_url(control_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nie udało się zbudować adresu API dla kortu %s: %s", kort_id, exc
        )
        return _mark_unavailable(kort_id, error=str(exc))
    http = session or requests
    try:
        response = http.get(output_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać danych dla kortu %s: %s", kort_id, exc)
        return _mark_unavailable(kort_id, error=str(exc))

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "Nie udało się zdekodować JSON dla kortu %s: %s", kort_id, exc
        )
        return _mark_unavailable(kort_id, error=str(exc))

    try:
        parsed = parse_overlay_json(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nie udało się przeparsować danych dla kortu %s: %s", kort_id, exc
        )
        return _mark_unavailable(kort_id, error=str(exc))

    players = parsed["players"]
    serving = parsed["serving"]

    payload = {
        "kort_id": str(kort_id),
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": _now_iso(),
        "players": {
            suffix: {
                **info,
                "is_serving": serving == suffix,
            }
            for suffix, info in players.items()
        },
        "raw": parsed["raw"],
        "serving": serving,
        "error": None,
    }

    with snapshots_lock:
        snapshots[str(kort_id)] = payload
    return payload


def _mark_unavailable(kort_id: str, *, error: Optional[str]) -> Dict[str, Any]:
    payload = {
        "kort_id": str(kort_id),
        "status": SNAPSHOT_STATUS_UNAVAILABLE,
        "last_updated": _now_iso(),
        "players": {},
        "raw": {},
        "serving": None,
        "error": error,
    }
    with snapshots_lock:
        snapshots[str(kort_id)] = payload
    return payload


def _update_once(
    app,
    overlay_links_supplier: Callable[[], Dict[str, Dict[str, str]]],
    *,
    session: Optional[requests.sessions.Session] = None,
) -> None:
    try:
        with app.app_context():
            links = overlay_links_supplier() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać listy kortów: %s", exc)
        return

    for kort_id, urls in links.items():
        ensure_snapshot_entry(kort_id)
        control_url = (urls or {}).get("control")
        if not control_url:
            logger.warning("Pominięto kort %s - brak adresu control", kort_id)
            continue
        update_snapshot_for_kort(kort_id, control_url, session=session)


_thread: Optional[threading.Thread] = None


def start_background_updater(
    app,
    overlay_links_supplier: Callable[[], Dict[str, Dict[str, str]]],
    *,
    session: Optional[requests.sessions.Session] = None,
) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return

    def runner() -> None:
        while True:
            _update_once(app, overlay_links_supplier, session=session)
            time.sleep(UPDATE_INTERVAL_SECONDS)

    # Ustawiamy wstępnie stan kortów na "brak danych"
    try:
        with app.app_context():
            links = overlay_links_supplier() or {}
        for kort_id in links:
            ensure_snapshot_entry(kort_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nie udało się wstępnie zainicjować snapshotów kortów: %s", exc
        )

    _thread = threading.Thread(target=runner, name="kort-snapshots", daemon=True)
    _thread.start()


__all__ = [
    "SNAPSHOT_STATUS_NO_DATA",
    "SNAPSHOT_STATUS_OK",
    "SNAPSHOT_STATUS_UNAVAILABLE",
    "build_output_url",
    "ensure_snapshot_entry",
    "parse_overlay_json",
    "snapshots",
    "start_background_updater",
    "update_snapshot_for_kort",
]
