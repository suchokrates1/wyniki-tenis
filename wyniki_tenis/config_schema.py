"""Data shaping utilities for overlay configuration."""

from __future__ import annotations

import copy
from typing import Any, Dict

from .constants import CORNER_POSITION_STYLES, CORNERS, DEFAULT_BASE_CONFIG
from .utils import as_float, as_int


ConfigDict = Dict[str, Any]


def get_default_corner_config(corner: str) -> Dict[str, Any]:
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


def merge_corner_config(default_corner: Dict[str, Any], override: Dict[str, Any] | None) -> Dict[str, Any]:
    result = copy.deepcopy(default_corner)
    if not override:
        return result

    for key, value in override.items():
        if key == "label":
            result.setdefault("label", {}).update(value or {})
        else:
            result[key] = value
    return result


def normalize_corner_types(corner: Dict[str, Any]) -> Dict[str, Any]:
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


def ensure_config_structure(config: ConfigDict | None) -> ConfigDict:
    config = dict(config or {})

    for key, default_value in DEFAULT_BASE_CONFIG.items():
        config[key] = config.get(key, default_value)

    config["view_width"] = as_int(config.get("view_width"), DEFAULT_BASE_CONFIG["view_width"])
    config["view_height"] = as_int(config.get("view_height"), DEFAULT_BASE_CONFIG["view_height"])
    config["display_scale"] = as_float(config.get("display_scale"), DEFAULT_BASE_CONFIG["display_scale"])
    config["left_offset"] = as_int(config.get("left_offset"), DEFAULT_BASE_CONFIG["left_offset"])
    config["label_position"] = config.get("label_position", DEFAULT_BASE_CONFIG["label_position"])

    existing_kort_all = config.get("kort_all") or {}
    ensured_kort_all: Dict[str, Any] = {}

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


def build_label_style(label_config: Dict[str, Any] | None) -> str:
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


__all__ = [
    "ConfigDict",
    "build_label_style",
    "ensure_config_structure",
    "get_default_corner_config",
    "merge_corner_config",
    "normalize_corner_types",
]
