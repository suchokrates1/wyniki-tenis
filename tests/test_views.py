import json

import pytest
from flask import render_template

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

    list_response = client.get("/api/overlay-links")
    assert list_response.status_code == 200
    links = list_response.get_json()
    assert any(link["kort_id"] == payload["kort_id"] for link in links)


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
        },
        "2": {
            "overlay": "https://app.overlays.uno/output/test-initial-2",
            "control": "https://app.overlays.uno/control/test-initial-2",
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
        },
        "3": {
            "overlay": "https://app.overlays.uno/output/test-new-3",
            "control": "https://app.overlays.uno/controlapps/test-new-3",
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
    assert links["3"].overlay_url == updated_data["3"]["overlay"]
    assert links["3"].control_url == updated_data["3"]["control"]


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
