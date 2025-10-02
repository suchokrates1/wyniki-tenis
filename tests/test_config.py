from html.parser import HTMLParser

import pytest

from main import (
    CORNERS,
    CORNER_LABELS,
    CORNER_POSITION_STYLES,
    DEFAULT_BASE_CONFIG,
    app,
    as_float,
    load_config,
    render_config,
    save_config,
    OverlayConfig,
)


class StartTagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def collect_start_tags(html: str):
    parser = StartTagCollector()
    parser.feed(html)
    return parser.tags


def _attr_matches(attrs, key, expected):
    actual = attrs.get(key)
    if actual is None:
        return False
    if key == "class":
        classes = actual.split()
        if isinstance(expected, str):
            return expected in classes
        return all(item in classes for item in expected)
    return actual == expected


def find_first_tag(tags, tag_name, **expected_attrs):
    for tag, attrs in tags:
        if tag != tag_name:
            continue
        if all(_attr_matches(attrs, key, value) for key, value in expected_attrs.items()):
            return attrs
    return None


def find_all_tags(tags, tag_name, **expected_attrs):
    found = []
    for tag, attrs in tags:
        if tag != tag_name:
            continue
        if all(_attr_matches(attrs, key, value) for key, value in expected_attrs.items()):
            found.append(attrs)
    return found


@pytest.fixture
def authorized_client(client, auth_headers):
    client.environ_base["HTTP_AUTHORIZATION"] = auth_headers["Authorization"]
    return client


def test_get_config_renders_form_and_preview(authorized_client):
    response = authorized_client.get("/config")

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "Konfiguracja Overlay" in html
    assert 'name="view_width"' in html
    assert 'name="kort_all[top_left][view_width]"' in html
    assert 'id="preview-stage"' in html


def test_post_config_updates_overlay_file(authorized_client):
    payload = {
        "view_width": "720",
        "view_height": "180",
        "display_scale": "1.2",
        "left_offset": "15",
        "label_position": "bottom-right",
        "kort_all[top_left][view_width]": "800",
        "kort_all[top_left][view_height]": "200",
        "kort_all[top_left][display_scale]": "1.1",
        "kort_all[top_left][offset_x]": "45",
        "kort_all[top_left][offset_y]": "6",
        "kort_all[top_left][label][position]": "bottom-center",
        "kort_all[top_left][label][offset_x]": "12",
        "kort_all[top_left][label][offset_y]": "18",
        "kort_all[bottom_right][view_width]": "640",
        "kort_all[bottom_right][offset_x]": "-12",
        "kort_all[bottom_right][label][position]": "top-right",
    }

    response = authorized_client.post("/config", data=payload, follow_redirects=True)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'value="720"' in html
    assert 'option value="bottom-right" selected' in html
    assert 'border-red-500/40' not in html

    with app.app_context():
        stored = OverlayConfig.query.first()
        assert stored is not None
        written = stored.to_dict()

    assert written["view_width"] == 720
    assert written["view_height"] == 180
    assert written["display_scale"] == pytest.approx(1.2)
    assert written["left_offset"] == 15
    assert written["label_position"] == "bottom-right"

    top_left = written["kort_all"]["top_left"]
    assert top_left["view_width"] == 800
    assert top_left["offset_x"] == 45
    assert top_left["label"]["position"] == "bottom-center"
    assert top_left["label"]["offset_x"] == 12
    assert top_left["label"]["offset_y"] == 18

    bottom_right = written["kort_all"]["bottom_right"]
    assert bottom_right["view_width"] == 640
    assert bottom_right["offset_x"] == -12
    assert bottom_right["label"]["position"] == "top-right"

    comma_payload = {
        "display_scale": "1,25",
    }

    response = authorized_client.post("/config", data=comma_payload, follow_redirects=True)

    assert response.status_code == 200

    with app.app_context():
        written = OverlayConfig.query.first().to_dict()
    assert written["display_scale"] == pytest.approx(1.25)


def test_post_config_accepts_comma_decimal_values(authorized_client):
    payload = {
        "display_scale": " 1,25 ",
        "kort_all[top_left][display_scale]": " 1,35 ",
    }

    response = authorized_client.post("/config", data=payload, follow_redirects=True)

    assert response.status_code == 200

    with app.app_context():
        written = OverlayConfig.query.first().to_dict()

    assert written["display_scale"] == pytest.approx(1.25)
    assert (
        written["kort_all"]["top_left"]["display_scale"] == pytest.approx(1.35)
    )


def test_config_preview_uses_comma_decimal_values_in_styles(authorized_client):
    payload = {
        "kort_all[top_left][view_width]": "640",
        "kort_all[top_left][view_height]": "200",
        "kort_all[top_left][display_scale]": " 1,25 ",
    }

    response = authorized_client.post("/config", data=payload, follow_redirects=True)

    assert response.status_code == 200

    tags = collect_start_tags(response.get_data(as_text=True))
    card = find_first_tag(tags, "div", **{"data-corner": "top_left"})
    assert card is not None

    style = card.get("style", "")
    assert "width" in style and "height" in style

    def extract_px_value(style_text, property_name):
        for declaration in style_text.split(";"):
            name, _, value = declaration.partition(":")
            if name.strip() == property_name:
                cleaned = value.strip().removesuffix("px")
                return float(cleaned)
        return None

    width_px = extract_px_value(style, "width")
    height_px = extract_px_value(style, "height")

    assert width_px is not None and height_px is not None
    assert width_px == pytest.approx(640 * 1.25)
    assert height_px == pytest.approx(200 * 1.25)

    overlay_response = authorized_client.get("/kort/all")
    assert overlay_response.status_code == 200

    overlay_tags = collect_start_tags(overlay_response.get_data(as_text=True))
    top_left_container = find_first_tag(
        overlay_tags, "div", **{"data-position": "top-left"}
    )
    assert top_left_container is not None

    container_style = top_left_container.get("style", "")
    container_width = extract_px_value(container_style, "width")
    container_height = extract_px_value(container_style, "height")

    assert container_width == pytest.approx(640 * 1.25)
    assert container_height == pytest.approx(200 * 1.25)

    iframe_candidates = find_all_tags(
        overlay_tags, "iframe", **{"class": "kort-frame"}
    )
    assert iframe_candidates
    iframe_styles = [attrs.get("style", "") for attrs in iframe_candidates]
    assert any("scale(1.25)" in style for style in iframe_styles)


def test_post_config_rejects_invalid_dimensions(authorized_client):
    payload = {
        "view_width": "0",
        "kort_all[top_left][view_height]": "-5",
    }

    response = authorized_client.post("/config", data=payload)

    assert response.status_code == 400

    html = response.get_data(as_text=True)
    assert "musi być nie mniejsze" in html
    tags = collect_start_tags(html)
    width_input = find_first_tag(tags, "input", name="view_width")
    assert width_input is not None
    assert width_input.get("value") == "0"
    corner_height_input = find_first_tag(
        tags, "input", name="kort_all[top_left][view_height]"
    )
    assert corner_height_input is not None
    assert corner_height_input.get("value") == "-5"

    with app.app_context():
        stored = OverlayConfig.query.first().to_dict()

    assert stored["view_width"] == DEFAULT_BASE_CONFIG["view_width"]
    assert (
        stored["kort_all"]["top_left"]["view_height"]
        == DEFAULT_BASE_CONFIG["view_height"]
    )


def test_post_config_rejects_invalid_label_position_with_json(authorized_client):
    payload = {
        "label_position": "middle",
    }

    response = authorized_client.post(
        "/config",
        data=payload,
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.is_json

    payload = response.get_json()
    assert payload["ok"] is False
    assert any("niedozwoloną wartość" in error for error in payload["errors"])

    with app.app_context():
        stored = OverlayConfig.query.first().to_dict()

    assert stored["label_position"] == DEFAULT_BASE_CONFIG["label_position"]


def test_as_float_supports_dot_and_comma_decimal_separators():
    assert as_float("1.25", 0.0) == pytest.approx(1.25)
    assert as_float("1,25", 0.0) == pytest.approx(1.25)
    assert as_float(" 1,25 ", 0.0) == pytest.approx(1.25)


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


def test_config_template_renders_with_full_context():
    config = load_config()

    with app.test_request_context():
        html = render_config(config)

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html


def test_config_template_handles_missing_corner_labels():
    config = load_config()

    with app.test_request_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_positions=CORNER_POSITION_STYLES,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html


def test_config_template_handles_absent_corner_positions_context():
    config = load_config()

    with app.test_request_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html
    assert "width:" in html and "height:" in html


def test_config_template_handles_missing_corner_position_entries():
    config = load_config()
    partial_positions = {"top_left": CORNER_POSITION_STYLES["top_left"]}

    with app.test_request_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_positions=partial_positions,
        )

    assert "Konfiguracja Overlay" in html
    for corner in CORNERS:
        assert f'data-corner="{corner}"' in html
    assert "width:" in html and "height:" in html


def test_config_preview_uses_safe_defaults_for_missing_corner_dimensions():
    config = load_config()
    config.setdefault("kort_all", {})["top_left"] = {}

    with app.test_request_context():
        template = app.jinja_env.get_template("config.html")
        html = template.render(
            config=config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_positions={},
        )

    assert "Konfiguracja Overlay" in html
    assert 'data-corner="top_left"' in html
    assert "width: 690.0px;" in html
    assert "height: 150.0px;" in html
