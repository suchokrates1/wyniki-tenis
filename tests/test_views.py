import pytest
from flask import render_template

from main import app as flask_app


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
        "overlay": "https://example.com/overlay",
        "control": "https://example.com/control",
    }
    response = client.post("/api/overlay-links", json=payload)
    assert response.status_code == 201
    created = response.get_json()
    assert created["kort_id"] == payload["kort_id"]

    list_response = client.get("/api/overlay-links")
    assert list_response.status_code == 200
    links = list_response.get_json()
    assert any(link["kort_id"] == payload["kort_id"] for link in links)


def test_index_renders_links_from_database(client):
    new_link = {
        "kort_id": "77",
        "overlay": "https://example.com/new-overlay",
        "control": "https://example.com/new-control",
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
        "overlay": "https://example.com/overlay-55",
        "control": "https://example.com/control-55",
    }
    create_response = client.post("/api/overlay-links", json=new_link)
    assert create_response.status_code == 201

    response = client.get("/kort/55")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert new_link["overlay"] in html


def test_overlay_links_page_renders(client):
    response = client.get("/overlay-links")
    assert response.status_code == 200
    assert "Linki do overlayów" in response.get_data(as_text=True)
