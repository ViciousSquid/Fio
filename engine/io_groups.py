"""Groups as I/O targets.

A connection's target may name a *group* instead of one entity: every member
then receives the input, in scene order, each exactly as if it had been the
target, and a member that does not accept the input ignores it (a ``TurnOff``
sent to a group of lights and a door turns the lights off; the door does
nothing). This is how one signal switches a whole bank of lights.

Two kinds of group are named in a map, and both are targets:

* **visgroups** -- the Scene Hierarchy's *Visgroup > New Visgroup from
  Selection*: any brushes and entities, lights included, by stable id
  (``engine.view_filters``; saved as the map's ``visgroups``);
* **brush groups** -- brushes sharing a ``brush_group`` name.

A group with the same name as an entity never shadows it: an entity name
resolves to that entity, exactly as before groups were targets.
"""

from engine.logic_groups import GROUP_KEY


def visgroups_of_state(state):
    """The user visgroups an editor or player world state carries."""
    filters = getattr(state, 'view_filters', None)
    return list(getattr(filters, 'visgroups', None) or ())


def group_names(state):
    """Every group name in *state*, visgroups first, without repeats."""
    names = []
    for group in visgroups_of_state(state):
        if group.name and group.name not in names:
            names.append(group.name)
    for brush in getattr(state, 'brushes', ()) or ():
        name = str(brush.get(GROUP_KEY) or '').strip()
        if name and name not in names:
            names.append(name)
    return names


def group_members(state, name):
    """The members of every group called *name*, in scene order (brushes,
    then entities), or None when no group has that name."""
    name = str(name or '').strip()
    if not name:
        return None
    ids = set()
    found = False
    for group in visgroups_of_state(state):
        if group.name == name:
            found = True
            ids.update(group.ids)
    members = []
    for brush in getattr(state, 'brushes', ()) or ():
        if str(brush.get(GROUP_KEY) or '').strip() == name:
            found = True
            members.append(brush)
        elif ids and str(brush.get('id') or '') in ids:
            members.append(brush)
    if ids:
        for thing in getattr(state, 'things', ()) or ():
            props = getattr(thing, 'properties', None)
            if isinstance(props, dict) and str(props.get('id') or '') in ids:
                members.append(thing)
    return members if found else None
