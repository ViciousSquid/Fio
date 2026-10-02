"""
Native play-session save / load for Fio.

Fio already knows how to *serialize a level* — :meth:`EditorState.get_level_data`
turns the live brush/thing scene (with every entity's current properties and
position) into a plain JSON-able dict, and :meth:`EditorState.load_from_data`
rebuilds it. That is the editor's document format, not a saved game: it captures
the map, but none of the *live* play-session state that only exists once you hit
Play — where the player is standing, how much health is left, which keys have
been picked up, which doors are open, which monsters are dead, and the active
cheat flags.

This module builds a *saved game* on top of that existing capability. A snapshot
is the serialized level (so entity health/dead/collected/hidden and positions
come along for free) plus a ``runtime`` block for the things the level format
never stores: the player transform and stats, the cheat flags, the collected-key
set, and the door / mover / monster animation state.

It is deliberately engine-native: no plugin, no bump to the plugin API
(:data:`plugins.api.API_VERSION` stays ``1.3.0``). :class:`~engine.logic_thread.LogicThread`
exposes :meth:`~engine.logic_thread.LogicThread.save_session` /
:meth:`~engine.logic_thread.LogicThread.load_session`, and the editor console
grows ``save`` / ``load`` / ``quicksave`` / ``quickload`` commands over them.

Snapshot shape::

    {
      "fio_savegame": true,
      "save_version": 2,
      "save_mode": "full",
      "saved_at": "2026-08-21T18:44:00",
      "map": "Simple_Map_Test.json",
      "level": { ... EditorState.get_level_data() ... },
      "player": { pos, angle, pitch, velocity, camera_height,
                  physics_enabled, on_ground, in_water, swimming },
      "player2": { ... } | null,
      "runtime": {
        "god_mode", "buddha_mode", "notarget",
        "camera_mode", "overhead_height", "overhead_tilt", "overhead_orientation",
        "active_weapon", "current_hud_message",
        "player_health", "player_max_health", "player_dead",
        "player2_health", "player2_max_health", "player2_dead",
        "collected_keys": [ ... ],
        "door_states":   { "<index>": {"state", "progress"} },
        "mover_states":  { "<index>": {"progress", "forward"} },
        "monster_states": { "<entity id>": { ... } }
      }
    }

Restore is applied as an *overlay* onto a live, already-playing session, so it
never rebuilds the scene mid-flight (which would invalidate the logic thread's
caches, spatial grid and object identities). Entity live state is matched back
by the stable UUID every brush and thing carries.

Save modes (``save_version`` 2)
-------------------------------
The same machinery serves three strategies, chosen by the ``save_mode`` metadata
key and reused rather than duplicated:

* ``full`` — the original behaviour: embed the whole level plus player/runtime
  state. Self-contained, largest, needs no base map to restore.
* ``delta`` — embed only the entities/brushes that *differ* from the base map
  (matched by UUID), plus player/runtime state. Smallest, but the base map must
  be present at load. A delta restores by overlaying just those changes onto a
  freshly-loaded base map — the exact same UUID overlay ``full`` uses, fed a
  partial level.
* ``both`` — store the compact delta *and* a complete fallback snapshot. The
  loader prefers the delta and silently falls back to the full snapshot when the
  base map is missing or incompatible.

Loading is automatic (:func:`restore_auto`): the mode is read from metadata, a
legacy save with no ``save_mode`` is treated as ``full``, and the base map is
validated by :func:`classify_base_map` (exact / related / incompatible) so the
loader only prompts when no safe automatic decision exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

#: Bump only when the snapshot layout changes incompatibly. This is the *save
#: file* format version and is unrelated to the plugin API version. v2 adds the
#: explicit ``save_mode`` metadata and delta/both support; v1 saves (no
#: ``save_mode``) still load, treated as legacy ``full``.
SAVE_VERSION = 2

#: Marker key so a stray JSON file is never mistaken for a Fio save.
_MAGIC = "fio_savegame"

#: The three save strategies. ``full`` is the default so behaviour is unchanged
#: for anyone who never touches the setting.
SAVE_MODE_FULL = "full"
SAVE_MODE_DELTA = "delta"
SAVE_MODE_BOTH = "both"
VALID_SAVE_MODES = frozenset({SAVE_MODE_FULL, SAVE_MODE_DELTA, SAVE_MODE_BOTH})

#: Optional ``world_mode`` metadata. ``"bigworld"`` marks a save whose delta is a
#: per-cell registry produced by a streaming (Big World) map, which forces delta
#: mode. Absent ⇒ an ordinary single-scene Fio map.
WORLD_MODE_BIGWORLD = "bigworld"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _vec3(v) -> list:
    """A glm.vec3 / sequence as a plain ``[x, y, z]`` list of floats."""
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except AttributeError:
        return [float(v[0]), float(v[1]), float(v[2])]


def _set_vec3(target, value) -> None:
    """Write ``[x, y, z]`` back into a glm.vec3 (in place) or a list."""
    if value is None:
        return
    x, y, z = float(value[0]), float(value[1]), float(value[2])
    try:
        target.x, target.y, target.z = x, y, z
    except AttributeError:
        target[0], target[1], target[2] = x, y, z


#: Brush dict keys the overlay is allowed to write back onto a live brush.
#: Everything else (geometry, positions, direction vectors, flags) comes from the
#: freshly-loaded map and must keep its runtime type — overwriting it with the
#: JSON-degraded (list) form breaks typed math in the engine. Only genuinely
#: gameplay-mutable brush state belongs here; door/mover animation is restored
#: separately via door_states/mover_states.
_BRUSH_OVERLAY_KEYS = frozenset({"hidden", "tint"})


def _public_state(state: dict) -> dict:
    """A state dict without its cached, underscore-prefixed runtime fields.

    Animation state dicts stash caches the engine recomputes lazily — e.g. a
    mover's ``_direction_np`` (a NumPy array). Those must never round-trip
    through JSON: the array would come back a plain list and the engine would
    then try to multiply a list by a float. Persist only the real state; the
    caches rebuild on the next tick.
    """
    return {k: v for k, v in state.items() if not str(k).startswith("_")}


def _thing_id(thing) -> str:
    try:
        return thing.properties.get("id", "")
    except AttributeError:
        return ""


def _capture_player(player) -> Optional[dict]:
    if player is None:
        return None
    return {
        "pos": _vec3(player.pos),
        "angle": float(getattr(player, "angle", 0.0)),
        "pitch": float(getattr(player, "pitch", 0.0)),
        "velocity": _vec3(getattr(player, "velocity", (0.0, 0.0, 0.0))),
        "camera_height": float(getattr(player, "camera_height", 40.0)),
        "physics_enabled": bool(getattr(player, "physics_enabled", True)),
        "on_ground": bool(getattr(player, "on_ground", False)),
        "in_water": bool(getattr(player, "in_water", False)),
        "swimming": bool(getattr(player, "swimming", False)),
    }


def _apply_player(player, data: Optional[dict]) -> None:
    if player is None or not data:
        return
    _set_vec3(player.pos, data.get("pos"))
    _set_vec3(getattr(player, "velocity", None), data.get("velocity"))
    if "angle" in data:
        player.angle = float(data["angle"])
    if "pitch" in data:
        player.pitch = float(data["pitch"])
    if "camera_height" in data:
        player.camera_height = float(data["camera_height"])
    if "physics_enabled" in data:
        player.physics_enabled = bool(data["physics_enabled"])
    if "on_ground" in data:
        player.on_ground = bool(data["on_ground"])
    if "in_water" in data:
        player.in_water = bool(data["in_water"])
    if "swimming" in data:
        player.swimming = bool(data["swimming"])


# ---------------------------------------------------------------------------
# snapshot build / restore
# ---------------------------------------------------------------------------

def _build_full_snapshot(logic, *, map_name: str = "") -> dict:
    """Capture the live play session on *logic* (a ``LogicThread``) as a dict.

    Call while a play session is active. The returned dict is JSON-serialisable
    and self-contained (it embeds the whole level), so it can be written to disk
    and loaded in a fresh session, or applied back in the same one.
    """
    level = {}
    try:
        level = logic.editor_state.get_level_data()
    except Exception:
        level = {}

    monster_states: Dict[str, Any] = {}
    try:
        raw = getattr(logic.monster_ai, "monster_states", {}) or {}
        by_obj_id = {id(t): t for t in getattr(logic, "_monster_things", [])}
        for obj_id, state in raw.items():
            mon = by_obj_id.get(obj_id)
            sid = _thing_id(mon) if mon is not None else ""
            if sid:
                # investigating_sound holds a transient (pos, expiry) tuple tied
                # to wall-clock time; drop it rather than persist a stale timer.
                # _public_state also strips any cached _-prefixed runtime fields.
                clean = _public_state(state)
                clean.pop("investigating_sound", None)
                monster_states[sid] = clean
    except Exception:
        monster_states = {}

    runtime = {
        "god_mode": bool(getattr(logic, "god_mode", False)),
        "buddha_mode": bool(getattr(logic, "buddha_mode", False)),
        "notarget": bool(getattr(logic, "notarget", False)),
        "camera_mode": getattr(logic, "camera_mode", "First Person"),
        "overhead_height": float(getattr(logic, "overhead_height", 800.0)),
        "overhead_tilt": float(getattr(logic, "overhead_tilt", 0.0)),
        "overhead_orientation": getattr(logic, "overhead_orientation", "north"),
        "active_weapon": getattr(logic, "active_weapon", None),
        "current_hud_message": getattr(logic, "current_hud_message", ""),
        "player_health": getattr(logic, "player_health", 100),
        "player_max_health": getattr(logic, "player_max_health", 100),
        "player_dead": bool(getattr(logic, "player_dead", False)),
        "player2_health": getattr(logic, "player2_health", 100),
        "player2_max_health": getattr(logic, "player2_max_health", 100),
        "player2_dead": bool(getattr(logic, "player2_dead", False)),
        "collected_keys": sorted(str(k) for k in getattr(logic, "collected_keys", set())),
        "door_states": {str(i): _public_state(s) for i, s in getattr(logic, "door_states", {}).items()},
        "mover_states": {str(i): _public_state(s) for i, s in getattr(logic, "mover_states", {}).items()},
        "monster_states": monster_states,
    }

    return {
        _MAGIC: True,
        "save_version": SAVE_VERSION,
        "save_mode": SAVE_MODE_FULL,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "map": map_name or "",
        "level": level,
        "player": _capture_player(getattr(logic, "player", None)),
        "player2": _capture_player(getattr(logic, "player2", None)),
        "runtime": runtime,
    }


# ---------------------------------------------------------------------------
# delta build  (only what differs from the base map, matched by UUID)
# ---------------------------------------------------------------------------

def _jsonify(value):
    """Round-trip *value* through JSON so two representations compare equal.

    NumPy arrays/scalars, glm vectors, tuples and sets all become the plain
    lists/numbers they serialize to, so a live (typed) value and a base value
    loaded from disk don't register a false difference purely from their runtime
    type. Used only for *comparison*; the stored data is the original value.
    """
    return json.loads(json.dumps(value, default=_json_default))


def _thing_state(t_data: dict) -> dict:
    """The gameplay-comparable part of a serialized thing.

    Just position and the public (non underscore-prefixed) properties, minus the
    I/O connections the overlay never writes back. This is exactly the surface
    :func:`_overlay_entities` restores, so comparing it is what decides whether a
    thing belongs in the delta.
    """
    props = {
        k: v for k, v in (t_data.get("properties") or {}).items()
        if not str(k).startswith("_") and k != "_io_connections"
    }
    return {"pos": _jsonify(t_data.get("pos")), "properties": _jsonify(props)}


def _brush_overlay(b_data: dict) -> dict:
    """The gameplay-mutable overlay slice of a serialized brush (by value)."""
    return {k: b_data[k] for k in _BRUSH_OVERLAY_KEYS if k in b_data}


def _brushes_by_id(brushes) -> dict:
    out = {}
    for b in brushes or []:
        bid = b.get("id") if isinstance(b, dict) else None
        if bid:
            out[bid] = b
    return out


def _things_by_id(things) -> dict:
    out = {}
    for t in things or []:
        tid = (t.get("properties") or {}).get("id")
        if tid:
            out[tid] = t
    return out


def compute_delta_level(base_level: dict, live_level: dict) -> dict:
    """A partial level holding only entities/brushes that changed from *base*.

    Both arguments are level documents in :meth:`EditorState.get_level_data`
    shape (see :func:`normalize_base_level`). The result is fed straight to
    :func:`_overlay_entities` on restore — a *partial* level of just the changed
    records — so delta restore reuses the full-save UUID overlay unchanged.

    Only meaningful gameplay differences survive: positions and public
    properties for things (dead/collected/hidden/health/… all live there), and
    the gameplay-mutable overlay keys for brushes. Unchanged world data is not
    duplicated. Entities that exist live but not in the base (e.g. spawned at
    runtime) are recorded best-effort; the UUID overlay simply skips them if the
    base map can't supply a match on restore.
    """
    base_things = _things_by_id((base_level or {}).get("things", []))
    delta_things: List[dict] = []
    for t in (live_level or {}).get("things", []):
        tid = (t.get("properties") or {}).get("id")
        if not tid:
            continue
        base_t = base_things.get(tid)
        if base_t is None or _thing_state(base_t) != _thing_state(t):
            delta_things.append(t)

    base_brushes = _brushes_by_id((base_level or {}).get("brushes", []))
    delta_brushes: List[dict] = []
    for b in (live_level or {}).get("brushes", []):
        bid = b.get("id")
        if not bid:
            continue
        base_b = base_brushes.get(bid)
        live_ov = _brush_overlay(b)
        base_ov = _brush_overlay(base_b) if base_b is not None else {}
        if base_b is None or _jsonify(live_ov) != _jsonify(base_ov):
            entry = {"id": bid}
            entry.update(live_ov)
            delta_brushes.append(entry)

    return {"things": delta_things, "brushes": delta_brushes}


def _base_map_identity(base_level: dict, map_name: str) -> dict:
    """Stable identity of the base map a delta was taken from.

    Records the map name/path plus a structural fingerprint (a hash over the
    sorted set of thing and brush UUIDs) and entity counts. The fingerprint is
    tiny and independent of order or of any gameplay mutation, so it stays valid
    for BigWorld maps and survives a reload. It is what :func:`classify_base_map`
    checks the current map against.
    """
    thing_ids = sorted(
        (t.get("properties") or {}).get("id", "")
        for t in (base_level or {}).get("things", [])
    )
    brush_ids = sorted(
        b.get("id", "") for b in (base_level or {}).get("brushes", [])
    )
    thing_ids = [i for i in thing_ids if i]
    brush_ids = [i for i in brush_ids if i]
    raw = ("\n".join(thing_ids) + "\x00" + "\n".join(brush_ids)).encode("utf-8")
    return {
        "name": os.path.basename(map_name or ""),
        "fingerprint": hashlib.sha1(raw).hexdigest()[:16],
        "thing_count": len(thing_ids),
        "brush_count": len(brush_ids),
    }


def normalize_base_level(raw_level: dict) -> dict:
    """Re-serialize an on-disk map document through the editor's own pipeline.

    Loading a raw map file and running it back out through
    :meth:`EditorState.get_level_data` reproduces exactly the normalization the
    live level went through (type coercion in ``Thing.to_dict``, brush-id
    backfill, I/O handling), so a delta diff compares like with like and doesn't
    invent changes from mere on-disk formatting. Falls back to the raw document
    if the editor state can't be constructed head-less.
    """
    try:
        from editor.editor_state import EditorState
        st = EditorState()
        st.load_from_data(raw_level or {})
        return st.get_level_data()
    except Exception:
        return raw_level or {}


def build_snapshot(logic, *, map_name: str = "",
                   save_mode: str = SAVE_MODE_FULL,
                   base_level: Optional[dict] = None,
                   world_mode: Optional[str] = None,
                   cell_deltas: Optional[dict] = None,
                   base_world: Optional[dict] = None) -> dict:
    """Build a save dict for the live play session in the requested *save_mode*.

    * ``full`` (default) — the self-contained snapshot; identical to v1 plus the
      explicit ``save_mode`` marker. Needs no *base_level*.
    * ``delta`` — metadata, base-map identity, the changed-only delta level, and
      player/runtime state. Requires *base_level* (the normalized base map).
    * ``both`` — the delta *and* the complete snapshot as a fallback.

    ``delta``/``both`` silently degrade to ``full`` when no *base_level* is
    available, so a save is never lost just because the base map couldn't be
    resolved.

    When *world_mode* is ``"bigworld"`` a Big World save is forced: the strategy
    is always ``delta`` (a full world snapshot is never written), the changes are
    stored as the per-cell *cell_deltas* registry keyed by cell id, and
    *base_world* carries the base-world identity. This is selected automatically
    for streaming maps; ordinary maps never reach it.
    """
    full = _build_full_snapshot(logic, map_name=map_name)

    if world_mode == WORLD_MODE_BIGWORLD:
        base_map = base_world or _base_map_identity(base_level or {}, map_name)
        return {
            _MAGIC: True,
            "save_version": SAVE_VERSION,
            "save_mode": SAVE_MODE_DELTA,   # forced: Big World never full-saves
            "world_mode": WORLD_MODE_BIGWORLD,
            "saved_at": full.get("saved_at"),
            "map": map_name or "",
            "base_map": base_map,
            "cell_deltas": cell_deltas or {},
            "player": full.get("player"),
            "player2": full.get("player2"),
            "runtime": full.get("runtime"),
        }

    if save_mode == SAVE_MODE_FULL or not base_level:
        return full

    delta_level = compute_delta_level(base_level, full.get("level", {}) or {})
    base_map = _base_map_identity(base_level, map_name)
    common = {
        _MAGIC: True,
        "save_version": SAVE_VERSION,
        "saved_at": full.get("saved_at"),
        "map": map_name or "",
        "base_map": base_map,
        "delta": {"level": delta_level},
        "player": full.get("player"),
        "player2": full.get("player2"),
        "runtime": full.get("runtime"),
    }
    if save_mode == SAVE_MODE_BOTH:
        common["save_mode"] = SAVE_MODE_BOTH
        # The complete snapshot rides along as a recovery/fallback path.
        common["level"] = full.get("level", {})
    else:
        common["save_mode"] = SAVE_MODE_DELTA
    return common


def _overlay_entities(logic, level: dict) -> None:
    """Restore live entity state (position + properties) from a saved level.

    Matches by stable UUID so it survives a full scene reload; entities present
    in the save but not the live scene (or vice-versa) are skipped quietly.
    """
    if not level:
        return

    # -- things: restore pos + mutable properties by id --------------------
    live_things = {}
    for t in getattr(logic, "things", []) or []:
        tid = _thing_id(t)
        if tid:
            live_things[tid] = t
    for t_data in level.get("things", []):
        props = t_data.get("properties", {}) or {}
        tid = props.get("id", "")
        live = live_things.get(tid)
        if live is None:
            continue
        try:
            if "pos" in t_data and t_data["pos"] is not None:
                live.pos = list(t_data["pos"])
            for k, v in props.items():
                if k == "_io_connections":
                    continue
                live.properties[k] = v
        except Exception:
            continue

    # -- brushes: restore only genuinely gameplay-mutable state by id ------
    # Static geometry (pos/size/direction/…) comes from the freshly-loaded map
    # and must keep its runtime type; door/mover animation is restored via the
    # door_states/mover_states dicts, not from the brush record.
    live_brushes = {b.get("id"): b for b in (getattr(logic, "brushes", []) or []) if b.get("id")}
    for b_data in level.get("brushes", []):
        live = live_brushes.get(b_data.get("id"))
        if live is None:
            continue
        for k in _BRUSH_OVERLAY_KEYS:
            if k in b_data:
                live[k] = b_data[k]
            else:
                live.pop(k, None)


def _restore_runtime_and_players(logic, data: dict) -> None:
    """Apply the player/runtime half of a save (shared by full and delta).

    The ``runtime``/``player``/``player2`` blocks are laid out identically in
    every mode, so full and delta restore both call this after their respective
    entity overlay. Caller is responsible for having overlaid entity state first.
    """
    runtime = data.get("runtime", {}) or {}

    # Player(s)
    _apply_player(getattr(logic, "player", None), data.get("player"))
    _apply_player(getattr(logic, "player2", None), data.get("player2"))

    # Player stats / cheat flags
    for attr in (
        "god_mode", "buddha_mode", "notarget",
        "camera_mode", "overhead_height", "overhead_tilt", "overhead_orientation",
        "active_weapon", "current_hud_message",
        "player_health", "player_max_health", "player_dead",
        "player2_health", "player2_max_health", "player2_dead",
    ):
        if attr in runtime:
            setattr(logic, attr, runtime[attr])

    # Collected keys — rebuild the set from the saved list.
    try:
        logic.collected_keys = set(runtime.get("collected_keys", []) or [])
    except Exception:
        pass

    # Collected-pickup set is keyed by id(thing) and can't be persisted across a
    # reload; rebuild it from the entity 'collected' flags we just overlaid.
    try:
        collected = set()
        for t in getattr(logic, "things", []) or []:
            if t.properties.get("collected"):
                collected.add(id(t))
        logic.collected_pickups = collected
    except Exception:
        pass

    # Door / mover animation state (keys serialize as strings → back to int).
    # _public_state drops any cached _-prefixed fields (e.g. a mover's
    # _direction_np NumPy cache) that an older save may still carry, so the
    # engine rebuilds them with the right type on the next tick.
    try:
        logic.door_states = {int(i): _public_state(s) for i, s in runtime.get("door_states", {}).items()}
    except Exception:
        pass
    try:
        logic.mover_states = {int(i): _public_state(s) for i, s in runtime.get("mover_states", {}).items()}
    except Exception:
        pass

    # Monster combat state, remapped from stable id back to the live object id.
    try:
        saved_ms = runtime.get("monster_states", {}) or {}
        if saved_ms and getattr(logic, "monster_ai", None) is not None:
            by_sid = {}
            for t in getattr(logic, "_monster_things", []) or []:
                sid = _thing_id(t)
                if sid:
                    by_sid[sid] = t
            remapped = {}
            for sid, state in saved_ms.items():
                mon = by_sid.get(sid)
                if mon is not None:
                    remapped[id(mon)] = _public_state(state)
            logic.monster_ai.monster_states = remapped
    except Exception:
        pass

    # Rebuild the I/O entity lookup caches, since we mutated properties, and
    # refresh the monster sprite cache so dead/alive billboards match the
    # restored health immediately.
    try:
        logic._build_entity_caches()
    except Exception:
        pass
    try:
        from editor.things import Monster
        Monster.clear_sprite_cache()
    except Exception:
        pass


def restore_snapshot(logic, data: dict) -> None:
    """Apply a full snapshot from :func:`build_snapshot` onto a live session.

    *logic* must be an active (play-mode) ``LogicThread`` whose loaded map
    matches the save. Restore is an overlay — the scene is not rebuilt — so
    object identities, caches and the spatial grid stay valid.
    """
    if not isinstance(data, dict) or not data.get(_MAGIC):
        raise ValueError("not a Fio save file")
    # Live entity state first, so anything derived from it below is consistent.
    _overlay_entities(logic, data.get("level", {}) or {})
    _restore_runtime_and_players(logic, data)


def restore_delta(logic, data: dict) -> None:
    """Apply a delta save onto a *freshly-loaded base map* live session.

    The delta's partial level (only the changed entities/brushes) is fed to the
    very same UUID overlay a full restore uses; entities the base map doesn't
    have are skipped safely. Player/runtime state is then restored as usual.
    """
    if not isinstance(data, dict) or not data.get(_MAGIC):
        raise ValueError("not a Fio save file")
    delta_level = ((data.get("delta") or {}).get("level")) or {}
    _overlay_entities(logic, delta_level)
    _restore_runtime_and_players(logic, data)


# ---------------------------------------------------------------------------
# base-map validation & automatic loading
# ---------------------------------------------------------------------------

#: How the current map relates to the base map a delta was taken from.
BASE_EXACT = "exact"            # identical fingerprint → apply the delta as-is
BASE_RELATED = "related"        # same map, minor edits → UUID overlay, warn
BASE_INCOMPATIBLE = "incompatible"  # different map → do not blindly apply


def classify_base_map(data: dict, current_level: dict,
                      current_map_name: str = "") -> str:
    """Decide how safely a delta save applies to the currently-loaded map.

    Compares the ``base_map`` identity stored in *data* against the live map
    (its normalized level document and name):

    * :data:`BASE_EXACT` — same structural fingerprint. Apply automatically.
    * :data:`BASE_RELATED` — same map name (or a large UUID overlap) but the
      fingerprint drifted from minor edits. Apply via UUID overlay, warn.
    * :data:`BASE_INCOMPATIBLE` — different map and little/no UUID overlap. Do
      not blindly apply.

    A save with no ``base_map`` block (shouldn't happen for delta/both) is
    treated as :data:`BASE_RELATED` so the safe overlay still runs.
    """
    base = data.get("base_map") or {}
    if not base:
        return BASE_RELATED

    current = _base_map_identity(current_level or {}, current_map_name)
    if base.get("fingerprint") and base.get("fingerprint") == current.get("fingerprint"):
        return BASE_EXACT

    same_name = (
        base.get("name")
        and base.get("name") == current.get("name")
    )
    # Measure UUID overlap so a renamed-but-same map is still recognised, and a
    # genuinely different map of coincidentally equal name is not over-trusted.
    base_ids = {
        (t.get("properties") or {}).get("id")
        for t in ((data.get("level") or {}).get("things", []))
    }
    base_ids.discard(None)
    if not base_ids:
        # Delta-only saves don't carry the full base thing set; fall back to the
        # cheaper name signal, treating a same-name map as related.
        return BASE_RELATED if same_name else BASE_INCOMPATIBLE

    live_ids = {
        (t.get("properties") or {}).get("id")
        for t in (current_level or {}).get("things", [])
    }
    live_ids.discard(None)
    if not base_ids:
        overlap = 0.0
    else:
        overlap = len(base_ids & live_ids) / float(len(base_ids))

    if same_name or overlap >= 0.5:
        return BASE_RELATED
    return BASE_INCOMPATIBLE


def restore_auto(logic, data: dict, *, current_map_name: str = "") -> dict:
    """Restore *data* automatically, choosing the path from its ``save_mode``.

    Returns a small report ``{"mode": <mode actually used>, "warning": str}``.
    Raises ``ValueError`` when no safe automatic restore is possible (e.g. a
    delta-only save against a clearly incompatible map, with no full fallback).
    The caller may surface a prompt in that ambiguous/blocked case; ordinary
    loads never prompt.
    """
    if not isinstance(data, dict) or not data.get(_MAGIC):
        raise ValueError("not a Fio save file")

    # Big World delta save: a per-cell registry rather than a flat delta level.
    if data.get("world_mode") == WORLD_MODE_BIGWORLD:
        return _restore_bigworld(logic, data, current_map_name)

    mode = data.get("save_mode")
    if mode is None:
        # Legacy v1 (or any) save without an explicit mode → full snapshot.
        mode = SAVE_MODE_FULL

    if mode == SAVE_MODE_FULL:
        restore_snapshot(logic, data)
        return {"mode": SAVE_MODE_FULL, "warning": ""}

    # delta / both both need the current map assessed against the base identity.
    try:
        current_level = logic.editor_state.get_level_data()
    except Exception:
        current_level = {}
    cls = classify_base_map(data, current_level, current_map_name)

    if mode == SAVE_MODE_DELTA:
        if cls == BASE_INCOMPATIBLE:
            raise ValueError(
                "this delta save was made on a different base map "
                f"('{(data.get('base_map') or {}).get('name', '?')}'); "
                "load that map first, or use a full/both save"
            )
        restore_delta(logic, data)
        warning = ""
        if cls == BASE_RELATED:
            warning = ("base map has changed since this delta was saved; "
                       "applied by UUID, entities that no longer exist were skipped")
        return {"mode": SAVE_MODE_DELTA, "warning": warning}

    if mode == SAVE_MODE_BOTH:
        if cls in (BASE_EXACT, BASE_RELATED):
            try:
                restore_delta(logic, data)
                warning = ("" if cls == BASE_EXACT else
                           "base map changed; applied delta by UUID, missing "
                           "entities skipped")
                return {"mode": SAVE_MODE_DELTA, "warning": warning}
            except Exception:
                pass  # fall through to the full fallback below
        # Incompatible base map (or the delta path failed) → the fallback.
        restore_snapshot(logic, data)
        return {
            "mode": SAVE_MODE_FULL,
            "warning": "base map incompatible; restored from the full fallback snapshot",
        }

    raise ValueError(f"unknown save_mode '{mode}'")


def _flatten_cell_deltas(cell_deltas: dict) -> dict:
    """Recombine a Big World per-cell registry into one partial level.

    Restoration matches by UUID, not by cell, so every cell's changed records are
    merged back into a single delta level for the standard overlay. Engine-side
    and Big-World-agnostic (mirrors ``persistence.flatten_cell_delta_registry``).
    """
    things: list = []
    brushes: list = []
    for cell in (cell_deltas or {}).values():
        if not isinstance(cell, dict):
            continue
        things.extend(cell.get("things", []) or [])
        brushes.extend(cell.get("brushes", []) or [])
    return {"things": things, "brushes": brushes}


def _restore_bigworld(logic, data: dict, current_map_name: str = "") -> dict:
    """Restore a Big World delta save: validate the world, overlay all cells.

    Validates the base-world identity (fail safe on a clearly different world),
    flattens the per-cell registry and overlays it onto the resident world by
    UUID via the same overlay a normal delta restore uses. The registry is also
    handed to the live Big World session so cells that stream in later carry the
    saved changes.
    """
    # Disk-streaming session: the world isn't fully resident, so it can't be
    # overlaid wholesale — hand off to the session, which streams cells in and
    # re-applies each cell's delta as it loads.
    session = getattr(logic, "_bigworld", None)
    if session is not None and getattr(session, "is_disk_streaming", False):
        return session.restore_saved(data, current_map_name=current_map_name)

    try:
        current_level = logic.editor_state.get_level_data()
    except Exception:
        current_level = {}
    cls = classify_base_map(data, current_level, current_map_name)
    if cls == BASE_INCOMPATIBLE:
        raise ValueError(
            "this Big World save was made on a different world "
            f"('{(data.get('base_map') or {}).get('name', '?')}'); "
            "load that world first"
        )

    cell_deltas = data.get("cell_deltas", {}) or {}
    _overlay_entities(logic, _flatten_cell_deltas(cell_deltas))
    _restore_runtime_and_players(logic, data)

    # Hand the registry to the live streaming session so a cell streamed in later
    # re-applies its saved changes (belt-and-braces for a disk-streamed future;
    # in the in-RAM model the overlay above already reached every resident cell).
    session = getattr(logic, "_bigworld", None)
    if session is not None:
        try:
            session.registry = cell_deltas
        except Exception:
            pass

    warning = ""
    if cls == BASE_RELATED:
        warning = ("base world changed since this save; applied by UUID, "
                   "missing entities skipped")
    return {"mode": SAVE_MODE_DELTA, "world_mode": WORLD_MODE_BIGWORLD,
            "warning": warning}


# ---------------------------------------------------------------------------
# file IO
# ---------------------------------------------------------------------------

def _json_default(o):
    """Coerce values the stock JSON encoder rejects into plain JSON.

    The embedded level can carry runtime-typed values that aren't JSON-native —
    NumPy arrays/scalars written by the geometry and terrain layers, glm vectors,
    Python sets. Rather than hunt down every field, this makes the whole snapshot
    safe: arrays/vectors become lists, NumPy scalars become Python numbers, sets
    become lists, and anything else falls back to its string form.
    """
    # NumPy arrays and scalars both expose tolist() (scalars → a Python scalar).
    tolist = getattr(o, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:
            pass
    if isinstance(o, (set, frozenset)):
        return list(o)
    # glm vectors and other iterable sequence-likes.
    try:
        return list(o)
    except TypeError:
        return str(o)


def write(path: str, snapshot: dict) -> None:
    """Write *snapshot* to *path* as JSON (creating parent dirs as needed)."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, default=_json_default)


def read(path: str) -> dict:
    """Read and validate a Fio save file at *path*; returns the snapshot dict."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not data.get(_MAGIC):
        raise ValueError(f"'{path}' is not a Fio save file")
    ver = data.get("save_version", 0)
    if ver > SAVE_VERSION:
        raise ValueError(
            f"save '{path}' is version {ver}, newer than this build supports "
            f"(v{SAVE_VERSION})"
        )
    return data
