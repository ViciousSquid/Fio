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

A solid model Prop carried that way takes its collision with it
(:class:`CarriedModels`): its collision proxy -- built once at Play start,
in the world where the model was (``LogicCollision.build_model_collision_brushes``)
-- is entered as a mover with a runtime transform, a translation by however
far its entity has been carried since. Every reader of a moving brush then
finds it where it is drawn: the spatial grid (filed for the whole of its
leader's travel), the AABB caches and broad phases, the player's mesh passes
(:func:`engine.brush_placement.collision_shape`), the grid's height columns
and the physics' shape poses. Only its position is carried, as the entity's
is: a turning leader orbits it but does not turn it, so neither the drawn
model nor its collision turns.

A solid model Prop with Rotate on (engine.model_rotate) is turned the same
way, grouped or not: its runtime transform also turns it by its Rotate angle
about its model axis through its origin (its ``pos``), in its own frame, as
the renderer draws it (``R(rotation) @ R_rotate @ S``). In world space that
is a turn by the same angle about ``R(rotation) . axis`` through ``pos``, so
the proxy's centre swings about the pivot and the shape turns about its
centre: a mesh proxy's triangles are turned once per transform for its
readers, an AABB proxy collides as its box turned (its broad phase reads
the AABB of the turned box). The grid files a turning proxy for every
centre the turn can give it (the sphere about its pivot) widened by its
box's radius, so a turn never takes it off range. A player standing on a
turning model is carried by the surface under them (moved, not turned).

A physics Prop is a body of its own, and a Prop with no collision has no
proxy: neither is touched.
"""

from __future__ import annotations

import math

import glm

from . import brush_placement
from .mover_table import CARRIED_TRAVEL_KEY, travel_range

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
        if props.get('type') == 'light' and str(props.get('path_target') or '').strip():
            continue        # a light on a path is moved by its path (engine.light_paths)
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


class CarriedModels:
    """The runtime source (:func:`engine.brush_placement.register_runtime`)
    for the collision proxies of carried or turning solid models: each proxy
    is a mover the resolver places where its drawn model is -- carried by
    however far its entity has moved since the proxy was built, and turned
    by its Rotate angle about its pivot -- and nothing else moves it.

    A tick costs a position read, an angle read and a comparison per model,
    and a stamp (:func:`~engine.brush_placement.note_runtime_change`) only
    when one moved; the readers' caches are keyed by that stamp, as they are
    for the mover table.
    """

    #: What :meth:`carry` writes into a proxy, or a reader caches on it from
    #: its transform, all taken out again by :meth:`clear` (``is_mover`` is
    #: put back to the False it was built with).
    _WRITTEN = (CARRIED_TRAVEL_KEY, '_travel_filed', '_aabb_sig', '_aabb_bounds',
                '_aabb_box', '_world_bounds', '_aabb_runtime', '_collision_entry',
                '_aabb_turn', '_collision_box', '_world_planes', '_turned_mesh',
                '_mesh_columns_turned')

    def __init__(self):
        #: ``id(proxy) -> (proxy, (cx, cy, cz, ax, ay, az, angle))`` while it
        #: is displaced or turned (the resolver reads this directly).
        self.runtime_entries = {}
        #: Per proxy: ``[proxy, entity, entity position when built, built
        #: centre, what it was last placed by ((position, angle)), the turn
        #: then (rows, or None), the authored rotation's rows (a turning
        #: model's, else None)]``.
        self._carried = []
        self._registered = False

    def __len__(self):
        return len(self._carried)

    def carry(self, proxies, carried, leader_of):
        """Carry the proxies (``_model_collision_brushes``) whose entity is
        in *carried* (``{id(entity): entity}``) or turns (Rotate on), and
        which are solid scenery, forgetting any carried before. *leader_of*:
        ``id(entity) -> leader brush``. Before the spatial grid is populated
        with them. Whether any was carried before or is now."""
        had = bool(self._carried)
        self.clear()
        for proxy in proxies:
            thing = proxy.get('_physics_entity')
            if thing is None or proxy.get('_physics_body') or not proxy.get('_model_collision'):
                continue
            grouped = id(thing) in carried
            basis = _turn_basis(thing)
            if not grouped and basis is None:
                continue
            pos = _position(thing)
            if pos is None:
                continue
            for key in self._WRITTEN:
                proxy.pop(key, None)
            centre = tuple(float(v) for v in brush_placement.authored_box(proxy)[:3])
            if grouped:
                low, high = _carried_travel(thing, pos, leader_of[id(thing)])
            else:
                low, high = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            if basis is not None:
                low, high = _turn_travel(low, high, centre, pos)
            proxy['is_mover'] = True
            proxy[CARRIED_TRAVEL_KEY] = (low, high, basis is not None)
            self._carried.append([proxy, thing, pos, centre, (pos, 0.0), None, basis])
        if self._carried and not self._registered:
            brush_placement.register_runtime(self)
            self._registered = True
        return had or bool(self._carried)

    def follow(self, player=None, ground=None):
        """Place each proxy where its model is drawn now; a player standing
        on one (*ground*) goes with the surface under them (moved, not
        turned: their facing is their own). Whether any moved."""
        changed = False
        entries = self.runtime_entries
        for item in self._carried:
            proxy, thing, built, home, seen, was_turn, basis = item
            pos = _position(thing)
            if pos is None:
                continue
            angle = _turn_angle(thing) if basis is not None else 0.0
            key = (pos, angle)
            if key == seen:
                continue
            item[4] = key
            turn = None
            if angle:
                axis = _world_axis(basis, getattr(thing, '_rotate_axis', 2))
                turn = brush_placement.rotation_matrix(axis, angle)
            if turn is None:
                centre = (home[0] + pos[0] - built[0], home[1] + pos[1] - built[1],
                          home[2] + pos[2] - built[2])
                runtime = centre + (0.0, 1.0, 0.0, 0.0)
            else:
                # The built centre turned about the pivot, where it is now.
                vx, vy, vz = home[0] - built[0], home[1] - built[1], home[2] - built[2]
                centre = (pos[0] + turn[0][0] * vx + turn[0][1] * vy + turn[0][2] * vz,
                          pos[1] + turn[1][0] * vx + turn[1][1] * vy + turn[1][2] * vz,
                          pos[2] + turn[2][0] * vx + turn[2][1] * vy + turn[2][2] * vz)
                runtime = centre + axis + (angle,)
            if player is not None and proxy is ground:
                _carry_rider(player, seen[0], was_turn, pos, turn)
            item[5] = turn
            changed = True
            if turn is None and centre == home:
                entries.pop(id(proxy), None)
            else:
                entries[id(proxy)] = (proxy, runtime)
                _check_range(proxy, centre)
        if changed:
            brush_placement.note_runtime_change()
        return changed

    def clear(self):
        """Every proxy carried is a still one again, and this source answers
        for none."""
        off_range = brush_placement.off_range
        count = len(off_range)
        for item in self._carried:
            proxy = item[0]
            off_range.pop(id(proxy), None)
            for key in self._WRITTEN:
                proxy.pop(key, None)
            proxy['is_mover'] = False
        if len(off_range) != count:
            brush_placement.note_off_range()
        self._carried = []
        self.runtime_entries = {}
        if self._registered:
            brush_placement.unregister_runtime(self)
            self._registered = False


def _position(thing):
    pos = getattr(thing, 'pos', None)
    try:
        return (float(pos[0]), float(pos[1]), float(pos[2]))
    except (TypeError, IndexError, ValueError):
        return None


def _turn_basis(thing):
    """The rows of the authored rotation of a model with Rotate on
    (engine.model_rotate) -- what carries its own axes into the world's --
    or None for one that does not turn."""
    props = getattr(thing, 'properties', None)
    if (not isinstance(props, dict) or not props.get('rotate_enabled')
            or not props.get('model_path')):
        return None
    from .logic_collision import model_basis
    basis = model_basis(props.get('rotation', (0.0, 0.0, 0.0)), (1.0, 1.0, 1.0))
    return tuple(tuple(float(v) for v in row) for row in basis)


def _turn_angle(thing):
    try:
        angle = float(getattr(thing, '_rotate_angle', 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return angle if math.isfinite(angle) else 0.0


def _world_axis(basis, index):
    """Model axis *index* (0, 1, 2) in world space: that column of *basis*."""
    try:
        i = int(index) % 3
    except (TypeError, ValueError):
        i = 2
    return (basis[0][i], basis[1][i], basis[2][i])


def _turn_travel(low, high, centre, pivot):
    """*low*/*high* (a centre's displacement range) widened by every place a
    turn about *pivot* can swing *centre* to: within the sphere through it
    about the pivot, whatever the axis or the angle."""
    offset = [centre[i] - pivot[i] for i in range(3)]
    radius = math.sqrt(sum(v * v for v in offset))
    return (tuple(low[i] - radius - offset[i] for i in range(3)),
            tuple(high[i] + radius - offset[i] for i in range(3)))


def _carry_rider(player, was_pos, was_turn, pos, turn):
    """Move a player standing on a model by how the model's motion moves
    the point they are at: ``p' = pos + R R_was^T (p - pos_was)``."""
    px, py, pz = float(player.pos.x), float(player.pos.y), float(player.pos.z)
    dx, dy, dz = px - was_pos[0], py - was_pos[1], pz - was_pos[2]
    if was_turn is not None:
        # Back to the built frame (R_was^T), then on by the turn now.
        dx, dy, dz = (was_turn[0][0] * dx + was_turn[1][0] * dy + was_turn[2][0] * dz,
                      was_turn[0][1] * dx + was_turn[1][1] * dy + was_turn[2][1] * dz,
                      was_turn[0][2] * dx + was_turn[1][2] * dy + was_turn[2][2] * dz)
    if turn is not None:
        dx, dy, dz = (turn[0][0] * dx + turn[0][1] * dy + turn[0][2] * dz,
                      turn[1][0] * dx + turn[1][1] * dy + turn[1][2] * dz,
                      turn[2][0] * dx + turn[2][1] * dy + turn[2][2] * dz)
    player.pos += glm.vec3(pos[0] + dx - px, pos[1] + dy - py, pos[2] + dz - pz)


def _carried_travel(thing, pos, leader):
    """``(low, high)``: the box a carried proxy's displacement from where it
    was built stays in while *leader* travels -- the leader's travel
    (mover_table.travel_range), widened, for a leader that turns, by every
    place the turn can swing the entity's offset to; less how far the entity
    had already been carried when the proxy was built."""
    lo, hi, turns, _reach = travel_range(leader)
    lead = brush_placement.authored_box(leader)
    home = thing.properties.get(ENTITY_HOME_KEY) or pos
    low = [lo[i] - lead[i] for i in range(3)]
    high = [hi[i] - lead[i] for i in range(3)]
    if turns:
        offset = [float(home[i]) - lead[i] for i in range(3)]
        radius = math.sqrt(sum(v * v for v in offset))
        for i in range(3):
            low[i] += -radius - offset[i]
            high[i] += radius - offset[i]
    already = [pos[i] - float(home[i]) for i in range(3)]
    return (tuple(low[i] - already[i] for i in range(3)),
            tuple(high[i] - already[i] for i in range(3)))


def _check_range(proxy, centre):
    """A proxy carried outside the travel the grid filed it for (a leader
    on a path, a restored placement) may be anywhere: off range, as a mover
    there is (MoverTable.check_range), until play stops."""
    filed = proxy.get('_travel_filed')
    if filed is None or id(proxy) in brush_placement.off_range:
        return
    lo, hi = filed[0], filed[1]
    if not all(lo[i] <= centre[i] <= hi[i] for i in range(3)):
        brush_placement.off_range[id(proxy)] = proxy
        brush_placement.note_off_range()


class LogicGroups:
    """Places each follower where its leader has carried it, once a tick."""

    def __init__(self, logic):
        self.logic = logic
        #: The collision proxies of the carried solid models.
        self.models = CarriedModels()
        self._leader_of = {}
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
        self._leader_of = {id(thing): leader for thing, leader in entities}
        self._links = list(by_leader.values())
        return bool(links or entities)

    def carry_models(self, proxies):
        """The model collision proxies were (re)built for play: carry those
        of carried or turning solid models (:class:`CarriedModels`). Called
        before the spatial grid is populated with them; whether any is
        carried, or was before (the grid must then be populated again)."""
        proxies = proxies or ()
        carried = {id(thing): thing for thing in self._entities}
        if not carried and not len(self.models) and not any(
                _turn_basis(p.get('_physics_entity')) is not None for p in proxies):
            return False
        return self.models.carry(proxies, carried, self._leader_of)

    def follow_models(self):
        """The carried or turning models' collision where they are drawn
        now (a turn has just been stepped: PropSession.update_rotations)."""
        if not len(self.models):
            return False
        player = self.logic.player_runtime.player
        ground = getattr(player, 'ground_object', None) if player else None
        return self.models.follow(player, ground)

    def detach(self):
        self.models.clear()
        self._leader_of = {}
        self._links = []
        for thing in self._entities:
            home = thing.properties.pop(ENTITY_HOME_KEY, None)
            if home is not None:
                thing.pos = list(home)
        self._entities = []
        detach(self.logic.editor_state.brushes)

    def update(self):
        if not self._links:
            if len(self.models):
                self.follow_models()        # models turning, none carried
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
        if len(self.models):
            # Their collision where they are now drawn.
            self.models.follow(player, ground)
