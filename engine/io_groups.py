"""Groups as I/O targets.

A connection's target may name a *group* instead of one entity: every member
then receives the input, in scene order, each exactly as if it had been the
target, and a member that does not accept the input ignores it (a ``TurnOff``
sent to a group of lights and a door turns the lights off; the door does
nothing). This is how one signal switches a whole bank of lights.

Two kinds of group are targets:

* **visgroups** -- the Scene Hierarchy's *Visgroup > New Visgroup from
  Selection*: any brushes and entities, lights included, by stable id
  (``engine.view_filters``; saved as the map's ``visgroups``). Each has its
  own UUID, and a connection stores it as its ``target_id``, exactly as it
  stores an entity's: the group is found by that id first and by name only
  when the id finds nothing, so renaming a group keeps its connections;
* **brush groups** -- brushes sharing a ``brush_group`` name, by that name.

Resolution follows the entity rule, id first: an entity's id, a group's id,
an entity's name, a group's name. A group never shadows an entity of the
same name.
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


def groups(state):
    """``(id, name)`` of every group in *state*: visgroups by their UUID,
    then brush groups (whose id is '')."""
    found = [(group.id, group.name) for group in visgroups_of_state(state)
             if group.name]
    named = {name for _gid, name in found}
    for name in group_names(state):
        if name not in named:
            found.append(('', name))
    return found


def group_name_of(state, group_id):
    """The current name of the visgroup whose id is *group_id*, or ''."""
    for group in visgroups_of_state(state):
        if group_id and group.id == group_id:
            return group.name
    return ''


def group_id_of(state, name):
    """The UUID of the first visgroup called *name*, or '' (none, or a
    brush group, which has no id)."""
    for group in visgroups_of_state(state):
        if group.name == name:
            return group.id
    return ''


def group_members_by_id(state, group_id):
    """The members of the visgroup whose UUID is *group_id*, in scene order,
    or None when no visgroup has that id."""
    group_id = str(group_id or '')
    if not group_id:
        return None
    for group in visgroups_of_state(state):
        if group.id == group_id:
            return _members(state, set(group.ids), None)
    return None


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
    if not found:
        found = any(str(brush.get(GROUP_KEY) or '').strip() == name
                    for brush in getattr(state, 'brushes', ()) or ())
    if not found:
        found = any(isinstance(getattr(thing, 'properties', None), dict)
                    and str(thing.properties.get(GROUP_KEY) or '').strip() == name
                    for thing in getattr(state, 'things', ()) or ())
    return _members(state, ids, name) if found else None


def resolve_group(state, name='', group_id=''):
    """The members of the group a connection targets -- by *group_id* first,
    then by *name* -- or None when neither names a group."""
    members = group_members_by_id(state, group_id)
    if members is None:
        members = group_members(state, name)
    return members


def _members(state, ids, brush_group):
    """Brushes then entities in scene order: those whose id is in *ids*, and
    those whose ``brush_group`` is *brush_group* -- a group may hold entities
    (models, lights, props) as well as brushes, and they are members too."""
    members = []
    for brush in getattr(state, 'brushes', ()) or ():
        if brush_group and str(brush.get(GROUP_KEY) or '').strip() == brush_group:
            members.append(brush)
        elif ids and str(brush.get('id') or '') in ids:
            members.append(brush)
    if ids or brush_group:
        for thing in getattr(state, 'things', ()) or ():
            props = getattr(thing, 'properties', None)
            if not isinstance(props, dict):
                continue
            if brush_group and str(props.get(GROUP_KEY) or '').strip() == brush_group:
                members.append(thing)
            elif ids and str(props.get('id') or '') in ids:
                members.append(thing)
    return members
