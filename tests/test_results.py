import json
import pytest
import requests
from requests import RequestException

from main import app
from results import (
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_UNAVAILABLE,
    build_output_url,
    snapshots,
    update_snapshot_for_kort,
)


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


def test_update_snapshot_marks_court_unavailable_on_json_decode_error(caplog):
    response = DummyResponse(None, json_error=ValueError("invalid json"))
    session = DummySession(response)

    snapshot = update_snapshot_for_kort(
        "4", "https://example.com/control/live", session=session
    )

    assert snapshot["status"] == SNAPSHOT_STATUS_UNAVAILABLE
    assert snapshot["error"]
    assert "kortu 4" in caplog.text
