"""Brush groups in Play: grouped brushes ride the group's mover or door.

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
position read and a comparison, however many brushes it carries.

What play writes into a follower's dict is put back when play stops
(:func:`detach`); ``group_follower`` marks one in a saved game, so loading
it does not mistake the follower for a mover of its own.
"""

from __future__ import annotations

from . import brush_placement

GROUP_KEY = 'brush_group'
FOLLOWER_KEY = 'group_follower'
#: What a follower's own values were before play (runtime-only: not saved).
_RESTORE_KEY = '_group_restore'
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


def attach(brushes):
    """Make every grouped brush that does not move a follower of its group's
    leader. Returns ``[(follower, leader)]``. Safe to call again."""
    by_group = {}
    for brush in brushes:
        name = str(brush.get(GROUP_KEY) or '').strip()
        if name:
            by_group.setdefault(name, []).append(brush)
    links = []
    for chosen in by_group.values():
        leaders = [b for b in chosen if _is_leader(b)]
        if not leaders:
            continue
        # A door first: a door with a mover's flag too still opens as a door.
        leader = next((b for b in leaders if b.get('is_door')), leaders[0])
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
        #: leader's authored centre)], where it was last seen (centre, angle)]
        self._links = []

    def attach(self):
        """Before the mover rows are built: enter the followers as movers."""
        links = attach(self.logic.editor_state.brushes)
        by_leader = {}
        for follower, leader in links:
            home = brush_placement.authored_box(follower)
            lead = brush_placement.authored_box(leader)
            entry = by_leader.setdefault(id(leader), [leader, [], None])
            entry[1].append((follower, (
                home[0] - lead[0], home[1] - lead[1], home[2] - lead[2])))
        self._links = list(by_leader.values())
        return bool(links)

    def detach(self):
        self._links = []
        detach(self.logic.editor_state.brushes)

    def update(self):
        if not self._links:
            return
        table = self.logic.mover_runtime._movers()
        for entry in self._links:
            leader, followers, seen = entry
            now = table.position_of(leader)
            spin = brush_placement.runtime_spin(leader)
            angle = float(spin[3]) if spin is not None else 0.0
            key = (now, angle)
            if key == seen:
                continue                # the leader is still: so is its group
            entry[2] = key
            turn = brush_placement.rotation_matrix(spin[:3], angle) if angle else None
            for follower, offset in followers:
                dx, dy, dz = offset
                if turn is not None:
                    dx, dy, dz = (
                        turn[0][0] * offset[0] + turn[0][1] * offset[1] + turn[0][2] * offset[2],
                        turn[1][0] * offset[0] + turn[1][1] * offset[1] + turn[1][2] * offset[2],
                        turn[2][0] * offset[0] + turn[2][1] * offset[1] + turn[2][2] * offset[2])
                table.restore_placement(
                    follower, (now[0] + dx, now[1] + dy, now[2] + dz), angle)
