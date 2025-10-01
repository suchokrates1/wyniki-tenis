import copy
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import urlparse

import requests

from results_state_machine import CourtPhase, CourtState, ScoreSnapshot

logger = logging.getLogger(__name__)

SNAPSHOT_STATUS_NO_DATA = "brak danych"
SNAPSHOT_STATUS_UNAVAILABLE = "niedostępny"
SNAPSHOT_STATUS_OK = "ok"

UPDATE_INTERVAL_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 5
NAME_STABILIZATION_TICKS = 12

PER_CONTROLAPP_MIN_INTERVAL_SECONDS = 1.0
GLOBAL_RATE_LIMIT_PER_SECOND = 4
GLOBAL_RATE_WINDOW_SECONDS = 1.0
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 10.0
RETRY_JITTER_MAX_SECONDS = 0.3

FULL_SNAPSHOT_COMMAND = "GetOverlayState"


CommandPlanEntry = Dict[str, Any]


COMMAND_PLAN: Dict[CourtPhase, Dict[str, CommandPlanEntry]] = {
    CourtPhase.IDLE_NAMES: {
        "GetNamePlayerA": {"commands": ("GetNamePlayerA",)},
        "GetNamePlayerB": {"commands": ("GetNamePlayerB",)},
    },
    CourtPhase.PRE_START: {
        "GetPoints": {
            "commands": (
                "GetOverlayVisibility",
                "GetMode",
                "GetServe",
                "GetPointsPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.LIVE_POINTS: {
        "GetPoints": {
            "commands": (
                "GetOverlayVisibility",
                "GetMode",
                "GetServe",
                "GetPointsPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.LIVE_GAMES: {
        "GetGames": {
            "commands": (
                "GetSet",
                "GetCurrentSetPlayer{player}",
            ),
            "players": ("A", "B"),
        },
        "ProbePoints": {
            "commands": (
                "GetServe",
                "GetPointsPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.LIVE_SETS: {
        "GetSets": {
            "commands": (
                "GetSet",
                "GetCurrentSetPlayer{player}",
            ),
            "players": ("A", "B"),
        },
        "ProbeGames": {
            "commands": (
                "GetServe",
                "GetCurrentSetPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.TIEBREAK7: {
        "GetPoints": {
            "commands": (
                "GetOverlayVisibility",
                "GetTieBreakVisibility",
                "GetServe",
                "GetTieBreakPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.SUPER_TB10: {
        "GetPoints": {
            "commands": (
                "GetOverlayVisibility",
                "GetTieBreakVisibility",
                "GetServe",
                "GetTieBreakPlayer{player}",
            ),
            "players": ("A", "B"),
        },
    },
    CourtPhase.FINISHED: {
        "GetNamePlayerA": {"commands": ("GetNamePlayerA",)},
        "GetNamePlayerB": {"commands": ("GetNamePlayerB",)},
    },
}

snapshots_lock = threading.Lock()
snapshots: Dict[str, Dict[str, Any]] = {}

states_lock = threading.Lock()
court_states: Dict[str, CourtState] = {}

_throttle_lock = threading.Lock()
_last_request_by_controlapp: Dict[str, float] = {}
_recent_request_timestamps: Deque[float] = deque()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_controlapp_identifier(control_url: str) -> str:
    parsed = urlparse(control_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    for marker in ("controlapps", "control"):
        if marker in segments:
            marker_index = segments.index(marker)
            try:
                return segments[marker_index + 1]
            except IndexError as exc:
                raise ValueError(
                    "Nie można wyodrębnić identyfikatora aplikacji kontrolnej z adresu"
                ) from exc
    raise ValueError("Nie można wyodrębnić identyfikatora aplikacji kontrolnej z adresu")


def build_output_url(control_url: str) -> str:
    if not control_url:
        return control_url

    identifier = _extract_controlapp_identifier(control_url)

    return f"https://app.overlays.uno/apiv2/controlapps/{identifier}/api"


def _sleep(duration: float) -> None:
    if duration <= 0:
        return
    time.sleep(duration)


def _throttle_request(controlapp_id: str, *, current_time: Optional[float] = None) -> None:
    simulated_time = current_time
    while True:
        with _throttle_lock:
            now_value = simulated_time if simulated_time is not None else time.time()
            last = _last_request_by_controlapp.get(controlapp_id)
            wait_for_controlapp = 0.0
            if last is not None:
                wait_for_controlapp = (last + PER_CONTROLAPP_MIN_INTERVAL_SECONDS) - now_value

            while _recent_request_timestamps and now_value - _recent_request_timestamps[0] >= GLOBAL_RATE_WINDOW_SECONDS:
                _recent_request_timestamps.popleft()

            global_available = len(_recent_request_timestamps) < GLOBAL_RATE_LIMIT_PER_SECOND

            if wait_for_controlapp <= 0 and global_available:
                _last_request_by_controlapp[controlapp_id] = now_value
                _recent_request_timestamps.append(now_value)
                return

            wait_time = max(wait_for_controlapp, 0.0)
            if not global_available and _recent_request_timestamps:
                earliest = _recent_request_timestamps[0]
                wait_time = max(wait_time, earliest + GLOBAL_RATE_WINDOW_SECONDS - now_value)

        if simulated_time is not None:
            simulated_time = now_value + wait_time
            continue

        _sleep(wait_time)


def _parse_retry_after(response: requests.Response) -> Optional[float]:
    try:
        header_value = response.headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None

    if not header_value:
        return None

    try:
        seconds = float(header_value)
        return max(0.0, seconds)
    except ValueError:
        try:
            retry_dt = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return None

        if retry_dt is None:
            return None

        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (retry_dt - now).total_seconds()
        return max(0.0, delta)


def _calculate_retry_delay(attempt: int, response: Optional[requests.Response] = None) -> float:
    if response is not None:
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return retry_after

    delay = min(
        RETRY_MAX_DELAY_SECONDS,
        RETRY_BASE_DELAY_SECONDS * (2 ** attempt),
    )
    jitter = random.uniform(0, RETRY_JITTER_MAX_SECONDS)
    return delay + jitter


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


def _select_command(state: CourtState, spec_name: str) -> Optional[str]:
    plan = COMMAND_PLAN.get(state.phase) or {}
    entry = plan.get(spec_name)
    if not entry:
        return None

    pending_entries = state.pending_players_by_spec.get(spec_name)
    if pending_entries:
        command_template, player = pending_entries.pop(0)
        if pending_entries:
            state.pending_players_by_spec[spec_name] = pending_entries
        else:
            state.pending_players_by_spec.pop(spec_name, None)
        if player is not None and "{player}" in command_template:
            return command_template.format(player=player)
        return command_template

    raw_commands = entry.get("commands")
    if raw_commands is None:
        command_template = entry.get("command")
        if not command_template:
            return None
        raw_commands = (command_template,)

    if isinstance(raw_commands, (str, dict)):
        commands: List[Any] = [raw_commands]
    else:
        commands = list(raw_commands)

    players: tuple[str, ...] = tuple(entry.get("players") or ())
    ordered_players: List[str] = []
    if players:
        start_player = state.next_player_by_spec.get(spec_name, players[0])
        ordered_players = _order_players(players, start_player)

    queue: List[tuple[str, Optional[str]]] = []
    has_player_command = False

    for item in commands:
        if isinstance(item, dict):
            command_template = item.get("command")
        else:
            command_template = str(item)

        if not command_template:
            continue

        if "{player}" in command_template:
            if not ordered_players:
                if not players:
                    continue
                ordered_players = _order_players(players, players[0])
            for player in ordered_players:
                queue.append((command_template, player))
            has_player_command = True
        else:
            queue.append((command_template, None))

    if not queue:
        return None

    if has_player_command and players and ordered_players:
        last_player = ordered_players[-1]
        try:
            idx = players.index(last_player)
        except ValueError:
            idx = 0
        next_idx = (idx + 1) % len(players)
        state.next_player_by_spec[spec_name] = players[next_idx]

    command_template, player = queue.pop(0)
    if queue:
        state.pending_players_by_spec[spec_name] = queue
    else:
        state.pending_players_by_spec.pop(spec_name, None)

    if player is not None and "{player}" in command_template:
        return command_template.format(player=player)
    return command_template


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
        entry["status"] = SNAPSHOT_STATUS_UNAVAILABLE
        entry.setdefault("players", {})
        entry.setdefault("raw", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry["last_updated"] = _now_iso()
        snapshot = copy.deepcopy(entry)
    return snapshot


def _format_http_error_details(command: str, response: requests.Response) -> str:
    try:
        url = response.url
    except Exception:  # noqa: BLE001
        url = "<unknown>"
    try:
        body = response.text or ""
    except Exception:  # noqa: BLE001
        body = "<unavailable>"
    body = body.strip()
    max_length = 256
    if len(body) > max_length:
        body = f"{body[:max_length]}…"
    content_type = ""
    try:
        content_type = response.headers.get("Content-Type", "")
    except Exception:  # noqa: BLE001
        content_type = ""
    parts = [
        f"HTTP {response.status_code}",
        f"url={url}",
        f"command={command}",
    ]
    if content_type:
        parts.append(f"content_type={content_type}")
    if body:
        parts.append(f"body={body}")
    return ", ".join(parts)


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


def _classify_phase(
    snapshot: Dict[str, Any], state: CourtState, score: ScoreSnapshot
) -> CourtPhase:
    if snapshot.get("status") != SNAPSHOT_STATUS_OK:
        return CourtPhase.IDLE_NAMES

    name_signature = state.compute_name_signature(snapshot)
    if not any(part.strip() for part in name_signature.split("|")):
        return CourtPhase.IDLE_NAMES

    if state.phase is CourtPhase.IDLE_NAMES and state.name_stability < 12:
        return CourtPhase.IDLE_NAMES

    if (
        state.name_stability < NAME_STABILIZATION_TICKS
        and not score.points_any
        and not score.games_any
        and not score.sets_present
    ):
        return CourtPhase.IDLE_NAMES

    sets_won_a, sets_won_b = score.sets_won
    finished_sets = score.sets_completed >= 1 and (
        max(sets_won_a, sets_won_b) >= 2 or score.sets_completed >= 3
    )
    if finished_sets and state.points_absent_streak >= 2:
        return CourtPhase.FINISHED

    if score.super_tb_active:
        return CourtPhase.SUPER_TB10

    if score.tie_break_active:
        return CourtPhase.TIEBREAK7

    if score.sets_present and score.sets_completed > 0:
        return CourtPhase.LIVE_SETS

    if score.games_positive:
        return CourtPhase.LIVE_GAMES

    if state.points_positive_streak >= 2:
        return CourtPhase.LIVE_POINTS

    if score.points_any or score.games_any or score.sets_present:
        return CourtPhase.PRE_START

    return CourtPhase.PRE_START


def _process_snapshot(state: CourtState, snapshot: Dict[str, Any], now: float) -> None:
    state.mark_polled(now)
    name_signature = state.compute_name_signature(snapshot)
    state.update_name_stability(name_signature)
    score_snapshot = state.compute_score_snapshot(snapshot)
    state.update_score_stability(score_snapshot)
    desired_phase = _classify_phase(snapshot, state, score_snapshot)
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
        response = http.get(
            output_url,
            params={"command": FULL_SNAPSHOT_COMMAND},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        logger.info(
            "Żądanie %s %s zakończone statusem %s",
            "GET",
            response.url,
            response.status_code,
        )
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
        spec_name = state.pop_due_command(current_time)
        if not spec_name:
            continue

        command = _select_command(state, spec_name)
        if not command:
            state.tick_counter += 1
            state.mark_polled(current_time)
            continue

        try:
            controlapp_identifier = _extract_controlapp_identifier(control_url)
            base_url = build_output_url(control_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Nie udało się przygotować adresu dla kortu %s: %s", kort_id, exc
            )
            snapshot = _handle_command_error(kort_id, error=str(exc))
            _process_snapshot(state, snapshot, current_time)
            state.tick_counter += 1
            continue

        http = session or requests
        params = {"command": command}
        attempt = 0
        final_snapshot: Optional[Dict[str, Any]] = None
        last_error: Optional[str] = None

        while True:
            response: Optional[requests.Response] = None
            should_retry = False
            try:
                _throttle_request(controlapp_identifier, current_time=current_time)
                response = http.get(
                    base_url,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                logger.debug(
                    "Żądanie %s %s zakończone statusem %s",
                    "GET",
                    response.url,
                    response.status_code,
                )
            except requests.Timeout as exc:
                should_retry = True
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Nie udało się pobrać komendy %s dla kortu %s: %s",
                    command,
                    kort_id,
                    exc,
                )
                final_snapshot = _handle_command_error(kort_id, error=str(exc))
                break

            if response is not None:
                status_code = response.status_code

                if status_code == 400:
                    diagnostics = _format_http_error_details(command, response)
                    logger.warning(
                        "Serwer zwrócił błąd 400 dla kortu %s (%s): %s",
                        kort_id,
                        command,
                        diagnostics,
                    )
                    final_snapshot = _handle_command_error(kort_id, error=diagnostics)
                    break

                if 400 <= status_code < 500 and status_code != 429:
                    diagnostics = _format_http_error_details(command, response)
                    logger.warning(
                        "Serwer zwrócił błąd %s dla kortu %s (%s): %s",
                        status_code,
                        kort_id,
                        command,
                        diagnostics,
                    )
                    final_snapshot = _handle_command_error(kort_id, error=diagnostics)
                    break

                if status_code == 429 or status_code >= 500:
                    should_retry = True
                    last_error = _format_http_error_details(command, response)
                else:
                    try:
                        response.raise_for_status()
                        payload = response.json()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Nie udało się pobrać komendy %s dla kortu %s: %s",
                            command,
                            kort_id,
                            exc,
                        )
                        final_snapshot = _handle_command_error(kort_id, error=str(exc))
                        break

                    flattened = _flatten_overlay_payload(payload)
                    final_snapshot = _merge_partial_payload(kort_id, flattened)
                    break

            if should_retry:
                if attempt >= MAX_RETRY_ATTEMPTS:
                    logger.warning(
                        "Wyczerpano próby pobierania komendy %s dla kortu %s po %s próbach",
                        command,
                        kort_id,
                        attempt + 1,
                    )
                    error_message = last_error or "Nie udało się pobrać danych kortu"
                    final_snapshot = _handle_command_error(kort_id, error=error_message)
                    break

                delay = _calculate_retry_delay(attempt, response=response)
                attempt += 1
                logger.debug(
                    "Ponawianie komendy %s dla kortu %s za %.2f s (próba %s)",
                    command,
                    kort_id,
                    delay,
                    attempt + 1,
                )
                _sleep(delay)
                continue

            break

        if final_snapshot is not None:
            _process_snapshot(state, final_snapshot, current_time)

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
