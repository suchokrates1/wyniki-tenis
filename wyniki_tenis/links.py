"""Overlay link persistence and validation helpers."""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterable, Tuple
from urllib.parse import urlparse

from flask import current_app, has_app_context

from .constants import APP_OVERLAYS_HOST, LINKS_PATH
from .extensions import db
from .models import OverlayLink

logger = logging.getLogger(__name__)


PathOption = Tuple[str, str]


def _path_has_identifier(path: str, prefix: str) -> bool:
    if not path.startswith(prefix):
        return False
    remainder = path[len(prefix) :]
    return bool(remainder) and "/" not in remainder


def _validate_app_overlays_url(url: str | None, *, field_label: str, path_options: Iterable[PathOption]):
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


def validate_overlay_url(url: str | None):
    return _validate_app_overlays_url(
        url,
        field_label="Adres overlayu",
        path_options=(("/output/", "/output/{id}"),),
    )


def validate_control_url(url: str | None):
    return _validate_app_overlays_url(
        url,
        field_label="Adres panelu sterowania",
        path_options=(
            ("/control/", "/control/{id}"),
            ("/controlapps/", "/controlapps/{id}"),
        ),
    )


def overlay_link_sort_key(link: OverlayLink):
    kort_id = link.kort_id
    if isinstance(kort_id, str) and kort_id.isdigit():
        return (0, int(kort_id))
    try:
        return (0, int(kort_id))
    except (TypeError, ValueError):
        pass
    return (1, str(kort_id))


def get_links_path() -> str:
    if has_app_context():
        configured = current_app.config.get("LINKS_PATH")
        if configured:
            return str(configured)
    try:
        from main import LINKS_PATH as main_links_path  # type: ignore
    except Exception:
        main_links_path = LINKS_PATH
    return str(main_links_path)


def ensure_overlay_links_seeded() -> None:
    db.create_all()
    if OverlayLink.query.first() is not None:
        return

    links_path = get_links_path()

    if not os.path.exists(links_path):
        logger.debug("Pomijam seedowanie linków - brak pliku %s", links_path)
        return

    with open(links_path, "r", encoding="utf-8") as handle:
        data = json.load(handle) or {}

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


def get_overlay_links() -> list[OverlayLink]:
    ensure_overlay_links_seeded()
    links = OverlayLink.query.order_by(OverlayLink.kort_id.asc()).all()
    return sorted(links, key=overlay_link_sort_key)


def overlay_links_by_kort_id() -> Dict[str, Dict[str, str]]:
    return {
        link.kort_id: {"overlay": link.overlay_url, "control": link.control_url}
        for link in get_overlay_links()
    }


def validate_overlay_link_data(data: Dict[str, str] | None):
    errors: Dict[str, str] = {}
    normalized: Dict[str, str] = {}

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


__all__ = [
    "ensure_overlay_links_seeded",
    "get_links_path",
    "get_overlay_links",
    "overlay_links_by_kort_id",
    "validate_control_url",
    "validate_overlay_link_data",
    "validate_overlay_url",
]
