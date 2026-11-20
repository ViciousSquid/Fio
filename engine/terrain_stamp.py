"""Terrain paint files: a map's terrain paint and placed stamps, kept apart
from the map.

A ``.paint`` file holds a terrain's paint -- the baked paint and the paint no
stamp owns -- and its placed stamps: each one's id, texture, world X/Z
position, radius, angle, feather and opacity. It holds no terrain height or
sculpting. A saved map keeps its paint in ``<map name>.paint`` beside it and
not in its own JSON (see "the map's paint file" below).

Import lays a file's *stamps* onto the terrain being edited, so they can be
put back on a map whose height has changed since; importing bakes them into
that terrain's paint. (A stamps-only file from an earlier build, format
``terrain_stamp``, imports the same way.)

Importing is idempotent: stamps are matched by id, so a stamp already on the
terrain is left alone (or moved back to the file's position if it was moved
since) and applying the same file twice never duplicates a stamp.
"""

import json
import os
from typing import Optional

from .fileio import write_json_atomic

STAMP_FORMAT = "terrain_stamp"          # stamps only (earlier builds)
PAINT_FORMAT = "terrain_paint"          # paint and stamps
_FORMATS = (STAMP_FORMAT, PAINT_FORMAT)
STAMP_VERSION = 1
STAMP_EXTENSION = ".paint"

_STAMP_FIELDS = ("id", "texture", "x", "z", "radius", "angle", "feather", "opacity")


class StampError(ValueError):
    """A stamp file that cannot be read."""


def default_stamp_path(map_path: Optional[str], fallback_dir: str = "") -> str:
    """``<map dir>/<map name>.paint`` for a map file, else ``untitled.paint``."""
    if map_path:
        base, _ext = os.path.splitext(os.path.abspath(map_path))
        return base + STAMP_EXTENSION
    return os.path.join(fallback_dir or os.getcwd(), "untitled" + STAMP_EXTENSION)


def export_stamp(terrain) -> dict:
    """The terrain's paint and placed stamps as a JSON-ready paint file dict
    -- what saving the map writes beside it."""
    to_dict = getattr(terrain, 'to_dict', None)
    if to_dict is None:                     # a bare list of stamps
        return paint_file({STAMPS_KEY: list(terrain.paint_stamps)})
    return paint_file(to_dict())


def save_stamp_file(terrain, path: str) -> dict:
    """Write the terrain's paint file to ``path`` (minified); returns it."""
    data = export_stamp(terrain)
    write_json_atomic(path, data, separators=(",", ":"))
    return data


def load_stamp_file(path: str) -> dict:
    """Read and validate a stamp file."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StampError(f"Could not read stamp file: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") not in _FORMATS:
        raise StampError("Not a terrain paint file")
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > STAMP_VERSION:
        raise StampError(f"Unsupported stamp version: {data.get('version')!r}")
    if not isinstance(data.get("stamps", []), list):
        raise StampError("Stamp file has no stamp list")
    return data


# -- the map's paint file --------------------------------------------------------
#
# A saved map keeps its terrain paint beside it, in ``<map name>.paint``
# (minified JSON), not in the map's JSON: the baked paint (what is drawn), the
# paint no stamp owns, and the placed stamps themselves. It is a delta over the
# map: whoever loads the map -- the editor, the engine reading a .fiopak, the
# Android player -- lays the paint file over the map's terrain. A map without
# its paint file is the same map with unpainted ground.

TERRAIN_KEY = 'terrain_data'
STAMPS_KEY = 'paint_stamps'
#: The terrain dict's paint, as ``Terrain.to_dict`` writes it.
PAINT_KEYS = ('texture_paint_resolution', 'texture_paint', 'texture_paint_base')
#: Set in a saved map's terrain when it has a paint file beside it.
MARKER_KEY = 'paint_file'
#: Everything that lives in the paint file instead of the map.
_MOVED_KEYS = PAINT_KEYS + (STAMPS_KEY,)
#: Read as well as :data:`STAMP_EXTENSION` beside a map (earlier builds).
LEGACY_EXTENSIONS = ('.terrainstamps',)


def paint_file(terrain_dict: dict) -> dict:
    """A paint file's contents for a terrain dict (``Terrain.to_dict``)."""
    return {
        "format": PAINT_FORMAT,
        "version": STAMP_VERSION,
        "stamps": [dict(stamp) for stamp in terrain_dict.get(STAMPS_KEY) or []],
        "paint": {key: terrain_dict[key] for key in PAINT_KEYS
                  if terrain_dict.get(key)},
    }


def split_level(level: dict):
    """``(level for the map file, paint file dict or None)``.

    The paint and the stamps are taken out of a shallow copy of the level's
    terrain dict; *level* itself is not changed. ``None`` when the level has
    no terrain to keep paint for.
    """
    terrain = level.get(TERRAIN_KEY) if isinstance(level, dict) else None
    if not isinstance(terrain, dict):
        return level, None
    data = paint_file(terrain)
    if any(key in terrain for key in _MOVED_KEYS) or MARKER_KEY in terrain:
        level = dict(level)
        kept = {key: value for key, value in terrain.items()
                if key not in _MOVED_KEYS and key != MARKER_KEY}
        if not _is_empty(data):
            # The map says it has a paint file, so loading it without one
            # can say what is missing (missing_paint_file).
            kept[MARKER_KEY] = True
        level[TERRAIN_KEY] = kept
    return level, data


def _is_empty(data) -> bool:
    return not data.get("stamps") and not data.get("paint")


def write_map_stamps(map_path: str, data) -> Optional[str]:
    """Write a map's paint file beside it, minified. With no paint there is
    nothing to keep: no file is made, and one already there is emptied so it
    cannot bring cleared paint back. Returns the path written, if any.

    That includes an earlier build's ``.terrainstamps`` (LEGACY_EXTENSIONS):
    it is read when no ``.paint`` is beside the map, so clearing the paint of
    a map that still has one used to bring its stamps back on the next load.
    An empty ``.paint`` is written over it instead, which is read first; the
    legacy file itself is the user's and is left alone."""
    if data is None:
        return None
    path = default_stamp_path(map_path)
    if _is_empty(data) and paint_path(map_path) is None:
        return None
    write_json_atomic(path, data, separators=(",", ":"))
    return path


def paint_path(map_path: str) -> Optional[str]:
    """The paint file beside *map_path*, if there is one."""
    base, _ext = os.path.splitext(os.path.abspath(map_path))
    for extension in (STAMP_EXTENSION,) + LEGACY_EXTENSIONS:
        if os.path.exists(base + extension):
            return base + extension
    return None


def missing_paint_file(level: dict, map_path: Optional[str]) -> Optional[str]:
    """The name of the paint file a map says it has but that is not beside
    it (``<map name>.paint``), or None. Ask before :func:`join_level`."""
    terrain = level.get(TERRAIN_KEY) if isinstance(level, dict) else None
    if not isinstance(terrain, dict) or not terrain.get(MARKER_KEY) or not map_path:
        return None
    if paint_path(map_path) is not None:
        return None
    return os.path.basename(default_stamp_path(map_path))


def missing_paint_message(name: str) -> str:
    return f"{name} was not found so this map will not display paint/stamps"


def join_level(level: dict, map_path: Optional[str]) -> dict:
    """Give a level read from *map_path* the paint saved beside it.

    A map that still carries its paint (saved before it moved out) keeps it.
    A missing or unreadable paint file leaves the map as it is: unpainted.
    """
    if not _wants_paint(level) or not map_path:
        return level
    path = paint_path(map_path)
    if path is None:
        return level
    try:
        data = load_stamp_file(path)
    except StampError as exc:
        print(f"[Terrain] {os.path.basename(path)} was not used: {exc}")
        return level
    return apply_stamp_delta(level, data)


def _wants_paint(level) -> bool:
    """A level with a terrain that does not already carry paint or stamps."""
    terrain = level.get(TERRAIN_KEY) if isinstance(level, dict) else None
    return isinstance(terrain, dict) and not any(
        terrain.get(key) for key in _MOVED_KEYS)


def apply_stamp_delta(level: dict, data) -> dict:
    """Lay a paint file over a level document: the map's terrain gets the
    paint and the stamps the file holds. *data* is a parsed paint file;
    anything else, or a level that already carries paint, leaves the level
    as it is."""
    if not _wants_paint(level) or not isinstance(data, dict) \
            or data.get("format") not in _FORMATS:
        return level
    terrain = level[TERRAIN_KEY]
    terrain.pop(MARKER_KEY, None)           # the file's own note; it is here
    paint = data.get("paint")
    if isinstance(paint, dict):
        for key in PAINT_KEYS:
            if paint.get(key):
                terrain[key] = paint[key]
    stamps = data.get("stamps")
    stamps = [s for s in stamps if isinstance(s, dict)] if isinstance(stamps, list) else []
    if stamps:
        terrain[STAMPS_KEY] = stamps
    return level


def stamp_member(map_member: str) -> str:
    """The archive name of a packaged map's paint file: beside the map."""
    base, _ext = os.path.splitext(str(map_member))
    return base + STAMP_EXTENSION


def stamp_file_text(data: dict) -> str:
    """A paint file's contents: minified JSON."""
    return json.dumps(data, separators=(",", ":"))


def apply_stamp(terrain, data: dict) -> int:
    """Merge a stamp file dict into ``terrain``; returns stamps added or moved."""
    stamps = data.get("stamps") if isinstance(data, dict) else None
    return terrain.merge_paint_stamps(stamps if isinstance(stamps, list) else [])


def apply_stamp_file(terrain, path: str) -> int:
    """Load ``path`` and merge its stamps into ``terrain``."""
    return apply_stamp(terrain, load_stamp_file(path))
