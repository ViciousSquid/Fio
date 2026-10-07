"""Which PlayerStart the player spawns at.

A level may have several PlayerStarts. Each has a ``name`` (the identifier a
LevelChanger's ``destination_spawn`` refers to) and an explicit ``primary``
flag: at most one start per level is primary, and that is where Play begins
unless a LevelChanger names another. Each also records ``creation_index``,
the order starts were created in its level, so "the earliest-created start"
never depends on the order entities happen to be listed or saved in.

The rules work on the starts' property dicts, so the editor (Things), the
logic thread and the standalone player (map records) all apply the same ones.
"""

from __future__ import annotations

PLAYER_START_TYPE = 'playerstart'


def _creation_key(position, properties):
    """Sort key for creation order; a start not yet given an index sorts last,
    in the order it is listed."""
    index = properties.get('creation_index')
    valid = isinstance(index, int) and not isinstance(index, bool)
    return (0, index, position) if valid else (1, 0, position)


def _earliest(starts, positions):
    positions = list(positions)
    if not positions:
        return None
    return min(positions, key=lambda i: _creation_key(i, starts[i]))


def earliest_created(starts):
    """Position in *starts* (property dicts) of the earliest-created, or None."""
    return _earliest(starts, range(len(starts)))


def pick_start(starts, name=None):
    """Position in *starts* (property dicts) of the start to spawn at, or None.

    With *name*: the start of that name (None if there is none -- it is never
    swapped for another). Without: the primary. A level whose starts were never
    settled (see :func:`settle`) has no primary yet; its earliest-created start
    is the one :func:`settle` would make primary.
    """
    if name:
        return _earliest(starts, [i for i, p in enumerate(starts) if p.get('name') == name])
    primaries = [i for i, p in enumerate(starts) if p.get('primary') is True]
    return _earliest(starts, primaries or range(len(starts)))


def settle(starts):
    """Hold *starts* (property dicts, edited in place) to the invariant.

    Every start gets a unique ``creation_index``: one without (just created, or
    from a map that predates it) is numbered after all the others, in listed
    order; a copy carrying its original's index is renumbered the same way.
    Then exactly one is primary when any exist: the earliest-created primary
    if several claim it, or -- when none does (the first start created, the
    primary was deleted, or an old map) -- the earliest-created start.

    Returns the positions of the starts that changed.
    """
    changed = set()
    valid = [p.get('creation_index') for p in starts
             if isinstance(p.get('creation_index'), int)
             and not isinstance(p.get('creation_index'), bool)]
    next_index = max(valid, default=-1) + 1
    seen = set()
    for position, properties in enumerate(starts):
        index = properties.get('creation_index')
        if (not isinstance(index, int) or isinstance(index, bool)
                or index in seen):
            properties['creation_index'] = next_index
            next_index += 1
            changed.add(position)
        seen.add(properties['creation_index'])
    if starts:
        keep = pick_start(starts)
        for position, properties in enumerate(starts):
            primary = position == keep
            if properties.get('primary') is not primary:
                properties['primary'] = primary
                changed.add(position)
    return sorted(changed)


def start_records(level_data):
    """The PlayerStart records of a map (``level_data['things']``)."""
    things = level_data.get('things') if isinstance(level_data, dict) else None
    return [t for t in (things or [])
            if isinstance(t, dict) and str(t.get('type', '')).lower() == PLAYER_START_TYPE]


def _record_properties(record):
    properties = record.get('properties')
    return properties if isinstance(properties, dict) else {}


def resolve_start_record(level_data, name=None):
    """The map record of the start to spawn at in *level_data*, or None."""
    records = start_records(level_data)
    position = pick_start([_record_properties(r) for r in records], name)
    return records[position] if position is not None else None


def start_names(level_data):
    """The names of a map's PlayerStarts, in creation order, primary first."""
    records = start_records(level_data)
    starts = [_record_properties(r) for r in records]
    primary = pick_start(starts)
    order = sorted(range(len(starts)), key=lambda i: (i != primary, _creation_key(i, starts[i])))
    names = []
    for i in order:
        name = starts[i].get('name')
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names
