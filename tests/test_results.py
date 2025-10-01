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
from results_state_machine import CourtPhase, STATE_INTERVALS


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
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status: {self.status_code}")


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

def test_build_output_url_replaces_control_segment():
    url = "https://example.com/control/stream"
    assert build_output_url(url) == "https://example.com/output/stream"


def test_update_snapshot_for_kort_parses_players_and_serving():
    html = """
    <div data-singular-name="PlayerA" data-singular-value="Player One"></div>
    <div data-singular-name="PlayerB" data-singular-value="Player Two"></div>
    <div data-singular-name="PointsPlayerA" data-singular-value="15"></div>
    <div data-singular-name="PointsPlayerB" data-singular-value="30"></div>
    <div data-singular-name="Set1PlayerA" data-singular-value="6"></div>
    <div data-singular-name="Set1PlayerB" data-singular-value="4"></div>
    <div data-singular-name="ServePlayerA" data-singular-value="true"></div>
    <div data-singular-name="ServePlayerB" data-singular-value="false"></div>
    """
    response = DummyResponse(html)
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
    assert session.requested_urls[0][0] == "https://example.com/output/live"


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
    html = "<div data-singular-name='PlayerA' data-singular-value='Solo'></div>"
    response = DummyResponse(html)
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

    sequence = [
        {
            "kort_id": "1",
            "status": SNAPSHOT_STATUS_OK,
            "last_updated": "t0",
            "players": {
                "A": {"name": "Player One", "points": None, "sets": {}},
                "B": {"name": "Player Two", "points": None, "sets": {}},
            },
            "raw": {},
            "serving": None,
            "error": None,
        },
        {
            "kort_id": "1",
            "status": SNAPSHOT_STATUS_OK,
            "last_updated": "t1",
            "players": {
                "A": {"name": "Player One", "points": "", "sets": {"Set1PlayerA": "6"}},
                "B": {"name": "Player Two", "points": "", "sets": {"Set1PlayerB": "4"}},
            },
            "raw": {"ScoreMatchStatus": "Finished"},
            "serving": None,
            "error": None,
        },
        {
            "kort_id": "1",
            "status": SNAPSHOT_STATUS_OK,
            "last_updated": "t2",
            "players": {
                "A": {"name": "New Player", "points": None, "sets": {}},
                "B": {"name": "Player Two", "points": None, "sets": {}},
            },
            "raw": {"ScoreMatchStatus": "Finished"},
            "serving": None,
            "error": None,
        },
    ]

    call_log = []

    def fake_update(kort_id, control_url, session=None):
        call_log.append((kort_id, control_url))
        snapshot = copy.deepcopy(sequence.pop(0))
        entry = results_module.ensure_snapshot_entry(kort_id)
        with results_module.snapshots_lock:
            archive = entry.get("archive", [])
            entry.update(snapshot)
            entry["archive"] = archive
            stored = copy.deepcopy(entry)
        return stored

    monkeypatch.setattr(results_module, "update_snapshot_for_kort", fake_update)

    results_module._update_once(app, supplier, now=0.0)

    state = results_module.court_states["1"]
    assert state.phase == CourtPhase.PRE_START
    assert pytest.approx(state.next_poll, rel=1e-6) == STATE_INTERVALS[CourtPhase.PRE_START]
    assert len(call_log) == 1

    results_module._update_once(app, supplier, now=state.next_poll)
    state = results_module.court_states["1"]
    assert state.phase == CourtPhase.FINISHED
    archive = snapshots["1"].get("archive")
    assert archive and len(archive) == 1
    expected_next = state.last_polled + STATE_INTERVALS[CourtPhase.FINISHED] + state.phase_offset
    assert pytest.approx(state.next_poll, rel=1e-6) == expected_next
    assert len(call_log) == 2

    results_module._update_once(app, supplier, now=expected_next - 1)
    assert len(call_log) == 2

    results_module._update_once(app, supplier, now=expected_next)
    state = results_module.court_states["1"]
    assert state.phase == CourtPhase.IDLE_NAMES
    assert state.next_poll == expected_next
    assert len(call_log) == 3
    assert snapshots["1"]["players"]["A"]["name"] == "New Player"
    assert len(snapshots["1"].get("archive") or []) == 1
    assert not sequence
