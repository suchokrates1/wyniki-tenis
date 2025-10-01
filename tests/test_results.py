import copy
import json
import pytest
import requests
from requests import RequestException

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
    assert snapshot["archive"] == []
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


def test_finished_state_archiving_and_reset(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {"1": {"control": "https://example.com/control/live"}}

    def supplier():
        return overlay_links

    idle_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "idle",
        "players": {
            "A": {"name": "Player One", "points": None, "sets": {}},
            "B": {"name": "Player Two", "points": None, "sets": {}},
        },
        "raw": {"ScoreMatchStatus": "Idle"},
        "serving": None,
        "error": None,
    }
    live_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "live",
        "players": {
            "A": {
                "name": "Player One",
                "points": "",
                "sets": {"Set1PlayerA": "6"},
            },
            "B": {
                "name": "Player Two",
                "points": "",
                "sets": {"Set1PlayerB": "4"},
            },
        },
        "raw": {"ScoreMatchStatus": "Live"},
        "serving": None,
        "error": None,
    }
    finished_snapshot = {
        "kort_id": "1",
        "status": SNAPSHOT_STATUS_OK,
        "last_updated": "fin",
        "players": {
            "A": {"name": "Player One", "points": "", "sets": {"Set1PlayerA": "6"}},
            "B": {"name": "Player Two", "points": "", "sets": {"Set1PlayerB": "4"}},
        },
        "raw": {"ScoreMatchStatus": "Finished"},
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
        "raw": {"ScoreMatchStatus": "Finished"},
        "serving": None,
        "error": None,
    }

    current_time = {"value": 0.0}

    def fake_update(kort_id, control_url, session=None):
        tick = current_time["value"]
        if tick < 30:
            template = idle_snapshot
        elif tick < 60:
            template = live_snapshot
        elif tick < 120:
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

    monkeypatch.setattr(results_module, "update_snapshot_for_kort", fake_update)

    phase_log = []
    total_ticks = 140
    for tick in range(total_ticks):
        current_time["value"] = float(tick)
        results_module._update_once(app, supplier, now=current_time["value"])
        state = results_module.court_states["1"]
        phase_log.append((tick, state.phase, state.name_stability))

    state = results_module.court_states["1"]
    archive = snapshots["1"].get("archive")
    assert archive and len(archive) >= 1
    assert archive[0]["players"]["A"]["name"] == "Player One"
    assert snapshots["1"]["players"]["A"]["name"] == "New Player"
    assert any(phase == CourtPhase.IDLE_NAMES for tick, phase, _ in phase_log if tick >= 120)
    assert state.phase == CourtPhase.FINISHED

    first_non_idle_tick = next(
        (tick for tick, phase, _ in phase_log if phase is not CourtPhase.IDLE_NAMES),
        None,
    )
    assert first_non_idle_tick is None or first_non_idle_tick >= 11
    assert any(phase == CourtPhase.PRE_START for tick, phase, _ in phase_log if 12 <= tick < 30)
    assert any(phase == CourtPhase.LIVE_GAMES for tick, phase, _ in phase_log if 30 <= tick < 60)
    assert any(phase == CourtPhase.FINISHED for tick, phase, _ in phase_log if 60 <= tick < 120)
    idle_stability = [stability for tick, phase, stability in phase_log if tick < 30]
    assert idle_stability and max(idle_stability) >= 12

    history = state.command_history
    early_names = [name for time, name in history if time < 6]
    assert early_names == [
        "GetNamePlayerA",
        "GetNamePlayerB",
        "GetNamePlayerA",
        "GetNamePlayerB",
        "GetNamePlayerA",
        "GetNamePlayerB",
    ]

    def consecutive_diffs(times):
        return [round(b - a, 6) for a, b in zip(times, times[1:])]

    points_times = [time for time, name in history if name == "GetPoints" and 12 <= time < 30]
    assert points_times
    assert all(diff == 2 for diff in consecutive_diffs(points_times))

    games_times = [time for time, name in history if name == "GetGames" and 30 <= time < 60]
    assert games_times
    assert all(diff in {4, 5} for diff in consecutive_diffs(games_times))

    probe_points_times = [
        time for time, name in history if name == "ProbePoints" and 30 <= time < 60
    ]
    assert probe_points_times
    assert all(diff in {6} for diff in consecutive_diffs(probe_points_times))

    finished_a_times = [
        time for time, name in history if name == "GetNamePlayerA" and 60 <= time < 120
    ]
    finished_b_times = [
        time for time, name in history if name == "GetNamePlayerB" and 60 <= time < 120
    ]
    assert finished_a_times and finished_b_times
    assert all(diff == 30 for diff in consecutive_diffs(finished_a_times))
    assert all(diff == 30 for diff in consecutive_diffs(finished_b_times))

    for b_time in finished_b_times:
        preceding_a = max(a for a in finished_a_times if a <= b_time)
        assert b_time - preceding_a == 15

    assert phase_log[-1][2] >= 1


def test_update_snapshot_marks_court_unavailable_on_json_decode_error(caplog):
    response = DummyResponse(None, json_error=ValueError("invalid json"))
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "4", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "kortu 4" in caplog.text
