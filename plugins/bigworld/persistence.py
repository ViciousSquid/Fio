"""
Persistence helpers for Big World.

Big World adds almost nothing new to persist, and by design:

* **UUIDs** are already Fio's stable identity for brushes (``brush['id']``) and
  entities (``thing.properties['id']``). Big World *reads* them and never writes
  them, so an object keeps the same UUID across load → activate → deactivate →
  save → reload → stream. Streaming is a runtime state change, not an identity
  change.
* **Cell assignment** is derived purely from an object's position and the shared
  512-unit grid. It is therefore recomputed on load and never needs to be stored
  — a brush cannot get "lost" from its cell because its cell *is* its position.
* **Everything else** (names, transforms, properties, custom key/values, I/O)
  is the object's own data, saved by Fio's existing serializer unchanged.

The only genuinely new datum is the map's Big World *config* (whether streaming
is on and the two radii). That travels as an ordinary :class:`BigWorldSettings`
entity, so it round-trips through Fio's normal save/load with no core change —
see :mod:`plugins.bigworld.entities`.

This module provides small, pure functions for reading that config out of a
loaded map dict (or a live ``things`` list), plus verification helpers the tests
and tools use to *prove* UUID stability across a streaming round-trip. Nothing
here imports the editor or engine, so it runs headless.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

from .cell import CELL_SIZE, cell_of_point

_SETTINGS_TYPE = "bigworldsettings"

#: The runtime marker keys the session writes onto objects while they are parked.
#: They must never be persisted — they are transient streaming state, not map
#: data. :func:`strip_runtime_keys` removes them before a save.
RUNTIME_KEYS = ("_bw_parked_hidden", "_bw_parked_disabled", "bw_active")

#: The two park markers hold an object's *real* (pre-streaming) hidden/disabled
#: value while it is parked in an inactive cell. Delta comparison must resolve
#: them back before diffing, or every currently-parked object would look like a
#: gameplay change (see :func:`normalize_streaming_state`).
_HID_MARK = "_bw_parked_hidden"
_DIS_MARK = "_bw_parked_disabled"


def _normalise_type(type_name) -> str:
    return str(type_name or "").replace("_", "").lower()


# ---------------------------------------------------------------------------
# Config extraction
# ---------------------------------------------------------------------------

def find_settings_dict(map_data: dict) -> Optional[dict]:
    """Return the ``BigWorldSettings`` entity dict in a loaded map, or None.

    Absent ⇒ no Big World metadata ⇒ the caller should behave as ordinary Fio
    (the §20 backwards-compatibility contract).
    """
    if not isinstance(map_data, dict):
        return None
    for t in map_data.get("things", []) or ():
        if not isinstance(t, dict):
            continue
        typ = t.get("type") or (t.get("properties", {}) or {}).get("type")
        if _normalise_type(typ) == _SETTINGS_TYPE:
            return t
    return None


def find_settings_thing(things) -> Optional[object]:
    """Return the live ``BigWorldSettings`` entity in a ``things`` list, or None."""
    for t in things or ():
        props = getattr(t, "properties", None)
        if isinstance(props, dict) and _normalise_type(props.get("type")) == _SETTINGS_TYPE:
            return t
    return None


def config_from_settings(settings) -> Dict:
    """Normalise a settings entity/dict into a plain config dict.

    Accepts either a live :class:`BigWorldSettings` (or any object with a
    ``properties`` dict) or a raw map-thing dict. Returns defaults for anything
    missing so a hand-edited or partial map never raises.
    """
    if settings is None:
        props = {}
    elif isinstance(settings, dict):
        props = settings.get("properties", settings) or {}
    else:
        props = getattr(settings, "properties", {}) or {}

    def _f(key, default):
        try:
            return float(props.get(key, default))
        except (TypeError, ValueError):
            return default

    def _b(key, default):
        val = props.get(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    act = _f("activation_radius", 2048.0)
    deact = max(_f("deactivation_radius", 2304.0), act)
    return {
        "enabled": _b("enabled", True),
        "activation_radius": act,
        "deactivation_radius": deact,
        "show_cell_debug": _b("show_cell_debug", True),
        "terrain_fill": _b("terrain_fill", False),
        "terrain_infinite": _b("terrain_infinite", False),
        "terrain_stream_radius": _f("terrain_stream_radius", 0.0),
        "disk_streaming": _b("disk_streaming", False),
    }


def map_has_bigworld(map_data: dict) -> bool:
    """True if the map carries Big World metadata (a settings entity)."""
    return find_settings_dict(map_data) is not None


# ---------------------------------------------------------------------------
# Save hygiene + UUID-stability verification
# ---------------------------------------------------------------------------

def strip_runtime_keys(map_data: dict) -> dict:
    """Remove transient Big World runtime markers from a map dict before saving.

    Streaming state (a parked brush's remembered ``hidden`` value, the live
    ``bw_active`` flag) must not leak into the saved file. Cell assignment and
    UUIDs are untouched — they are legitimate map data / derived state. Mutates
    and returns *map_data*.
    """
    for brush in map_data.get("brushes", []) or ():
        if isinstance(brush, dict):
            for k in RUNTIME_KEYS:
                brush.pop(k, None)
    for t in map_data.get("things", []) or ():
        if isinstance(t, dict):
            props = t.get("properties")
            if isinstance(props, dict):
                for k in RUNTIME_KEYS:
                    props.pop(k, None)
    return map_data


def collect_uuids(brushes, things) -> Dict[str, set]:
    """Gather the UUID sets of a world, for before/after stability checks."""
    b = {str(br.get("id", "")) for br in (brushes or ()) if isinstance(br, dict)}
    t = set()
    for th in things or ():
        props = getattr(th, "properties", None)
        if isinstance(props, dict):
            t.add(str(props.get("id", "")))
    b.discard("")
    t.discard("")
    return {"brushes": b, "things": t}


def uuids_stable(before: Dict[str, set], after: Dict[str, set]) -> bool:
    """True if every UUID present before is still present after (none regenerated)."""
    return (before["brushes"] == after["brushes"]
            and before["things"] == after["things"])


# ---------------------------------------------------------------------------
# Persistent cell delta registry
# ---------------------------------------------------------------------------
#
# A Big World save is a *forced delta*: rather than a full world snapshot, it
# stores only what changed from the base world, bucketed by the same
# ``(cell_x, cell_z)`` cell identity the streaming manager uses. Because the
# in-RAM streaming model keeps every object resident (parking toggles flags, it
# never frees objects), the changes of a cell that has since been unloaded are
# still present in the live world and so are captured here too — that is the
# invariant the save must preserve: *a cell's persistent gameplay changes
# survive unload → save → load → re-stream*.
#
# The registry reuses the core save system's UUID delta (``compute_delta_level``)
# so identity, normalization and the immutable-vs-mutable safety rules are
# single-sourced with ``engine.savegame`` — Big World only adds the per-cell
# bucketing and the streaming-state normalization below.


#: Keys whose falsy value is indistinguishable from the key being absent — the
#: engine reads them with ``.get(key, False)``. Parking *adds* them (set to True
#: with the real value stashed), so after un-parking a value of False must be
#: canonicalised to *absent*, or an untouched parked object would diff against a
#: base that never carried the key at all.
_DEFAULT_FALSE_KEYS = ("hidden", "disabled")


def _canonicalise_false(container: dict) -> None:
    for k in _DEFAULT_FALSE_KEYS:
        if k in container and not container[k]:
            container.pop(k, None)


def _unpark_props(props: dict) -> None:
    """Resolve a thing's park markers back to its real gameplay state, in place."""
    if _HID_MARK in props:
        props["hidden"] = props.get(_HID_MARK)
    if _DIS_MARK in props:
        props["disabled"] = props.get(_DIS_MARK)
    for k in RUNTIME_KEYS:
        props.pop(k, None)
    _canonicalise_false(props)


def normalize_streaming_state(level: dict) -> dict:
    """A copy of a serialized level with Big World streaming state resolved away.

    While a cell is parked its objects carry ``hidden``/``disabled`` set to True
    with the pre-park values stashed in ``_bw_parked_*`` markers. For a delta the
    object's *gameplay* state is the pre-park value — so this restores it and
    drops the transient markers (and ``bw_active``). Without it, every currently
    unloaded object would diff as a spurious change. Non-destructive: the live
    objects are untouched.
    """
    out: Dict[str, list] = {"things": [], "brushes": []}
    for t in level.get("things", []) or []:
        t2 = copy.deepcopy(t)
        props = t2.get("properties")
        if isinstance(props, dict):
            _unpark_props(props)
        out["things"].append(t2)
    for b in level.get("brushes", []) or []:
        b2 = copy.deepcopy(b)
        if isinstance(b2, dict):
            if _HID_MARK in b2:
                b2["hidden"] = b2.get(_HID_MARK)
            for k in RUNTIME_KEYS:
                b2.pop(k, None)
            _canonicalise_false(b2)
        out["brushes"].append(b2)
    for k, v in level.items():
        if k not in ("things", "brushes"):
            out[k] = v
    return out


def cell_key_for_pos(pos, cell_size: float = CELL_SIZE) -> str:
    """The registry key ``"cx,cz"`` for a world position — the manager's cell id.

    Uses the exact same ``cell_of_point`` the streaming grid uses, so a delta is
    filed under the very cell that would stream it back in. String form keeps the
    registry JSON-serialisable while remaining the real cell identity.
    """
    try:
        cx, cz = cell_of_point(float(pos[0]), float(pos[2]), cell_size)
    except (TypeError, ValueError, IndexError):
        cx, cz = 0, 0
    return f"{cx},{cz}"


def build_cell_delta_registry(base_level: dict, live_level: dict,
                              cell_size: float = CELL_SIZE) -> dict:
    """Bucket the world's UUID delta (live − base) into per-cell deltas.

    *live_level* should already be :func:`normalize_streaming_state`-d. Returns
    ``{"cx,cz": {"things": [...], "brushes": [...]}}`` holding only cells that
    actually changed — unchanged cells are never emitted. Each entry stores the
    same records ``engine.savegame`` would (full changed-thing dicts; brush
    overlay slices), so restoration overlays them by UUID with the core code.
    """
    from engine import savegame  # engine-free at import time; safe headless
    delta = savegame.compute_delta_level(base_level or {}, live_level or {})

    registry: Dict[str, dict] = {}

    def _bucket(key: str):
        return registry.setdefault(key, {"things": [], "brushes": []})

    for t in delta.get("things", []):
        pos = t.get("pos") or (0.0, 0.0, 0.0)
        _bucket(cell_key_for_pos(pos, cell_size))["things"].append(t)

    # Brush delta entries carry only {id, overlay keys}; recover a position for
    # bucketing from the live/base brush record (falls back to origin).
    pos_by_bid: Dict[str, object] = {}
    for src in (live_level, base_level):
        for b in (src or {}).get("brushes", []) or []:
            if isinstance(b, dict):
                bid = b.get("id")
                if bid and bid not in pos_by_bid and b.get("pos") is not None:
                    pos_by_bid[bid] = b.get("pos")
    for b in delta.get("brushes", []):
        pos = pos_by_bid.get(b.get("id"), (0.0, 0.0, 0.0))
        _bucket(cell_key_for_pos(pos, cell_size))["brushes"].append(b)

    return registry


def flatten_cell_delta_registry(registry: dict) -> dict:
    """Recombine a per-cell registry into one delta level for UUID overlay.

    The inverse of :func:`build_cell_delta_registry`'s bucketing: since
    restoration matches by UUID (not by cell), the loader can flatten every
    cell's records back into a single partial level and hand it to the core
    ``_overlay_entities`` overlay unchanged.
    """
    things: List[dict] = []
    brushes: List[dict] = []
    for cell in (registry or {}).values():
        things.extend(cell.get("things", []) or [])
        brushes.extend(cell.get("brushes", []) or [])
    return {"things": things, "brushes": brushes}
