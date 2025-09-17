import copy
import json
import logging
import os
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

# Wczytujemy zmienne środowiskowe najpierw z bieżącego katalogu roboczego,
# a następnie (bez nadpisywania istniejących wartości) z katalogu projektu.
load_dotenv()
load_dotenv(BASE_DIR / ".env", override=False)

def configure_logging():
    level_name = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)


configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

app.config.setdefault(
    "SQLALCHEMY_DATABASE_URI",
    os.environ.get("DATABASE_URL", "sqlite:///overlay.db"),
)
app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

db = SQLAlchemy(app)

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

LINKS_PATH = "overlay_links.json"


def get_config_auth_credentials():
    username = os.environ.get("CONFIG_AUTH_USERNAME")
    password = os.environ.get("CONFIG_AUTH_PASSWORD")
    if username is None or password is None:
        return None
    return username, password


def unauthorized_response():
    response = app.make_response(("Unauthorized", 401))
    response.headers["WWW-Authenticate"] = 'Basic realm="Overlay Config"'
    return response


def requires_config_auth(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        credentials = get_config_auth_credentials()
        if not credentials:
            return unauthorized_response()

        auth = request.authorization
        if auth and (auth.username, auth.password) == credentials:
            return view_func(*args, **kwargs)

        return unauthorized_response()

    return wrapper


class OverlayConfig(db.Model):
    __tablename__ = "overlay_config"

    id = db.Column(db.Integer, primary_key=True)
    view_width = db.Column(db.Integer, nullable=False)
    view_height = db.Column(db.Integer, nullable=False)
    display_scale = db.Column(db.Float, nullable=False)
    left_offset = db.Column(db.Integer, nullable=False)
    label_position = db.Column(db.String(64), nullable=False)
    kort_all = db.Column(db.Text, nullable=False)

    def to_dict(self):
        return ensure_config_structure(
            {
                "view_width": self.view_width,
                "view_height": self.view_height,
                "display_scale": self.display_scale,
                "left_offset": self.left_offset,
                "label_position": self.label_position,
                "kort_all": json.loads(self.kort_all or "{}"),
            }
        )


def serialize_overlay_config(data, instance=None):
    ensured = ensure_config_structure(data)
    target = instance or OverlayConfig()
    target.view_width = ensured["view_width"]
    target.view_height = ensured["view_height"]
    target.display_scale = ensured["display_scale"]
    target.left_offset = ensured["left_offset"]
    target.label_position = ensured["label_position"]
    target.kort_all = json.dumps(ensured["kort_all"])
    return target, ensured


class OverlayLink(db.Model):
    __tablename__ = "overlay_links"

    id = db.Column(db.Integer, primary_key=True)
    kort_id = db.Column(db.String(128), unique=True, nullable=False)
    overlay_url = db.Column(db.String(1024), nullable=False)
    control_url = db.Column(db.String(1024), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "kort_id": self.kort_id,
            "overlay": self.overlay_url,
            "control": self.control_url,
        }


def is_valid_url(value):
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def overlay_link_sort_key(link):
    kort_id = link.kort_id
    if isinstance(kort_id, str) and kort_id.isdigit():
        return (0, int(kort_id))
    try:
        return (0, int(kort_id))
    except (TypeError, ValueError):
        pass
    return (1, str(kort_id))


def ensure_overlay_links_seeded():
    db.create_all()
    if OverlayLink.query.first() is not None:
        return

    if not os.path.exists(LINKS_PATH):
        logger.debug("Pomijam seedowanie linków - brak pliku %s", LINKS_PATH)
        return

    with open(LINKS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f) or {}

    created = False
    created_count = 0
    for kort_id, payload in data.items():
        overlay_url = (payload or {}).get("overlay")
        control_url = (payload or {}).get("control")
        if not (is_valid_url(overlay_url) and is_valid_url(control_url)):
            logger.warning(
                "Pominięto link dla kortu %s - niepoprawne adresy overlay=%s control=%s",
                kort_id,
                overlay_url,
                control_url,
            )
            continue
        link = OverlayLink(
            kort_id=str(kort_id),
            overlay_url=overlay_url,
            control_url=control_url,
        )
        db.session.add(link)
        created = True
        created_count += 1

    if created:
        db.session.commit()
        logger.info("Dodano %s linków do bazy overlay", created_count)


def get_overlay_links():
    ensure_overlay_links_seeded()
    links = OverlayLink.query.order_by(OverlayLink.kort_id.asc()).all()
    return sorted(links, key=overlay_link_sort_key)


def overlay_links_by_kort_id():
    return {
        link.kort_id: {"overlay": link.overlay_url, "control": link.control_url}
        for link in get_overlay_links()
    }


def validate_overlay_link_data(data):
    errors = {}
    normalized = {}

    kort_id = str((data or {}).get("kort_id", "")).strip()
    if not kort_id:
        errors["kort_id"] = "ID kortu jest wymagane."
    else:
        normalized["kort_id"] = kort_id

    overlay_url = (data or {}).get("overlay")
    if not is_valid_url(overlay_url):
        errors["overlay"] = "Niepoprawny adres URL overlayu."
    else:
        normalized["overlay_url"] = overlay_url

    control_url = (data or {}).get("control")
    if not is_valid_url(control_url):
        errors["control"] = "Niepoprawny adres URL panelu sterowania."
    else:
        normalized["control_url"] = control_url

    return normalized, errors


@app.route("/api/overlay-links", methods=["GET", "POST"])
def overlay_links_api():
    ensure_overlay_links_seeded()

    if request.method == "GET":
        return jsonify([link.to_dict() for link in get_overlay_links()])

    payload, errors = validate_overlay_link_data(request.get_json(silent=True))
    if errors:
        return jsonify({"errors": errors}), 400

    existing = OverlayLink.query.filter_by(kort_id=payload["kort_id"]).first()
    if existing is not None:
        return (
            jsonify({"errors": {"kort_id": "Kort o podanym ID już istnieje."}}),
            400,
        )

    link = OverlayLink(**payload)
    db.session.add(link)
    db.session.commit()
    return jsonify(link.to_dict()), 201


@app.route("/api/overlay-links/<int:link_id>", methods=["GET", "PUT", "DELETE"])
def overlay_link_detail_api(link_id):
    ensure_overlay_links_seeded()
    link = OverlayLink.query.get(link_id)
    if link is None:
        return jsonify({"error": "Not found"}), 404

    if request.method == "GET":
        return jsonify(link.to_dict())

    if request.method == "DELETE":
        db.session.delete(link)
        db.session.commit()
        return ("", 204)

    payload, errors = validate_overlay_link_data(request.get_json(silent=True))
    if errors:
        return jsonify({"errors": errors}), 400

    existing = (
        OverlayLink.query.filter(OverlayLink.id != link_id, OverlayLink.kort_id == payload["kort_id"])
        .first()
    )
    if existing is not None:
        return (
            jsonify({"errors": {"kort_id": "Kort o podanym ID już istnieje."}}),
            400,
        )

    link.kort_id = payload["kort_id"]
    link.overlay_url = payload["overlay_url"]
    link.control_url = payload["control_url"]
    db.session.commit()
    return jsonify(link.to_dict())


@app.route("/overlay-links")
def overlay_links_page():
    links = [link.to_dict() for link in get_overlay_links()]
    return render_template("overlay_links.html", links=links)


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
        normalized = value
        if not isinstance(normalized, str):
            normalized = str(normalized)
        normalized = normalized.strip().replace(",", ".")
        return float(normalized)
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
    with app.app_context():
        db.create_all()
        record = OverlayConfig.query.first()
        if not record:
            record, ensured = serialize_overlay_config(dict(DEFAULT_BASE_CONFIG))
            db.session.add(record)
            db.session.commit()
            return ensured

        return record.to_dict()


def save_config(config):
    with app.app_context():
        db.create_all()
        record = OverlayConfig.query.first()
        record, ensured = serialize_overlay_config(config, instance=record)
        if record.id is None:
            db.session.add(record)
        db.session.commit()
        return ensured


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


def render_config(config_dict):
    return render_template(
        "config.html",
        config=config_dict,
        corners=CORNERS,
        corner_labels=CORNER_LABELS,
        corner_positions=CORNER_POSITION_STYLES,
    )


@app.route("/")
def index():
    links = overlay_links_by_kort_id()
    links_management_url = None
    if "overlay_links_page" in app.view_functions:
        links_management_url = url_for("overlay_links_page")

    return render_template(
        "index.html",
        links=links,
        links_management_url=links_management_url,
    )


@app.route("/kort/<int:kort_id>")
def overlay_kort(kort_id):
    kort_id = str(kort_id)

    links_by_id = overlay_links_by_kort_id()

    if kort_id not in links_by_id:
        return f"Nieznany kort {kort_id}", 404

    # HOT reload konfiguracji przy każdym żądaniu
    overlay_config = load_config()
    kort_all_config = overlay_config.get("kort_all", {})
    mini_config = kort_all_config.get("top_left") or get_default_corner_config("top_left")
    mini_label_style = build_label_style(mini_config.get("label"))

    main_overlay = links_by_id[kort_id]["overlay"]
    mini = [(k, v["overlay"]) for k, v in links_by_id.items() if k != kort_id]

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
    sorted_overlays = [link.to_dict() for link in get_overlay_links()]

    for link, corner_key in zip(sorted_overlays, CORNERS):
        kort_id = link["kort_id"]
        corner_config = overlay_config["kort_all"].get(corner_key, get_default_corner_config(corner_key))
        overlays.append(
            {
                "id": kort_id,
                "overlay": link["overlay"],
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
@requires_config_auth
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

        return render_config(saved_config)

    return render_config(current_config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
