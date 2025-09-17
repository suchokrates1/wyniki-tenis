import json
from pathlib import Path

import pytest

from main import (
    CONFIG_PATH,
    CORNERS,
    CORNER_LABELS,
    CORNER_POSITION_STYLES,
    OVERLAY_LINKS,
    app,
    as_float,
    get_corner_overlay_mapping,
    load_config,
    save_config,
)


@pytest.mark.parametrize(
    "value, default, expected",
    [
        ("1.25", 0.0, 1.25),
        ("1,25", 0.0, 1.25),
        (" 2,5 ", 0.0, 2.5),
        ("", 0.0, 0.0),
        (None, 0.0, 0.0),
        ("not-a-number", 1.0, 1.0),
    ],
)
def test_as_float_handles_decimal_separators(value, default, expected):
    assert as_float(value, default) == expected


def test_get_config_renders_form_and_preview(client):
    response = client.get("/config")

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "Konfiguracja Overlay" in html
    assert 'name="view_width"' in html
    assert 'name="kort_all[top_left][view_width]"' in html
    assert 'id="preview-stage"' in html


def test_post_config_updates_overlay_file(client):
    payload = {
        "view_width": "720",
        "view_height": "180",
        "display_scale": "1,25",
        "left_offset": "15",
        "label_position": "bottom-right",
        "kort_all[top_left][view_width]": "800",
        "kort_all[top_left][view_height]": "200",
        "kort_all[top_left][display_scale]": "1,05",
        "kort_all[top_left][offset_x]": "45",
        "kort_all[top_left][offset_y]": "6",
        "kort_all[top_left][label][position]": "bottom-center",
        "kort_all[top_left][label][offset_x]": "12",
        "kort_all[top_left][label][offset_y]": "18",
        "kort_all[bottom_right][view_width]": "640",
        "kort_all[bottom_right][display_scale]": "0,75",
        "kort_all[bottom_right][offset_x]": "-12",
        "kort_all[bottom_right][label][position]": "top-right",
    }

    response = client.post("/config", data=payload, follow_redirects=True)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'value="720"' in html
    assert 'option value="bottom-right" selected' in html
    assert 'value="1.25"' in html

    config_path = Path(CONFIG_PATH)
    assert config_path.exists()
    written = json.loads(config_path.read_text())

    assert written["view_width"] == 720
    assert written["view_height"] == 180
    assert written["display_scale"] == pytest.approx(1.25)
    assert written["left_offset"] == 15
    assert written["label_position"] == "bottom-right"

    top_left = written["kort_all"]["top_left"]
    assert top_left["view_width"] == 800
    assert top_left["display_scale"] == pytest.approx(1.05)
    assert top_left["offset_x"] == 45
    assert top_left["label"]["position"] == "bottom-center"
    assert top_left["label"]["offset_x"] == 12
    assert top_left["label"]["offset_y"] == 18

    bottom_right = written["kort_all"]["bottom_right"]
    assert bottom_right["view_width"] == 640
    assert bottom_right["display_scale"] == pytest.approx(0.75)
    assert bottom_right["offset_x"] == -12
    assert bottom_right["label"]["position"] == "top-right"

    assert "width: 840.0px;" in html
    assert "height: 210.0px;" in html


def test_kort_route_uses_overlay_configuration(client):
    config = load_config()
    config["kort_all"]["top_left"].update(
        {
            "display_scale": 1.5,
            "offset_x": 100,
            "offset_y": 20,
            "label": {"position": "bottom-right", "offset_x": 14, "offset_y": 10},
        }
    )
    save_config(config)

    kort_id = 1
    response = client.get(f"/kort/{kort_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'class="overlay-main"' in html
    assert html.count('class="mini-overlay"') == 3
    assert "Kort 2" in html and "Kort 4" in html
    assert "transform: scale(1.5);" in html
    assert "left: 100px;" in html
    assert "bottom: 20px;" in html


def test_kort_all_renders_all_courts_with_labels(client):
    config = load_config()
    for corner in CORNERS:
        corner_config = config["kort_all"][corner]
        corner_config["display_scale"] = 0.9
        corner_config["label"]["position"] = "top-right"
    save_config(config)

    response = client.get("/kort/all")

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert html.count('class="kort-frame"') == len(CORNERS)
    for corner in CORNERS:
        position = CORNER_POSITION_STYLES[corner]["name"]
        assert f'data-position="{position}"' in html
    assert "Kort 1" in html and "Kort 4" in html
    assert "transform: scale(0.9);" in html


def test_config_template_handles_missing_corner_labels():
    config = load_config()

    with app.app_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_positions=CORNER_POSITION_STYLES,
            corner_overlays=get_corner_overlay_mapping(),
            overlay_links=OVERLAY_LINKS,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html


def test_config_template_handles_absent_corner_positions_context():
    config = load_config()

    with app.app_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_overlays=get_corner_overlay_mapping(),
            overlay_links=OVERLAY_LINKS,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html
    assert "width:" in html and "height:" in html


def test_config_template_handles_missing_corner_position_entries():
    config = load_config()
    partial_positions = {"top_left": CORNER_POSITION_STYLES["top_left"]}

    with app.app_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_positions=partial_positions,
            corner_overlays=get_corner_overlay_mapping(),
            overlay_links=OVERLAY_LINKS,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html
    assert "width:" in html and "height:" in html


def test_config_preview_uses_safe_defaults_for_missing_corner_dimensions():
    config = load_config()
    config.setdefault("kort_all", {})["top_left"] = {}

    with app.app_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_positions={},
            corner_overlays=get_corner_overlay_mapping(),
            overlay_links=OVERLAY_LINKS,
        )

    assert "Konfiguracja Overlay" in html
    assert 'data-corner="top_left"' in html
    assert "width: 690.0px;" in html
    assert "height: 150.0px;" in html


def test_config_preview_embeds_real_overlay_urls(client):
    response = client.get("/config")

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    for overlay in OVERLAY_LINKS.values():
        assert overlay["overlay"] in html
