"""Brush groups: brushes that are attached to one another.

A group is every brush carrying the same ``brush_group`` name (saved with the
map as an ordinary brush key). Grouped brushes move together:

* in the editor, a move or a rotation of one member carries the others
  (:func:`with_mates` widens the set a drag moves);
* in Play, a member that is a mover or a door carries the members that are
  not (``engine.logic_groups``) -- a door and the glass set into it.

Qt-free: the hierarchy, the group dialog and the Visgroups window all read
groups through here.
"""

GROUP_KEY = 'brush_group'

#: Outline / tint colours handed to groups in the order they first appear in
#: the brush list. Distinct from Fio's selection orange and search yellow.
PALETTE = (
    '#4FC3F7', '#81C784', '#BA68C8', '#FFD54F', '#F06292', '#4DB6AC',
    '#9575CD', '#AED581', '#FF8A65', '#7986CB', '#E57373', '#4DD0E1',
)


#: A group moves as one body in Play, carried by its one mover or door.
MAX_MOVERS_MESSAGE = "Maximum of 1 mover per group"


def is_mover(brush):
    """A brush that moves by itself in Play: a mover or a door."""
    return bool(isinstance(brush, dict)
                and (brush.get('is_mover') or brush.get('is_door')))


def too_many_movers(brushes):
    """Whether *brushes*, as one group, would hold more than one mover."""
    seen, count = set(), 0
    for brush in brushes:
        if id(brush) not in seen and is_mover(brush):
            seen.add(id(brush))
            count += 1
    return count > 1


def group_of(brush):
    """The name of the group *brush* is in, or ``''``."""
    if not isinstance(brush, dict):
        return ''
    return str(brush.get(GROUP_KEY) or '').strip()


def groups(brushes):
    """``{name: [members]}`` in the order the groups first appear."""
    found = {}
    for brush in brushes:
        name = group_of(brush)
        if name:
            found.setdefault(name, []).append(brush)
    return found


def members(brushes, name):
    return [b for b in brushes if name and group_of(b) == name]


def colours(brushes):
    """``{name: '#rrggbb'}``: each group's colour, by order of appearance."""
    return {name: PALETTE[i % len(PALETTE)]
            for i, name in enumerate(groups(brushes))}


def with_mates(brushes, objects):
    """*objects* plus every brush grouped with one of them, each once.

    The order of *objects* is kept and the mates follow, so "the first
    object" is still the one that was grabbed.
    """
    objects = list(objects)
    names = {group_of(o) for o in objects}
    names.discard('')
    if not names:
        return objects
    seen = {id(o) for o in objects}
    for brush in brushes:
        if id(brush) not in seen and group_of(brush) in names:
            seen.add(id(brush))
            objects.append(brush)
    return objects


def unique_name(brushes, base='Group'):
    taken = set(groups(brushes))
    number = 1
    while '%s %d' % (base, number) in taken:
        number += 1
    return '%s %d' % (base, number)


def set_group(brushes, name):
    """Put *brushes* in group *name* (``''`` takes them out of any group)."""
    name = str(name or '').strip()
    for brush in brushes:
        if name:
            brush[GROUP_KEY] = name
        else:
            brush.pop(GROUP_KEY, None)


def rename(brushes, old, new):
    """Rename group *old* to *new*; a name already in use merges the two."""
    new = str(new or '').strip()
    if not new or new == old:
        return False
    for brush in members(brushes, old):
        brush[GROUP_KEY] = new
    return True


def ordered(indexed_brushes):
    """``[(index, brush)]`` with each group's members made adjacent.

    A group sits where its first member was; everything else keeps its
    place, so any sort order stays recognisable.
    """
    indexed_brushes = list(indexed_brushes)
    by_group = {}
    for entry in indexed_brushes:
        name = group_of(entry[1])
        if name:
            by_group.setdefault(name, []).append(entry)
    result, placed = [], set()
    for entry in indexed_brushes:
        name = group_of(entry[1])
        if not name:
            result.append(entry)
        elif name not in placed:
            placed.add(name)
            result.extend(by_group[name])
    return result
