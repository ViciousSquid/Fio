"""Brush groups in Play: grouped brushes and entities ride the group's mover
or door.

Brushes sharing a ``brush_group`` name are attached to one another (the
editor moves them together: ``editor.brush_groups``). In Play the member
that moves -- a mover or a door, the group's *leader* -- carries the members
that do not: a door and the glass set into it, a lift and its railings.

A carried brush (a *follower*) has to be a brush with a runtime transform to
every reader -- rendering, collision, the physics grid, picking -- and those
all ask one question: is it a mover or a door. So for the play session a
follower is entered in the mover table as a mover that never moves by itself
(``start_on`` off, no distance, no speed), and each tick it is placed where
its leader has carried it (:meth:`MoverTable.restore_placement`, the call a
saved game places a mover with): its authored offset from the leader, turned
by the leader's spin about the leader's centre, and turned by the same spin
itself. A group whose leader has not moved since the last tick costs one
position read and a comparison, however many brushes it carries. A player
standing on a follower is carried with it, as one on the mover itself is.

What play writes into a follower's dict is put back when play stops
(:func:`detach`); ``group_follower`` marks one in a saved game, so loading
it does not mistake the follower for a mover of its own.

Entities in a group (``brush_group`` in their properties) are carried the
same way: each tick the leader has moved, an entity is placed at its
authored offset from the leader, turned by the leader's spin about the
leader's centre. Its position is all that is carried (an entity's facing is
its own). An entity parented to a mover (``parent_mover``) follows that
mover instead. Where an entity was authored is put back when play stops.
"""

from __future__ import annotations

import glm

from . import brush_placement

GROUP_KEY = 'brush_group'
FOLLOWER_KEY = 'group_follower'
#: What a follower's own values were before play (runtime-only: not saved).
_RESTORE_KEY = '_group_restore'
#: Where a carried entity was before play (runtime-only: not saved).
ENTITY_HOME_KEY = '_group_home'
_MISSING = object()


def _is_leader(brush):
    return bool((brush.get('is_mover') or brush.get('is_door'))
                and not brush.get(FOLLOWER_KEY))


def _follower_values(leader):
    """What makes a brush a mover that only goes where it is put."""
    return {
        'is_mover': True,
        'start_on': False,
        'move_once': False,
        'path_target': '',
        'distance': 0.0,
        'speed': 0.0,
        'rotate': bool(leader.get('is_mover') and leader.get('rotate')),
        'rot_axis': list(leader.get('rot_axis') or (0.0, 1.0, 0.0)),
        FOLLOWER_KEY: True,
    }


def _group_name(props):
    return str(props.get(GROUP_KEY) or '').strip() if isinstance(props, dict) else ''


def _brushes_by_group(brushes):
    by_group = {}
    for brush in brushes:
        name = _group_name(brush)
        if name:
            by_group.setdefault(name, []).append(brush)
    return by_group


def _leader_of(chosen):
    leaders = [b for b in chosen if _is_leader(b)]
    if not leaders:
        return None
    # A door first: a door with a mover's flag too still opens as a door.
    return next((b for b in leaders if b.get('is_door')), leaders[0])


def leaders(brushes):
    """``{group name: leader brush}`` for every group that has a mover or a
    door among its brushes."""
    found = {}
    for name, chosen in _brushes_by_group(brushes).items():
        leader = _leader_of(chosen)
        if leader is not None:
            found[name] = leader
    return found


def carried_entities(brushes, things):
    """``[(entity, leader)]``: every entity in a group whose brushes have a
    leader, except one parented to a mover (``parent_mover``: that mover
    carries it)."""
    by_name = leaders(brushes)
    if not by_name:
        return []
    links = []
    for thing in things:
        props = getattr(thing, 'properties', None)
        name = _group_name(props)
        if not name or name not in by_name or props.get('parent_mover'):
            continue
        if getattr(thing, 'pos', None) is None:
            continue
        links.append((thing, by_name[name]))
    return links


def attach(brushes):
    """Make every grouped brush that does not move a follower of its group's
    leader. Returns ``[(follower, leader)]``. Safe to call again."""
    links = []
    for chosen in _brushes_by_group(brushes).values():
        leader = _leader_of(chosen)
        if leader is None:
            continue
        for brush in chosen:
            if _is_leader(brush):
                continue
            values = _follower_values(leader)
            if _RESTORE_KEY not in brush:
                brush[_RESTORE_KEY] = (
                    {} if brush.get(FOLLOWER_KEY)      # from a saved game
                    else {key: brush.get(key, _MISSING) for key in values})
            brush.update(values)
            links.append((brush, leader))
    return links


def detach(brushes):
    """Play stopped: followers are plain brushes again."""
    for brush in brushes:
        saved = brush.pop(_RESTORE_KEY, None)
        if saved is None and not brush.get(FOLLOWER_KEY):
            continue
        for key in _follower_values(brush):
            value = (saved or {}).get(key, _MISSING)
            if value is _MISSING:
                brush.pop(key, None)
            else:
                brush[key] = value


class LogicGroups:
    """Places each follower where its leader has carried it, once a tick."""

    def __init__(self, logic):
        self.logic = logic
        #: One entry per leader: [leader, [(follower, offset from the
        #: leader's authored centre)], where it was last seen (centre, angle),
        #: [(entity, offset from the leader's authored centre)]]
        self._links = []
        self._entities = []

    def attach(self):
        """Before the mover rows are built: enter the followers as movers,
        and note the entities each leader carries."""
        state = self.logic.editor_state
        links = attach(state.brushes)
        by_leader = {}

        def entry_for(leader):
            return by_leader.setdefault(id(leader), [leader, [], None, []])

        for follower, leader in links:
            home = brush_placement.authored_box(follower)
            lead = brush_placement.authored_box(leader)
            entry_for(leader)[1].append((follower, (
                home[0] - lead[0], home[1] - lead[1], home[2] - lead[2])))
        entities = carried_entities(state.brushes, getattr(state, 'things', ()) or ())
        self._entities = []
        for thing, leader in entities:
            props = thing.properties
            if ENTITY_HOME_KEY not in props:
                props[ENTITY_HOME_KEY] = [float(v) for v in thing.pos[:3]]
            home = props[ENTITY_HOME_KEY]
            lead = brush_placement.authored_box(leader)
            entry_for(leader)[3].append((thing, (
                home[0] - lead[0], home[1] - lead[1], home[2] - lead[2])))
            self._entities.append(thing)
        self._links = list(by_leader.values())
        return bool(links or entities)

    def detach(self):
        self._links = []
        for thing in self._entities:
            home = thing.properties.pop(ENTITY_HOME_KEY, None)
            if home is not None:
                thing.pos = list(home)
        self._entities = []
        detach(self.logic.editor_state.brushes)

    def update(self):
        if not self._links:
            return
        table = self.logic.mover_runtime._movers()
        # A player standing on a carried brush goes with it, as one standing
        # on the mover itself does.
        player = self.logic.player_runtime.player
        ground = getattr(player, 'ground_object', None) if player else None
        for entry in self._links:
            leader, followers, seen, entities = entry
            now = table.position_of(leader)
            spin = brush_placement.runtime_spin(leader)
            angle = float(spin[3]) if spin is not None else 0.0
            key = (now, angle)
            if key == seen:
                continue                # the leader is still: so is its group
            entry[2] = key
            turn = brush_placement.rotation_matrix(spin[:3], angle) if angle else None

            def carried(offset):
                dx, dy, dz = offset
                if turn is not None:
                    dx, dy, dz = (
                        turn[0][0] * offset[0] + turn[0][1] * offset[1] + turn[0][2] * offset[2],
                        turn[1][0] * offset[0] + turn[1][1] * offset[1] + turn[1][2] * offset[2],
                        turn[2][0] * offset[0] + turn[2][1] * offset[1] + turn[2][2] * offset[2])
                return now[0] + dx, now[1] + dy, now[2] + dz

            for follower, offset in followers:
                x, y, z = carried(offset)
                if follower is ground:
                    was = table.position_of(follower)
                    player.pos += glm.vec3(x - was[0], y - was[1], z - was[2])
                table.restore_placement(follower, (x, y, z), angle)
            for thing, offset in entities:
                thing.pos = [float(v) for v in carried(offset)]
