import copy
import json
import pytest
import requests
from requests import RequestException
from urllib.parse import parse_qs, urlparse

from main import app
import results as results_module
from results import (
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


# --- Testy widoku /wyniki -----------------------------------------------------

def test_results_page_renders_data(client, snapshots_dir):
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
    (snapshots_dir / "latest.json").write_text(json.dumps(sample_data), encoding="utf-8")

    response = client.get("/wyniki")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<table" in html
    assert 'aria-live="polite"' in html
    assert "Kort Centralny" in html
    assert "▶" in html
    assert "brak danych" in html.lower()


def test_results_page_shows_placeholder_for_finished_section(client, snapshots_dir):
    response = client.get("/wyniki")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Brak zakończonych meczów do wyświetlenia." in html
    assert "Aktualne spotkania i status kortów" in html


# --- Pomocnicze klasy do testów parsera --------------------------------------

class DummyResponse:
    def __init__(self, payload, status_code: int = 200, json_error: Exception | None = None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status: {self.status_code}")

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class DummySession:
    def __init__(self, response: DummyResponse):
        self._response = response
        self.requested_urls = []

    def get(self, url: str, timeout: int):
        self.requested_urls.append((url, timeout))
        return self._response


class FailingSession:
    def __init__(self, exc: Exception):
        self._exc = exc

    def get(self, url: str, timeout: int):
        raise self._exc


# --- Testy logiki parsera/snapshotów -----------------------------------------

def test_build_output_url_extracts_identifier():
    url = "https://app.overlays.uno/control/abc123"
    assert (
        build_output_url(url)
        == "https://app.overlays.uno/apiv2/controlapps/abc123/api"
    )


def test_update_snapshot_for_kort_parses_players_and_serving():
    payload = {
        "data": {
            "PlayerA": {"value": "Player One"},
            "PlayerB": {"value": "Player Two"},
            "PointsPlayerA": {"value": "15"},
            "PointsPlayerB": {"value": "30"},
            "Set1PlayerA": {"value": "6"},
            "Set1PlayerB": {"value": "4"},
            "ServePlayerA": {"value": "true"},
            "ServePlayerB": {"value": "false"},
        }
    }
    response = DummyResponse(payload)
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "1", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_OK
    assert snapshot["players"]["A"]["name"] == "Player One"
    assert snapshot["players"]["B"]["points"] == "30"
    assert snapshot["players"]["A"]["sets"] == {"Set1PlayerA": "6"}
    assert snapshot["players"]["A"]["is_serving"] is True
    assert snapshot["players"]["B"]["is_serving"] is False
    assert snapshot.get("archive") == []
    assert snapshots["1"] == snapshot
    assert (
        session.requested_urls[0][0]
        == "https://app.overlays.uno/apiv2/controlapps/live/api"
    )


def test_update_snapshot_marks_court_unavailable_on_network_error(caplog):
    session = FailingSession(RequestException("boom"))

    snapshot = update_snapshot_for_kort(
        "2", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "boom" in snapshot["error"]
    assert "kortu 2" in caplog.text


def test_update_snapshot_marks_court_unavailable_on_parse_error(caplog):
    payload = {"PlayerA": "Solo"}
    response = DummyResponse(payload)
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "3", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "kortu 3" in caplog.text


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
    early_names = [name for time, name in history if time < 6]
    assert early_names[:4] == [
        "GetNamePlayerA",
        "GetNamePlayerB",
        "GetNamePlayerA",
        "GetNamePlayerB",
    ]
    assert set(early_names).issubset({"GetNamePlayerA", "GetNamePlayerB"})

    def consecutive_diffs(times):
        return [round(b - a, 6) for a, b in zip(times, times[1:])]

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
        parse_qs(urlparse(url).query).get("command", [""])[0]
        for url, _ in session.requested_urls
    ]

    assert issued_commands and all("command=" in url for url, _ in session.requested_urls)
    assert all("GetMatchStatus" not in command for command in issued_commands)

    expected_commands = {
        CourtPhase.IDLE_NAMES: {
            "GetNamePlayerA": {"GetPlayerNameA"},
            "GetNamePlayerB": {"GetPlayerNameB"},
        },
        CourtPhase.PRE_START: {
            "GetPoints": {"GetPointsPlayerA", "GetPointsPlayerB"},
        },
        CourtPhase.LIVE_POINTS: {
            "GetPoints": {"GetPointsPlayerA", "GetPointsPlayerB"},
        },
        CourtPhase.LIVE_GAMES: {
            "GetGames": {"GetCurrentGamePlayerA", "GetCurrentGamePlayerB"},
            "ProbePoints": {"GetPointsPlayerA", "GetPointsPlayerB"},
        },
        CourtPhase.LIVE_SETS: {
            "GetSets": {"GetSetsPlayerA", "GetSetsPlayerB"},
            "ProbeGames": {"GetCurrentGamePlayerA", "GetCurrentGamePlayerB"},
        },
        CourtPhase.TIEBREAK7: {
            "GetPoints": {"GetTieBreakPlayerA", "GetTieBreakPlayerB"},
        },
        CourtPhase.SUPER_TB10: {
            "GetPoints": {"GetTieBreakPlayerA", "GetTieBreakPlayerB"},
        },
        CourtPhase.FINISHED: {
            "GetNamePlayerA": {"GetPlayerNameA"},
            "GetNamePlayerB": {"GetPlayerNameB"},
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
    assert tie_break_commands and all(
        command.startswith("GetTieBreakPlayer") for command in tie_break_commands
    )


def test_update_snapshot_marks_court_unavailable_on_json_decode_error(caplog):
    response = DummyResponse(None, json_error=ValueError("invalid json"))
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "4", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "kortu 4" in caplog.text
