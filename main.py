from flask import Flask, render_template, request
from flask_cors import CORS
import copy
import json
import os

app = Flask(__name__)
CORS(app)

LINKS_PATH = "overlay_links.json"
CONFIG_PATH = "overlay_config.json"

CORNERS = ["top_left", "top_right", "bottom_left", "bottom_right"]

CORNER_POSITION_STYLES = {
    "top_left": {"name": "top-left", "style": "top: 0; left: 0;"},
    "top_right": {"name": "top-right", "style": "top: 0; right: 0;"},
    "bottom_left": {"name": "bottom-left", "style": "bottom: 0; left: 0;"},
    "bottom_right": {"name": "bottom-right", "style": "bottom: 0; right: 0;"},
}

CORNER_LABELS = {
    "top_left": "Lewy górny narożnik",
    "top_right": "Prawy górny narożnik",
    "bottom_left": "Lewy dolny narożnik",
    "bottom_right": "Prawy dolny narożnik",
}

DEFAULT_BASE_CONFIG = {
    "view_width": 690,
    "view_height": 150,
    "display_scale": 0.8,
    "left_offset": -30,
    "label_position": "top-left",
}


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_default_corner_config(corner):
    label_position = CORNER_POSITION_STYLES[corner]["name"]
    return {
        "view_width": DEFAULT_BASE_CONFIG["view_width"],
        "view_height": DEFAULT_BASE_CONFIG["view_height"],
        "display_scale": DEFAULT_BASE_CONFIG["display_scale"],
        "offset_x": DEFAULT_BASE_CONFIG["left_offset"],
        "offset_y": 0,
        "label": {
            "position": label_position,
            "offset_x": 8,
            "offset_y": 6,
        },
    }


def merge_corner_config(default_corner, override):
    result = copy.deepcopy(default_corner)
    if not override:
        return result

    for key, value in override.items():
        if key == "label":
            label_override = value or {}
            result_label = result.setdefault("label", {})
            for label_key, label_value in label_override.items():
                if label_value is not None:
                    result_label[label_key] = label_value
        elif value is not None:
            result[key] = value

    return result


def normalize_corner_types(corner):
    corner["view_width"] = as_int(corner.get("view_width"), DEFAULT_BASE_CONFIG["view_width"])
    corner["view_height"] = as_int(corner.get("view_height"), DEFAULT_BASE_CONFIG["view_height"])
    corner["display_scale"] = as_float(corner.get("display_scale"), DEFAULT_BASE_CONFIG["display_scale"])
    corner["offset_x"] = as_int(corner.get("offset_x"), DEFAULT_BASE_CONFIG["left_offset"])
    corner["offset_y"] = as_int(corner.get("offset_y"), 0)

    label_defaults = {
        "position": corner.get("label", {}).get("position", "top-left"),
        "offset_x": 8,
        "offset_y": 6,
    }

    label = corner.setdefault("label", {})
    label["position"] = label.get("position", label_defaults["position"])
    label["offset_x"] = as_int(label.get("offset_x"), label_defaults["offset_x"])
    label["offset_y"] = as_int(label.get("offset_y"), label_defaults["offset_y"])

    return corner


def ensure_config_structure(config):
    config = dict(config or {})

    for key, default_value in DEFAULT_BASE_CONFIG.items():
        config[key] = config.get(key, default_value)

    config["view_width"] = as_int(config.get("view_width"), DEFAULT_BASE_CONFIG["view_width"])
    config["view_height"] = as_int(config.get("view_height"), DEFAULT_BASE_CONFIG["view_height"])
    config["display_scale"] = as_float(config.get("display_scale"), DEFAULT_BASE_CONFIG["display_scale"])
    config["left_offset"] = as_int(config.get("left_offset"), DEFAULT_BASE_CONFIG["left_offset"])
    config["label_position"] = config.get("label_position", DEFAULT_BASE_CONFIG["label_position"])

    existing_kort_all = config.get("kort_all") or {}
    ensured_kort_all = {}

    top_left_base = {
        "view_width": config["view_width"],
        "view_height": config["view_height"],
        "display_scale": config["display_scale"],
        "offset_x": config["left_offset"],
        "offset_y": 0,
        "label": {
            "position": config["label_position"],
            "offset_x": 8,
            "offset_y": 6,
        },
    }

    for corner in CORNERS:
        default_corner = get_default_corner_config(corner)
        if corner == "top_left":
            default_corner = merge_corner_config(default_corner, top_left_base)

        corner_override = existing_kort_all.get(corner, {})
        merged = merge_corner_config(default_corner, corner_override)
        ensured_kort_all[corner] = normalize_corner_types(merged)

    config["kort_all"] = ensured_kort_all

    return config


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return ensure_config_structure(dict(DEFAULT_BASE_CONFIG))

    with open(CONFIG_PATH) as f:
        raw_config = json.load(f)

    return ensure_config_structure(raw_config)


def save_config(config):
    prepared = ensure_config_structure(config)
    with open(CONFIG_PATH, "w") as f:
        json.dump(prepared, f, indent=2)
    return prepared


def build_label_style(label_config):
    position = (label_config or {}).get("position", "top-left")
    offset_x = as_int((label_config or {}).get("offset_x"), 0)
    offset_y = as_int((label_config or {}).get("offset_y"), 0)

    style_parts = ["position: absolute;"]

    if "top" in position:
        style_parts.append(f"top: {offset_y}px;")
    else:
        style_parts.append(f"bottom: {offset_y}px;")

    if "center" in position:
        style_parts.append(f"left: calc(50% + {offset_x}px);")
        style_parts.append("transform: translateX(-50%);")
    elif "right" in position:
        style_parts.append(f"right: {offset_x}px;")
    else:
        style_parts.append(f"left: {offset_x}px;")

    return " ".join(style_parts)

# Stałe linki do overlayów
with open(LINKS_PATH) as f:
    OVERLAY_LINKS = json.load(f)


@app.route("/")
def index():
    return render_template("index.html", links=OVERLAY_LINKS)


@app.route("/kort/<kort_id>")
def overlay_kort(kort_id):
    kort_id = str(kort_id)

    if kort_id not in OVERLAY_LINKS:
        return f"Nieznany kort {kort_id}", 404

    # HOT reload konfiguracji przy każdym żądaniu
    overlay_config = load_config()
    mini_config = overlay_config["kort_all"].get("top_left", get_default_corner_config("top_left"))
    mini_label_style = build_label_style(mini_config.get("label"))

    main_overlay = OVERLAY_LINKS[kort_id]["overlay"]
    mini = [(k, v["overlay"]) for k, v in OVERLAY_LINKS.items() if k != kort_id]

    return render_template(
        "kort.html",
        kort_id=kort_id,
        main_overlay=main_overlay,
        mini_overlays=mini,
        config=overlay_config,
        mini_config=mini_config,
        mini_label_style=mini_label_style,
    )


@app.route("/kort/all")
def overlay_all():
    """Renderuje widok z czterema kortami rozmieszczonymi w rogach."""
    overlay_config = load_config()

    overlays = []
    sorted_overlays = sorted(
        OVERLAY_LINKS.items(),
        key=lambda item: int(item[0]) if str(item[0]).isdigit() else item[0]
    )

    for (kort_id, data), corner_key in zip(sorted_overlays, CORNERS):
        corner_config = overlay_config["kort_all"].get(corner_key, get_default_corner_config(corner_key))
        overlays.append(
            {
                "id": kort_id,
                "overlay": data["overlay"],
                "position": CORNER_POSITION_STYLES[corner_key],
                "corner_key": corner_key,
                "config": corner_config,
                "label_style": build_label_style(corner_config.get("label")),
            }
        )

    return render_template(
        "kort_all.html",
        overlays=overlays,
        config=overlay_config,
    )


@app.route("/config", methods=["GET", "POST"])
def config():
    current_config = load_config()

    if request.method == "POST":
        form = request.form

        data = {
            "view_width": as_int(form.get("view_width", current_config["view_width"]), current_config["view_width"]),
            "view_height": as_int(form.get("view_height", current_config["view_height"]), current_config["view_height"]),
            "display_scale": as_float(form.get("display_scale", current_config["display_scale"]), current_config["display_scale"]),
            "left_offset": as_int(form.get("left_offset", current_config["left_offset"]), current_config["left_offset"]),
            "label_position": form.get("label_position", current_config["label_position"]),
        }

        kort_all = {}
        for corner in CORNERS:
            existing_corner = current_config["kort_all"].get(corner, get_default_corner_config(corner))
            prefix = f"kort_all[{corner}]"
            label_prefix = f"{prefix}[label]"

            kort_all[corner] = {
                "view_width": as_int(form.get(f"{prefix}[view_width]", existing_corner["view_width"]), existing_corner["view_width"]),
                "view_height": as_int(form.get(f"{prefix}[view_height]", existing_corner["view_height"]), existing_corner["view_height"]),
                "display_scale": as_float(form.get(f"{prefix}[display_scale]", existing_corner["display_scale"]), existing_corner["display_scale"]),
                "offset_x": as_int(form.get(f"{prefix}[offset_x]", existing_corner["offset_x"]), existing_corner["offset_x"]),
                "offset_y": as_int(form.get(f"{prefix}[offset_y]", existing_corner["offset_y"]), existing_corner["offset_y"]),
                "label": {
                    "position": form.get(f"{label_prefix}[position]", existing_corner["label"]["position"]),
                    "offset_x": as_int(form.get(f"{label_prefix}[offset_x]", existing_corner["label"]["offset_x"]), existing_corner["label"]["offset_x"]),
                    "offset_y": as_int(form.get(f"{label_prefix}[offset_y]", existing_corner["label"]["offset_y"]), existing_corner["label"]["offset_y"]),
                },
            }

        data["kort_all"] = kort_all
        saved_config = save_config(data)

        return render_template(
            "config.html",
            config=saved_config,
            corners=CORNERS,
            corner_labels=CORNER_LABELS,
            corner_positions=CORNER_POSITION_STYLES,
        )

    return render_template(
        "config.html",
        config=current_config,
        corners=CORNERS,
        corner_labels=CORNER_LABELS,
        corner_positions=CORNER_POSITION_STYLES,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
