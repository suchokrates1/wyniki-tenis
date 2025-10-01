import copy
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import requests

from results_state_machine import CourtPhase, CourtState

logger = logging.getLogger(__name__)

SNAPSHOT_STATUS_NO_DATA = "brak danych"
SNAPSHOT_STATUS_UNAVAILABLE = "niedostępny"
SNAPSHOT_STATUS_OK = "ok"

UPDATE_INTERVAL_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 5
NAME_STABILIZATION_TICKS = 12


CommandPlanEntry = Dict[str, Any]


COMMAND_PLAN: Dict[CourtPhase, List[CommandPlanEntry]] = {
    CourtPhase.IDLE_NAMES: [
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
        {"command": "GetMatchStatus"},
    ],
    CourtPhase.PRE_START: [
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.LIVE_POINTS: [
        {"command": "GetPointsPlayer{player}", "players": ("A", "B")},
        {"command": "GetServePlayer{player}", "players": ("A", "B")},
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.LIVE_GAMES: [
        {"command": "GetPointsPlayer{player}", "players": ("A", "B")},
        {"command": "GetServePlayer{player}", "players": ("A", "B")},
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.LIVE_SETS: [
        {"command": "GetPointsPlayer{player}", "players": ("A", "B")},
        {"command": "GetServePlayer{player}", "players": ("A", "B")},
        {"command": "GetMatchStatus"},
        {"command": "GetSetsPlayer{player}", "players": ("A", "B")},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.TIEBREAK7: [
        {"command": "GetPointsPlayer{player}", "players": ("A", "B")},
        {"command": "GetServePlayer{player}", "players": ("A", "B")},
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.SUPER_TB10: [
        {"command": "GetPointsPlayer{player}", "players": ("A", "B")},
        {"command": "GetServePlayer{player}", "players": ("A", "B")},
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
    CourtPhase.FINISHED: [
        {"command": "GetMatchStatus"},
        {
            "command": "GetPlayerName{player}",
            "players": ("A", "B"),
            "stabilize": True,
        },
    ],
}

snapshots_lock = threading.Lock()
snapshots: Dict[str, Dict[str, Any]] = {}

states_lock = threading.Lock()
court_states: Dict[str, CourtState] = {}


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
                "archive": [],
            },
        )
    return entry


def _order_players(players: tuple[str, ...], start: str) -> List[str]:
    if not players:
        return []
    if start in players:
        start_index = players.index(start)
    else:
        start_index = 0
    ordered = list(players[start_index:]) + list(players[:start_index])
    return ordered


def _select_command(state: CourtState) -> Optional[str]:
    plan = COMMAND_PLAN.get(state.phase) or []
    if not plan:
        return None

    plan_length = len(plan)

    if state.pending_players:
        entry = plan[state.command_index % plan_length]
        player = state.pending_players.pop(0)
        command_template: str = entry["command"]
        command = command_template.format(player=player)
        if state.pending_players:
            state.next_player = state.pending_players[0]
        else:
            players = entry.get("players") or ()
            if players:
                try:
                    idx = players.index(player)
                except ValueError:
                    idx = 0
                next_idx = (idx + 1) % len(players)
                state.next_player = players[next_idx]
            state.command_index = (state.command_index + 1) % plan_length
        return command

    attempts = 0
    while attempts < plan_length:
        entry = plan[state.command_index % plan_length]
        players = entry.get("players") or ()
        if entry.get("stabilize") and players:
            if state.tick_counter % NAME_STABILIZATION_TICKS != 0:
                state.command_index = (state.command_index + 1) % plan_length
                attempts += 1
                continue
        if players:
            ordered = _order_players(players, state.next_player)
            if not ordered:
                state.command_index = (state.command_index + 1) % plan_length
                attempts += 1
                continue
            state.pending_players = ordered
            return _select_command(state)
        command = entry["command"]
        state.command_index = (state.command_index + 1) % plan_length
        return command
    return None


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


def _ensure_court_state(kort_id: str) -> CourtState:
    with states_lock:
        state = court_states.get(str(kort_id))
        if state is None:
            state = CourtState(str(kort_id))
            court_states[str(kort_id)] = state
        return state


def _merge_partial_payload(kort_id: str, partial: Dict[str, Any]) -> Dict[str, Any]:
    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        raw = dict(entry.get("raw") or {})
        raw.update(partial)
        entry["raw"] = raw
        entry["kort_id"] = str(kort_id)
        entry.setdefault("players", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry.setdefault("status", SNAPSHOT_STATUS_NO_DATA)
        entry.setdefault("serving", None)
        entry["last_updated"] = _now_iso()
        entry["error"] = None

        if "PlayerA" in raw and "PlayerB" in raw:
            try:
                parsed = parse_overlay_json(raw)
            except Exception:  # noqa: BLE001
                snapshots[str(kort_id)] = entry
                return copy.deepcopy(entry)

            players = parsed["players"]
            serving = parsed["serving"]
            entry.update(
                {
                    "status": SNAPSHOT_STATUS_OK,
                    "players": {
                        suffix: {
                            **info,
                            "is_serving": serving == suffix,
                        }
                        for suffix, info in players.items()
                    },
                    "serving": serving,
                }
            )

        snapshots[str(kort_id)] = entry
        snapshot = copy.deepcopy(entry)
    return snapshot


def _handle_command_error(kort_id: str, error: str) -> Dict[str, Any]:
    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        entry["error"] = error
        entry.setdefault("status", SNAPSHOT_STATUS_NO_DATA)
        entry.setdefault("players", {})
        entry.setdefault("raw", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry["last_updated"] = _now_iso()
        snapshot = copy.deepcopy(entry)
    return snapshot


def _archive_snapshot(kort_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    archive_entry = {
        "kort_id": snapshot.get("kort_id"),
        "status": snapshot.get("status"),
        "last_updated": snapshot.get("last_updated"),
        "players": copy.deepcopy(snapshot.get("players")),
        "serving": snapshot.get("serving"),
        "raw": copy.deepcopy(snapshot.get("raw")),
        "error": snapshot.get("error"),
    }
    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        history = entry.setdefault("archive", [])
        history.append(archive_entry)
        entry["archive"] = history
        snapshots[str(kort_id)] = entry
    return archive_entry


def _is_truthy(value: Any) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {
        "1",
        "true",
        "yes",
        "on",
        "tak",
        "finished",
        "complete",
        "completed",
        "done",
    }


def _classify_phase(snapshot: Dict[str, Any], state: CourtState) -> CourtPhase:
    if snapshot.get("status") != SNAPSHOT_STATUS_OK:
        return CourtPhase.IDLE_NAMES

    name_signature = state.compute_name_signature(snapshot)
    if not any(part.strip() for part in name_signature.split("|")):
        return CourtPhase.IDLE_NAMES

    if state.phase is CourtPhase.IDLE_NAMES and state.name_stability < 12:
        return CourtPhase.IDLE_NAMES

    raw = snapshot.get("raw") or {}
    raw_status = str(
        raw.get("ScoreMatchStatus")
        or raw.get("MatchStatus")
        or raw.get("MatchState")
        or ""
    ).lower()

    if raw_status in {"finished", "finish", "complete", "completed", "done"} or _is_truthy(
        raw.get("MatchFinished")
    ):
        return CourtPhase.FINISHED

    if _is_truthy(raw.get("SuperTieBreak")) or raw_status == "super_tiebreak":
        return CourtPhase.SUPER_TB10

    if _is_truthy(raw.get("TieBreak")) or raw_status == "tiebreak":
        return CourtPhase.TIEBREAK7

    if raw_status in {"live", "playing", "in_progress", "progress"}:
        return CourtPhase.LIVE_POINTS

    players = snapshot.get("players") or {}
    points_ready = all(
        (players.get(suffix) or {}).get("points") not in (None, "")
        for suffix in ("A", "B")
    )
    if points_ready:
        return CourtPhase.LIVE_POINTS

    set_counts = [
        len(((players.get(suffix) or {}).get("sets") or {})) for suffix in ("A", "B")
    ]
    if any(count >= 3 for count in set_counts):
        return CourtPhase.LIVE_SETS
    if any(count > 0 for count in set_counts):
        return CourtPhase.LIVE_GAMES

    return CourtPhase.PRE_START


def _process_snapshot(state: CourtState, snapshot: Dict[str, Any], now: float) -> None:
    state.mark_polled(now)
    name_signature = state.compute_name_signature(snapshot)
    state.update_name_stability(name_signature)
    desired_phase = _classify_phase(snapshot, state)
    raw_signature = state.compute_raw_signature(snapshot)

    if (
        state.phase is CourtPhase.FINISHED
        and desired_phase is CourtPhase.FINISHED
        and state.finished_name_signature
        and name_signature != state.finished_name_signature
    ):
        state.transition(CourtPhase.IDLE_NAMES, now)
        return

    if (
        state.phase is CourtPhase.FINISHED
        and desired_phase is CourtPhase.FINISHED
        and state.finished_raw_signature
        and raw_signature != state.finished_raw_signature
    ):
        state.transition(CourtPhase.IDLE_NAMES, now)
        return

    previous_phase = state.phase
    state.transition(desired_phase, now)

    if state.phase is CourtPhase.FINISHED:
        if previous_phase is not CourtPhase.FINISHED:
            _archive_snapshot(state.kort_id, snapshot)
        state.finished_name_signature = name_signature
        state.finished_raw_signature = raw_signature
    else:
        state.finished_name_signature = None
        state.finished_raw_signature = None

    # Harmonogram komend aktualizowany jest w CourtState podczas przejść


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

    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        archive = entry.get("archive", [])
        entry.update(payload)
        entry["archive"] = archive
        payload = copy.deepcopy(entry)
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
    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        archive = entry.get("archive", [])
        entry.update(payload)
        entry["archive"] = archive
        payload = copy.deepcopy(entry)
    return payload


def _update_once(
    app,
    overlay_links_supplier: Callable[[], Dict[str, Dict[str, str]]],
    *,
    session: Optional[requests.sessions.Session] = None,
    now: Optional[float] = None,
) -> None:
    current_time = now if now is not None else time.time()
    try:
        with app.app_context():
            links = overlay_links_supplier() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać listy kortów: %s", exc)
        return

    for kort_id, urls in links.items():
        ensure_snapshot_entry(kort_id)
        state = _ensure_court_state(kort_id)
        control_url = (urls or {}).get("control")
        if not control_url:
            logger.warning("Pominięto kort %s - brak adresu control", kort_id)
            continue
        command = state.pop_due_command(current_time)
        if not command:
            continue

        command = _select_command(state)
        if not command:
            state.tick_counter += 1
            state.mark_polled(current_time)
            state.schedule_next(current_time)
            continue

        try:
            base_url = build_output_url(control_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Nie udało się przygotować adresu dla kortu %s: %s", kort_id, exc
            )
            snapshot = _handle_command_error(kort_id, error=str(exc))
            _process_snapshot(state, snapshot, current_time)
            state.tick_counter += 1
            continue

        command_url = f"{base_url}?command={quote_plus(command)}"
        http = session or requests
        try:
            response = http.get(command_url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Nie udało się pobrać komendy %s dla kortu %s: %s",
                command,
                kort_id,
                exc,
            )
            snapshot = _handle_command_error(kort_id, error=str(exc))
            _process_snapshot(state, snapshot, current_time)
            state.tick_counter += 1
            continue

        flattened = _flatten_overlay_payload(payload)
        snapshot = _merge_partial_payload(kort_id, flattened)
        _process_snapshot(state, snapshot, current_time)
        state.tick_counter += 1


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
            tick_start = time.time()
            _update_once(app, overlay_links_supplier, session=session, now=tick_start)
            elapsed = time.time() - tick_start
            sleep_time = max(0.0, UPDATE_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_time)

    # Ustawiamy wstępnie stan kortów na "brak danych"
    try:
        with app.app_context():
            links = overlay_links_supplier() or {}
        for kort_id in links:
            ensure_snapshot_entry(kort_id)
            _ensure_court_state(kort_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nie udało się wstępnie zainicjować snapshotów kortów: %s", exc
        )

    _thread = threading.Thread(target=runner, name="kort-snapshots", daemon=True)
    _thread.start()


__all__ = [
    "COMMAND_PLAN",
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
