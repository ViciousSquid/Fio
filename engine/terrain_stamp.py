"""Terrain stamp files: authored terrain work saved apart from the map.

A ``.stamp`` file holds the hand-authored layers of a terrain -- sculpt
offsets, baked texture paint and CSG cuts -- without the generated base
(biome, seed, heightmap, bounds). Exporting one preserves that work while the
underlying map keeps changing; importing it merges the work back in.

Merging is idempotent: every authored cell in the stamp *replaces* the
matching cell of the terrain rather than adding to it, so applying the same
stamp twice, or applying it to a map that still carries the same work, leaves
the terrain unchanged.

* Sculpt offsets: the stamp's offset for a grid cell overwrites the terrain's.
  Cells the stamp does not touch keep their current offset.
* Texture paint: every texel the stamp has paint on (alpha > 0) replaces the
  terrain texel. A stamp texel already holds the full composite of every
  stroke made over it, so nothing is lost; unpainted texels are left alone.
* CSG cuts: added only if an identical cut is not already present.

Stamps written against a different chunk size, terrain offset or sculpt grid
are resampled onto the target terrain's grids by world position.
"""

import base64
import json
import math
import os
import zlib
from typing import Dict, Optional, Tuple

import numpy as np

STAMP_FORMAT = "terrain_stamp"
STAMP_VERSION = 1
STAMP_EXTENSION = ".stamp"


class StampError(ValueError):
    """A stamp file that cannot be read or applied."""


def default_stamp_path(map_path: Optional[str], fallback_dir: str = "") -> str:
    """``<map dir>/<map name>.stamp`` for a map file, else ``untitled.stamp``."""
    if map_path:
        base, _ext = os.path.splitext(os.path.abspath(map_path))
        return base + STAMP_EXTENSION
    return os.path.join(fallback_dir or os.getcwd(), "untitled" + STAMP_EXTENSION)


def has_authored_work(terrain) -> bool:
    """True when the terrain carries anything a stamp would save."""
    return bool(terrain.sculpt_offsets or terrain.csg_subtractions
                or any(paint is not None and np.any(paint[..., 3])
                       for paint in terrain.texture_paint_maps.values()))


# -- Export --------------------------------------------------------------------

def export_stamp(terrain) -> dict:
    """The terrain's authored layers as a JSON-ready stamp dict."""
    return {
        "format": STAMP_FORMAT,
        "version": STAMP_VERSION,
        "chunk_size": float(terrain.chunk_size),
        "offset_x": float(terrain.offset_x),
        "offset_z": float(terrain.offset_z),
        "sculpt_grid_resolution": float(terrain.sculpt_grid_resolution),
        "sculpt_offsets": [
            [int(gx), int(gz), float(val)]
            for (gx, gz), val in sorted(terrain.sculpt_offsets.items())
        ],
        "texture_paint_resolution": int(terrain.TERRAIN_PAINT_RESOLUTION),
        "texture_paint": terrain._serialize_texture_paint(),
        "csg_subtractions": [list(cut) for cut in terrain.csg_subtractions],
    }


def save_stamp_file(terrain, path: str) -> dict:
    """Write the terrain's authored layers to ``path``; returns the stamp."""
    stamp = export_stamp(terrain)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(stamp, fh, separators=(",", ":"))
    os.replace(tmp_path, path)
    return stamp


# -- Import --------------------------------------------------------------------

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
    return data


def _float(data, key, default):
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _decode_sculpt(data) -> Dict[Tuple[int, int], float]:
    offsets = {}
    for entry in data.get("sculpt_offsets", []) or []:
        try:
            gx, gz, val = entry
            val = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val):
            offsets[(int(gx), int(gz))] = val
    return offsets


def _decode_paint(data, res: int) -> Dict[Tuple[int, int], np.ndarray]:
    maps = {}
    expected = res * res * 4
    for entry in data.get("texture_paint", []) or []:
        if not isinstance(entry, dict):
            continue
        coord = entry.get("coord", [])
        payload = entry.get("data", "")
        if not isinstance(coord, (list, tuple)) or len(coord) != 2 or not payload:
            continue
        try:
            raw = zlib.decompress(base64.b64decode(payload))
            key = (int(coord[0]), int(coord[1]))
        except (TypeError, ValueError, zlib.error):
            continue
        if len(raw) != expected:
            continue
        maps[key] = np.frombuffer(raw, dtype=np.uint8).reshape(res, res, 4)
    return maps


def _merge_sculpt(terrain, stamp_offsets, stamp_res) -> int:
    """Overwrite terrain sculpt cells with the stamp's; returns cells changed."""
    if not stamp_offsets:
        return 0
    target = terrain.sculpt_offsets
    res = float(terrain.sculpt_grid_resolution)
    if math.isclose(stamp_res, res):
        resampled = stamp_offsets
    else:
        # Bilinear resample onto the terrain's grid, writing only where the
        # stamp has data in at least one surrounding cell.
        resampled = {}
        keys = np.array(list(stamp_offsets.keys()), dtype=np.int64)
        lo = keys.min(axis=0) * stamp_res
        hi = keys.max(axis=0) * stamp_res
        for gx in range(int(math.floor(lo[0] / res)), int(math.ceil(hi[0] / res)) + 1):
            for gz in range(int(math.floor(lo[1] / res)), int(math.ceil(hi[1] / res)) + 1):
                sx, sz = gx * res / stamp_res, gz * res / stamp_res
                x0, z0 = int(math.floor(sx)), int(math.floor(sz))
                fx, fz = sx - x0, sz - z0
                corners = (
                    ((x0, z0), (1 - fx) * (1 - fz)),
                    ((x0 + 1, z0), fx * (1 - fz)),
                    ((x0, z0 + 1), (1 - fx) * fz),
                    ((x0 + 1, z0 + 1), fx * fz),
                )
                if not any(key in stamp_offsets and w > 0 for key, w in corners):
                    continue
                resampled[(gx, gz)] = sum(
                    stamp_offsets.get(key, 0.0) * w for key, w in corners)

    changed = 0
    for key, val in resampled.items():
        if target.get(key) != val:
            target[key] = val
            changed += 1
    if changed:
        terrain._touch_sculpt()
    return changed


def _merge_paint_aligned(terrain, stamp_maps) -> int:
    changed = 0
    res = terrain.TERRAIN_PAINT_RESOLUTION
    for key, src in stamp_maps.items():
        covered = src[..., 3] > 0
        if not covered.any():
            continue
        dst = terrain.texture_paint_maps.get(key)
        if dst is None:
            dst = np.zeros((res, res, 4), dtype=np.uint8)
        elif np.array_equal(dst[covered], src[covered]):
            continue
        else:
            dst = dst.copy()
        dst[covered] = src[covered]
        terrain.texture_paint_maps[key] = dst
        terrain._touch_texture_paint(key)
        changed += 1
    return changed


def _merge_paint_resampled(terrain, stamp_maps, s_res, s_chunk, s_off_x, s_off_z) -> int:
    """Nearest-texel resample of a stamp painted on a different chunk grid."""
    if not stamp_maps:
        return 0
    s_cell = s_chunk / float(s_res)
    coords = np.array(list(stamp_maps.keys()), dtype=np.float64)
    min_x = s_off_x + coords[:, 0].min() * s_chunk
    max_x = s_off_x + (coords[:, 0].max() + 1) * s_chunk
    min_z = s_off_z + coords[:, 1].min() * s_chunk
    max_z = s_off_z + (coords[:, 1].max() + 1) * s_chunk

    res = terrain.TERRAIN_PAINT_RESOLUTION
    chunk = float(terrain.chunk_size)
    cell = chunk / float(res)
    changed = 0
    for cx in range(int(math.floor((min_x - terrain.offset_x) / chunk)),
                    int(math.floor((max_x - terrain.offset_x) / chunk)) + 1):
        xs = terrain.offset_x + cx * chunk + (np.arange(res) + 0.5) * cell
        for cz in range(int(math.floor((min_z - terrain.offset_z) / chunk)),
                        int(math.floor((max_z - terrain.offset_z) / chunk)) + 1):
            zs = terrain.offset_z + cz * chunk + (np.arange(res) + 0.5) * cell
            wx, wz = np.meshgrid(xs, zs, indexing="ij")
            scx = np.floor((wx - s_off_x) / s_chunk).astype(np.int64)
            scz = np.floor((wz - s_off_z) / s_chunk).astype(np.int64)
            six = np.clip(np.floor((wx - s_off_x - scx * s_chunk) / s_cell), 0, s_res - 1).astype(np.int64)
            siz = np.clip(np.floor((wz - s_off_z - scz * s_chunk) / s_cell), 0, s_res - 1).astype(np.int64)

            sampled = np.zeros((res, res, 4), dtype=np.uint8)
            for skey in set(zip(scx.ravel().tolist(), scz.ravel().tolist())):
                src = stamp_maps.get(skey)
                if src is None:
                    continue
                mask = (scx == skey[0]) & (scz == skey[1])
                sampled[mask] = src[six[mask], siz[mask]]

            covered = sampled[..., 3] > 0
            if not covered.any():
                continue
            key = (int(cx), int(cz))
            dst = terrain.texture_paint_maps.get(key)
            if dst is None:
                dst = np.zeros((res, res, 4), dtype=np.uint8)
            elif np.array_equal(dst[covered], sampled[covered]):
                continue
            else:
                dst = dst.copy()
            dst[covered] = sampled[covered]
            terrain.texture_paint_maps[key] = dst
            terrain._touch_texture_paint(key)
            changed += 1
    return changed


def _merge_csg(terrain, data) -> int:
    added = 0
    for cut in data.get("csg_subtractions", []) or []:
        try:
            values = [float(v) for v in cut]
        except (TypeError, ValueError):
            continue
        if len(values) != 6 or not all(math.isfinite(v) for v in values):
            continue
        lo = np.minimum(values[:3], values[3:])
        hi = np.maximum(values[:3], values[3:])
        if not np.all((hi - lo) > 1e-6):
            continue
        normalised = [float(lo[0]), float(lo[1]), float(lo[2]),
                      float(hi[0]), float(hi[1]), float(hi[2])]
        if normalised in terrain.csg_subtractions:
            continue
        terrain.csg_subtractions.append(normalised)
        added += 1
    return added


def apply_stamp(terrain, data: dict) -> dict:
    """Merge a stamp dict into ``terrain``; returns what changed.

    The result maps ``sculpt_cells``, ``paint_chunks`` and ``csg_cuts`` to the
    number of sculpt cells, paint chunks and cuts that actually changed, so an
    all-zero result means the stamp was already fully applied.
    """
    if not isinstance(data, dict) or data.get("format") != STAMP_FORMAT:
        raise StampError("Not a terrain stamp")

    sculpt_cells = _merge_sculpt(
        terrain, _decode_sculpt(data),
        _float(data, "sculpt_grid_resolution", terrain.sculpt_grid_resolution))

    try:
        s_res = int(data.get("texture_paint_resolution", terrain.TERRAIN_PAINT_RESOLUTION))
    except (TypeError, ValueError):
        s_res = 0
    paint_chunks = 0
    if s_res > 0:
        stamp_maps = _decode_paint(data, s_res)
        s_chunk = _float(data, "chunk_size", terrain.chunk_size)
        s_off_x = _float(data, "offset_x", terrain.offset_x)
        s_off_z = _float(data, "offset_z", terrain.offset_z)
        aligned = (
            s_res == terrain.TERRAIN_PAINT_RESOLUTION
            and math.isclose(s_chunk, terrain.chunk_size)
            and math.isclose(s_off_x, terrain.offset_x)
            and math.isclose(s_off_z, terrain.offset_z)
        )
        if aligned:
            paint_chunks = _merge_paint_aligned(terrain, stamp_maps)
        elif s_chunk > 0:
            paint_chunks = _merge_paint_resampled(
                terrain, stamp_maps, s_res, s_chunk, s_off_x, s_off_z)

    csg_cuts = _merge_csg(terrain, data)

    if sculpt_cells or csg_cuts:
        terrain.mark_all_dirty()
    return {"sculpt_cells": sculpt_cells, "paint_chunks": paint_chunks,
            "csg_cuts": csg_cuts}


def apply_stamp_file(terrain, path: str) -> dict:
    """Load ``path`` and merge it into ``terrain``."""
    return apply_stamp(terrain, load_stamp_file(path))
