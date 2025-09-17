import pytest
from flask import render_template

from main import app as flask_app


@pytest.mark.parametrize("kort_id", ["1", "2"])
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


def test_overlay_kort_not_found(client):
    response = client.get("/kort/999")
    assert response.status_code == 404
    assert "Nieznany kort" in response.get_data(as_text=True)


def test_config_page_renders(client):
    response = client.get("/config")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Konfiguracja Overlay" in html
    assert "UndefinedError" not in html


def test_kort_template_with_empty_context():
    with flask_app.app_context():
        render_template("kort.html")
