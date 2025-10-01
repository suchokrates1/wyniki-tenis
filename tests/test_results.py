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


class CommandSession:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.requested_urls: list[str] = []
        self.requested_commands: list[str] = []

    def get(self, url: str, timeout: int):
        self.requested_urls.append(url)
        parsed = urlparse(url)
        command = parse_qs(parsed.query).get("command", [None])[0]
        if command is None:
            raise AssertionError("command parameter required")
        self.requested_commands.append(command)
        payload = self._responses.get(command)
        if payload is None:
            raise AssertionError(f"unexpected command {command}")
        return DummyResponse(payload)


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


def test_update_once_cycles_commands_and_transitions(monkeypatch):
    snapshots.clear()
    results_module.court_states.clear()

    overlay_links = {"1": {"control": "https://app.overlays.uno/control/abc123"}}

    def supplier():
        return overlay_links

    responses = {
        "GetPlayerNameA": {"PlayerA": {"Value": "Anna"}},
        "GetPlayerNameB": {"PlayerB": {"Value": "Bella"}},
        "GetMatchStatus": {"MatchStatus": {"Value": "live"}},
        "GetPointsPlayerA": {"PointsPlayerA": {"Value": "15"}},
        "GetPointsPlayerB": {"PointsPlayerB": {"Value": "30"}},
        "GetServePlayerA": {"ServePlayerA": {"Value": "true"}},
        "GetServePlayerB": {"ServePlayerB": {"Value": "false"}},
    }

    session = CommandSession(responses)

    expected_sequence = [
        "GetPlayerNameA",
        "GetPlayerNameB",
        "GetMatchStatus",
        "GetPointsPlayerA",
        "GetPointsPlayerB",
        "GetServePlayerA",
        "GetServePlayerB",
    ]

    now = 0.0
    for expected_command in expected_sequence:
        results_module._update_once(app, supplier, session=session, now=now)
        assert session.requested_commands[-1] == expected_command
        state = results_module.court_states["1"]
        now = state.next_poll

    snapshot = copy.deepcopy(snapshots["1"])
    assert snapshot["status"] == SNAPSHOT_STATUS_OK
    assert snapshot["players"]["A"]["name"] == "Anna"
    assert snapshot["players"]["B"]["points"] == "30"
    assert snapshot["serving"] == "A"
    assert snapshot["raw"]["PointsPlayerA"] == "15"

    state = results_module.court_states["1"]
    assert state.phase == CourtPhase.LIVE_POINTS

    responses["GetMatchStatus"] = {"ScoreMatchStatus": {"Value": "Finished"}}

    results_module._update_once(app, supplier, session=session, now=state.next_poll)
    assert session.requested_commands[-1] == "GetMatchStatus"

    state = results_module.court_states["1"]
    assert state.phase == CourtPhase.FINISHED
    archive = snapshots["1"].get("archive") or []
    assert len(archive) == 1


def test_update_snapshot_marks_court_unavailable_on_json_decode_error(caplog):
    response = DummyResponse(None, json_error=ValueError("invalid json"))
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "4", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "kortu 4" in caplog.text
