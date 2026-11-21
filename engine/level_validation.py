"""Structural validation for maps and save games, before they reach the world.

Maps and ``.fiosave`` files are shareable input. The runtime indexes their
vectors, converts their scalars and keys dictionaries by their names without
checking each access, so a value of the wrong type only surfaced as an
exception once Play had started -- or when it tried to stop. These checks
refuse such a document up front, with a ``ValueError`` naming the field, before
the current scene or session is touched.

Kept free of Qt and editor imports so the player and ``engine.savegame`` can
use it as well as the editor.
"""

from __future__ import annotations

import math
import numbers

#: Brush fields that are always objects (per-face tables, angled geometry).
BRUSH_OBJECT_FIELDS = ('textures', 'uv_scale', 'uv_angle', 'uv_shift',
                       'uv_offset', 'uv_natural', 'geometry')

#: Brush scalars play start converts with float() and no guard.
BRUSH_NUMBER_FIELDS = ('door_speed', 'door_distance', 'door_lip', 'speed', 'distance')

#: Save ``runtime`` fields and the type each must have when present.
_RUNTIME_NUMBERS = ('player_health', 'player_max_health', 'player_armor', 'player2_health',
                    'player2_max_health', 'player_ammo', 'overhead_height',
                    'overhead_tilt')
_RUNTIME_STRINGS = ('current_hud_message', 'active_weapon', 'camera_mode',
                    'overhead_orientation')
#: State fields the engine reads without a default, so a save must carry them.
_STATE_REQUIRED = {
    'mover_path_states': ('lerp_t', 'wait_remaining', 'waiting',
                          'current_node', 'origin'),
}
_PLAYER_VECTORS = ('pos', 'velocity')
_PLAYER_NUMBERS = ('angle', 'pitch', 'camera_height')

#: Per-entity runtime state a save restores verbatim, and the type of each
#: field the engine reads without converting (taken from real saves).
_STATE_SCHEMAS = {
    'door_states': {'progress': 'num', 'open_timer': 'num', 'speed': 'num',
                    'distance': 'num', 'state': 'str', 'direction': 'vec'},
    'mover_states': {'progress': 'num', 'forward': 'bool'},
    'mover_path_states': {'lerp_t': 'num', 'wait_remaining': 'num',
                          'waiting': 'bool', 'current_node': 'str',
                          'origin': 'vec'},
    'timer_states': {'interval': 'num', 'remaining': 'num'},
    'monster_states': {'shoot_timer': 'num', 'anim_timer': 'num',
                       'vel_y': 'num', 'in_sight': 'bool'},
}


def finite_number(value) -> bool:
    """A real, finite number -- not a bool, string, NaN or infinity.

    ``numbers.Real`` so the NumPy scalars generated levels carry still pass.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return False
    return math.isfinite(value)


def _vector(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(finite_number(v) for v in value))


#: Largest coordinate a map may place anything at. Far beyond any authored
#: world (the bundled maps stay within 10,000 units); a value like 1e308
#: passes ``isfinite`` but overflows the spatial grid at play start.
WORLD_COORDINATE_LIMIT = 1.0e7


def _world_vector(value) -> bool:
    return _vector(value) and all(abs(v) <= WORLD_COORDINATE_LIMIT for v in value)


#: Most planes one angled brush may have. Building a brush's faces clips each
#: plane by every other (``engine.brush_geometry.compute_windings``), so the
#: cost grows with the square of the count: 1000 planes took 8.5 s for one
#: brush, and a crafted map could hang its load for minutes. Real brushes are
#: nowhere near: the bundled maps' largest has 9, a hull rebuild is limited to
#: 40 corners (at most 76 faces), and a missed clip adds no plane. 256 builds
#: in well under a second.
MAX_BRUSH_PLANES = 256


def _validate_planes(geometry, label):
    """An angled brush's planes: each a finite, non-zero normal ``n`` and a
    finite offset ``d`` within the world. A NaN normal used to load, leave the
    brush without a size, and fail Play start half-way."""
    planes = geometry.get('planes')
    if planes is None:
        return
    if not isinstance(planes, list):
        raise ValueError(f"{label}['geometry']['planes'] must be a list")
    if len(planes) > MAX_BRUSH_PLANES:
        raise ValueError(f"{label}['geometry']['planes'] has {len(planes)} planes "
                         f"(a brush may have at most {MAX_BRUSH_PLANES})")
    for index, plane in enumerate(planes):
        where = f"{label}['geometry']['planes'][{index}]"
        if not isinstance(plane, dict):
            raise ValueError(f"{where} must be an object")
        normal = plane.get('n')
        if not _vector(normal) or not any(abs(v) > 1e-12 for v in normal):
            raise ValueError(f"{where}['n'] must be a non-zero 3-element vector")
        offset = plane.get('d')
        if not finite_number(offset) or abs(offset) > WORLD_COORDINATE_LIMIT * 4:
            raise ValueError(f"{where}['d'] must be a number within the world")


#: ``terrain_data`` scalars the terrain converts or does arithmetic with.
TERRAIN_NUMBER_FIELDS = ('chunk_size', 'mesh_scale', 'base_resolution',
                         'offset_x', 'offset_y', 'offset_z',
                         'min_chunk_x', 'max_chunk_x', 'min_chunk_z', 'max_chunk_z',
                         'grass_density', 'grass_height', 'sculpt_grid_resolution',
                         'texture_paint_resolution', 'heightmap_strength')
#: ``terrain_data`` fields that are objects when present.
TERRAIN_OBJECT_FIELDS = ('appearance', 'custom_biome')
#: ``terrain_data`` fields that are lists of records when present.
TERRAIN_LIST_FIELDS = ('sculpt_offsets', 'sculpt_tiles', 'csg_subtractions', 'texture_paint',
                       'texture_paint_base', 'paint_stamps', 'texture_stamps')
#: A custom biome's noise parameters (engine.terrain.BiomeConfig).
TERRAIN_BIOME_NUMBER_FIELDS = (
    'base_height', 'height_scale', 'hills_scale', 'hills_intensity',
    'mountains_scale', 'mountains_intensity', 'mountains_sharpness',
    'valleys_scale', 'valleys_depth', 'plateaus_scale', 'plateaus_intensity',
    'plateaus_flatness', 'tree_density', 'terrain_height_scale')
#: Largest terrain seed: the noise seeds NumPy's generator with ``seed`` and
#: ``seed + 2000`` (engine.terrain.TerrainFeatures), which must fit 32 bits.
TERRAIN_SEED_MAX = 2 ** 32 - 1 - 2000


def _validate_terrain(terrain, where):
    """A map's ``terrain_data``.

    The terrain is rebuilt from it after the scene has been replaced, so a
    seed of ``"abc"``, a list ``biome`` or a string ``grass_density`` used to
    fail half-way through a load and leave a half-built untitled scene (or end
    Play, for a LevelChanger). Values the terrain already coerces safely --
    colours, paint, stamps -- are left to it; what would raise or put NaN in
    the heights is refused here, before anything changes.
    """
    label = f"a {where}'s 'terrain_data'"
    if not isinstance(terrain, dict):
        raise ValueError(f"{label} must be an object")
    seed = terrain.get('seed')
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, numbers.Integral)
                             or not 0 <= seed <= TERRAIN_SEED_MAX):
        raise ValueError(f"terrain_data['seed'] must be a whole number from 0 to "
                         f"{TERRAIN_SEED_MAX}")
    for field in TERRAIN_NUMBER_FIELDS:
        if field in terrain and not finite_number(terrain[field]):
            raise ValueError(f"terrain_data['{field}'] must be a number")
    for field in ('biome', 'heightmap_blob', 'heightmap_blend'):
        value = terrain.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"terrain_data['{field}'] must be a string")
    for field in TERRAIN_OBJECT_FIELDS:
        value = terrain.get(field)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"terrain_data['{field}'] must be an object")
    for field in TERRAIN_LIST_FIELDS:
        value = terrain.get(field)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"terrain_data['{field}'] must be a list")
    biome = terrain.get('custom_biome')
    if isinstance(biome, dict):
        for field in TERRAIN_BIOME_NUMBER_FIELDS:
            if field in biome and not finite_number(biome[field]):
                raise ValueError(f"terrain_data['custom_biome']['{field}'] must be a number")
    _validate_sculpt(terrain.get('sculpt_offsets'))
    for index, cut in enumerate(terrain.get('csg_subtractions') or ()):
        if (not isinstance(cut, (list, tuple)) or len(cut) != 6
                or not all(finite_number(v) and abs(v) <= WORLD_COORDINATE_LIMIT for v in cut)):
            raise ValueError(f"terrain_data['csg_subtractions'][{index}] must be six "
                             f"numbers within the world")


def _validate_sculpt(entries):
    """Sculpt offsets: ``[gx, gz, offset]`` triples of finite numbers.

    JSON as Python reads it allows ``NaN`` and ``Infinity``; either would
    reach the heights, and from there collision. A sculpted terrain carries up
    to millions of entries, so the common, valid case is checked in one NumPy
    pass and only a failure is walked entry by entry for its message.
    """
    if not entries:
        return
    import numpy as np
    try:
        packed = np.asarray(entries)
        if (packed.dtype.kind in 'iuf' and packed.ndim == 2 and packed.shape[1] == 3
                and np.isfinite(packed).all()):
            return
    except (TypeError, ValueError, OverflowError):
        pass
    for index, entry in enumerate(entries):
        if (not isinstance(entry, (list, tuple)) or len(entry) != 3
                or not all(finite_number(v) for v in entry)):
            raise ValueError(f"terrain_data['sculpt_offsets'][{index}] must be "
                             f"[gx, gz, offset], three finite numbers")


def validate_level(level_data, *, partial=False, where="map") -> None:
    """Raise ``ValueError`` if *level_data* is not shaped like a map.

    ``partial`` is for save-game deltas, whose records carry only the fields
    that changed: nothing is required, but whatever is present is checked.
    """
    if not isinstance(level_data, dict):
        raise ValueError(f"a {where} must be a JSON object")
    for kind in ('brushes', 'things'):
        items = level_data.get(kind) or []
        if not isinstance(items, list):
            raise ValueError(f"a {where}'s '{kind}' must be a list")
        for index, item in enumerate(items):
            _validate_record(item, kind, f"{kind}[{index}]", partial)
    _validate_items(level_data.get('items'), where)
    if level_data.get('terrain_data') is not None:
        _validate_terrain(level_data['terrain_data'], where)


def _validate_items(items, where):
    """A map's ``items``: definitions of the custom item slots, by id.

    Only the shape is checked here. A definition whose *settings* are wrong
    still loads -- it is kept as authored, and its slot resolves to no item
    until it is fixed (see :mod:`engine.items`).
    """
    if items is None:
        return
    from engine.items import CUSTOM_ITEM_IDS, ITEM_IDS
    if not isinstance(items, dict):
        raise ValueError(f"a {where}'s 'items' must be an object")
    for item_id, definition in items.items():
        if item_id in ITEM_IDS and item_id not in CUSTOM_ITEM_IDS:
            raise ValueError(f"items['{item_id}'] is a built-in item and cannot be redefined")
        if item_id not in CUSTOM_ITEM_IDS:
            raise ValueError(f"items['{item_id}'] is not an item slot "
                             f"(custom slots: {', '.join(CUSTOM_ITEM_IDS)})")
        if not isinstance(definition, dict):
            raise ValueError(f"items['{item_id}'] must be an object")


def _has_planes(item):
    geometry = item.get('geometry')
    return isinstance(geometry, dict) and bool(geometry.get('planes'))


def _validate_record(item, kind, label, partial):
    if not isinstance(item, dict):
        raise ValueError(f"{label} is not an object")

    fields = ('pos', 'size') if kind == 'brushes' else ('pos',)
    for field in fields:
        value = item.get(field)
        if value is None:
            # Every brush has a pos; play start indexes it. Every box has a
            # size; an angled brush's is derived from its planes and is not
            # saved from map version 5 on (engine.brush_placement).
            if field == 'size' and _has_planes(item):
                continue
            if kind == 'brushes' and not partial:
                raise ValueError(f"{label} has no '{field}'")
            continue
        if not _vector(value):
            raise ValueError(f"{label}['{field}'] must be a 3-element vector")
        if not _world_vector(value):
            raise ValueError(f"{label}['{field}'] lies beyond the world "
                             f"(more than {WORLD_COORDINATE_LIMIT:g} units out)")

    if kind == 'brushes':
        for field in BRUSH_OBJECT_FIELDS:
            value = item.get(field)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{label}['{field}'] must be an object")
        if isinstance(item.get('geometry'), dict):
            _validate_planes(item['geometry'], label)
        for field in BRUSH_NUMBER_FIELDS:
            if field in item and not finite_number(item[field]):
                raise ValueError(f"{label}['{field}'] must be a number")

    connections = item.get('io_connections')
    if connections is not None and (
            not isinstance(connections, list)
            or not all(isinstance(c, dict) for c in connections)):
        raise ValueError(f"{label}['io_connections'] must be a list of objects")

    props = item if kind == 'brushes' else item.get('properties')
    if props is None:
        return
    if not isinstance(props, dict):
        raise ValueError(f"{label}['properties'] must be an object")
    # Position-valued fields (a mover's ``original_pos``, ``parent_local_pos``,
    # the runtime's ``_prop_home_pos`` a save carries) are copied into ``pos``.
    for field, value in props.items():
        if field.endswith('_pos') and value is not None and not _vector(value):
            raise ValueError(f"{label}['{field}'] must be a 3-element vector")
    if kind == 'brushes' and props.get('direction') is not None \
            and not _vector(props['direction']):
        raise ValueError(f"{label}['direction'] must be a 3-element vector")
    # Names and ids key the runtime's lookup tables. An odd but hashable value
    # (a numeric name) is tolerated; a list or object cannot be a key at all.
    for field in ('name', 'id'):
        value = props.get(field)
        if isinstance(value, (list, dict)):
            raise ValueError(f"{label}['{field}'] must be a name, not a {type(value).__name__}")
    if kind == 'things':
        # Entity colours are RGB lists (brushes carry their own format).
        for field in ('colour', 'color'):
            value = props.get(field)
            if value is not None and (
                    not isinstance(value, (list, tuple)) or len(value) < 3
                    or not all(finite_number(v) for v in value[:3])):
                raise ValueError(f"{label}['{field}'] must be an RGB list")
        record_type = str(item.get('type', '')).lower()
        if record_type == 'playerstart':
            if 'primary' in props and not isinstance(props['primary'], bool):
                raise ValueError(f"{label}['primary'] must be true or false")
            index = props.get('creation_index')
            if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
                raise ValueError(f"{label}['creation_index'] must be a whole number")
        elif record_type == 'levelchanger':
            if not isinstance(props.get('destination_spawn', ''), str):
                raise ValueError(f"{label}['destination_spawn'] must be a PlayerStart name")



def validate_snapshot(data) -> None:
    """Raise ``ValueError`` if a save game's contents are malformed.

    Called on read, so a bad save is refused before the live session changes.
    """
    level = data.get('level')
    if level is not None:
        validate_level(level, where="save's level")
    delta = data.get('delta')
    if delta is not None:
        if not isinstance(delta, dict):
            raise ValueError("a save's 'delta' must be an object")
        if delta.get('level') is not None:
            validate_level(delta['level'], partial=True, where="save's delta")
    cells = data.get('cell_deltas')
    if cells is not None:
        if not isinstance(cells, dict):
            raise ValueError("a save's 'cell_deltas' must be an object")
        for cell, record in cells.items():
            validate_level(record, partial=True, where=f"save's cell {cell}")

    for key in ('player', 'player2'):
        player = data.get(key)
        if player is None:
            continue
        if not isinstance(player, dict):
            raise ValueError(f"a save's '{key}' must be an object")
        for field in _PLAYER_VECTORS:
            if player.get(field) is not None and not _vector(player[field]):
                raise ValueError(f"save {key}['{field}'] must be a 3-element vector")
        for field in _PLAYER_NUMBERS:
            if player.get(field) is not None and not finite_number(player[field]):
                raise ValueError(f"save {key}['{field}'] must be a number")

    runtime = data.get('runtime')
    if runtime is None:
        return
    if not isinstance(runtime, dict):
        raise ValueError("a save's 'runtime' must be an object")
    for field in _RUNTIME_NUMBERS:
        if runtime.get(field) is not None and not finite_number(runtime[field]):
            raise ValueError(f"save runtime['{field}'] must be a number")
    for field in _RUNTIME_STRINGS:
        if runtime.get(field) is not None and not isinstance(runtime[field], str):
            raise ValueError(f"save runtime['{field}'] must be a string")
    weapons = runtime.get('weapons')
    if weapons is not None and not (isinstance(weapons, list)
                                    and all(isinstance(w, str) for w in weapons)):
        raise ValueError("save runtime['weapons'] must be a list of item ids")
    for family, schema in _STATE_SCHEMAS.items():
        states = runtime.get(family)
        if states is None:
            continue
        if not isinstance(states, dict):
            raise ValueError(f"save runtime['{family}'] must be an object")
        for key, state in states.items():
            if not isinstance(state, dict):
                raise ValueError(f"save runtime['{family}'][{key!r}] must be an object")
            missing = [f for f in _STATE_REQUIRED.get(family, ()) if f not in state]
            if missing:
                raise ValueError(
                    f"save runtime['{family}'][{key!r}] is missing {', '.join(missing)}")
            for field, kind in schema.items():
                if field in state and not _CHECKS[kind](state[field]):
                    raise ValueError(
                        f"save runtime['{family}'][{key!r}]['{field}'] has the wrong type")
    keys = runtime.get('collected_keys')
    if keys is not None and (not isinstance(keys, list)
                             or not all(isinstance(k, str) for k in keys)):
        raise ValueError("save runtime['collected_keys'] must be a list of names")


_CHECKS = {
    'num': finite_number,
    'vec': _vector,
    'bool': lambda v: isinstance(v, bool),
    'str': lambda v: isinstance(v, str),
}
