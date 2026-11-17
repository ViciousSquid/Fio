"""Terrain stamp files: placed texture stamps saved apart from the map.

A ``.terrainstamps`` file is a delta: just the stamps placed on a terrain -- each one's
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
STAMP_EXTENSION = ".terrainstamps"

_STAMP_FIELDS = ("id", "texture", "x", "z", "radius", "angle", "feather", "opacity")


class StampError(ValueError):
    """A stamp file that cannot be read."""


def default_stamp_path(map_path: Optional[str], fallback_dir: str = "") -> str:
    """``<map dir>/<map name>.terrainstamps`` for a map file, else ``untitled.terrainstamps``."""
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
        with open(path, "r", encoding="utf-8-sig") as fh:
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


# -- the map's own stamp file ---------------------------------------------------
#
# A saved map keeps its placed stamps beside it, in ``<map name>.terrainstamps``
# (minified JSON), not in the map's JSON. The map still holds the terrain's
# baked paint, so it draws -- in the editor, in Play, packaged, on Android --
# with or without the stamp file; the file is what makes each stamp a thing
# that can still be selected, moved and removed.

TERRAIN_KEY = 'terrain_data'
STAMPS_KEY = 'paint_stamps'


def split_level(level: dict):
    """``(level for the map file, stamp file dict or None)``.

    The stamp records are taken out of a shallow copy of the level's terrain
    dict, each written whole (``tiled`` included), so reading them back gives
    the terrain exactly what it saved. *level* itself is not changed. ``None``
    when the level has no terrain to keep stamps for.
    """
    terrain = level.get(TERRAIN_KEY) if isinstance(level, dict) else None
    if not isinstance(terrain, dict):
        return level, None
    stamps = terrain.get(STAMPS_KEY) or []
    if STAMPS_KEY in terrain:
        terrain = {key: value for key, value in terrain.items() if key != STAMPS_KEY}
        level = dict(level)
        level[TERRAIN_KEY] = terrain
    return level, {
        "format": STAMP_FORMAT,
        "version": STAMP_VERSION,
        "stamps": [dict(stamp) for stamp in stamps],
    }


def write_map_stamps(map_path: str, stamps) -> Optional[str]:
    """Write a map's stamp file beside it, minified. With no stamps there is
    nothing to keep: no file is made, and one already there is emptied so it
    cannot bring removed stamps back. Returns the path written, if any."""
    if stamps is None:
        return None
    path = default_stamp_path(map_path)
    if not stamps.get("stamps") and not os.path.exists(path):
        return None
    write_json_atomic(path, stamps, separators=(",", ":"))
    return path


def join_level(level: dict, map_path: Optional[str]) -> dict:
    """Give a level read from *map_path* the stamps saved beside it.

    A map that still carries its stamps (saved before they moved out) keeps
    them. A missing or unreadable stamp file leaves the map as it is: its
    paint is all there, just not as separate stamps.
    """
    terrain = level.get(TERRAIN_KEY) if isinstance(level, dict) else None
    if not isinstance(terrain, dict) or terrain.get(STAMPS_KEY) or not map_path:
        return level
    path = default_stamp_path(map_path)
    if not os.path.exists(path):
        return level
    try:
        stamps = [s for s in load_stamp_file(path).get("stamps", [])
                  if isinstance(s, dict)]
    except StampError as exc:
        print(f"[Terrain] {os.path.basename(path)} was not used: {exc}")
        return level
    if stamps:
        terrain[STAMPS_KEY] = stamps
    return level


def apply_stamp(terrain, data: dict) -> int:
    """Merge a stamp file dict into ``terrain``; returns stamps added or moved."""
    return terrain.merge_paint_stamps(data.get("stamps", []))


def apply_stamp_file(terrain, path: str) -> int:
    """Load ``path`` and merge its stamps into ``terrain``."""
    return apply_stamp(terrain, load_stamp_file(path))
