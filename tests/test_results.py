import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests
from requests import RequestException

from main import app, normalize_snapshot_entry
import results as results_module
import results_state_machine
from results import (
    SNAPSHOT_STATUS_NO_DATA,
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_UNAVAILABLE,
    build_output_url,
    snapshots,
    update_snapshot_for_kort,
)
from results_state_machine import CourtPhase


# --- Fixtures -----------------------------------------------------------------

@pytest.fixture
def snapshots_dir(tmp_path):
    original = app.config.get("SNAPSHOTS_DIR")
    app.config["SNAPSHOTS_DIR"] = tmp_path
    yield tmp_path
    app.config["SNAPSHOTS_DIR"] = original


def setup_function(function):
    # Czyścimy globalny magazyn snapshotów między testami logicznymi
    snapshots.clear()
    results_module.court_states.clear()
    results_module._last_request_by_controlapp.clear()
    results_module._recent_request_timestamps.clear()
    results_module._next_allowed_request_by_controlapp.clear()


# --- Testy widoku /wyniki -----------------------------------------------------

def test_results_page_renders_data(client, snapshot_injector):
    sample_data = [
        {
            "kort_id": "1",
            "kort": "Kort Centralny",
            "status": "active",
            "players": [
                {"name": "A. Kowalski", "sets": 1, "games": 3},
                {"name": "B. Nowak", "sets": 0, "games": 2},
            ],
            "serving": "A. Kowalski",
            "set_score": "1-0",
            "game_score": "40-30",
        },
        {
            "kort_id": "2",
            "kort": "Kort 2",
            "status": "finished",
            "players": [
                {"name": "C. Zielińska", "sets": 2, "games": 6},
                {"name": "D. Wiśniewska", "sets": 0, "games": 3},
            ],
            "serving": "player2",
            "set_score": "2-0",
            "game_score": "6-3, 6-3",
        },
    ]

    snapshot_injector({entry["kort_id"]: entry for entry in sample_data})

    response = client.get("/wyniki")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<table" in html
    assert "section-active" in html
    assert "Kort Centralny" in html
    assert "▶" in html
    assert "Overlay: ON" in html
    assert "Ostatnia aktualizacja:" in html
    assert "Status: W trakcie" in html


def test_results_page_shows_placeholder_for_finished_section(client, snapshots_dir):
    response = client.get("/wyniki")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Brak zakończonych meczów do wyświetlenia." in html
    assert "Aktualne spotkania i status kortów" in html


def test_results_page_marks_unavailable_with_notice(client, snapshot_injector):
    sample_data = [
        {
            "kort_id": "3",
            "kort": "Kort 3",
            "status": "active",
            "available": False,
            "players": [
                {"name": "E. Kowal", "sets": 0, "games": 0},
                {"name": "F. Maj", "sets": 0, "games": 0},
            ],
        }
    ]

    snapshot_injector({entry["kort_id"]: entry for entry in sample_data})

    response = client.get("/wyniki")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Brak potwierdzonych aktywnych spotkań" in html
    assert "Overlay: OFF" in html
    assert "Status: Niedostępny" in html


# --- Pomocnicze klasy do testów parsera --------------------------------------

class DummyResponse:
    def __init__(self, payload, status_code: int = 200, json_error: Exception | None = None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error
        self.headers: dict[str, str] = {}
        self.url: str | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status: {self.status_code}")

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, (dict, list)):
            try:
                return json.dumps(self._payload)
            except TypeError:
                return str(self._payload)
        return "" if self._payload is None else str(self._payload)


class DummySession:
    def __init__(self, response: DummyResponse):
        self._response = response
        self.requests: list[dict[str, object]] = []

    def put(self, url: str, timeout: int, json: dict | None = None):
        self.requests.append(
            {
                "method": "PUT",
                "url": url,
                "timeout": timeout,
                "json": copy.deepcopy(json),
            }
        )
        self._response.url = url
        return self._response


class FailingSession:
    def __init__(self, exc: Exception):
        self._exc = exc

    def put(self, url: str, timeout: int, json: dict | None = None):
        raise self._exc


class SequenceSession:
    def __init__(self, responses: list[DummyResponse]):
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def put(self, url: str, timeout: int, json: dict | None = None):
        if not self._responses:
            raise AssertionError("No more responses configured")
        response = self._responses.pop(0)
        self.requests.append(
            {
                "method": "PUT",
                "url": url,
                "timeout": timeout,
                "json": copy.deepcopy(json),
            }
        )
        response.url = url
        return response


class TimeController:
    def __init__(self, start: float = 0.0):
        self.current = start
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        return self.current

    def sleep(self, duration: float) -> None:
        self.sleep_calls.append(duration)
        self.current += duration


# --- Testy logiki parsera/snapshotów -----------------------------------------

def test_build_output_url_extracts_identifier():
    url = "https://app.overlays.uno/control/abc123"
    assert (
        build_output_url(url)
        == "https://app.overlays.uno/apiv2/controlapps/abc123/api"
    )


def test_update_snapshot_for_kort_initializes_entry_without_requests():
    snapshots.clear()
    session = DummySession(DummyResponse({}))

    snapshot = update_snapshot_for_kort(
        "1", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA
    assert snapshot["players"] == {}
    assert snapshot["raw"] == {}
    assert snapshot["serving"] is None


def test_update_once_logs_rate_limit_headers(caplog, snapshots_dir):
    _ = snapshots_dir
    caplog.set_level(logging.DEBUG, logger=results_module.logger.name)

    response = DummyResponse(
        {
            "PlayerA": {"Name": "Test A"},
            "PlayerB": {"Name": "Test B"},
        }
    )
    response.headers = {
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Limit": "10",
        "X-RateLimit-Reset": "170000",
        "Retry-After": "2",
    }
    session = DummySession(response)

    control_url = "https://app.overlays.uno/control/abc123"

    results_module._update_once(
        app,
        lambda: {"kort-1": {"control": control_url}},
        session=session,
        now=10.0,
    )

    log_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == results_module.logger.name
    ]

    assert any(
        "limity: remaining=5, limit=10, reset=170000, retry_after=2" in message
        for message in log_messages
    )


def test_merge_partial_payload_maps_single_player_fields():
    kort_id = "1"
    flattened = results_module._flatten_overlay_payload({"NamePlayerA": {"value": "A. Nowak"}})

    snapshot = results_module._merge_partial_payload(kort_id, flattened)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA
    assert snapshot["raw"].get("PlayerA", {}).get("Name") == "A. Nowak"
    assert snapshot["players"]["A"]["name"] == "A. Nowak"
    assert "B" not in snapshot["players"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"OverlayVisibility": "on"}, True),
        ({"OverlayVisibility": "off"}, False),
        ({"OverlayVisibility": {"Value": "tak"}}, True),
        ({"OverlayVisibility": {"Value": "nie"}}, False),
        ({"OverlayVisibility": True}, True),
        ({"OverlayVisibility": 0}, False),
    ],
)
def test_merge_partial_payload_sets_available_flag(payload, expected):
    kort_id = "visibility"
    flattened = results_module._flatten_overlay_payload(payload)

    snapshot = results_module._merge_partial_payload(kort_id, flattened)

    assert snapshot["available"] is expected


def test_merge_partial_payload_defaults_available_to_false():
    kort_id = "no-visibility"

    snapshot = results_module._merge_partial_payload(kort_id, {})

    assert snapshot["available"] is False


def test_merge_partial_payload_single_player_payload_does_not_set_ok():
    kort_id = "single-player"
    first = results_module._flatten_overlay_payload(
        {"NamePlayerA": "A. Kowalski", "PointsPlayerA": 15}
    )

    snapshot = results_module._merge_partial_payload(kort_id, first)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA


def test_merge_partial_payload_name_only_updates_keep_status_no_data():
    kort_id = "name-only"

    first = results_module._flatten_overlay_payload({"NamePlayerA": "A. Kowalski"})
    snapshot = results_module._merge_partial_payload(kort_id, first)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA

    second = results_module._flatten_overlay_payload({"NamePlayerB": "B. Zielińska"})
    snapshot = results_module._merge_partial_payload(kort_id, second)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA


def test_partial_updates_allow_state_progression():
    kort_id = "2"
    state = results_module._ensure_court_state(kort_id)
    now = 0.0

    first = results_module._flatten_overlay_payload({"NamePlayerA": "A. Kowalski"})
    snapshot = results_module._merge_partial_payload(kort_id, first)

    results_module._process_snapshot(state, snapshot, now)
    assert state.phase is CourtPhase.IDLE_NAMES

    second = results_module._flatten_overlay_payload({"NamePlayerB": "B. Zielińska"})
    snapshot = results_module._merge_partial_payload(kort_id, second)

    for _ in range(results_module.NAME_STABILIZATION_TICKS):
        now += 1
        results_module._process_snapshot(state, snapshot, now)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA
    assert state.phase is CourtPhase.PRE_START

    third = results_module._flatten_overlay_payload(
        {"PointsPlayerA": 0, "PointsPlayerB": 0}
    )
    snapshot = results_module._merge_partial_payload(kort_id, third)

    for _ in range(results_module.NAME_STABILIZATION_TICKS):
        now += 1
        results_module._process_snapshot(state, snapshot, now)

    assert snapshot["status"] == SNAPSHOT_STATUS_OK
    assert snapshot["raw"].get("PlayerB", {}).get("Name") == "B. Zielińska"
    assert snapshot["players"]["B"]["name"] == "B. Zielińska"
    assert state.phase is CourtPhase.PRE_START
    assert snapshot["error"] is None
    assert snapshot.get("archive") == []
    assert snapshot["last_updated"] is not None


def test_name_stabilization_triggers_points_schedule_and_snapshot_completion():
    kort_id = "phase-schedule"
    state = results_module._ensure_court_state(kort_id)
    state.phase_offset = 0.0
    state._configure_phase_commands(now=0.0)
    now = 0.0

    first = results_module._flatten_overlay_payload({"NamePlayerA": "A. Kowalski"})
    snapshot = results_module._merge_partial_payload(kort_id, first)
    results_module._process_snapshot(state, snapshot, now)

    second = results_module._flatten_overlay_payload({"NamePlayerB": "B. Zielińska"})
    snapshot = results_module._merge_partial_payload(kort_id, second)

    for _ in range(results_module.NAME_STABILIZATION_TICKS):
        now += 1.0
        results_module._process_snapshot(state, snapshot, now)

    assert snapshot["status"] == SNAPSHOT_STATUS_NO_DATA
    assert state.phase is CourtPhase.PRE_START

    schedule = state.command_schedules.get("GetPoints")
    assert schedule is not None

    initial_due = state.peek_next_due()
    assert initial_due is not None

    interval = schedule.spec.interval
    commands: list[str] = []
    current_snapshot = snapshot

    for step in range(5):
        tick_time = initial_due + step * interval + 0.001
        spec = state.pop_due_command(tick_time)
        assert spec == "GetPoints"
        command = results_module._select_command(state, spec)
        commands.append(command)

        if command == "GetOverlayVisibility":
            payload = {"OverlayVisibility": "on"}
        elif command == "GetMode":
            payload = {"Mode": "Match"}
        elif command == "GetServe":
            payload = {"ServePlayerA": 1}
        elif command == "GetPointsPlayerA":
            payload = {"PointsPlayerA": 30}
        elif command == "GetPointsPlayerB":
            payload = {"PointsPlayerB": 15}
        else:
            payload = {}

        flattened = results_module._flatten_overlay_payload(payload)
        current_snapshot = results_module._merge_partial_payload(kort_id, flattened)
        results_module._process_snapshot(state, current_snapshot, tick_time)

    assert "GetPointsPlayerA" in commands
    assert "GetPointsPlayerB" in commands

    assert current_snapshot["status"] == SNAPSHOT_STATUS_OK
    assert current_snapshot["players"]["A"]["points"] == 30
    assert current_snapshot["players"]["B"]["points"] == 15
    assert state.phase is CourtPhase.LIVE_POINTS

def test_archive_snapshot_capped_to_limit():
    kort_id = "archive-limit"

    for index in range(results_module.ARCHIVE_LIMIT + 10):
        snapshot = {
            "kort_id": kort_id,
            "status": SNAPSHOT_STATUS_OK,
            "last_updated": str(index),
            "players": {},
            "raw": {},
            "serving": None,
            "error": None,
        }
        results_module._archive_snapshot(kort_id, snapshot)

    archive = results_module.snapshots[kort_id]["archive"]
    assert len(archive) == results_module.ARCHIVE_LIMIT
    assert [entry["last_updated"] for entry in archive] == [
        str(index)
        for index in range(10, results_module.ARCHIVE_LIMIT + 10)
    ]


def test_update_once_cycles_commands_and_transitions(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {"1": {"control": "https://app.overlays.uno/control/abc123"}}

    def supplier():
        return overlay_links

    # Scenariusz czasowy: IDLE (0-29s) -> PRE_START (30-39s) -> LIVE_POINTS (40-59s)
    # -> LIVE_GAMES (60-79s) -> LIVE_SETS (80-89s) -> TIEBREAK (90-99s)
    # -> SUPER_TB (100-109s) -> FINISHED (110-139s) -> reset nazwisk (>=140s)
    idle_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "idle",
        "players": {
            "A": {"name": "Player One", "points": None, "sets": {}},
            "B": {"name": "Player Two", "points": None, "sets": {}},
        },
        "raw": {},
        "serving": None,
        "error": None,
    }
    pre_start_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "pre",
        "players": {
            "A": {"name": "Player One", "points": "0", "sets": {}},
            "B": {"name": "Player Two", "points": "0", "sets": {}},
        },
        "raw": {"PointsPlayerA": "0", "PointsPlayerB": "0"},
        "serving": None,
        "error": None,
    }
    live_games_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "games",
        "players": {
            "A": {"name": "Player One", "points": "30", "sets": {}},
            "B": {"name": "Player Two", "points": "15", "sets": {}},
        },
        "raw": {
            "PointsPlayerA": "30",
            "PointsPlayerB": "15",
            "CurrentGamePlayerA": "3",
            "CurrentGamePlayerB": "2",
        },
        "serving": None,
        "error": None,
    }
    live_sets_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "sets",
        "players": {
            "A": {
                "name": "Player One",
                "points": "0",
                "sets": {"Set1PlayerA": "6"},
            },
            "B": {
                "name": "Player Two",
                "points": "0",
                "sets": {"Set1PlayerB": "4"},
            },
        },
        "raw": {
            "PointsPlayerA": "0",
            "PointsPlayerB": "0",
            "Set1PlayerA": "6",
            "Set1PlayerB": "4",
        },
        "serving": None,
        "error": None,
    }
    live_points_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "points",
        "players": {
            "A": {"name": "Player One", "points": "15", "sets": {}},
            "B": {"name": "Player Two", "points": "30", "sets": {}},
        },
        "raw": {"PointsPlayerA": "15", "PointsPlayerB": "30"},
        "serving": None,
        "error": None,
    }
    tiebreak_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "tb",
        "players": {
            "A": {
                "name": "Player One",
                "points": "5",
                "sets": {"Set1PlayerA": "6"},
            },
            "B": {
                "name": "Player Two",
                "points": "4",
                "sets": {"Set1PlayerB": "6"},
            },
        },
        "raw": {
            "TieBreak": "true",
            "PointsPlayerA": "5",
            "PointsPlayerB": "4",
            "Set1PlayerA": "6",
            "Set1PlayerB": "6",
        },
        "serving": None,
        "error": None,
    }
    super_tb_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "stb",
        "players": {
            "A": {
                "name": "Player One",
                "points": "7",
                "sets": {"Set1PlayerA": "6", "Set2PlayerA": "10"},
            },
            "B": {
                "name": "Player Two",
                "points": "6",
                "sets": {"Set1PlayerB": "6", "Set2PlayerB": "8"},
            },
        },
        "raw": {
            "SuperTieBreak": "1",
            "PointsPlayerA": "7",
            "PointsPlayerB": "6",
            "Set1PlayerA": "6",
            "Set1PlayerB": "6",
            "Set2PlayerA": "10",
            "Set2PlayerB": "8",
        },
        "serving": None,
        "error": None,
    }
    finished_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "fin",
        "players": {
            "A": {
                "name": "Player One",
                "points": None,
                "sets": {
                    "Set1PlayerA": "6",
                    "Set2PlayerA": "6",
                },
            },
            "B": {
                "name": "Player Two",
                "points": None,
                "sets": {
                    "Set1PlayerB": "4",
                    "Set2PlayerB": "3",
                },
            },
        },
        "raw": {
            "Set1PlayerA": "6",
            "Set1PlayerB": "4",
            "Set2PlayerA": "6",
            "Set2PlayerB": "3",
        },
        "serving": None,
        "error": None,
    }
    reset_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "reset",
        "players": {
            "A": {"name": "New Player", "points": None, "sets": {}},
            "B": {"name": "Player Two", "points": None, "sets": {}},
        },
        "raw": {},
        "serving": None,
        "error": None,
    }

    current_time = {"value": 0.0}

    original_throttle = results_module._throttle_request
    current_time_ref = current_time

    def simulated_throttle(
        controlapp_id: str,
        *,
        simulate: bool = False,
        current_time=None,
    ) -> None:
        _ = simulate, current_time
        return original_throttle(
            controlapp_id,
            simulate=True,
            current_time=current_time_ref["value"],
        )

    monkeypatch.setattr(results_module, "_throttle_request", simulated_throttle)

    def fake_merge(kort_id, partial):
        tick = current_time["value"]
        if tick < 30:
            template = idle_snapshot
        elif tick < 40:
            template = pre_start_snapshot
        elif tick < 60:
            template = live_points_snapshot
        elif tick < 80:
            template = live_games_snapshot
        elif tick < 90:
            template = live_sets_snapshot
        elif tick < 100:
            template = tiebreak_snapshot
        elif tick < 110:
            template = super_tb_snapshot
        elif tick < 140:
            template = finished_snapshot
        else:
            template = reset_snapshot
        snapshot = copy.deepcopy(template)
        entry = results_module.ensure_snapshot_entry(kort_id)
        with results_module.snapshots_lock:
            archive = entry.get("archive", [])
            entry.update(snapshot)
            entry["archive"] = archive
            stored = copy.deepcopy(entry)
        return stored

    phase_command_log: list[tuple[CourtPhase, str, str]] = []

    original_select = results_module._select_command

    def logging_select(state, spec_name):
        command = original_select(state, spec_name)
        if command:
            phase_command_log.append((state.phase, spec_name, command))
        return command

    session = DummySession(DummyResponse({}))

    monkeypatch.setattr(results_module, "_merge_partial_payload", fake_merge)
    monkeypatch.setattr(results_module, "_select_command", logging_select)

    phase_log = []
    total_ticks = 170
    for tick in range(total_ticks):
        current_time["value"] = float(tick)
        results_module._update_once(
            app, supplier, now=current_time["value"], session=session
        )
        state = results_module.court_states["1"]
        phase_log.append((tick, state.phase, state.name_stability))

    state = results_module.court_states["1"]
    archive = snapshots["1"].get("archive")
    assert archive and len(archive) >= 1
    assert archive[0]["players"]["A"]["name"] == "Player One"
    assert snapshots["1"]["players"]["A"]["name"] == "New Player"
    assert any(phase == CourtPhase.IDLE_NAMES for tick, phase, _ in phase_log if tick >= 160)
    assert state.phase in {CourtPhase.FINISHED, CourtPhase.IDLE_NAMES, CourtPhase.PRE_START}

    first_non_idle_tick = next(
        (tick for tick, phase, _ in phase_log if phase is not CourtPhase.IDLE_NAMES),
        None,
    )
    assert first_non_idle_tick is None or first_non_idle_tick >= 11
    assert any(phase == CourtPhase.PRE_START for tick, phase, _ in phase_log if 12 <= tick < 40)
    assert any(phase == CourtPhase.LIVE_POINTS for tick, phase, _ in phase_log if 40 <= tick < 60)
    assert any(phase == CourtPhase.LIVE_GAMES for tick, phase, _ in phase_log if 60 <= tick < 80)
    assert any(phase == CourtPhase.LIVE_SETS for tick, phase, _ in phase_log if 80 <= tick < 90)
    assert any(phase == CourtPhase.TIEBREAK7 for tick, phase, _ in phase_log if 90 <= tick < 100)
    assert any(phase == CourtPhase.SUPER_TB10 for tick, phase, _ in phase_log if 100 <= tick < 110)
    assert any(phase == CourtPhase.FINISHED for tick, phase, _ in phase_log if 110 <= tick < 140)
    idle_stability = [stability for tick, phase, stability in phase_log if tick < 30]
    assert idle_stability and max(idle_stability) >= 12

    history = state.command_history
    early_name_specs = [
        name
        for time, name in history
        if time < 6 and name in {"GetNamePlayerA", "GetNamePlayerB"}
    ]
    assert early_name_specs
    assert early_name_specs[0] == "GetNamePlayerA"
    assert "GetNamePlayerB" in early_name_specs[:3]
    assert early_name_specs.count("GetNamePlayerA") >= 2
    assert set(early_name_specs).issubset({"GetNamePlayerA", "GetNamePlayerB"})
    assert any(
        name == "ProbeAvailability" and time < 10 for time, name in history
    )

    def consecutive_diffs(times):
        return [round(b - a, 6) for a, b in zip(times, times[1:])]

    idle_command_times = [time for time, _ in history if time < 30]
    assert idle_command_times
    assert all(diff == 1 for diff in consecutive_diffs(idle_command_times[:8]))

    idle_a_times = [
        time for time, name in history if name == "GetNamePlayerA" and time < 30
    ]
    idle_b_times = [
        time for time, name in history if name == "GetNamePlayerB" and time < 30
    ]
    assert idle_a_times and idle_b_times
    paired_idle = list(zip(idle_a_times, idle_b_times))
    assert paired_idle and all(1 <= round(b - a, 6) <= 2 for a, b in paired_idle)

    pre_start_points = [
        time for time, name in history if name == "GetPoints" and 12 <= time < 40
    ]
    assert pre_start_points
    assert all(diff == 2 for diff in consecutive_diffs(pre_start_points))

    live_points_times = [
        time for time, name in history if name == "GetPoints" and 40 <= time < 60
    ]
    assert live_points_times
    live_diffs = consecutive_diffs(live_points_times)
    assert live_diffs and min(live_diffs) <= 1
    assert all(1 <= diff <= 3 for diff in live_diffs)

    games_times = [time for time, name in history if name == "GetGames" and 60 <= time < 80]
    assert games_times
    assert all(diff in {4, 5} for diff in consecutive_diffs(games_times))

    probe_points_times = [
        time for time, name in history if name == "ProbePoints" and 60 <= time < 80
    ]
    assert probe_points_times
    assert all(diff in {6} for diff in consecutive_diffs(probe_points_times))

    finished_a_times = [
        time for time, name in history if name == "GetNamePlayerA" and 110 <= time < 140
    ]
    finished_b_times = [
        time for time, name in history if name == "GetNamePlayerB" and 110 <= time < 140
    ]
    assert finished_a_times and finished_b_times
    assert all(diff == 30 for diff in consecutive_diffs(finished_a_times))
    assert all(diff == 30 for diff in consecutive_diffs(finished_b_times))

    for b_time in finished_b_times:
        preceding_a = max(a for a in finished_a_times if a <= b_time)
        assert b_time - preceding_a == 15

    assert phase_log[-1][2] >= 1

    issued_commands = [
        request["json"]["command"]
        for request in session.requests
        if isinstance(request.get("json"), dict)
        and "command" in request["json"]
    ]

    assert session.requests and all(
        request["method"] == "PUT" for request in session.requests
    )
    assert issued_commands
    assert all("GetMatchStatus" not in command for command in issued_commands)

    expected_commands = {
        CourtPhase.IDLE_NAMES: {
            "GetNamePlayerA": {"GetNamePlayerA"},
            "GetNamePlayerB": {"GetNamePlayerB"},
            "ProbeAvailability": {"GetOverlayVisibility"},
        },
        CourtPhase.PRE_START: {
            "GetPoints": {
                "GetOverlayVisibility",
                "GetMode",
                "GetServe",
                "GetPointsPlayerA",
                "GetPointsPlayerB",
            },
        },
        CourtPhase.LIVE_POINTS: {
            "GetPoints": {
                "GetOverlayVisibility",
                "GetMode",
                "GetServe",
                "GetPointsPlayerA",
                "GetPointsPlayerB",
            },
        },
        CourtPhase.LIVE_GAMES: {
            "GetGames": {"GetSet", "GetCurrentSetPlayerA", "GetCurrentSetPlayerB"},
            "ProbePoints": {"GetServe", "GetPointsPlayerA", "GetPointsPlayerB"},
        },
        CourtPhase.LIVE_SETS: {
            "GetSets": {"GetSet", "GetCurrentSetPlayerA", "GetCurrentSetPlayerB"},
            "ProbeGames": {"GetServe", "GetCurrentSetPlayerA", "GetCurrentSetPlayerB"},
        },
        CourtPhase.TIEBREAK7: {
            "GetPoints": {
                "GetOverlayVisibility",
                "GetTieBreakVisibility",
                "GetServe",
                "GetTieBreakPlayerA",
                "GetTieBreakPlayerB",
            },
        },
        CourtPhase.SUPER_TB10: {
            "GetPoints": {
                "GetOverlayVisibility",
                "GetTieBreakVisibility",
                "GetServe",
                "GetTieBreakPlayerA",
                "GetTieBreakPlayerB",
            },
        },
        CourtPhase.FINISHED: {
            "GetNamePlayerA": {"GetNamePlayerA"},
            "GetNamePlayerB": {"GetNamePlayerB"},
        },
    }

    assert phase_command_log
    for phase, spec_name, command in phase_command_log:
        expected_for_phase = expected_commands.get(phase)
        if not expected_for_phase or spec_name not in expected_for_phase:
            continue
        assert command in expected_for_phase[spec_name]

    tie_break_commands = [
        command
        for phase, spec_name, command in phase_command_log
        if phase in {CourtPhase.TIEBREAK7, CourtPhase.SUPER_TB10}
        and spec_name == "GetPoints"
    ]
    tie_break_player_commands = [
        command for command in tie_break_commands if command.startswith("GetTieBreakPlayer")
    ]
    assert tie_break_player_commands
    assert {"GetTieBreakPlayerA", "GetTieBreakPlayerB"}.issubset(
        set(tie_break_player_commands)
    )

    tb_phase_windows = [
        (90, 100),
        (100, 110),
    ]
    for start, end in tb_phase_windows:
        tb_points_times = [
            time
            for time, name in history
            if name == "GetPoints" and start <= time < end
        ]
        assert tb_points_times
        if len(tb_points_times) > 1:
            diffs = consecutive_diffs(tb_points_times)
            assert diffs
            trailing_diffs = diffs[1:] if diffs[0] != 1 else diffs
            assert trailing_diffs and all(diff == 1 for diff in trailing_diffs)



def test_update_once_skips_disabled_courts(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {
            "control": "https://app.overlays.uno/control/disabled",
            "enabled": False,
        }
    }

    def supplier():
        return overlay_links

    session = DummySession(DummyResponse({}, status_code=200))

    results_module._update_once(app, supplier, session=session, now=0.0)

    assert session.requests == []
    assert "1" not in results_module.court_states

def test_update_once_logs_successful_payload(monkeypatch, caplog):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {"1": {"control": "https://app.overlays.uno/control/logs"}}

    def supplier():
        return overlay_links

    success_payload = {
        "PlayerA": {"value": "Player One"},
        "PlayerB": {"value": "Player Two"},
        "PointsPlayerA": {"value": "15"},
        "PointsPlayerB": {"value": "30"},
        "authToken": "super-secret-token",
    }

    session = DummySession(DummyResponse(success_payload, status_code=200))

    fake_time = TimeController(start=10.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)

    caplog.set_level("DEBUG", logger=results_module.logger.name)

    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    success_logs = [
        record for record in caplog.records if "Odpowiedź komendy" in record.getMessage()
    ]
    assert success_logs, f"Brak logów sukcesu w caplog: {caplog.text}"
    message = success_logs[0].getMessage()
    assert "GetNamePlayer" in message
    assert "PointsPlayerA" in message
    assert "***" in message  # maskowanie wrażliwych danych
    assert "super-secret-token" not in message


def test_update_once_emits_json_logs_with_request_id(monkeypatch, caplog):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {"1": {"control": "https://app.overlays.uno/control/json"}}

    def supplier():
        return overlay_links

    failing_response = DummyResponse({}, status_code=500)
    success_response = DummyResponse({}, status_code=200)
    extra_response = DummyResponse({}, status_code=200)
    session = SequenceSession(
        [failing_response, success_response, extra_response, extra_response]
    )

    fake_time = TimeController(start=42.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    caplog.set_level("INFO", logger=results_module.logger.name)

    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    info_logs: list[dict[str, object]] = []
    for record in caplog.records:
        if record.name != results_module.logger.name or record.levelno != logging.INFO:
            continue
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        info_logs.append(payload)

    assert info_logs, f"Brak logów JSON w caplog: {caplog.text}"

    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in info_logs:
        request_id = entry.get("request_id")
        if not isinstance(request_id, str):
            continue
        grouped.setdefault(request_id, []).append(entry)

    matching_entries: list[dict[str, object]] | None = None
    for entries in grouped.values():
        if len(entries) < 2:
            continue
        statuses = {entry.get("status_code") for entry in entries}
        if 500 in statuses and 200 in statuses:
            matching_entries = sorted(entries, key=lambda item: (bool(item.get("retry")), item.get("status_code")))
            break

    assert matching_entries is not None, f"Brak kompletnego wpisu retry w logach: {info_logs}"

    first_attempt, second_attempt = matching_entries[:2]
    assert first_attempt.get("kort_id") == second_attempt.get("kort_id") == "1"
    assert first_attempt.get("status_code") == 500
    assert second_attempt.get("status_code") == 200
    assert not first_attempt.get("retry")
    assert second_attempt.get("retry")
    assert first_attempt.get("request_id") == second_attempt.get("request_id")
    assert first_attempt.get("duration_ms") is not None
    assert second_attempt.get("duration_ms") is not None


def test_update_once_respects_rate_limit_cooldown(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/test429"}
    }

    def supplier():
        return overlay_links

    retry_response = DummyResponse({"error": "too many"}, status_code=429)
    retry_response.headers["Retry-After"] = "2"
    retry_response.headers["X-RateLimit-Reset"] = "102"
    success_payload = {
        "PlayerA": {"value": "Player One"},
        "PlayerB": {"value": "Player Two"},
        "PointsPlayerA": {"value": "15"},
        "PointsPlayerB": {"value": "30"},
    }
    success_response = DummyResponse(success_payload, status_code=200)
    session = SequenceSession([retry_response, success_response])

    fake_time = TimeController(start=100.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 1
    cooldown = results_module._next_allowed_request_by_controlapp.get("test429")
    assert cooldown is not None
    assert cooldown == pytest.approx(102.0, rel=1e-6)
    assert fake_time.sleep_calls == []

    fake_time.current = 101.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 1

    fake_time.current = 103.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 2
    assert "test429" not in results_module._next_allowed_request_by_controlapp
    snapshot = snapshots["1"]
    assert snapshot["status"] == SNAPSHOT_STATUS_OK
    assert snapshot["players"]["A"]["points"] == "15"


def test_update_once_waits_until_cooldown_expires_before_retry(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/cooldown"}
    }

    def supplier():
        return overlay_links

    retry_response = DummyResponse({"error": "too many"}, status_code=429)
    retry_response.headers["Retry-After"] = "3"
    retry_response.headers["X-RateLimit-Reset"] = "83"
    success_payload = {
        "PlayerA": {"value": "Player One"},
        "PlayerB": {"value": "Player Two"},
        "PointsPlayerA": {"value": "15"},
        "PointsPlayerB": {"value": "30"},
    }
    success_response = DummyResponse(success_payload, status_code=200)
    session = SequenceSession([retry_response, success_response])

    fake_time = TimeController(start=80.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    original_throttle = results_module._throttle_request
    call_times: list[float] = []

    def simulated_throttle(controlapp_id: str, *, simulate: bool = False, current_time=None):
        call_times.append(fake_time.time())
        return original_throttle(
            controlapp_id,
            simulate=True,
            current_time=fake_time.time(),
        )

    monkeypatch.setattr(results_module, "_throttle_request", simulated_throttle)

    first_tick = fake_time.time()
    results_module._update_once(app, supplier, session=session, now=first_tick)

    assert len(session.requests) == 1
    assert call_times == [first_tick]

    cooldown = results_module._next_allowed_request_by_controlapp.get("cooldown")
    assert cooldown is not None

    fake_time.current = cooldown - 0.5
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 1
    assert call_times == [first_tick]

    fake_time.current = cooldown + 0.1
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 2
    assert call_times[-1] == pytest.approx(fake_time.current, rel=1e-6)


def test_update_once_applies_real_pause_after_delayed_429(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/first"},
        "2": {"control": "https://app.overlays.uno/control/second"},
    }

    def supplier():
        return overlay_links

    success_payload = {
        "PlayerA": {"value": "Player One"},
        "PlayerB": {"value": "Player Two"},
        "PointsPlayerA": {"value": "15"},
        "PointsPlayerB": {"value": "30"},
    }

    responses = [
        DummyResponse(success_payload, status_code=200),
        DummyResponse({"error": "too many"}, status_code=429),
        DummyResponse(success_payload, status_code=200),
        DummyResponse(success_payload, status_code=200),
        DummyResponse(success_payload, status_code=200),
    ]

    fake_time = TimeController(start=100.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    class RecordingSession(SequenceSession):
        def __init__(self, responses_list, clock):
            super().__init__(responses_list)
            self._clock = clock
            self.request_times: list[float] = []

        def put(self, url: str, timeout: int, json: dict | None = None):  # type: ignore[override]
            call_index = len(self.requests)
            self.request_times.append(self._clock.time())
            response = super().put(url, timeout=timeout, json=json)
            if call_index == 0:
                self._clock.current += 1.5
            return response

    session = RecordingSession(responses, fake_time)

    results_module._update_once(app, supplier, session=session)

    assert len(session.requests) == 2
    second_requests = [
        (req, ts)
        for req, ts in zip(session.requests, session.request_times)
        if "controlapps/second" in req["url"]
    ]
    assert len(second_requests) == 1
    first_second_time = second_requests[0][1]
    assert first_second_time == pytest.approx(101.5, rel=1e-6)

    cooldown = results_module._next_allowed_request_by_controlapp.get("second")
    assert cooldown is not None
    assert cooldown > first_second_time

    fake_time.current = first_second_time + 0.5
    results_module._update_once(app, supplier, session=session)

    second_requests = [
        (req, ts)
        for req, ts in zip(session.requests, session.request_times)
        if "controlapps/second" in req["url"]
    ]
    assert len(second_requests) == 1

    fake_time.current = first_second_time + 1.1
    results_module._update_once(app, supplier, session=session)

    second_requests = [
        (req, ts)
        for req, ts in zip(session.requests, session.request_times)
        if "controlapps/second" in req["url"]
    ]
    assert len(second_requests) == 2
    second_times = [ts for _, ts in second_requests]
    assert second_times[1] - second_times[0] > 1.0


def test_update_once_switches_polling_when_overlay_unavailable(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/slow"}
    }

    def supplier():
        return overlay_links

    unavailable_payload = {"OverlayVisibility": "off"}
    available_payload = {"OverlayVisibility": "on"}
    session = SequenceSession(
        [
            DummyResponse(unavailable_payload, status_code=200),
            DummyResponse(available_payload, status_code=200),
        ]
    )

    fake_time = TimeController(start=200.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    start = fake_time.time()
    results_module._update_once(app, supplier, session=session, now=start)

    state = results_module.court_states["1"]
    assert len(session.requests) == 1
    assert state.availability_paused_until is not None
    assert state.availability_paused_until == pytest.approx(
        start + results_module.UNAVAILABLE_SLOW_POLL_SECONDS, rel=1e-6
    )
    assert state.is_paused(start)

    fake_time.current = start + 10.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 1
    assert state.is_paused(fake_time.current)

    fake_time.current = start + results_module.UNAVAILABLE_SLOW_POLL_SECONDS + 1.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 2
    assert state.availability_paused_until is None
    assert not state.is_paused(fake_time.current)


def test_idle_names_overlay_probe_limits_request_frequency(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/idle-probe"}
    }

    def supplier():
        return overlay_links

    session = SequenceSession(
        [
            DummyResponse({"PlayerA": {"Value": ""}}, status_code=200),
            DummyResponse({"OverlayVisibility": "off"}, status_code=200),
            DummyResponse({"OverlayVisibility": "on"}, status_code=200),
        ]
    )

    fake_time = TimeController(start=0.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    first_tick = fake_time.time()
    results_module._update_once(
        app, supplier, session=session, now=first_tick
    )

    assert len(session.requests) == 1
    assert session.requests[0]["json"] == {"command": "GetNamePlayerA"}

    pause_start = fake_time.time()
    results_module._update_once(
        app, supplier, session=session, now=pause_start
    )

    assert len(session.requests) == 2
    assert session.requests[1]["json"] == {"command": "GetOverlayVisibility"}

    state = results_module.court_states["1"]
    assert state.availability_paused_until is not None
    expected_pause_base = fake_time.current
    assert state.availability_paused_until == pytest.approx(
        expected_pause_base + results_module.UNAVAILABLE_SLOW_POLL_SECONDS, rel=1e-6
    )
    assert state.is_paused(expected_pause_base)

    fake_time.current = expected_pause_base + 30.0
    results_module._update_once(
        app, supplier, session=session, now=fake_time.time()
    )

    assert len(session.requests) == 2
    assert state.is_paused(fake_time.time())

    fake_time.current = expected_pause_base + 59.5
    results_module._update_once(
        app, supplier, session=session, now=fake_time.time()
    )

    assert len(session.requests) == 2
    assert state.is_paused(fake_time.time())

    fake_time.current = expected_pause_base + results_module.UNAVAILABLE_SLOW_POLL_SECONDS
    results_module._update_once(
        app, supplier, session=session, now=fake_time.time()
    )

    assert len(session.requests) == 3
    assert session.requests[2]["json"]["command"] in {
        "GetNamePlayerA",
        "GetNamePlayerB",
    }
    assert state.availability_paused_until is None
    assert not state.is_paused(fake_time.time())


def test_update_once_logs_details_on_retry_exhaustion(monkeypatch, caplog):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/test500"}
    }

    def supplier():
        return overlay_links

    failing_response_1 = DummyResponse({"error": "server"}, status_code=500)
    failing_response_2 = DummyResponse({"error": "server"}, status_code=500)
    session = SequenceSession([failing_response_1, failing_response_2])

    fake_time = TimeController(start=150.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_module, "MAX_RETRY_ATTEMPTS", 1)

    caplog.set_level("WARNING", logger=results_module.logger.name)

    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    warning_messages = [
        record.getMessage()
        for record in caplog.records
        if "Wyczerpano próby" in record.getMessage()
    ]
    assert warning_messages, f"Brak logu wyczerpanych prób: {caplog.text}"
    message = warning_messages[-1]

    assert "kortu 1" in message
    assert "GetNamePlayerA" in message
    assert 'payload={"command": "GetNamePlayerA"}' in message
    assert "po 2 próbach" in message
    assert "HTTP 500" in message


def test_update_once_does_not_retry_on_400(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/test400"}
    }

    def supplier():
        return overlay_links

    error_response = DummyResponse({"error": "bad request"}, status_code=400)
    session = SequenceSession([error_response])

    fake_time = TimeController(start=50.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)

    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 1
    assert fake_time.sleep_calls == []
    snapshot = snapshots["1"]
    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert "HTTP 400" in snapshot["error"]


def test_update_once_handles_404_with_cooldown_and_badge(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {
        "1": {"control": "https://app.overlays.uno/control/notfound"}
    }

    def supplier():
        return overlay_links

    not_found_response = DummyResponse({"error": "missing"}, status_code=404)
    success_payload = {
        "PlayerA": {"value": "Player One"},
        "PlayerB": {"value": "Player Two"},
        "PointsPlayerA": {"value": "15"},
        "PointsPlayerB": {"value": "30"},
    }
    session = SequenceSession(
        [
            not_found_response,
            DummyResponse(success_payload, status_code=200),
        ]
    )

    fake_time = TimeController(start=500.0)
    monkeypatch.setattr(results_module.time, "time", fake_time.time)
    monkeypatch.setattr(results_module.time, "sleep", fake_time.sleep)
    monkeypatch.setattr(results_module.random, "uniform", lambda *_: 0.0)
    monkeypatch.setattr(results_state_machine, "_default_offset", lambda *_: 0.0)

    start = fake_time.time()
    results_module._update_once(app, supplier, session=session, now=start)

    assert len(session.requests) == 1
    cooldown = results_module._next_allowed_request_by_controlapp.get("notfound")
    assert cooldown is not None
    expected_cooldown = start + results_module.NOT_FOUND_COOLDOWN_SECONDS
    assert cooldown == pytest.approx(expected_cooldown, rel=1e-6)

    snapshot_after_404 = copy.deepcopy(snapshots["1"])
    assert any(
        badge.get("label") == "404" for badge in snapshot_after_404.get("badges", [])
    )

    normalized = normalize_snapshot_entry("1", snapshot_after_404, overlay_links["1"])
    assert any(badge["label"] == "404" for badge in normalized["badges"])

    fake_time.current = start + results_module.NOT_FOUND_COOLDOWN_SECONDS - 1.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())
    assert len(session.requests) == 1

    fake_time.current = start + results_module.NOT_FOUND_COOLDOWN_SECONDS + 1.0
    results_module._update_once(app, supplier, session=session, now=fake_time.time())

    assert len(session.requests) == 2
    assert "notfound" not in results_module._next_allowed_request_by_controlapp
    final_snapshot = snapshots["1"]
    assert final_snapshot.get("badges") == []
    assert fake_time.sleep_calls == []


def test_normalize_snapshot_entry_marks_disabled_status():
    snapshot = {
        "status": "ok",
        "available": True,
        "players": [],
    }
    link_meta = {"enabled": False, "hidden": True}

    normalized = normalize_snapshot_entry("7", snapshot, link_meta)

    assert normalized["status"] == "disabled"
    assert normalized["status_label"] == "Wyłączony"
    assert normalized["overlay_is_on"] is False
    assert normalized["enabled"] is False
    assert normalized["hidden"] is True


def test_sqlite_directory_is_created_for_custom_database_url(tmp_path):
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///tmp/test/overlay.db"

    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{project_root}{os.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = str(project_root)

    script = textwrap.dedent(
        """
        from main import app, db

        with app.app_context():
            db.create_all()
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    target_dir = project_root / "instance" / "tmp" / "test"
    try:
        assert target_dir.is_dir(), result.stderr
        assert (target_dir / "overlay.db").exists(), result.stderr
    finally:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        tmp_root = target_dir.parent
        if tmp_root.exists() and not any(tmp_root.iterdir()):
            tmp_root.rmdir()

