"""Terrain stamp files: placed texture stamps saved apart from the map.

A ``.stamp`` file is a delta: just the stamps placed on a terrain -- each one's
id, texture, world X/Z position, radius, angle, feather and opacity. It holds
no terrain height, sculpting or baked pixels, so the stamps can be laid back
onto a map whose height has changed since; importing bakes them into that
terrain's paint.

Importing is idempotent: stamps are matched by id, so a stamp already on the
terrain is left alone (or moved back to the file's position if it was moved
since) and applying the same file twice never duplicates a stamp.
"""

import json
import os
from typing import Optional

from .fileio import write_json_atomic

STAMP_FORMAT = "terrain_stamp"
STAMP_VERSION = 1
STAMP_EXTENSION = ".stamp"

_STAMP_FIELDS = ("id", "texture", "x", "z", "radius", "angle", "feather", "opacity")


class StampError(ValueError):
    """A stamp file that cannot be read."""


def default_stamp_path(map_path: Optional[str], fallback_dir: str = "") -> str:
    """``<map dir>/<map name>.stamp`` for a map file, else ``untitled.stamp``."""
    if map_path:
        base, _ext = os.path.splitext(os.path.abspath(map_path))
        return base + STAMP_EXTENSION
    return os.path.join(fallback_dir or os.getcwd(), "untitled" + STAMP_EXTENSION)


def export_stamp(terrain) -> dict:
    """The terrain's placed stamps as a JSON-ready stamp file dict."""
    return {
        "format": STAMP_FORMAT,
        "version": STAMP_VERSION,
        "stamps": [{key: stamp[key] for key in _STAMP_FIELDS}
                   for stamp in terrain.paint_stamps],
    }


def save_stamp_file(terrain, path: str) -> dict:
    """Write the terrain's placed stamps to ``path``; returns the file dict."""
    data = export_stamp(terrain)
    write_json_atomic(path, data, indent=1)
    return data


def load_stamp_file(path: str) -> dict:
    """Read and validate a stamp file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StampError(f"Could not read stamp file: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != STAMP_FORMAT:
        raise StampError("Not a terrain stamp file")
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > STAMP_VERSION:
        raise StampError(f"Unsupported stamp version: {data.get('version')!r}")
    if not isinstance(data.get("stamps", []), list):
        raise StampError("Stamp file has no stamp list")
    return data


def apply_stamp(terrain, data: dict) -> int:
    """Merge a stamp file dict into ``terrain``; returns stamps added or moved."""
    return terrain.merge_paint_stamps(data.get("stamps", []))


def apply_stamp_file(terrain, path: str) -> int:
    """Load ``path`` and merge its stamps into ``terrain``."""
    return apply_stamp(terrain, load_stamp_file(path))
