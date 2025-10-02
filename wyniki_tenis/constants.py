"""Application wide constants and metadata."""

from __future__ import annotations

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

APP_OVERLAYS_HOST = "app.overlays.uno"
LINKS_PATH = "overlay_links.json"

__all__ = [
    "ACTIVE_STATUSES",
    "APP_OVERLAYS_HOST",
    "CORNER_LABELS",
    "CORNER_POSITION_STYLES",
    "CORNERS",
    "DEFAULT_BASE_CONFIG",
    "FINISHED_STATUSES",
    "LINKS_PATH",
    "NO_DATA_STATUSES",
    "STATUS_LABELS",
    "STATUS_ORDER",
    "STATUS_VIEW_META",
    "UNAVAILABLE_STATUSES",
]
