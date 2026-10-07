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


def _validate_record(item, kind, label, partial):
    if not isinstance(item, dict):
        raise ValueError(f"{label} is not an object")

    fields = ('pos', 'size') if kind == 'brushes' else ('pos',)
    for field in fields:
        value = item.get(field)
        if value is None:
            # Every brush ever written has both; play start indexes them.
            if kind == 'brushes' and not partial:
                raise ValueError(f"{label} has no '{field}'")
            continue
        if not _vector(value):
            raise ValueError(f"{label}['{field}'] must be a 3-element vector")

    if kind == 'brushes':
        for field in BRUSH_OBJECT_FIELDS:
            value = item.get(field)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{label}['{field}'] must be an object")
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
