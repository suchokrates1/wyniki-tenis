import copy
import ipaddress
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from zoneinfo import ZoneInfo

from flask import Flask, flash, jsonify, render_template, request, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

from sqlalchemy import inspect, text
from sqlalchemy.sql import expression

import requests

from results import (
    build_output_url,
    get_metrics_snapshot,
    snapshots,
    start_background_updater,
)


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
    "disabled": "Wyłączony",
    "unavailable": "Niedostępny",
    "brak_danych": "Brak danych",
}

UNAVAILABLE_STATUSES = {"unavailable", "niedostępny", "niedostepny"}
NO_DATA_STATUSES = {"brak danych", "brak_danych", "no data", "no_data"}
STATUS_ORDER = ["active", "finished", "disabled", "unavailable", "brak_danych"]
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
    "disabled": {
        "title": "Wyłączone korty",
        "caption": "Korty z zatrzymanym odpytywaniem",
        "empty_message": "Brak wyłączonych kortów.",
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

BOOL_TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
BOOL_FALSE_VALUES = {"0", "false", "f", "no", "n", "off"}


def coerce_bool(value, *, default=None):
    if value is None:
        if default is not None:
            return bool(default)
        raise ValueError("Brak wartości logicznej")

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        try:
            return bool(int(value))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            raise ValueError(f"Nie można zinterpretować {value!r} jako bool") from None

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in BOOL_TRUE_VALUES:
            return True
        if normalized in BOOL_FALSE_VALUES:
            return False
        raise ValueError(f"Nie można zinterpretować {value!r} jako bool")

    raise ValueError(f"Nie można zinterpretować {value!r} jako bool")


def ensure_overlay_links_schema():
    with db.engine.begin() as connection:
        inspector = inspect(connection)
        if "overlay_links" not in inspector.get_table_names():
            return

        existing_columns = {column["name"] for column in inspector.get_columns("overlay_links")}

        if "enabled" not in existing_columns:
            connection.execute(
                text("ALTER TABLE overlay_links ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
            )

        if "hidden" not in existing_columns:
            connection.execute(
                text("ALTER TABLE overlay_links ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0")
            )



DEFAULT_BASE_CONFIG = {
    "view_width": 690,
    "view_height": 150,
    "display_scale": 0.8,
    "left_offset": -30,
    "label_position": "top-left",
}

LINKS_PATH = "overlay_links.json"

CONTROL_TEST_COMMAND = "GetStatus"
CONTROL_TEST_TIMEOUT_SECONDS = 5
CONTROL_TEST_MAX_PAYLOAD_CHARS = 400
CONTROL_TEST_MAX_PAYLOAD_KEYS = 4


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


def _is_local_request() -> bool:
    remote_addr = request.remote_addr or ""
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return remote_addr.startswith("127.")
    return ip.is_loopback


@app.route("/debug/metrics")
def debug_metrics():
    if not _is_local_request():
        response = jsonify({"error": "forbidden"})
        response.status_code = 403
        return response
    return jsonify(get_metrics_snapshot())


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
    enabled = db.Column(
        db.Boolean,
        nullable=False,
        server_default=expression.true(),
        default=True,
    )
    hidden = db.Column(
        db.Boolean,
        nullable=False,
        server_default=expression.false(),
        default=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "kort_id": self.kort_id,
            "overlay": self.overlay_url,
            "control": self.control_url,
            "enabled": bool(self.enabled),
            "hidden": bool(self.hidden),
        }


APP_OVERLAYS_HOST = "app.overlays.uno"
WARSAW_TIMEZONE = ZoneInfo("Europe/Warsaw")


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
    ensure_overlay_links_schema()
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
        try:
            enabled = coerce_bool((payload or {}).get("enabled"), default=True)
        except ValueError:
            logger.warning(
                "Pominięto link dla kortu %s - pole enabled musi być wartością logiczną.",
                kort_id,
            )
            continue

        try:
            hidden = coerce_bool((payload or {}).get("hidden"), default=False)
        except ValueError:
            logger.warning(
                "Pominięto link dla kortu %s - pole hidden musi być wartością logiczną.",
                kort_id,
            )
            continue
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
            enabled=enabled,
            hidden=hidden,
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
        link.kort_id: {
            "overlay": link.overlay_url,
            "control": link.control_url,
            "enabled": bool(link.enabled) if link.enabled is not None else True,
            "hidden": bool(link.hidden) if link.hidden is not None else False,
        }
        for link in get_overlay_links()
    }


def _control_test_payload_excerpt(response):
    if response is None:
        return ""

    try:
        parsed = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:CONTROL_TEST_MAX_PAYLOAD_CHARS]

    if isinstance(parsed, dict):
        excerpt_items = []
        for index, (key, value) in enumerate(parsed.items()):
            excerpt_items.append((key, value))
            if index + 1 >= CONTROL_TEST_MAX_PAYLOAD_KEYS:
                break
        excerpt_dict = {key: value for key, value in excerpt_items}
        return json.dumps(excerpt_dict, ensure_ascii=False, indent=2)

    return json.dumps(parsed, ensure_ascii=False)[:CONTROL_TEST_MAX_PAYLOAD_CHARS]


def _control_test_response_time_ms(response):
    elapsed = getattr(response, "elapsed", None)
    if elapsed is not None:
        try:
            total_seconds = elapsed.total_seconds()
        except Exception:  # noqa: BLE001
            total_seconds = None
        if total_seconds is not None:
            return round(total_seconds * 1000, 2)
    return None


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
        return None, None

    moment = None

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return text, None
    else:
        text = str(value).strip()
        if not text:
            return None, None
        return text, None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    moment_utc = moment.astimezone(timezone.utc)
    moment_warsaw = moment.astimezone(WARSAW_TIMEZONE)

    display_value = moment_warsaw.strftime("%H:%M %Z")
    utc_iso = moment_utc.isoformat().replace("+00:00", "Z")

    return display_value, utc_iso


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
    link_meta = link_meta or {}
    try:
        link_enabled = coerce_bool(link_meta.get("enabled"), default=True)
    except ValueError:
        link_enabled = True
    try:
        link_hidden = coerce_bool(link_meta.get("hidden"), default=False)
    except ValueError:
        link_hidden = False

    available = snapshot.get("available", True) if has_snapshot else False
    status = normalize_status(snapshot.get("status"), available, has_snapshot)
    if not link_enabled:
        available = False
        status = "disabled"

    status_label = STATUS_LABELS.get(status, status.replace("_", " ").capitalize())

    overlay_is_on = bool(available)
    overlay_label = "ON" if overlay_is_on else "OFF"
    last_updated_display, last_updated_iso = normalize_last_updated(
        snapshot.get("last_updated")
        or snapshot.get("updated_at")
        or snapshot.get("timestamp")
    )

    kort_label = (
        snapshot.get("court_name")
        or snapshot.get("kort_name")
        or snapshot.get("kort")
        or snapshot.get("court")
        or link_meta.get("name")
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

    pause_minutes = snapshot.get("pause_minutes")
    try:
        pause_minutes_value = int(pause_minutes) if pause_minutes is not None else None
    except (TypeError, ValueError):
        pause_minutes_value = None
    pause_active = bool(snapshot.get("pause_active"))
    pause_until = snapshot.get("pause_until")
    pause_label = None
    if pause_active:
        if pause_minutes_value is not None:
            pause_label = f"Pauza ({pause_minutes_value} min)"
        else:
            pause_label = "Pauza"

    badges_raw = snapshot.get("badges") or []
    normalized_badges: list[dict[str, str]] = []
    if isinstance(badges_raw, (list, tuple)):
        for badge in badges_raw:
            if isinstance(badge, dict):
                label = display_value(badge.get("label"), fallback=None)
                if not label:
                    continue
                normalized_badge = {"label": label}
                key = badge.get("key")
                if key is not None:
                    normalized_badge["key"] = str(key)
                else:
                    normalized_badge["key"] = "custom"
                description = display_value(badge.get("description"), fallback=None)
                if description:
                    normalized_badge["description"] = description
                normalized_badges.append(normalized_badge)
            else:
                label = display_value(badge, fallback=None)
                if label:
                    normalized_badges.append({"label": label, "key": "custom"})

    return {
        "kort_id": str(kort_id),
        "kort_label": display_name(kort_label, fallback=f"Kort {kort_id}" if kort_id else "Kort"),
        "status": status,
        "status_label": status_label,
        "available": available,
        "enabled": link_enabled,
        "hidden": link_hidden,
        "has_snapshot": has_snapshot,
        "overlay_is_on": overlay_is_on,
        "overlay_label": overlay_label,
        "last_updated": last_updated_display,
        "last_updated_iso": last_updated_iso,
        "players": players,
        "row_span": row_span,
        "score_summary": score_summary,
        "set_summary": set_summary,
        "pause_active": pause_active,
        "pause_minutes": pause_minutes_value,
        "pause_label": pause_label,
        "pause_until": pause_until,
        "badges": normalized_badges,
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

    try:
        normalized["enabled"] = coerce_bool((data or {}).get("enabled"), default=True)
    except ValueError:
        errors["enabled"] = "Pole enabled musi być wartością logiczną (true/false)."

    try:
        normalized["hidden"] = coerce_bool((data or {}).get("hidden"), default=False)
    except ValueError:
        errors["hidden"] = "Pole hidden musi być wartością logiczną (true/false)."

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
    ensure_overlay_links_schema()

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
        try:
            enabled = coerce_bool(payload.get("enabled"), default=True)
        except ValueError:
            logger.warning(
                "Pominięto link dla kortu %s - pole enabled musi być wartością logiczną.",
                kort_id_str,
            )
            continue

        try:
            hidden = coerce_bool(payload.get("hidden"), default=False)
        except ValueError:
            logger.warning(
                "Pominięto link dla kortu %s - pole hidden musi być wartością logiczną.",
                kort_id_str,
            )
            continue

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
            changed = False
            if existing.overlay_url != overlay_valid:
                existing.overlay_url = overlay_valid
                changed = True
            if existing.control_url != control_valid:
                existing.control_url = control_valid
                changed = True
            if bool(existing.enabled) != bool(enabled):
                existing.enabled = enabled
                changed = True
            if bool(existing.hidden) != bool(hidden):
                existing.hidden = hidden
                changed = True
            if changed:
                updated += 1
        else:
            db.session.add(
                OverlayLink(
                    kort_id=kort_id_str,
                    overlay_url=overlay_valid,
                    control_url=control_valid,
                    enabled=enabled,
                    hidden=hidden,
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
    link.enabled = payload["enabled"]
    link.hidden = payload["hidden"]
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


def build_wyniki_context():
    snapshots = load_snapshots()
    links = overlay_links_by_kort_id()

    hidden_ids = {str(kort_id) for kort_id, meta in links.items() if (meta or {}).get("hidden")}
    known_ids = {str(kort_id) for kort_id in links.keys() if str(kort_id) not in hidden_ids}
    known_ids.update(
        str(kort_id)
        for kort_id in snapshots.keys()
        if str(kort_id) not in hidden_ids
    )
    sorted_ids = sorted(known_ids, key=kort_id_sort_key)

    matches_by_status = {status: [] for status in STATUS_ORDER}

    for kort_id in sorted_ids:
        link_meta = links.get(kort_id)
        normalized = normalize_snapshot_entry(kort_id, snapshots.get(kort_id), link_meta)
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

    return {
        "sections": sections,
        "has_running_matches": has_running_matches,
        "has_non_running_snapshots": has_non_running_snapshots,
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@app.route("/wyniki")
def wyniki():
    context = build_wyniki_context()
    context.update(
        {
            "fragment_url": url_for("wyniki_fragment"),
            "live_api_url": url_for("wyniki_live_api"),
        }
    )
    return render_template("wyniki.html", **context)


@app.route("/wyniki/fragment")
def wyniki_fragment():
    context = build_wyniki_context()
    context.update(
        {
            "fragment_url": url_for("wyniki_fragment"),
            "live_api_url": url_for("wyniki_live_api"),
        }
    )
    return render_template("partials/wyniki_sections.html", **context)


@app.route("/api/wyniki/live")
def wyniki_live_api():
    context = build_wyniki_context()
    context.update(
        {
            "fragment_url": url_for("wyniki_fragment"),
            "live_api_url": url_for("wyniki_live_api"),
        }
    )
    return jsonify(context)


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
        if (data or {}).get("hidden"):
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
                "last_updated_iso": normalized["last_updated_iso"],
                "pause_active": normalized["pause_active"],
                "pause_label": normalized["pause_label"],
                "pause_minutes": normalized["pause_minutes"],
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


@app.route("/kort/<int:kort_id>/test", methods=["POST"])
def kort_control_test(kort_id):
    kort_id = str(kort_id)

    links = overlay_links_by_kort_id()
    link = links.get(kort_id)
    if not link:
        return f"Nieznany kort {kort_id}", 404

    control_url = link.get("control")
    if not control_url:
        result = {
            "status": "error",
            "command": CONTROL_TEST_COMMAND,
            "remote_status": None,
            "response_time_ms": None,
            "payload_excerpt": "",
            "error": "Brak skonfigurowanego adresu panelu sterowania.",
        }
        return render_template("partials/control_test_result.html", result=result)

    try:
        api_url = build_output_url(control_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nie udało się przygotować adresu testowego dla kortu %s: %s",
            kort_id,
            exc,
        )
        result = {
            "status": "error",
            "command": CONTROL_TEST_COMMAND,
            "remote_status": None,
            "response_time_ms": None,
            "payload_excerpt": "",
            "error": "Nieprawidłowy adres panelu sterowania.",
        }
        return render_template("partials/control_test_result.html", result=result)

    payload = {"command": CONTROL_TEST_COMMAND}
    response = None
    start = time.perf_counter()

    try:
        response = requests.put(
            api_url,
            json=payload,
            timeout=CONTROL_TEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.warning(
            "Test komendy kontrolnej dla kortu %s zakończył się błędem: %s",
            kort_id,
            exc,
        )
        result = {
            "status": "error",
            "command": CONTROL_TEST_COMMAND,
            "remote_status": None,
            "response_time_ms": duration_ms,
            "payload_excerpt": "",
            "error": str(exc),
        }
        return render_template("partials/control_test_result.html", result=result)

    duration_ms = _control_test_response_time_ms(response)
    if duration_ms is None:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

    payload_excerpt = _control_test_payload_excerpt(response)

    status = "ok" if response.ok else "remote_error"
    error_message = None
    if not response.ok:
        error_message = f"Serwer zwrócił status {response.status_code}."

    result = {
        "status": status,
        "command": CONTROL_TEST_COMMAND,
        "remote_status": response.status_code,
        "response_time_ms": duration_ms,
        "payload_excerpt": payload_excerpt,
        "error": error_message,
    }

    return render_template("partials/control_test_result.html", result=result)


@app.route("/kort/all")
def overlay_all():
    """Renderuje widok z czterema kortami rozmieszczonymi w rogach."""
    overlay_config = load_config()

    overlays = []
    sorted_overlays = [link.to_dict() for link in get_overlay_links() if not link.hidden]
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
                "last_updated_iso": normalized["last_updated_iso"],
                "pause_active": normalized["pause_active"],
                "pause_label": normalized["pause_label"],
                "pause_minutes": normalized["pause_minutes"],
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
