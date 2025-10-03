import json
from datetime import timedelta

import pytest
from flask import render_template

import requests

import main
import results
from main import app as flask_app, OverlayLink


@pytest.mark.parametrize("kort_id", [1, 2])
def test_overlay_kort_existing(client, kort_id):
    response = client.get(f"/kort/{kort_id}")
    assert response.status_code == 200
    html = response.data.decode()
    assert '<iframe class="overlay-main"' in html
    assert "Kort" in html
    assert "top-strip" in html


def test_overlay_all_view(client):
    response = client.get("/kort/all")
    assert response.status_code == 200
    html = response.data.decode()
    assert "class=\"stage\"" in html or "class=\"stage\"".replace('"', '&quot;') in html
    assert html.count("kort-frame") >= 1
    assert "Kort 1" in html and "Kort 4" in html
    assert "Overlay:" in html
    assert "Ostatnia aktualizacja" in html


def test_overlay_all_route_registered():
    rules = [rule.rule for rule in flask_app.url_map.iter_rules("overlay_all")]
    assert "/kort/all" in rules


def test_overlay_kort_not_found(client):
    response = client.get("/kort/999")
    assert response.status_code == 404
    assert "Nieznany kort" in response.get_data(as_text=True)


def test_overlay_all_and_non_numeric_kort(client):
    all_response = client.get("/kort/all")
    assert all_response.status_code == 200

    non_numeric_response = client.get("/kort/not-a-number")
    assert non_numeric_response.status_code == 404


def test_wyniki_view_localizes_last_updated(client, tmp_path, monkeypatch):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()

    snapshot_payload = {
        "snapshots": [
            {
                "kort_id": "1",
                "status": "ok",
                "available": True,
                "players": [],
                "last_updated": "2024-07-01T14:32:00+00:00",
            }
        ]
    }
    (snapshot_dir / "sample.json").write_text(json.dumps(snapshot_payload))

    monkeypatch.setitem(main.app.config, "SNAPSHOTS_DIR", snapshot_dir)
    monkeypatch.setattr(main, "overlay_links_by_kort_id", lambda: {})

    response = client.get("/wyniki")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "Ostatnia aktualizacja: 16:32 CEST" in html
    assert 'datetime="2024-07-01T14:32:00Z"' in html
    assert 'title="2024-07-01T14:32:00Z"' in html


def test_wyniki_hides_hidden_and_marks_disabled(client):
    with flask_app.app_context():
        main.ensure_overlay_links_seeded()
        link1 = OverlayLink.query.filter_by(kort_id="1").first()
        link2 = OverlayLink.query.filter_by(kort_id="2").first()
        assert link1 is not None and link2 is not None
        link1.enabled = False
        link1.hidden = False
        link2.hidden = True
        main.db.session.commit()

    response = client.get("/wyniki")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "status-disabled" in html
    assert "Wyłączony" in html
    assert "Kort 2" not in html


def test_config_page_renders(client, auth_headers):
    response = client.get("/config", headers=auth_headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Konfiguracja Overlay" in html
    assert "UndefinedError" not in html


def test_kort_template_with_empty_context():
    with flask_app.app_context():
        render_template("kort.html")


def test_overlay_links_api_create_and_list(client):
    payload = {
        "kort_id": "99",
        "overlay": "https://app.overlays.uno/output/test99",
        "control": "https://app.overlays.uno/control/test99",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 201
    created = response.get_json()
    assert created["kort_id"] == payload["kort_id"]
    assert created["enabled"] is True
    assert created["hidden"] is False

    list_response = client.get("/api/overlay-links")
    assert list_response.status_code == 200
    links = list_response.get_json()
    assert any(link["kort_id"] == payload["kort_id"] and link["enabled"] is True for link in links)


def test_overlay_links_api_accepts_flags(client):
    payload = {
        "kort_id": "104",
        "overlay": "https://app.overlays.uno/output/test104",
        "control": "https://app.overlays.uno/control/test104",
        "enabled": False,
        "hidden": True,
    }

    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 201
    created = response.get_json()
    assert created["enabled"] is False
    assert created["hidden"] is True

    list_response = client.get("/api/overlay-links")
    flags = next(link for link in list_response.get_json() if link["kort_id"] == payload["kort_id"])
    assert flags["enabled"] is False
    assert flags["hidden"] is True


def test_overlay_links_api_rejects_invalid_scheme(client):
    payload = {
        "kort_id": "100",
        "overlay": "http://app.overlays.uno/output/test100",
        "control": "https://app.overlays.uno/control/test100",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert errors["overlay"] == "Adres overlayu musi używać protokołu HTTPS."


def test_overlay_links_api_rejects_invalid_host(client):
    payload = {
        "kort_id": "101",
        "overlay": "https://example.com/output/test101",
        "control": "https://app.overlays.uno/control/test101",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert errors["overlay"] == "Adres overlayu musi wskazywać na app.overlays.uno."


def test_overlay_links_api_rejects_invalid_path(client):
    payload = {
        "kort_id": "102",
        "overlay": "https://app.overlays.uno/not-output/test102",
        "control": "https://app.overlays.uno/control/test102",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert errors["overlay"] == "Adres overlayu musi mieć ścieżkę w formacie /output/{id}."


def test_overlay_links_api_rejects_invalid_control_path(client):
    payload = {
        "kort_id": "103",
        "overlay": "https://app.overlays.uno/output/test103",
        "control": "https://app.overlays.uno/not-control/test103",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert (
        errors["control"]
        == "Adres panelu sterowania musi mieć ścieżkę w formacie /control/{id} lub /controlapps/{id}."
    )


def test_overlay_links_api_rejects_invalid_enabled_flag(client):
    payload = {
        "kort_id": "105",
        "overlay": "https://app.overlays.uno/output/test105",
        "control": "https://app.overlays.uno/control/test105",
        "enabled": "invalid",
    }

    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert errors["enabled"] == "Pole enabled musi być wartością logiczną (true/false)."


def test_index_renders_links_from_database(client):
    new_link = {
        "kort_id": "77",
        "overlay": "https://app.overlays.uno/output/test77",
        "control": "https://app.overlays.uno/controlapps/test77",
    }
    post_response = client.post("/api/overlay-links", json=new_link)
    assert post_response.status_code == 201

    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Kort 77" in html
    assert new_link["control"] in html


def test_overlay_kort_uses_new_link(client):
    new_link = {
        "kort_id": "55",
        "overlay": "https://app.overlays.uno/output/test55",
        "control": "https://app.overlays.uno/control/test55",
    }
    create_response = client.post("/api/overlay-links", json=new_link)
    assert create_response.status_code == 201

    response = client.get("/kort/55")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert new_link["overlay"] in html
    assert "Overlay:" in html


def test_kort_page_includes_control_test_button(client):
    kort_id = "60"
    new_link = {
        "kort_id": kort_id,
        "overlay": f"https://app.overlays.uno/output/test{kort_id}",
        "control": f"https://app.overlays.uno/control/test{kort_id}",
    }
    create_response = client.post("/api/overlay-links", json=new_link)
    assert create_response.status_code == 201

    response = client.get(f"/kort/{kort_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f"hx-post=\"/kort/{kort_id}/test\"" in html
    assert "id=\"control-test-result\"" in html
    assert "Sprawdź panel sterowania" in html


def test_kort_control_test_success(client, monkeypatch):
    kort_id = "61"
    new_link = {
        "kort_id": kort_id,
        "overlay": f"https://app.overlays.uno/output/test{kort_id}",
        "control": f"https://app.overlays.uno/controlapps/test{kort_id}",
    }
    create_response = client.post("/api/overlay-links", json=new_link)
    assert create_response.status_code == 201

    class DummyResponse:
        def __init__(self):
            self.status_code = 200
            self.elapsed = timedelta(milliseconds=120)
            self._json = {
                "status": "ok",
                "overlay": "on",
                "extra": {"visibility": "visible"},
            }
            self.url = "https://app.overlays.uno/apiv2/controlapps/test/api"

        def json(self):
            return self._json

        @property
        def text(self):
            return json.dumps(self._json)

        @property
        def ok(self):
            return True

        def raise_for_status(self):
            return None

    called = {}

    def fake_put(url, json=None, timeout=None):
        called["url"] = url
        called["json"] = json
        called["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setattr(main.requests, "put", fake_put)

    response = client.post(f"/kort/{kort_id}/test")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "HTTP status:" in body
    assert "200" in body
    assert "Czas odpowiedzi" in body
    assert '&#34;status&#34;: &#34;ok&#34;' in body
    assert called["json"] == {"command": main.CONTROL_TEST_COMMAND}
    assert called["timeout"] == main.CONTROL_TEST_TIMEOUT_SECONDS


def test_kort_control_test_handles_request_error(client, monkeypatch):
    kort_id = "62"
    new_link = {
        "kort_id": kort_id,
        "overlay": f"https://app.overlays.uno/output/test{kort_id}",
        "control": f"https://app.overlays.uno/controlapps/test{kort_id}",
    }
    create_response = client.post("/api/overlay-links", json=new_link)
    assert create_response.status_code == 201

    def fake_put(url, json=None, timeout=None):  # noqa: ARG001
        raise requests.Timeout("timeout during test")

    monkeypatch.setattr(main.requests, "put", fake_put)

    response = client.post(f"/kort/{kort_id}/test")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "HTTP status:" in body
    assert "brak" in body
    assert "Błąd:" in body
    assert "timeout" in body.lower()


def test_overlay_links_page_renders(client):
    response = client.get("/overlay-links")
    assert response.status_code == 200
    assert "Linki do overlayów" in response.get_data(as_text=True)


def test_overlay_links_reload_updates_database(client, auth_headers, tmp_path, monkeypatch):
    json_path = tmp_path / "overlay_links.json"
    initial_data = {
        "1": {
            "overlay": "https://app.overlays.uno/output/test-initial-1",
            "control": "https://app.overlays.uno/control/test-initial-1",
            "enabled": False,
            "hidden": True,
        },
        "2": {
            "overlay": "https://app.overlays.uno/output/test-initial-2",
            "control": "https://app.overlays.uno/control/test-initial-2",
            "enabled": True,
            "hidden": False,
        },
    }
    json_path.write_text(json.dumps(initial_data))
    monkeypatch.setattr(main, "LINKS_PATH", str(json_path))

    first_response = client.post("/api/overlay-links/reload", headers=auth_headers)
    assert first_response.status_code == 200
    first_payload = first_response.get_json()
    assert first_payload == {"created": 2, "updated": 0, "removed": 0}

    updated_data = {
        "2": {
            "overlay": "https://app.overlays.uno/output/test-updated-2",
            "control": "https://app.overlays.uno/control/test-updated-2",
            "enabled": False,
            "hidden": True,
        },
        "3": {
            "overlay": "https://app.overlays.uno/output/test-new-3",
            "control": "https://app.overlays.uno/controlapps/test-new-3",
            "enabled": True,
            "hidden": False,
        },
    }
    json_path.write_text(json.dumps(updated_data))

    second_response = client.post("/api/overlay-links/reload", headers=auth_headers)
    assert second_response.status_code == 200
    second_payload = second_response.get_json()
    assert second_payload == {"created": 1, "updated": 1, "removed": 1}

    with flask_app.app_context():
        links = {link.kort_id: link for link in OverlayLink.query.all()}

    assert set(links.keys()) == {"2", "3"}
    assert links["2"].overlay_url == updated_data["2"]["overlay"]
    assert links["2"].control_url == updated_data["2"]["control"]
    assert links["2"].enabled is False
    assert links["2"].hidden is True
    assert links["3"].overlay_url == updated_data["3"]["overlay"]
    assert links["3"].control_url == updated_data["3"]["control"]
    assert links["3"].enabled is True
    assert links["3"].hidden is False


def test_debug_metrics_endpoint_counts(client):
    results.reset_metrics()

    first_response = client.get("/debug/metrics")
    assert first_response.status_code == 200
    first_payload = first_response.get_json()

    assert set(first_payload.keys()) >= {
        "started_at",
        "ticks_total",
        "responses",
        "retries",
        "snapshots",
    }
    assert isinstance(first_payload["responses"], dict)
    assert isinstance(first_payload["responses"]["by_status_code"], dict)
    assert isinstance(first_payload["responses"]["by_error"], dict)

    results._record_tick()
    results._record_response_event(status_code=200)
    results._record_retry_event("Timeout")
    results._record_snapshot_metrics({"status": results.SNAPSHOT_STATUS_OK})

    second_response = client.get("/debug/metrics")
    assert second_response.status_code == 200
    second_payload = second_response.get_json()

    assert second_payload["ticks_total"] == first_payload["ticks_total"] + 1
    assert second_payload["responses"]["total"] == first_payload["responses"]["total"] + 1
    assert second_payload["responses"]["by_status_code"].get("200") == (
        first_payload["responses"]["by_status_code"].get("200", 0) + 1
    )
    assert second_payload["retries"]["total"] == first_payload["retries"]["total"] + 1
    assert second_payload["snapshots"]["total"] == first_payload["snapshots"]["total"] + 1

def test_overlay_links_reload_requires_authentication(client):
    response = client.post("/api/overlay-links/reload")
    assert response.status_code == 401
