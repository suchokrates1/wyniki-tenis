import copy
import json
import logging
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from results_state_machine import CourtPhase, CourtState, ScoreSnapshot

logger = logging.getLogger(__name__)

SNAPSHOT_STATUS_NO_DATA = "brak danych"
SNAPSHOT_STATUS_UNAVAILABLE = "niedostępny"
SNAPSHOT_STATUS_OK = "ok"

_SENSITIVE_FIELD_MARKERS = (
    "token",
    "secret",
    "password",
    "key",
    "auth",
)

UPDATE_INTERVAL_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 5
NAME_STABILIZATION_TICKS = 3

PER_CONTROLAPP_MIN_INTERVAL_SECONDS = 1.0
GLOBAL_RATE_LIMIT_PER_SECOND = 2.0
GLOBAL_TOKEN_BUCKET_CAPACITY = GLOBAL_RATE_LIMIT_PER_SECOND
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 5.0
RETRY_MAX_DELAY_SECONDS = 120.0
RETRY_JITTER_MAX_SECONDS = 0.3

COMMAND_ERROR_PAUSE_THRESHOLD = 3
COMMAND_ERROR_PAUSE_MINUTES = 5
UNAVAILABLE_SLOW_POLL_SECONDS = 60.0
NOT_FOUND_COOLDOWN_SECONDS = 10 * 60.0

FULL_SNAPSHOT_COMMAND = None

ARCHIVE_LIMIT = 50


CommandPlanEntry = Dict[str, Any]


NOT_FOUND_BADGE = {
    "key": "not_found",
    "label": "404",
    "description": "Kort nie został znaleziony (HTTP 404)",
}


_PLAYER_FIELD_PATTERN = re.compile(
    r"^(Name|Points|Set\d+|CurrentSet|TieBreak)Player([AB])$"
)


COMMAND_PLAN: Dict[CourtPhase, Dict[str, CommandPlanEntry]] = {
    CourtPhase.IDLE_NAMES: {
        "GetNamePlayerA": {"commands": ("GetNamePlayerA",)},
        "GetNamePlayerB": {"commands": ("GetNamePlayerB",)},
        "ProbeAvailability": {"commands": ("GetOverlayVisibility",)},
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

metrics_lock = threading.Lock()


def get_all_snapshots() -> Dict[str, Dict[str, Any]]:
    """Return a deep copy of all current snapshots under a thread lock."""

    with snapshots_lock:
        return copy.deepcopy(snapshots)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial_metrics_state() -> Dict[str, Any]:
    return {
        "started_at": _now_iso(),
        "last_response_at": None,
        "last_retry_at": None,
        "last_snapshot_at": None,
        "last_tick_at": None,
        "ticks_total": 0,
        "responses": {
            "total": 0,
            "by_status_code": {},
            "by_error": {},
        },
        "retries": {
            "total": 0,
            "by_reason": {},
        },
        "snapshots": {
            "total": 0,
            "by_status": {},
        },
    }


metrics: Dict[str, Any] = _initial_metrics_state()


def reset_metrics() -> None:
    with metrics_lock:
        metrics.clear()
        metrics.update(_initial_metrics_state())


def _increment_counter(storage: Dict[str, int], key: str) -> None:
    storage[key] = storage.get(key, 0) + 1


def _record_response_event(*, status_code: Optional[int] = None, error: Optional[str] = None) -> None:
    with metrics_lock:
        metrics["responses"]["total"] += 1
        metrics["last_response_at"] = _now_iso()
        if status_code is not None:
            bucket = metrics["responses"]["by_status_code"]
            _increment_counter(bucket, str(status_code))
        elif error is not None:
            bucket = metrics["responses"]["by_error"]
            _increment_counter(bucket, error)


def _record_retry_event(reason: str) -> None:
    with metrics_lock:
        metrics["retries"]["total"] += 1
        metrics["last_retry_at"] = _now_iso()
        bucket = metrics["retries"]["by_reason"]
        _increment_counter(bucket, reason)


def _record_snapshot_metrics(snapshot: Dict[str, Any]) -> None:
    status = snapshot.get("status") or "unknown"
    with metrics_lock:
        metrics["snapshots"]["total"] += 1
        metrics["last_snapshot_at"] = _now_iso()
        bucket = metrics["snapshots"]["by_status"]
        _increment_counter(bucket, str(status))


def _record_tick() -> None:
    with metrics_lock:
        metrics["ticks_total"] += 1
        metrics["last_tick_at"] = _now_iso()


def get_metrics_snapshot() -> Dict[str, Any]:
    with metrics_lock:
        return copy.deepcopy(metrics)
court_states: Dict[str, CourtState] = {}

_throttle_lock = threading.Lock()
_last_request_by_controlapp: Dict[str, float] = {}
_next_allowed_request_by_controlapp: Dict[str, float] = {}
_global_token_balance: float = GLOBAL_TOKEN_BUCKET_CAPACITY
_global_tokens_last_refill: Optional[float] = time.time()


def _shorten_for_logging(text: str, max_length: int = 256) -> str:
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"


def _is_sensitive_key(key: Any) -> bool:
    try:
        key_text = str(key).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in key_text for marker in _SENSITIVE_FIELD_MARKERS)


def _sanitize_for_logging(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: Dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[key] = "***"
            else:
                sanitized[key] = _sanitize_for_logging(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_logging(item) for item in value]
    if isinstance(value, str):
        return _shorten_for_logging(value, max_length=128)
    return value


def _format_payload_for_logging(payload: Any, *, max_length: int = 512) -> str:
    sanitized = _sanitize_for_logging(payload)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(sanitized)
    return _shorten_for_logging(text, max_length=max_length)


def _format_rate_limit_headers(response: Optional[requests.Response]) -> str:
    if response is None:
        return ""

    headers = getattr(response, "headers", None)
    if not headers:
        return ""

    header_mapping = (
        ("X-RateLimit-Remaining", "remaining"),
        ("X-RateLimit-Limit", "limit"),
        ("X-RateLimit-Reset", "reset"),
        ("Retry-After", "retry_after"),
        ("X-Singular-RateLimit-Daily-Calls", "daily"),
    )

    parts: List[str] = []
    for header_name, label in header_mapping:
        value = headers.get(header_name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(f"{label}={text}")

    if not parts:
        return ""

    return f" (limity: {', '.join(parts)})"


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


def _throttle_request(
    controlapp_id: str,
    *,
    simulate: bool = False,
    current_time: Optional[float] = None,
) -> None:
    if simulate:
        if current_time is None:
            raise ValueError("current_time is required when simulate=True")
        simulated_time = current_time
    else:
        simulated_time = None
    while True:
        with _throttle_lock:
            now_value = simulated_time if simulated_time is not None else time.time()
            global _global_token_balance, _global_tokens_last_refill

            last_refill = _global_tokens_last_refill
            if last_refill is None:
                last_refill = now_value
                _global_tokens_last_refill = now_value
            elapsed = max(0.0, now_value - last_refill)
            if elapsed > 0.0:
                refill = elapsed * GLOBAL_RATE_LIMIT_PER_SECOND
                _global_token_balance = min(
                    GLOBAL_TOKEN_BUCKET_CAPACITY,
                    _global_token_balance + refill,
                )
                _global_tokens_last_refill = now_value

            last = _last_request_by_controlapp.get(controlapp_id)
            wait_for_controlapp = 0.0
            if last is not None:
                wait_for_controlapp = (last + PER_CONTROLAPP_MIN_INTERVAL_SECONDS) - now_value

            cooldown_until = _next_allowed_request_by_controlapp.get(controlapp_id)
            wait_for_cooldown = 0.0
            if cooldown_until is not None:
                wait_for_cooldown = cooldown_until - now_value

            global_available = _global_token_balance >= 1.0 - 1e-9

            if wait_for_controlapp <= 0 and wait_for_cooldown <= 0 and global_available:
                _last_request_by_controlapp[controlapp_id] = now_value
                _global_token_balance = max(0.0, _global_token_balance - 1.0)
                if cooldown_until is not None and now_value + 1e-9 >= cooldown_until:
                    _next_allowed_request_by_controlapp.pop(controlapp_id, None)
                return

            wait_time = max(wait_for_controlapp, wait_for_cooldown, 0.0)
            if not global_available:
                deficit = max(0.0, 1.0 - _global_token_balance)
                wait_for_tokens = deficit / GLOBAL_RATE_LIMIT_PER_SECOND
                wait_time = max(wait_time, wait_for_tokens)

        if simulated_time is not None:
            simulated_time = now_value + wait_time
            continue

        _sleep(wait_time)


def _schedule_controlapp_resume(controlapp_id: str, allowed_from: float) -> None:
    with _throttle_lock:
        allowed = max(0.0, allowed_from)
        current = _next_allowed_request_by_controlapp.get(controlapp_id)
        if current is None or allowed > current:
            _next_allowed_request_by_controlapp[controlapp_id] = allowed


def _controlapp_cooldown_until(controlapp_id: str, now: float) -> Optional[float]:
    with _throttle_lock:
        allowed_from = _next_allowed_request_by_controlapp.get(controlapp_id)
        if allowed_from is None:
            return None
        if now + 1e-9 >= allowed_from:
            _next_allowed_request_by_controlapp.pop(controlapp_id, None)
            return None
        return allowed_from


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


def _parse_reset_header_value(value: str, *, reference_time: float) -> Optional[float]:
    value = (value or "").strip()
    if not value:
        return None

    try:
        numeric_value = float(value)
    except ValueError:
        try:
            reset_dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if reset_dt is None:
            return None
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        return reset_dt.timestamp()

    if numeric_value > reference_time + 1.0:
        return numeric_value
    if numeric_value >= 0:
        return reference_time + numeric_value

    return None


def _parse_rate_limit_reset(
    response: requests.Response, *, reference_time: float
) -> Optional[float]:
    try:
        header_value = response.headers.get("X-RateLimit-Reset")
    except Exception:  # noqa: BLE001
        return None

    if not header_value:
        return None

    return _parse_reset_header_value(str(header_value), reference_time=reference_time)


def _parse_singular_daily_calls_header(
    response: requests.Response, *, reference_time: float
) -> Optional[Dict[str, Optional[float]]]:
    try:
        header_value = response.headers.get("X-Singular-RateLimit-Daily-Calls")
    except Exception:  # noqa: BLE001
        return None

    if not header_value:
        return None

    items: Dict[str, str] = {}
    for part in re.split(r"[;,]", str(header_value)):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        items[key] = value

    if not items:
        return None

    def _parse_int(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    limit = _parse_int(items.get("limit"))
    remaining = _parse_int(items.get("remaining"))
    reset_raw = items.get("reset")
    reset_at = (
        _parse_reset_header_value(reset_raw, reference_time=reference_time)
        if reset_raw
        else None
    )

    return {"limit": limit, "remaining": remaining, "reset": reset_at}


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
                "available": False,
                "archive": [],
                "pause_active": False,
                "pause_minutes": COMMAND_ERROR_PAUSE_MINUTES,
                "pause_until": None,
                "badges": [],
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

    nested_updates: Dict[str, Dict[str, Any]] = {}
    for key, value in list(flat.items()):
        match = _PLAYER_FIELD_PATTERN.match(str(key))
        if not match:
            continue
        field, suffix = match.groups()
        player_key = f"Player{suffix}"
        player_fields = nested_updates.setdefault(player_key, {})
        player_fields[field] = value

    for player_key, fields in nested_updates.items():
        existing = flat.get(player_key)
        base: Dict[str, Any]
        if isinstance(existing, dict):
            base = dict(existing)
        elif existing is None:
            base = {}
        else:
            base = {"Value": existing}
        base.update(fields)
        flat[player_key] = base

    return flat


def _map_command_response(command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    flattened = _flatten_overlay_payload(payload)
    mapped: Dict[str, Any] = dict(flattened)

    command = command or ""

    def _ensure_player_entry(suffix: str) -> Dict[str, Any]:
        player_key = f"Player{suffix}"
        existing = mapped.get(player_key)
        if isinstance(existing, dict):
            entry = existing
        elif existing is None:
            entry = {}
            mapped[player_key] = entry
        else:
            entry = {"Value": existing}
            mapped[player_key] = entry
        return entry

    def _assign_player_field(suffix: str, field: str, value: Any) -> None:
        key = f"{field}Player{suffix}"
        mapped[key] = value
        entry = _ensure_player_entry(suffix)
        entry[field] = value

    def _extract_from_player(
        suffix: str, keys: List[str], fallback_keys: Optional[List[str]] = None
    ) -> Optional[Any]:
        for key in keys:
            candidate_key = key.format(player=suffix)
            if candidate_key in flattened:
                return flattened[candidate_key]

        player_key = f"Player{suffix}"
        nested = flattened.get(player_key)
        if isinstance(nested, dict):
            for key in keys:
                normalized_key = key.replace("Player{player}", "")
                normalized_key = normalized_key.replace("{player}", "")
                candidate = nested.get(normalized_key) or nested.get(
                    normalized_key.capitalize()
                )
                if candidate is not None:
                    return candidate
        elif nested is not None:
            return nested

        if fallback_keys:
            for key in fallback_keys:
                if key in flattened:
                    return flattened[key]
        return None

    player_suffix: Optional[str] = None
    player_match = re.search(r"Player([AB])$", command)
    if player_match:
        player_suffix = player_match.group(1)

    if command.startswith("GetNamePlayer") and player_suffix:
        value = _extract_from_player(
            player_suffix,
            [f"NamePlayer{{player}}", "Name", "name", "Value", "value"],
        )
        if value is not None:
            _assign_player_field(player_suffix, "Name", value)

    elif command.startswith("GetPointsPlayer") and player_suffix:
        value = _extract_from_player(
            player_suffix,
            [f"PointsPlayer{{player}}", "Points", "points", "Value", "value"],
        )
        if value is not None:
            _assign_player_field(player_suffix, "Points", value)

    elif command.startswith("GetCurrentSetPlayer") and player_suffix:
        value = _extract_from_player(
            player_suffix,
            [
                f"CurrentSetPlayer{{player}}",
                "CurrentSet",
                "current_set",
                "Value",
                "value",
            ],
        )
        if value is not None:
            _assign_player_field(player_suffix, "CurrentSet", value)

    elif command.startswith("GetTieBreakPlayer") and player_suffix:
        value = _extract_from_player(
            player_suffix,
            [
                f"TieBreakPlayer{{player}}",
                "TieBreak",
                "tiebreak",
                "Value",
                "value",
            ],
        )
        if value is not None:
            _assign_player_field(player_suffix, "TieBreak", value)

    elif command == "GetServe":
        server_indicator: Optional[str] = None
        for key in ("Server", "Serve", "CurrentServer", "value", "Value"):
            candidate = flattened.get(key)
            if isinstance(candidate, str):
                normalized = candidate.strip().upper()
                if normalized in {"A", "B"}:
                    server_indicator = normalized
                    break

        if server_indicator is not None:
            mapped[f"ServePlayer{server_indicator}"] = True
            _ensure_player_entry(server_indicator)["Serve"] = True
            other = "B" if server_indicator == "A" else "A"
            mapped[f"ServePlayer{other}"] = False
            _ensure_player_entry(other)["Serve"] = False
        else:
            for suffix in ("A", "B"):
                candidate = _extract_from_player(
                    suffix,
                    [f"ServePlayer{{player}}", f"Player{{player}}", suffix],
                    fallback_keys=[f"Serve{suffix}"]
                )
                if candidate is None:
                    continue
                interpreted = _interpret_visibility_value(candidate)
                if interpreted is None:
                    if isinstance(candidate, (int, float)):
                        interpreted = candidate != 0
                    elif isinstance(candidate, str):
                        interpreted = candidate.strip().lower() in {"true", "tak", "on", "1"}
                if interpreted is None:
                    continue
                mapped[f"ServePlayer{suffix}"] = interpreted
                _ensure_player_entry(suffix)["Serve"] = interpreted

    elif command == "GetOverlayVisibility":
        interpreted: Optional[bool] = None
        for key in flattened.keys():
            interpreted = _interpret_visibility_value(flattened[key])
            if interpreted is not None:
                break
        if interpreted is not None:
            mapped["OverlayVisibility"] = interpreted

    elif command == "GetTieBreakVisibility":
        interpreted: Optional[bool] = None
        for key in flattened.keys():
            interpreted = _interpret_visibility_value(flattened[key])
            if interpreted is not None:
                break
        if interpreted is not None:
            mapped["TieBreakVisibility"] = interpreted

    elif command == "GetMode":
        value = None
        for key in ("Mode", "mode", "Value", "value"):
            if key in flattened:
                value = flattened[key]
                break
        if value is not None:
            mapped["Mode"] = value

    elif command == "GetSet":
        for key, value in list(flattened.items()):
            match = _PLAYER_FIELD_PATTERN.match(str(key))
            if not match:
                continue
            field, suffix = match.groups()
            _assign_player_field(suffix, field, value)

    if player_suffix and command.startswith("GetTieBreakPlayer"):
        mapped.setdefault("TieBreakVisibility", mapped.get("TieBreakVisibility"))

    return mapped


def _interpret_visibility_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "tak", "visible", "show"}:
            return True
        if normalized in {"0", "false", "no", "off", "nie", "hidden", "hide"}:
            return False
        return None

    if isinstance(value, dict):
        for key in ("value", "Value", "visibility", "Visibility"):
            if key in value:
                interpreted = _interpret_visibility_value(value[key])
                if interpreted is not None:
                    return interpreted
        return None

    return None


def _detect_overlay_visibility(data: Dict[str, Any]) -> Optional[bool]:
    for key, value in data.items():
        key_text = str(key).lower()
        if "overlay" not in key_text:
            continue
        if "visibility" not in key_text and key_text != "overlayvisible":
            continue
        interpreted = _interpret_visibility_value(value)
        if interpreted is not None:
            return interpreted
    return None


def parse_overlay_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Niepoprawna struktura JSON – oczekiwano obiektu")

    normalized = _flatten_overlay_payload(payload)

    players = _extract_players(normalized)
    serving = _detect_server(normalized)
    available = _detect_overlay_visibility(normalized)

    return {
        "players": players,
        "serving": serving,
        "available": available,
        "raw": normalized,
    }


def _extract_players(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}
    for suffix in ("A", "B"):
        player_key = f"Player{suffix}"
        nested = data.get(player_key)
        name: Any = None
        points: Any = None
        sets: Dict[str, Any] = {}

        if isinstance(nested, dict):
            if "Name" in nested:
                name = nested.get("Name")
            elif "Value" in nested:
                name = nested.get("Value")
            if "Points" in nested:
                points = nested.get("Points")
            for key, value in nested.items():
                if key.startswith("Set"):
                    sets[f"{key}Player{suffix}"] = value
                elif key == "CurrentSet":
                    sets[f"CurrentSetPlayer{suffix}"] = value
                elif key == "TieBreak":
                    sets[f"TieBreakPlayer{suffix}"] = value

        if name is None:
            fallback_name = data.get(player_key)
            if isinstance(fallback_name, dict):
                name = fallback_name.get("Name") or fallback_name.get("Value")
            elif fallback_name is not None:
                name = fallback_name
        if name is None:
            name = data.get(f"NamePlayer{suffix}")

        if points is None:
            points = data.get(f"PointsPlayer{suffix}")

        for key, value in data.items():
            if key.startswith("Set") and key.endswith(f"Player{suffix}"):
                sets.setdefault(key, value)

        players[suffix] = {
            "name": name,
            "points": points,
            "sets": sets,
        }
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
        for key, value in partial.items():
            if key in {"PlayerA", "PlayerB"} and isinstance(value, dict):
                existing = raw.get(key)
                if isinstance(existing, dict):
                    merged = dict(existing)
                    merged.update(value)
                    raw[key] = merged
                else:
                    raw[key] = dict(value)
            else:
                raw[key] = value
        entry["raw"] = raw
        entry["kort_id"] = str(kort_id)
        entry.setdefault("players", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry.setdefault("status", SNAPSHOT_STATUS_NO_DATA)
        entry.setdefault("serving", None)
        entry.setdefault("available", False)
        entry["last_updated"] = _now_iso()
        entry["error"] = None
        entry["pause_minutes"] = entry.get("pause_minutes") or COMMAND_ERROR_PAUSE_MINUTES
        entry["pause_active"] = False
        entry["pause_until"] = None
        entry["badges"] = []

        try:
            parsed = parse_overlay_json(raw)
        except Exception:  # noqa: BLE001
            entry["available"] = False
            snapshots[str(kort_id)] = entry
            return copy.deepcopy(entry)

        players = parsed["players"]
        serving = parsed["serving"]
        available_value = parsed.get("available")
        entry["available"] = bool(available_value) if available_value is not None else False

        def _has_player_info(info: Any) -> bool:
            if not isinstance(info, dict):
                return False
            if info.get("name") not in (None, ""):
                return True
            if info.get("points") is not None:
                return True
            sets_value = info.get("sets")
            if isinstance(sets_value, dict) and sets_value:
                return True
            return False

        merged_players: Dict[str, Dict[str, Any]] = copy.deepcopy(entry.get("players") or {})
        for suffix in ("A", "B"):
            info = players.get(suffix) or {}
            if not _has_player_info(info):
                continue
            player_entry = merged_players.setdefault(suffix, {})
            name = info.get("name")
            if name is not None:
                player_entry["name"] = name
            points = info.get("points")
            if points is not None:
                player_entry["points"] = points
            sets = info.get("sets") or {}
            if sets:
                existing_sets = dict(player_entry.get("sets") or {})
                existing_sets.update(sets)
                player_entry["sets"] = existing_sets
            else:
                player_entry.setdefault("sets", {})

        for suffix in ("A", "B"):
            player_entry = merged_players.get(suffix)
            if player_entry is not None:
                player_entry.setdefault("sets", {})
                player_entry["is_serving"] = serving == suffix

        entry["players"] = merged_players
        entry["serving"] = serving

        def _has_content(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, bool):
                return value
            if isinstance(value, (dict, list, tuple, set)):
                return bool(value)
            return True

        def _player_has_payload(player_raw: Any) -> bool:
            if not isinstance(player_raw, dict):
                return False

            name_value: Optional[Any] = None
            for key in ("name", "Name", "Value"):
                if key not in player_raw:
                    continue
                candidate = player_raw.get(key)
                if isinstance(candidate, str):
                    if candidate.strip():
                        name_value = candidate
                        break
                elif candidate not in (None, ""):
                    name_value = candidate
                    break
            if not isinstance(name_value, str) or not name_value.strip():
                return False

            points_value: Optional[Any] = None
            for key in ("points", "Points"):
                if key not in player_raw:
                    continue
                candidate = player_raw.get(key)
                if isinstance(candidate, str):
                    if candidate.strip():
                        points_value = candidate
                        break
                elif candidate is not None:
                    points_value = candidate
                    break
            has_points = points_value is not None

            set_values: list[Any] = []
            sets_mapping = player_raw.get("sets")
            if isinstance(sets_mapping, dict):
                for key, value in sets_mapping.items():
                    if "set" in str(key).lower():
                        set_values.append(value)
            if not set_values:
                for key, value in player_raw.items():
                    key_text = str(key).lower()
                    if "set" in key_text:
                        set_values.append(value)
            has_sets = any(_has_content(value) for value in set_values)

            return has_points or has_sets

        entry["status"] = SNAPSHOT_STATUS_NO_DATA
        if all(_player_has_payload(merged_players.get(suffix)) for suffix in ("A", "B")):
            entry["status"] = SNAPSHOT_STATUS_OK

        snapshots[str(kort_id)] = entry
        snapshot = copy.deepcopy(entry)
    return snapshot


def _update_command_error_state(
    state: CourtState,
    *,
    now: float,
    spec_name: Optional[str] = None,
) -> tuple[bool, bool]:
    previous_pause_until = state.paused_until or 0.0
    state.command_error_streak = min(state.command_error_streak + 1, 10_000)
    if spec_name:
        current = state.command_error_streak_by_spec.get(spec_name, 0) + 1
        state.command_error_streak_by_spec[spec_name] = min(current, 10_000)

    pause_active = False
    new_pause_started = False
    if state.command_error_streak >= COMMAND_ERROR_PAUSE_THRESHOLD:
        pause_seconds = COMMAND_ERROR_PAUSE_MINUTES * 60
        candidate_until = now + pause_seconds
        if state.paused_until is None or candidate_until > state.paused_until:
            state.paused_until = candidate_until
        pause_active = state.paused_until is not None and state.paused_until > now
        if pause_active and previous_pause_until <= now:
            new_pause_started = True

    return pause_active, new_pause_started


def _update_snapshot_pause_state(
    kort_id: str, state: CourtState, *, now: float
) -> Dict[str, Any]:
    entry = ensure_snapshot_entry(kort_id)
    pause_until_iso = (
        datetime.fromtimestamp(state.paused_until, timezone.utc).isoformat()
        if state.paused_until is not None
        else None
    )
    is_paused = state.paused_until is not None and state.paused_until > now

    with snapshots_lock:
        entry.setdefault("kort_id", str(kort_id))
        if not isinstance(entry.get("players"), dict):
            entry["players"] = entry.get("players") or {}
        else:
            entry.setdefault("players", {})
        if not isinstance(entry.get("raw"), dict):
            entry["raw"] = entry.get("raw") or {}
        else:
            entry.setdefault("raw", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry.setdefault("status", entry.get("status", SNAPSHOT_STATUS_NO_DATA))
        entry.setdefault("serving", entry.get("serving"))
        entry.setdefault("available", entry.get("available", False))
        entry.setdefault("badges", entry.get("badges", []))
        entry.setdefault("error", entry.get("error"))
        entry["pause_minutes"] = entry.get("pause_minutes") or COMMAND_ERROR_PAUSE_MINUTES
        entry["pause_active"] = is_paused
        entry["pause_until"] = pause_until_iso
        entry["last_updated"] = _now_iso()
        snapshot = copy.deepcopy(entry)

    return snapshot


def _handle_command_error(
    kort_id: str,
    error: str,
    *,
    state: Optional[CourtState] = None,
    now: Optional[float] = None,
    spec_name: Optional[str] = None,
    badges: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if now is None:
        now = time.time()
    if state is None:
        state = _ensure_court_state(kort_id)

    pause_active, new_pause_started = _update_command_error_state(
        state, now=now, spec_name=spec_name
    )

    if new_pause_started:
        logger.warning(
            "Kort %s przechodzi w pauzę na %s min po %s kolejnych błędach",
            kort_id,
            COMMAND_ERROR_PAUSE_MINUTES,
            state.command_error_streak,
        )

    pause_until_iso = (
        datetime.fromtimestamp(state.paused_until, timezone.utc).isoformat()
        if state.paused_until is not None
        else None
    )
    is_paused = pause_active

    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        entry["error"] = error
        entry["status"] = SNAPSHOT_STATUS_UNAVAILABLE
        entry.setdefault("players", {})
        entry.setdefault("raw", {})
        entry.setdefault("archive", entry.get("archive", []))
        entry["last_updated"] = _now_iso()
        entry["available"] = False
        entry["pause_minutes"] = entry.get("pause_minutes") or COMMAND_ERROR_PAUSE_MINUTES
        entry["pause_active"] = is_paused
        entry["pause_until"] = pause_until_iso
        entry["badges"] = copy.deepcopy(badges or [])
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
        if len(history) > ARCHIVE_LIMIT:
            del history[:-ARCHIVE_LIMIT]
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
    name_signature = state.compute_name_signature(snapshot)
    has_any_name = any(part.strip() for part in name_signature.split("|"))

    status = snapshot.get("status")
    if status != SNAPSHOT_STATUS_OK:
        # Pozostań w IDLE dopóki nie ustabilizujemy nazwisk – nawet jeśli
        # częściowe dane są już dostępne w innych polach.
        if not has_any_name:
            return CourtPhase.IDLE_NAMES
        if state.name_stability < NAME_STABILIZATION_TICKS:
            return CourtPhase.IDLE_NAMES
    elif not has_any_name:
        return CourtPhase.IDLE_NAMES

    if (
        state.phase is CourtPhase.IDLE_NAMES
        and NAME_STABILIZATION_TICKS > 0
        and state.name_stability < NAME_STABILIZATION_TICKS
    ):
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
    state.update_name_stability(
        name_signature, required_ticks=NAME_STABILIZATION_TICKS
    )
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

    availability_value = snapshot.get("available")
    raw_payload = snapshot.get("raw")
    has_visibility_flag = False
    if isinstance(raw_payload, dict):
        for key in raw_payload.keys():
            key_text = str(key).lower()
            if "overlayvisibility" in key_text or key_text == "overlayvisible":
                has_visibility_flag = True
                break

    if availability_value is False and has_visibility_flag:
        state.apply_availability_pause(now, UNAVAILABLE_SLOW_POLL_SECONDS)
    elif availability_value is True:
        state.clear_availability_pause()


def update_snapshot_for_kort(
    kort_id: str,
    control_url: str,
    *,
    session: Optional[requests.sessions.Session] = None,
) -> Dict[str, Any]:
    entry = ensure_snapshot_entry(kort_id)
    with snapshots_lock:
        snapshot = copy.deepcopy(entry)
    return snapshot


def _mark_unavailable(kort_id: str, *, error: Optional[str]) -> Dict[str, Any]:
    payload = {
        "kort_id": str(kort_id),
        "status": SNAPSHOT_STATUS_UNAVAILABLE,
        "last_updated": _now_iso(),
        "players": {},
        "raw": {},
        "serving": None,
        "error": error,
        "available": False,
        "pause_minutes": COMMAND_ERROR_PAUSE_MINUTES,
        "pause_active": False,
        "pause_until": None,
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
    overlay_links_supplier: Callable[[], Dict[str, Dict[str, Any]]],
    *,
    session: Optional[requests.sessions.Session] = None,
    now: Optional[float] = None,
) -> None:
    time_source = time.time
    reference_time = time_source()
    time_offset = (now - reference_time) if now is not None else 0.0

    def refresh_current_time() -> float:
        return time_source() + time_offset

    current_time = now if now is not None else reference_time
    try:
        with app.app_context():
            links = overlay_links_supplier() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać listy kortów: %s", exc)
        return

    first_iteration = True
    for kort_id, urls in links.items():
        if first_iteration:
            if now is None:
                current_time = refresh_current_time()
            first_iteration = False
        else:
            current_time = refresh_current_time()

        if not (urls or {}).get("enabled", True):
            logger.debug("Pominięto kort %s - polling wyłączony", kort_id)
            continue

        ensure_snapshot_entry(kort_id)
        state = _ensure_court_state(kort_id)
        control_url = (urls or {}).get("control")
        if not control_url:
            logger.warning("Pominięto kort %s - brak adresu control", kort_id)
            continue
        try:
            controlapp_identifier = _extract_controlapp_identifier(control_url)
            base_url = build_output_url(control_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Nie udało się przygotować adresu dla kortu %s: %s", kort_id, exc
            )
            snapshot = _handle_command_error(
                kort_id,
                error=str(exc),
                state=state,
                now=current_time,
            )
            _record_snapshot_metrics(snapshot)
            _process_snapshot(state, snapshot, current_time)
            state.tick_counter += 1
            _record_tick()
            continue

        cooldown_until = _controlapp_cooldown_until(controlapp_identifier, current_time)
        if cooldown_until is not None:
            remaining = max(0.0, cooldown_until - current_time)
            logger.debug(
                "Pominięto żądanie dla kortu %s z powodu limitu (pozostało %.2f s)",
                kort_id,
                remaining,
            )
            state.tick_counter += 1
            _record_tick()
            continue

        if state.is_paused(current_time):
            pause_until = state.effective_pause_until() or current_time
            remaining = max(0.0, pause_until - current_time)
            logger.debug(
                "Pominięto żądanie dla kortu %s z powodu pauzy (pozostało %.2f s)",
                kort_id,
                remaining,
            )
            state.tick_counter += 1
            _record_tick()
            continue

        spec_name = state.pop_due_command(current_time)
        if not spec_name:
            continue

        command = _select_command(state, spec_name)
        if not command:
            state.tick_counter += 1
            _record_tick()
            current_time = refresh_current_time()
            state.mark_polled(current_time)
            continue

        http = session or requests
        payload = {"command": command}
        attempt = 0
        final_snapshot: Optional[Dict[str, Any]] = None
        last_error: Optional[str] = None
        last_response: Optional[requests.Response] = None
        last_error_label: Optional[str] = None

        command_succeeded = False

        request_id = uuid.uuid4().hex

        while True:
            response: Optional[requests.Response] = None
            should_retry = False
            attempt_started = time.perf_counter()
            status_code: Optional[int] = None
            try:
                try:
                    _throttle_request(controlapp_identifier)
                    response = http.put(
                        base_url,
                        json=payload,
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    rate_limits_desc = _format_rate_limit_headers(response)
                    logger.debug(
                        "Żądanie %s %s zakończone statusem %s%s",
                        "PUT",
                        response.url,
                        response.status_code,
                        rate_limits_desc,
                    )
                    status_code = response.status_code
                except requests.Timeout as exc:
                    should_retry = True
                    last_error = str(exc)
                    last_response = None
                    last_error_label = exc.__class__.__name__
                    _record_response_event(error="timeout")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Nie udało się pobrać komendy %s dla kortu %s: %s",
                        command,
                        kort_id,
                        exc,
                    )
                    last_error_label = exc.__class__.__name__
                    _record_response_event(error="exception")
                    final_snapshot = _handle_command_error(
                        kort_id,
                        error=str(exc),
                        state=state,
                        now=current_time,
                        spec_name=spec_name,
                    )
                    break

                if response is not None:
                    response_time = refresh_current_time()
                    current_time = response_time
                    _record_response_event(status_code=status_code)
                    daily_limits = _parse_singular_daily_calls_header(
                        response, reference_time=response_time
                    )
                    if (
                        daily_limits
                        and daily_limits.get("remaining") is not None
                        and daily_limits.get("remaining") <= 0
                    ):
                        reset_candidate = daily_limits.get("reset")
                        if reset_candidate is not None:
                            _schedule_controlapp_resume(
                                controlapp_identifier, float(reset_candidate)
                            )
                    if status_code == 404:
                        diagnostics = _format_http_error_details(command, response)
                        cooldown_until = current_time + NOT_FOUND_COOLDOWN_SECONDS
                        _schedule_controlapp_resume(controlapp_identifier, cooldown_until)
                        cooldown_seconds = max(0.0, cooldown_until - current_time)
                        resume_iso = datetime.fromtimestamp(
                            cooldown_until, timezone.utc
                        ).isoformat()
                        logger.warning(
                            (
                                "Serwer zwrócił 404 dla kortu %s (%s) - "
                                "pauza %.0f s (do %s) (%s)%s"
                            ),
                            kort_id,
                            command,
                            cooldown_seconds,
                            resume_iso,
                            diagnostics,
                            rate_limits_desc,
                        )
                        final_snapshot = _handle_command_error(
                            kort_id,
                            error=diagnostics,
                            state=state,
                            now=current_time,
                            spec_name=spec_name,
                            badges=[NOT_FOUND_BADGE],
                        )
                        break

                    if status_code == 400:
                        diagnostics = _format_http_error_details(command, response)
                        logger.warning(
                            "Serwer zwrócił błąd 400 dla kortu %s (%s): %s",
                            kort_id,
                            command,
                            diagnostics,
                        )
                        final_snapshot = _handle_command_error(
                            kort_id,
                            error=diagnostics,
                            state=state,
                            now=current_time,
                            spec_name=spec_name,
                        )
                        break

                    if 400 <= status_code < 500 and status_code not in {404, 429}:
                        diagnostics = _format_http_error_details(command, response)
                        logger.warning(
                            "Serwer zwrócił błąd %s dla kortu %s (%s): %s",
                            status_code,
                            kort_id,
                            command,
                            diagnostics,
                        )
                        final_snapshot = _handle_command_error(
                            kort_id,
                            error=diagnostics,
                            state=state,
                            now=current_time,
                            spec_name=spec_name,
                        )
                        break

                    if status_code == 429:
                        retry_after_header = None
                        reset_header = None
                        try:
                            retry_after_header = response.headers.get("Retry-After")
                            reset_header = response.headers.get("X-RateLimit-Reset")
                        except Exception:  # noqa: BLE001
                            pass

                        retry_after_seconds = _parse_retry_after(response)
                        reset_timestamp = _parse_rate_limit_reset(
                            response, reference_time=current_time
                        )
                        cooldown_candidates: List[float] = []
                        if retry_after_seconds is not None:
                            cooldown_candidates.append(current_time + retry_after_seconds)
                        if reset_timestamp is not None:
                            cooldown_candidates.append(max(current_time, reset_timestamp))

                        if cooldown_candidates:
                            cooldown_until = max(cooldown_candidates)
                        else:
                            cooldown_until = (
                                current_time + PER_CONTROLAPP_MIN_INTERVAL_SECONDS
                            )

                        _schedule_controlapp_resume(controlapp_identifier, cooldown_until)

                        cooldown_seconds = max(0.0, cooldown_until - current_time)
                        reset_iso = datetime.fromtimestamp(
                            cooldown_until, timezone.utc
                        ).isoformat()
                        logger.warning(
                            (
                                "Serwer zwrócił 429 dla kortu %s (%s) - "
                                "pauza %.2f s (Retry-After=%s, X-RateLimit-Reset=%s, do=%s)%s"
                            ),
                            kort_id,
                            command,
                            cooldown_seconds,
                            retry_after_header or "brak",
                            reset_header or "brak",
                            reset_iso,
                            rate_limits_desc,
                        )
                        _pause_active, new_pause_started = _update_command_error_state(
                            state, now=current_time, spec_name=spec_name
                        )
                        if new_pause_started:
                            logger.warning(
                                "Kort %s przechodzi w pauzę na %s min po %s kolejnych błędach",
                                kort_id,
                                COMMAND_ERROR_PAUSE_MINUTES,
                                state.command_error_streak,
                            )
                        _update_snapshot_pause_state(
                            kort_id, state, now=current_time
                        )
                        break

                    if status_code >= 500:
                        should_retry = True
                        last_error = _format_http_error_details(command, response)
                        last_response = response
                        last_error_label = f"HTTP_{status_code}"
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
                            final_snapshot = _handle_command_error(
                                kort_id,
                                error=str(exc),
                                state=state,
                                now=current_time,
                                spec_name=spec_name,
                            )
                            break

                        logger.debug(
                            "Odpowiedź komendy %s dla kortu %s: %s%s",
                            command,
                            kort_id,
                            _format_payload_for_logging(payload),
                            rate_limits_desc,
                        )
                        mapped_payload = _map_command_response(command, payload)
                        final_snapshot = _merge_partial_payload(kort_id, mapped_payload)
                        command_succeeded = True
                        break
            finally:
                duration_ms = (time.perf_counter() - attempt_started) * 1000.0
                log_payload = {
                    "kort_id": kort_id,
                    "command": command,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "retry": attempt > 0,
                    "request_id": request_id,
                }
                logger.info(json.dumps(log_payload, ensure_ascii=False))

            if should_retry:
                retry_reason = last_error_label or (
                    f"HTTP_{response.status_code}" if response is not None else "unknown"
                )
                _record_retry_event(str(retry_reason))
                if attempt >= MAX_RETRY_ATTEMPTS:
                    payload_summary = _format_payload_for_logging(payload)
                    diagnostics = last_error
                    if not diagnostics and last_response is not None:
                        diagnostics = _format_http_error_details(command, last_response)
                    if not diagnostics:
                        diagnostics = "brak dodatkowych informacji"
                    diagnostics = _shorten_for_logging(str(diagnostics))
                    attempt_count = attempt + 1
                    logger.warning(
                        (
                            "Wyczerpano próby pobierania komendy %s "
                            "dla kortu %s po %s próbach "
                            "(payload=%s, ostatnia_odpowiedź=%s)"
                        ),
                        command,
                        kort_id,
                        attempt_count,
                        payload_summary,
                        diagnostics,
                    )
                    error_message = last_error or diagnostics or "Nie udało się pobrać danych kortu"
                    final_snapshot = _handle_command_error(
                        kort_id,
                        error=error_message,
                        state=state,
                        now=current_time,
                        spec_name=spec_name,
                    )
                    break

                delay = _calculate_retry_delay(attempt, response=response)
                attempt += 1
                retry_reason = last_error or (
                    f"HTTP {response.status_code}" if response is not None else "nieznany powód"
                )
                logger.debug(
                    "Ponawianie komendy %s dla kortu %s za %.2f s (próba %s, powód: %s)",
                    command,
                    kort_id,
                    delay,
                    attempt + 1,
                    _shorten_for_logging(str(retry_reason)),
                )
                _sleep(delay)
                continue

            break

        if command_succeeded:
            state.clear_pause()

        if final_snapshot is not None:
            current_time = refresh_current_time()
            _record_snapshot_metrics(final_snapshot)
            _process_snapshot(state, final_snapshot, current_time)

        state.tick_counter += 1
        _record_tick()


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
    "get_all_snapshots",
    "get_metrics_snapshot",
    "parse_overlay_json",
    "reset_metrics",
    "snapshots",
    "start_background_updater",
    "update_snapshot_for_kort",
]

