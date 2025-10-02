import copy
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, flash, jsonify, render_template, request, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

from results import snapshots, start_background_updater


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
app.config.setdefault("SNAPSHOTS_DIR", BASE_DIR / "snapshots")

db = SQLAlchemy(app)

__all__ = ["app", "db", "snapshots"]

CORNERS = ["top_left", "top_right", "bottom_left", "bottom_right"]

ALLOWED_LABEL_POSITIONS = {
    "top-left",
    "top-center",
    "top-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
}

DIMENSION_MIN = 1
DIMENSION_MAX = 4096
DISPLAY_SCALE_MIN = 0.1
DISPLAY_SCALE_MAX = 5.0
OFFSET_MIN = -1000
OFFSET_MAX = 1000

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

FINISHED_STATUSES = {
    "finished",
    "complete",
    "completed",
    "done",
    "zakończony",
    "zakończone",
}

ACTIVE_STATUSES = {
    "active",
    "in_progress",
    "live",
    "ongoing",
    "running",
}

STATUS_LABELS = {
    "active": "W trakcie",
    "finished": "Zakończony",
    "unavailable": "Niedostępny",
    "brak_danych": "Brak danych",
}

UNAVAILABLE_STATUSES = {"unavailable", "niedostępny", "niedostepny"}
NO_DATA_STATUSES = {"brak danych", "brak_danych", "no data", "no_data"}
STATUS_ORDER = ["active", "finished", "unavailable", "brak_danych"]
STATUS_VIEW_META = {
    "active": {
        "title": "Aktywne mecze",
        "caption": "Aktualne spotkania i status kortów",
        "empty_message": "Aktualnie brak danych o aktywnych kortach.",
    },
    "finished": {
        "title": "Zakończone mecze",
        "caption": "Zakończone spotkania",
        "empty_message": "Brak zakończonych meczów do wyświetlenia.",
    },
    "unavailable": {
        "title": "Korty niedostępne",
        "caption": "Ostatnio obserwowane korty bez dostępu",
        "empty_message": "Wszystkie korty są obecnie dostępne.",
    },
    "brak_danych": {
        "title": "Korty bez danych",
        "caption": "Korty bez ostatnich danych pomiarowych",
        "empty_message": "Brak kortów bez danych do wyświetlenia.",
    },
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


APP_OVERLAYS_HOST = "app.overlays.uno"


def _path_has_identifier(path: str, prefix: str) -> bool:
    if not path.startswith(prefix):
        return False
    remainder = path[len(prefix) :]
    return bool(remainder) and "/" not in remainder


def _validate_app_overlays_url(url, *, field_label, path_options):
    if not url:
        return None, f"{field_label} jest wymagany."

    try:
        parsed = urlparse(url)
    except ValueError:
        return None, f"{field_label} ma niepoprawny format."

    if parsed.scheme != "https":
        return None, f"{field_label} musi używać protokołu HTTPS."

    if parsed.netloc.lower() != APP_OVERLAYS_HOST:
        return None, f"{field_label} musi wskazywać na {APP_OVERLAYS_HOST}."

    for prefix, description in path_options:
        if _path_has_identifier(parsed.path or "", prefix):
            return url, None

    description = " lub ".join(desc for _, desc in path_options)
    return None, f"{field_label} musi mieć ścieżkę w formacie {description}."


def validate_overlay_url(url):
    return _validate_app_overlays_url(
        url,
        field_label="Adres overlayu",
        path_options=(("/output/", "/output/{id}"),),
    )


def validate_control_url(url):
    return _validate_app_overlays_url(
        url,
        field_label="Adres panelu sterowania",
        path_options=(
            ("/control/", "/control/{id}"),
            ("/controlapps/", "/controlapps/{id}"),
        ),
    )


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
        overlay_valid, overlay_error = validate_overlay_url(overlay_url)
        control_valid, control_error = validate_control_url(control_url)
        if overlay_error or control_error:
            logger.warning(
                "Pominięto link dla kortu %s - %s %s",
                kort_id,
                overlay_error or "",
                control_error or "",
            )
            continue
        link = OverlayLink(
            kort_id=str(kort_id),
            overlay_url=overlay_valid,
            control_url=control_valid,
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


def kort_id_sort_key(value):
    if isinstance(value, str) and value.isdigit():
        return (0, int(value))
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        pass
    return (1, str(value))


def extract_kort_id(entry, fallback):
    candidate = (
        (entry or {}).get("kort_id")
        or (entry or {}).get("court_id")
        or (entry or {}).get("id")
        or (entry or {}).get("kort")
        or (entry or {}).get("court")
    )
    if candidate is None:
        return str(fallback)

    if isinstance(candidate, (int, float)):
        return str(int(candidate))

    candidate_str = str(candidate).strip()
    digits = re.findall(r"\d+", candidate_str)
    if digits:
        return digits[0]
    return candidate_str or str(fallback)


def normalize_status(raw_status, available, has_snapshot):
    if not has_snapshot:
        return "brak_danych"

    status_text = str(raw_status or "").strip().lower()

    if status_text in NO_DATA_STATUSES:
        return "brak_danych"

    if status_text in FINISHED_STATUSES:
        base_status = "finished"
    elif status_text in ACTIVE_STATUSES or status_text == "ok":
        base_status = "active"
    elif status_text in UNAVAILABLE_STATUSES:
        base_status = "unavailable"
    else:
        base_status = "active"

    if not available and base_status != "brak_danych":
        return "unavailable"

    return base_status


def display_value(value, fallback="brak danych"):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return fallback if not value else ", ".join(map(str, value))
    text = str(value).strip()
    return text if text else fallback


def display_name(value, fallback="brak danych"):
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def is_player_serving(serving_marker, index, name):
    if serving_marker is None:
        return False

    if isinstance(serving_marker, (int, float)):
        try:
            return int(serving_marker) == index
        except (TypeError, ValueError):
            return False

    if isinstance(serving_marker, str):
        marker = serving_marker.strip().lower()
        if marker in {"player1", "p1"}:
            return index == 0
        if marker in {"player2", "p2"}:
            return index == 1
        if name:
            return marker == str(name).strip().lower()
        return False

    if isinstance(serving_marker, dict):
        if "index" in serving_marker:
            try:
                return int(serving_marker["index"]) == index
            except (TypeError, ValueError):
                pass
        marker_name = serving_marker.get("name") or serving_marker.get("player")
        if marker_name and name:
            return str(marker_name).strip().lower() == str(name).strip().lower()
    return False


def normalize_players(players_data, serving_marker):
    normalized = []

    if isinstance(players_data, dict):
        iterable = list(players_data.values())
    elif isinstance(players_data, list):
        iterable = players_data
    else:
        iterable = []

    for index, raw_player in enumerate(iterable):
        if isinstance(raw_player, dict):
            name = raw_player.get("name") or raw_player.get("player") or raw_player.get("label")
            sets = (
                raw_player.get("sets")
                or raw_player.get("set_score")
                or raw_player.get("sets_won")
                or raw_player.get("set")
            )
            games = (
                raw_player.get("games")
                or raw_player.get("games_won")
                or raw_player.get("score")
                or raw_player.get("points")
            )
        else:
            name = raw_player
            sets = None
            games = None

        normalized.append(
            {
                "display_name": display_name(name),
                "display_sets": display_value(sets),
                "display_games": display_value(games),
                "is_serving": is_player_serving(serving_marker, index, name),
            }
        )

    if not normalized:
        normalized.append(
            {
                "display_name": "brak danych",
                "display_sets": "brak danych",
                "display_games": "brak danych",
                "is_serving": False,
            }
        )

    return normalized


def normalize_last_updated(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return text
    else:
        return str(value)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).isoformat()


def load_snapshots():
    directory = Path(app.config.get("SNAPSHOTS_DIR", BASE_DIR / "snapshots"))
    if not directory.exists():
        return {}

    snapshots = {}
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Nie udało się wczytać pliku snapshot %s: %s", path, exc)
            continue

        if isinstance(payload, dict) and isinstance(payload.get("snapshots"), list):
            entries = payload["snapshots"]
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = [payload]

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            fallback = f"{path.stem}-{index}"
            kort_id = extract_kort_id(entry, fallback)
            snapshots[str(kort_id)] = entry

    return snapshots


def normalize_snapshot_entry(kort_id, snapshot, link_meta=None):
    snapshot = snapshot or {}
    has_snapshot = bool(snapshot)
    available = snapshot.get("available", True) if has_snapshot else False
    status = normalize_status(snapshot.get("status"), available, has_snapshot)
    status_label = STATUS_LABELS.get(status, status.replace("_", " ").capitalize())

    overlay_is_on = bool(available)
    overlay_label = "ON" if overlay_is_on else "OFF"
    last_updated = normalize_last_updated(
        snapshot.get("last_updated")
        or snapshot.get("updated_at")
        or snapshot.get("timestamp")
    )

    kort_label = (
        snapshot.get("court_name")
        or snapshot.get("kort_name")
        or snapshot.get("kort")
        or snapshot.get("court")
        or (link_meta or {}).get("name")
        or (f"Kort {kort_id}" if kort_id else "Kort")
    )

    players = normalize_players(snapshot.get("players"), snapshot.get("serving"))
    row_span = max(len(players), 1)

    score_summary = display_value(
        snapshot.get("game_score")
        or snapshot.get("score_summary")
        or snapshot.get("score"),
    )

    set_summary = display_value(snapshot.get("set_score") or snapshot.get("sets"))

    return {
        "kort_id": str(kort_id),
        "kort_label": display_name(kort_label, fallback=f"Kort {kort_id}" if kort_id else "Kort"),
        "status": status,
        "status_label": status_label,
        "available": available,
        "has_snapshot": has_snapshot,
        "overlay_is_on": overlay_is_on,
        "overlay_label": overlay_label,
        "last_updated": last_updated,
        "players": players,
        "row_span": row_span,
        "score_summary": score_summary,
        "set_summary": set_summary,
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
    overlay_valid, overlay_error = validate_overlay_url(overlay_url)
    if overlay_error:
        errors["overlay"] = overlay_error
    else:
        normalized["overlay_url"] = overlay_valid

    control_url = (data or {}).get("control")
    control_valid, control_error = validate_control_url(control_url)
    if control_error:
        errors["control"] = control_error
    else:
        normalized["control_url"] = control_valid

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


@app.route("/api/overlay-links/reload", methods=["POST"])
@requires_config_auth
def overlay_links_reload():
    db.create_all()

    if not os.path.exists(LINKS_PATH):
        return jsonify({"error": f"Brak pliku {LINKS_PATH}."}), 404

    try:
        with open(LINKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except json.JSONDecodeError as exc:
        logger.error("Nie udało się odczytać pliku %s: %s", LINKS_PATH, exc)
        return jsonify({"error": "Plik overlay_links.json ma niepoprawny format JSON."}), 400

    if not isinstance(data, dict):
        return (
            jsonify({"error": "Plik overlay_links.json musi zawierać obiekt z mapowaniem kortów."}),
            400,
        )

    existing_links = {link.kort_id: link for link in OverlayLink.query.all()}
    seen_ids = set()
    created = 0
    updated = 0
    removed = 0

    for kort_id, payload in data.items():
        kort_id_str = str(kort_id)
        seen_ids.add(kort_id_str)
        payload = payload or {}
        overlay_valid, overlay_error = validate_overlay_url(payload.get("overlay"))
        control_valid, control_error = validate_control_url(payload.get("control"))

        if overlay_error or control_error:
            logger.warning(
                "Pominięto link dla kortu %s - %s %s",
                kort_id_str,
                overlay_error or "",
                control_error or "",
            )
            continue

        existing = existing_links.get(kort_id_str)
        if existing is not None:
            if existing.overlay_url != overlay_valid or existing.control_url != control_valid:
                existing.overlay_url = overlay_valid
                existing.control_url = control_valid
                updated += 1
        else:
            db.session.add(
                OverlayLink(
                    kort_id=kort_id_str,
                    overlay_url=overlay_valid,
                    control_url=control_valid,
                )
            )
            created += 1

    for kort_id, link in existing_links.items():
        if kort_id not in seen_ids:
            db.session.delete(link)
            removed += 1

    db.session.commit()

    return jsonify({"created": created, "updated": updated, "removed": removed})


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


@app.route("/wyniki")
def wyniki():
    snapshots = load_snapshots()
    links = overlay_links_by_kort_id()

    known_ids = {str(kort_id) for kort_id in links.keys()}
    known_ids.update(str(kort_id) for kort_id in snapshots.keys())
    sorted_ids = sorted(known_ids, key=kort_id_sort_key)

    matches_by_status = {status: [] for status in STATUS_ORDER}

    for kort_id in sorted_ids:
        normalized = normalize_snapshot_entry(kort_id, snapshots.get(kort_id), links.get(kort_id))
        matches_by_status.setdefault(normalized["status"], []).append(normalized)

    for status_matches in matches_by_status.values():
        status_matches.sort(key=lambda match: kort_id_sort_key(match["kort_id"]))

    has_running_matches = bool(matches_by_status.get("active"))
    has_non_running_snapshots = any(
        matches_by_status.get(status)
        for status in STATUS_ORDER
        if status not in {"active", "finished"}
    )

    sections = []
    for status in STATUS_ORDER:
        meta = STATUS_VIEW_META.get(status, {})
        sections.append(
            {
                "status": status,
                "title": meta.get("title", status.replace("_", " ").title()),
                "caption": meta.get("caption", ""),
                "empty_message": meta.get("empty_message", ""),
                "matches": matches_by_status.get(status, []),
            }
        )

    return render_template(
        "wyniki.html",
        sections=sections,
        has_running_matches=has_running_matches,
        has_non_running_snapshots=has_non_running_snapshots,
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

    snapshots = load_snapshots()
    main_overlay = links_by_id[kort_id]["overlay"]
    main_snapshot = normalize_snapshot_entry(
        kort_id,
        snapshots.get(kort_id),
        links_by_id.get(kort_id),
    )

    mini = []
    for mini_id, data in links_by_id.items():
        if mini_id == kort_id:
            continue
        normalized = normalize_snapshot_entry(
            mini_id,
            snapshots.get(mini_id),
            data,
        )
        mini.append(
            {
                "kort_id": mini_id,
                "overlay": data["overlay"],
                "status_label": normalized["status_label"],
                "status": normalized["status"],
                "overlay_is_on": normalized["overlay_is_on"],
                "overlay_label": normalized["overlay_label"],
                "last_updated": normalized["last_updated"],
            }
        )

    return render_template(
        "kort.html",
        kort_id=kort_id,
        main_overlay=main_overlay,
        main_snapshot=main_snapshot,
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
    snapshots = load_snapshots()

    for link, corner_key in zip(sorted_overlays, CORNERS):
        kort_id = link["kort_id"]
        corner_config = overlay_config["kort_all"].get(corner_key, get_default_corner_config(corner_key))
        normalized = normalize_snapshot_entry(kort_id, snapshots.get(kort_id), link)
        overlays.append(
            {
                "id": kort_id,
                "overlay": link["overlay"],
                "position": CORNER_POSITION_STYLES[corner_key],
                "corner_key": corner_key,
                "config": corner_config,
                "label_style": build_label_style(corner_config.get("label")),
                "status": normalized["status"],
                "status_label": normalized["status_label"],
                "overlay_is_on": normalized["overlay_is_on"],
                "overlay_label": normalized["overlay_label"],
                "last_updated": normalized["last_updated"],
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

        def parse_int_field(raw_value, *, current_value, label):
            display_value = raw_value if raw_value is not None else current_value
            if raw_value is None:
                return current_value, None, display_value

            if isinstance(raw_value, str):
                stripped = raw_value.strip()
                if stripped == "":
                    return current_value, f"{label} nie może być puste.", display_value
                normalized = stripped.replace(",", ".")
            else:
                normalized = raw_value

            try:
                if isinstance(normalized, str):
                    if "." in normalized:
                        value = int(float(normalized))
                    else:
                        value = int(normalized)
                else:
                    value = int(normalized)
            except (TypeError, ValueError):
                return current_value, f"{label} musi być liczbą całkowitą.", display_value

            return value, None, value

        def parse_float_field(raw_value, *, current_value, label):
            display_value = raw_value if raw_value is not None else current_value
            if raw_value is None:
                return current_value, None, display_value

            if isinstance(raw_value, str):
                stripped = raw_value.strip()
                if stripped == "":
                    return current_value, f"{label} nie może być puste.", display_value
                normalized = stripped.replace(",", ".")
            else:
                normalized = raw_value

            try:
                value = float(normalized)
            except (TypeError, ValueError):
                return current_value, f"{label} musi być liczbą.", display_value

            return value, None, value

        errors = []
        submitted_config = copy.deepcopy(current_config)
        validated_config = copy.deepcopy(current_config)

        field_specs = [
            ("view_width", "📐 Szerokość wycinka", DIMENSION_MIN, DIMENSION_MAX),
            ("view_height", "📏 Wysokość wycinka", DIMENSION_MIN, DIMENSION_MAX),
            ("left_offset", "↔️ Przesunięcie w lewo", OFFSET_MIN, OFFSET_MAX),
        ]

        for field, label, min_value, max_value in field_specs:
            raw_value = form.get(field)
            if raw_value is None:
                continue

            value, error, display_value = parse_int_field(
                raw_value,
                current_value=current_config[field],
                label=label,
            )
            if error:
                errors.append(error)
                submitted_config[field] = display_value
            else:
                if value < min_value:
                    errors.append(f"{label} musi być nie mniejsze niż {min_value}.")
                    submitted_config[field] = raw_value
                elif value > max_value:
                    errors.append(f"{label} nie może być większe niż {max_value}.")
                    submitted_config[field] = raw_value
                else:
                    validated_config[field] = value
                    submitted_config[field] = value

        raw_scale = form.get("display_scale")
        if raw_scale is not None:
            value, error, display_value = parse_float_field(
                raw_scale,
                current_value=current_config["display_scale"],
                label="🔍 Skala wyświetlania",
            )
            if error:
                errors.append(error)
                submitted_config["display_scale"] = display_value
            else:
                if value < DISPLAY_SCALE_MIN or value > DISPLAY_SCALE_MAX:
                    errors.append(
                        f"🔍 Skala wyświetlania musi mieścić się w przedziale od {DISPLAY_SCALE_MIN} do {DISPLAY_SCALE_MAX}."
                    )
                    submitted_config["display_scale"] = raw_scale
                else:
                    validated_config["display_scale"] = value
                    submitted_config["display_scale"] = value

        raw_label_position = form.get("label_position")
        if raw_label_position is not None:
            if raw_label_position not in ALLOWED_LABEL_POSITIONS:
                errors.append('📍 Pozycja napisu "Kort X" zawiera niedozwoloną wartość.')
                submitted_config["label_position"] = raw_label_position
            else:
                validated_config["label_position"] = raw_label_position
                submitted_config["label_position"] = raw_label_position

        submitted_corners = submitted_config.setdefault("kort_all", {})
        validated_corners = validated_config.setdefault("kort_all", {})

        for corner in CORNERS:
            existing_corner = current_config["kort_all"].get(corner, get_default_corner_config(corner))
            submitted_corner = copy.deepcopy(existing_corner)
            validated_corner = copy.deepcopy(existing_corner)

            prefix = f"kort_all[{corner}]"
            label_prefix = f"{prefix}[label]"

            dimension_fields = [
                ("view_width", "Szerokość", DIMENSION_MIN, DIMENSION_MAX),
                ("view_height", "Wysokość", DIMENSION_MIN, DIMENSION_MAX),
            ]

            for field, label, min_value, max_value in dimension_fields:
                raw_value = form.get(f"{prefix}[{field}]")
                if raw_value is None:
                    continue

                value, error, display_value = parse_int_field(
                    raw_value,
                    current_value=existing_corner[field],
                    label=f"{CORNER_LABELS.get(corner, corner)} – {label}",
                )
                if error:
                    errors.append(error)
                    submitted_corner[field] = display_value
                else:
                    if value < min_value:
                        errors.append(
                            f"{CORNER_LABELS.get(corner, corner)} – {label} musi być nie mniejsze niż {min_value}."
                        )
                        submitted_corner[field] = raw_value
                    elif value > max_value:
                        errors.append(
                            f"{CORNER_LABELS.get(corner, corner)} – {label} nie może być większe niż {max_value}."
                        )
                        submitted_corner[field] = raw_value
                    else:
                        validated_corner[field] = value
                        submitted_corner[field] = value

            raw_scale = form.get(f"{prefix}[display_scale]")
            if raw_scale is not None:
                value, error, display_value = parse_float_field(
                    raw_scale,
                    current_value=existing_corner["display_scale"],
                    label=f"{CORNER_LABELS.get(corner, corner)} – Skala",
                )
                if error:
                    errors.append(error)
                    submitted_corner["display_scale"] = display_value
                else:
                    if value < DISPLAY_SCALE_MIN or value > DISPLAY_SCALE_MAX:
                        errors.append(
                            f"{CORNER_LABELS.get(corner, corner)} – Skala musi mieścić się w przedziale od {DISPLAY_SCALE_MIN} do {DISPLAY_SCALE_MAX}."
                        )
                        submitted_corner["display_scale"] = raw_scale
                    else:
                        validated_corner["display_scale"] = value
                        submitted_corner["display_scale"] = value

            for field, label in ("offset_x", "Offset X"), ("offset_y", "Offset Y"):
                raw_value = form.get(f"{prefix}[{field}]")
                if raw_value is None:
                    continue

                value, error, display_value = parse_int_field(
                    raw_value,
                    current_value=existing_corner[field],
                    label=f"{CORNER_LABELS.get(corner, corner)} – {label}",
                )
                if error:
                    errors.append(error)
                    submitted_corner[field] = display_value
                else:
                    if value < OFFSET_MIN or value > OFFSET_MAX:
                        errors.append(
                            f"{CORNER_LABELS.get(corner, corner)} – {label} musi mieścić się w przedziale od {OFFSET_MIN} do {OFFSET_MAX}."
                        )
                        submitted_corner[field] = raw_value
                    else:
                        validated_corner[field] = value
                        submitted_corner[field] = value

            raw_label_position = form.get(f"{label_prefix}[position]")
            if raw_label_position is not None:
                if raw_label_position not in ALLOWED_LABEL_POSITIONS:
                    errors.append(
                        f"{CORNER_LABELS.get(corner, corner)} – Pozycja etykiety zawiera niedozwoloną wartość."
                    )
                    submitted_corner.setdefault("label", {})["position"] = raw_label_position
                else:
                    validated_corner.setdefault("label", {})["position"] = raw_label_position
                    submitted_corner.setdefault("label", {})["position"] = raw_label_position

            for field, label in ("offset_x", "Offset etykiety X"), ("offset_y", "Offset etykiety Y"):
                raw_value = form.get(f"{label_prefix}[{field}]")
                if raw_value is None:
                    continue

                value, error, display_value = parse_int_field(
                    raw_value,
                    current_value=existing_corner["label"][field],
                    label=f"{CORNER_LABELS.get(corner, corner)} – {label}",
                )
                if error:
                    errors.append(error)
                    submitted_corner.setdefault("label", {})[field] = display_value
                else:
                    if value < OFFSET_MIN or value > OFFSET_MAX:
                        errors.append(
                            f"{CORNER_LABELS.get(corner, corner)} – {label} musi mieścić się w przedziale od {OFFSET_MIN} do {OFFSET_MAX}."
                        )
                        submitted_corner.setdefault("label", {})[field] = raw_value
                    else:
                        validated_corner.setdefault("label", {})[field] = value
                        submitted_corner.setdefault("label", {})[field] = value

            submitted_corners[corner] = submitted_corner
            validated_corners[corner] = validated_corner

        wants_json = request.is_json or (
            request.accept_mimetypes.accept_json
            and not request.accept_mimetypes.accept_html
        )

        if errors:
            for error in errors:
                flash(error, "error")

            if wants_json:
                return jsonify({"ok": False, "errors": errors}), 400

            response = render_config(submitted_config)
            return response, 400

        saved_config = save_config(validated_config)

        if wants_json:
            return jsonify({"ok": True, "config": saved_config})

        return render_config(saved_config)

    return render_config(current_config)


start_background_updater(app, overlay_links_by_kort_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
