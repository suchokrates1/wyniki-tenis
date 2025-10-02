"""Flask views for the tennis overlay application."""

from __future__ import annotations

import json
import logging
import os
from typing import Dict

from flask import Blueprint, current_app, jsonify, render_template, request, url_for

from .auth import requires_config_auth
from .config_schema import build_label_style, get_default_corner_config
from .config_storage import load_config, save_config
from .constants import (
    CORNER_LABELS,
    CORNER_POSITION_STYLES,
    CORNERS,
    STATUS_ORDER,
    STATUS_VIEW_META,
)
from .links import (
    ensure_overlay_links_seeded,
    get_links_path,
    get_overlay_links,
    overlay_links_by_kort_id,
    validate_overlay_link_data,
    validate_control_url,
    validate_overlay_url,
)
from .extensions import db
from .models import OverlayLink
from .snapshots import load_snapshots, normalize_snapshot_entry
from .utils import as_float, as_int, kort_id_sort_key

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

LEGACY_ENDPOINTS = (
    ("index", "index", "/", ["GET"]),
    ("wyniki", "wyniki", "/wyniki", ["GET"]),
    ("overlay_kort", "overlay_kort", "/kort/<int:kort_id>", ["GET"]),
    ("overlay_all", "overlay_all", "/kort/all", ["GET"]),
    ("config", "config", "/config", ["GET", "POST"]),
    ("overlay_links_api", "overlay_links_api", "/api/overlay-links", ["GET", "POST"]),
    (
        "overlay_links_reload",
        "overlay_links_reload",
        "/api/overlay-links/reload",
        ["POST"],
    ),
    (
        "overlay_link_detail_api",
        "overlay_link_detail_api",
        "/api/overlay-links/<int:link_id>",
        ["GET", "PUT", "DELETE"],
    ),
    ("overlay_links_page", "overlay_links_page", "/overlay-links", ["GET"]),
)


def render_config(config_dict):
    return render_template(
        "config.html",
        config=config_dict,
        corners=CORNERS,
        corner_labels=CORNER_LABELS,
        corner_positions=CORNER_POSITION_STYLES,
    )


@bp.route("/")
def index():
    links = overlay_links_by_kort_id()
    links_management_url = None
    if "main.overlay_links_page" in current_app.view_functions:
        links_management_url = url_for("main.overlay_links_page")

    return render_template(
        "index.html",
        links=links,
        links_management_url=links_management_url,
    )


@bp.route("/wyniki")
def wyniki():
    snapshots = load_snapshots()
    links = overlay_links_by_kort_id()

    known_ids = {str(kort_id) for kort_id in links.keys()}
    known_ids.update(str(kort_id) for kort_id in snapshots.keys())
    sorted_ids = sorted(known_ids, key=kort_id_sort_key)

    matches_by_status: Dict[str, list] = {status: [] for status in STATUS_ORDER}

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


@bp.route("/kort/<int:kort_id>")
def overlay_kort(kort_id: int):
    kort_id_str = str(kort_id)

    links_by_id = overlay_links_by_kort_id()

    if kort_id_str not in links_by_id:
        return f"Nieznany kort {kort_id_str}", 404

    overlay_config = load_config()
    kort_all_config = overlay_config.get("kort_all", {})
    mini_config = kort_all_config.get("top_left") or get_default_corner_config("top_left")
    mini_label_style = build_label_style(mini_config.get("label"))

    snapshots = load_snapshots()
    main_overlay = links_by_id[kort_id_str]["overlay"]
    main_snapshot = normalize_snapshot_entry(
        kort_id_str,
        snapshots.get(kort_id_str),
        links_by_id.get(kort_id_str),
    )

    mini = []
    for mini_id, data in links_by_id.items():
        if mini_id == kort_id_str:
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
        kort_id=kort_id_str,
        main_overlay=main_overlay,
        main_snapshot=main_snapshot,
        mini_overlays=mini,
        config=overlay_config,
        mini_config=mini_config,
        mini_label_style=mini_label_style,
    )


@bp.route("/kort/all")
def overlay_all():
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


@bp.route("/config", methods=["GET", "POST"])
@requires_config_auth
def config():
    current_config = load_config()

    if request.method == "POST":
        form = request.form

        data = {
            "view_width": as_int(form.get("view_width", current_config["view_width"]), current_config["view_width"]),
            "view_height": as_int(form.get("view_height", current_config["view_height"]), current_config["view_height"]),
            "display_scale": as_float(
                form.get("display_scale", current_config["display_scale"]), current_config["display_scale"]
            ),
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
                "view_height": as_int(
                    form.get(f"{prefix}[view_height]", existing_corner["view_height"]), existing_corner["view_height"]
                ),
                "display_scale": as_float(
                    form.get(f"{prefix}[display_scale]", existing_corner["display_scale"]), existing_corner["display_scale"]
                ),
                "offset_x": as_int(form.get(f"{prefix}[offset_x]", existing_corner["offset_x"]), existing_corner["offset_x"]),
                "offset_y": as_int(form.get(f"{prefix}[offset_y]", existing_corner["offset_y"]), existing_corner["offset_y"]),
                "label": {
                    "position": form.get(f"{label_prefix}[position]", existing_corner["label"]["position"]),
                    "offset_x": as_int(
                        form.get(f"{label_prefix}[offset_x]", existing_corner["label"]["offset_x"]),
                        existing_corner["label"]["offset_x"],
                    ),
                    "offset_y": as_int(
                        form.get(f"{label_prefix}[offset_y]", existing_corner["label"]["offset_y"]),
                        existing_corner["label"]["offset_y"],
                    ),
                },
            }

        data["kort_all"] = kort_all
        saved_config = save_config(data)

        return render_config(saved_config)

    return render_config(current_config)


@bp.route("/api/overlay-links", methods=["GET", "POST"])
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


@bp.route("/api/overlay-links/reload", methods=["POST"])
@requires_config_auth
def overlay_links_reload():
    db.create_all()

    links_path = get_links_path()

    if not os.path.exists(links_path):
        return jsonify({"error": f"Brak pliku {links_path}."}), 404

    try:
        with open(links_path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except json.JSONDecodeError as exc:
        logger.error("Nie udało się odczytać pliku %s: %s", links_path, exc)
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


@bp.route("/api/overlay-links/<int:link_id>", methods=["GET", "PUT", "DELETE"])
def overlay_link_detail_api(link_id: int):
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


@bp.route("/overlay-links")
def overlay_links_page():
    links = [link.to_dict() for link in get_overlay_links()]
    return render_template("overlay_links.html", links=links)


__all__ = [
    "bp",
    "LEGACY_ENDPOINTS",
    "config",
    "index",
    "overlay_all",
    "overlay_kort",
    "overlay_link_detail_api",
    "overlay_links_api",
    "overlay_links_page",
    "overlay_links_reload",
    "render_config",
    "wyniki",
]
