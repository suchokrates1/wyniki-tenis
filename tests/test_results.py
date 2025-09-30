import requests
from requests import RequestException

from results import (
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_UNAVAILABLE,
    build_output_url,
    snapshots,
    update_snapshot_for_kort,
)


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


def setup_function(function):
    snapshots.clear()


def test_build_output_url_replaces_control_segment():
    url = "https://example.com/control/stream"
    assert (
        build_output_url(url)
        == "https://example.com/output/stream"
    )


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
