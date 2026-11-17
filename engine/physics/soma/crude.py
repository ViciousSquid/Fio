"""Soma's crude tier: Fio's dynamic bodies exactly as they were before Soma.

The engine's original ``PhysicsWorld``, kept as it was so that a map can
play as it always did: a box that falls, slides, rests on box tops, bounces,
rolls as a ball does and is pushed by the player. What the full tier adds is
deliberately absent -- bodies do not meet bodies, a slope is the box around
it, the terrain is not ground, and a body that hits a wall goes back to
where its step began.

It predates the rule that a physics keeps no copy of the world, and is the
one exception to it: it snapshots the grid's boxes per cell when it is
rebuilt, so a door or mover is a wall where play began. That is how it was,
and how it stays. The class is otherwise the code that shipped, with the
contract's ``reset_controls`` added and its names changed; the tests it
shipped with still pin it (``tests/physics/test_crude_*.py``).
"""
import math

import glm
import numpy as np

from ...change_journal import JOURNAL, OVERFLOW, STATE, VISIBILITY, touch
from ...brush_placement import world_bounds, world_box
from ...spatial import TIER_NEAR, cells_of_points, tier_of
from ...spatial_grid import SpatialGrid
from ..api import SV_GRAVITY


def _quat_multiply(a, b):
    aw, ax, ay, az = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    bw, bx, by, bz = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ), axis=1)


def _axis_quat(axis, radians):
    q = np.zeros((radians.shape[0], 4), dtype=np.float64)
    q[:, 0] = np.cos(radians * 0.5)
    q[:, 1 + axis] = np.sin(radians * 0.5)
    return q


def quat_from_rotation(rotation):
    """Quaternions for Fio ``rotation`` rows ``[x, y, z]`` in degrees.

    Fio draws a model with ``Ry(y) * Rx(x) * Rz(z)`` (see
    ``engine.entity_table._model_transform_columns``); this is that product.
    """
    r = np.radians(np.asarray(rotation, dtype=np.float64).reshape(-1, 3))
    return _quat_multiply(_quat_multiply(_axis_quat(1, r[:, 1]),
                                         _axis_quat(0, r[:, 0])),
                          _axis_quat(2, r[:, 2]))


def rotation_from_quat(q):
    """Fio ``rotation`` rows (Y-X-Z Euler degrees) for (N, 4) quaternions."""
    q = np.asarray(q, dtype=np.float64).reshape(-1, 4)
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    r12 = 2.0 * (y * z - w * x)
    pitch = np.arcsin(np.clip(-r12, -1.0, 1.0))
    yaw = np.arctan2(2.0 * (x * z + w * y), 1.0 - 2.0 * (x * x + y * y))
    roll = np.arctan2(2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z))
    # Looking straight up or down, the Y and Z turns are one turn: all yaw.
    locked = np.abs(r12) > 0.999999
    if np.any(locked):
        yaw[locked] = np.arctan2(-2.0 * (x * z - w * y)[locked],
                                 (1.0 - 2.0 * (y * y + z * z))[locked])
        roll[locked] = 0.0
    return np.degrees(np.stack((pitch, yaw, roll), axis=1))


def _rolled(q, velocity, radius, dt):
    """Quaternions *q* turned as bodies of *radius* roll at *velocity* for *dt*.

    Rolling without slipping turns a body ``|v_horizontal| * dt / r`` radians
    about ``(vz, 0, -vx)``, the horizontal axis square to its motion -- its
    spin is read from its speed, so there is no spin to simulate.
    """
    vx = velocity[:, 0].astype(np.float64)
    vz = velocity[:, 2].astype(np.float64)
    speed = np.hypot(vx, vz)
    moving = speed > 1e-9
    half = 0.5 * speed * float(dt) / np.maximum(radius.astype(np.float64), 1e-6)
    s = np.sin(half) / np.where(moving, speed, 1.0)
    turn = np.zeros((q.shape[0], 4), dtype=np.float64)
    turn[:, 0] = np.cos(half)
    turn[:, 1] = vz * s
    turn[:, 3] = -vx * s
    turn[~moving] = (1.0, 0.0, 0.0, 0.0)
    # The axis is fixed in the world, so the turn is applied on the left.
    out = _quat_multiply(turn, q)
    out /= np.linalg.norm(out, axis=1, keepdims=True)
    return out


class CrudeBody:
    """Lightweight handle for one row in the engine's batched physics state."""

    def __init__(self, world, entity, entity_id):
        self.world = world
        self.entity = entity
        self.entity_id = entity_id
        self.rest_callback = None

    @property
    def _index(self):
        # Registration only queues a row; the index exists once the world packs.
        # Pack on demand so a handle read right after register_body() reports
        # the body's real state instead of the not-found defaults. _pack()
        # early-outs unless the world is dirty, so this costs nothing on the
        # steady-state path.
        self.world._pack()
        return self.world._indices.get(self.entity_id)

    @property
    def velocity(self):
        i = self._index
        if i is None:
            return [0.0, 0.0, 0.0]
        return self.world._velocity[i].tolist()

    @velocity.setter
    def velocity(self, value):
        i = self._index
        if i is not None:
            self.world._velocity[i] = np.asarray(value, dtype=np.float32)

    @property
    def mass(self):
        i = self._index
        return float(self.world._mass[i]) if i is not None else 1.0

    # ------------------------------------------------------------------
    # Shape and authored-material accessors.
    #
    # Before the batched-NumPy rewrite these were plain attributes on the
    # body. Packing the state into the world's SoA arrays kept the arrays
    # but dropped the handle's read side, so callers -- and this class's
    # own tests -- lost the ability to ask a body what shape it is. They
    # are restored here as index-backed properties in the same style as
    # ``velocity``/``mass`` above, reading the packed row rather than
    # duplicating any state.
    # ------------------------------------------------------------------

    @property
    def half_extents(self):
        """Half the body's collision box, as the world stores it."""
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) for v in self.world._half[i])

    @property
    def size(self):
        """The body's full collision box.

        ``size`` means full extent everywhere else in Fio (``brush['size']``,
        ``collision_size``), and it meant full extent on this handle before
        the rewrite, so it keeps that meaning. Use :attr:`half_extents` for
        the half-size the batched maths works in.
        """
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) * 2.0 for v in self.world._half[i])

    @property
    def offset(self):
        """Collision-box centre relative to the entity's origin."""
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) for v in self.world._offset[i])

    @property
    def solid(self):
        i = self._index
        return bool(self.world._solid[i]) if i is not None else False

    @property
    def gravity(self):
        i = self._index
        return bool(self.world._gravity[i]) if i is not None else True

    @property
    def friction(self):
        i = self._index
        return float(self.world._friction[i]) if i is not None else 0.55

    @property
    def linear_damping(self):
        i = self._index
        return float(self.world._damping[i]) if i is not None else 0.08

    @property
    def angular_velocity(self):
        i = self._index
        if i is None:
            return [0.0, 0.0, 0.0]
        return self.world._angular_velocity[i].tolist()

    @property
    def awake(self):
        i = self._index
        return bool(self.world._awake[i]) if i is not None else False

    @awake.setter
    def awake(self, value):
        i = self._index
        if i is not None:
            self.world._awake[i] = bool(value)

    @property
    def kinematic(self):
        i = self._index
        return bool(self.world._kinematic[i]) if i is not None else False

    @kinematic.setter
    def kinematic(self, value):
        self.world._set_kinematic_index(self._index, bool(value))

    def set_kinematic(self, value):
        self.kinematic = value

    def wake(self, velocity=None):
        self.world._wake_index(self._index, velocity)

    def set_rest_callback(self, callback):
        self.rest_callback = callback

    def clear(self):
        self.rest_callback = None


class SomaCrude:
    """Soma's crude tier: Fio's physics as it was before Soma.

    Engine-owned batched dynamic-body simulation.

    Body state is stored as structure-of-arrays NumPy buffers. The per-tick
    integration, damping, gravity, push response, sleep test and position
    updates are vectorised. Python remains only at the engine-object boundary
    and for the small number of world-collision cell/contact groups that
    cannot be expressed as one dense operation without wasting large amounts
    of memory.
    """

    #: The one gravity in the game. The live world's ``gravity`` (the console's
    #: ``phys_gravity`` changes it) is what bodies, the player, monsters and
    #: dropped props all fall by; see :func:`world_gravity`.
    #: Which of Soma's tiers this is (:mod:`engine.physics.soma`).
    tier = 'crude'

    GRAVITY = np.float32(-SV_GRAVITY)
    #: Active-body count from which the grouped floor query is worth its setup.
    #: Below it the NumPy assembly costs more than the scalar raycasts it saves
    #: -- measured crossover is around 56-64 bodies on this scene shape, so the
    #: grouped path is only taken where it is demonstrably faster. Both paths
    #: are bit-identical (tests/physics/test_dynamic_bodies.py), so this is
    #: purely a cost choice and never a behavioural one.
    FLOOR_BATCH_MIN_BODIES = 64
    MAX_STEP = np.float32(0.05)
    REST_SPEED = np.float32(1.0)

    #: The share of its ``friction`` a body that ``rolls`` loses to the ground:
    #: rolling loses far less speed than sliding.
    ROLLING_FRICTION_SCALE = 0.05
    #: Mass at and above which a body is heavy: it does not bounce, and the
    #: player cannot push it -- it pushes the player instead, and crushes a
    #: player it drives into a wall. A body of mass 1 or less bounces by its
    #: full ``bounciness``; between the two the bounce fades linearly.
    HEAVY_MASS = 10.0
    #: A rebound slower than this (units per second) is not a bounce: the
    #: body settles instead of chattering on the floor.
    MIN_BOUNCE_SPEED = 60.0
    #: A heavy body moving into the player faster than this (units per
    #: second) crushes them if it pins them against a wall.
    CRUSH_SPEED = 5.0
    #: Damage a crushing body does: enough to kill through any armour. God
    #: mode still stops it, as it stops all player damage.
    CRUSH_DAMAGE = 100000

    @classmethod
    def restitution(cls, bounciness, mass):
        """The share of impact speed a body keeps when it bounces.

        *bounciness* (0..1) is what the author asked for; *mass* takes it away,
        to nothing at :data:`HEAVY_MASS`. Scalars or arrays.
        """
        bounciness = np.clip(np.asarray(bounciness, dtype=np.float32), 0.0, 1.0)
        mass = np.asarray(mass, dtype=np.float32)
        weight = np.clip((cls.HEAVY_MASS - mass) / (cls.HEAVY_MASS - 1.0), 0.0, 1.0)
        return (bounciness * weight).astype(np.float32)

    def _bounce_speed(self, impact_speed, coefficient):
        """Rebound speed for an impact, or 0 where it is too slow to bounce."""
        rebound = np.asarray(impact_speed, dtype=np.float32) * coefficient
        return np.where(rebound > self.MIN_BOUNCE_SPEED, rebound, 0.0).astype(np.float32)

    # Runtime physics controls. Entity-authored mass/friction/damping remain
    # intact; these values provide global tuning/debug controls for play mode.
    DEFAULT_TIME_SCALE = np.float32(1.0)
    DEFAULT_FRICTION_SCALE = np.float32(1.0)
    DEFAULT_DAMPING_SCALE = np.float32(1.0)
    DEFAULT_SLEEP_ENABLED = True

    def __init__(self, spatial_grid):
        self.spatial_grid = spatial_grid
        self.bodies = {}
        self._indices = {}

        self._entities = []
        self._velocity = np.empty((0, 3), dtype=np.float32)
        self._position = np.empty((0, 3), dtype=np.float32)
        self._offset = np.empty((0, 3), dtype=np.float32)
        self._half = np.empty((0, 3), dtype=np.float32)
        self._mass = np.empty(0, dtype=np.float32)
        self._friction = np.empty(0, dtype=np.float32)
        self._damping = np.empty(0, dtype=np.float32)
        self._gravity = np.empty(0, dtype=np.float32)
        self._angular_velocity = np.empty((0, 3), dtype=np.float32)
        self._solid = np.empty(0, dtype=np.bool_)
        self._physics_enabled = np.empty(0, dtype=np.bool_)
        self._awake = np.empty(0, dtype=np.bool_)
        self._kinematic = np.empty(0, dtype=np.bool_)
        # Last observed hidden/disabled state, retained across dense repacks.
        self._was_inactive = np.empty(0, dtype=np.bool_)
        # Bodies initially dormant (or reactivated by a flag change) need the
        # one-time placement correction before their first active movement.
        self._pending_settle = np.empty(0, dtype=np.bool_)
        self._clear_rolling()
        self._dirty = False
        self._static_cells = {}
        self._static_query_cache = {}
        # Baseline for entities_that_changed_cell(): one (N, 2) cell array.
        self._reported_cell = None

        # Console-adjustable world controls.
        self.gravity = float(self.GRAVITY)
        self.time_scale = float(self.DEFAULT_TIME_SCALE)
        self.friction_scale = float(self.DEFAULT_FRICTION_SCALE)
        self.damping_scale = float(self.DEFAULT_DAMPING_SCALE)
        self.sleep_enabled = bool(self.DEFAULT_SLEEP_ENABLED)
        # Physics consumes authored visibility/enable transitions without
        # polling every sleeping entity on every simulation tick.
        JOURNAL.subscribe(self, flags=STATE | VISIBILITY)

    def clear(self):
        # Discard notifications for the world being torn down. The subscriber
        # itself remains registered for the next rebuild of this world.
        JOURNAL.drain(self)
        for body in self.bodies.values():
            body.clear()
        self.bodies.clear()
        self._indices.clear()
        self._entities.clear()
        self._velocity = np.empty((0, 3), dtype=np.float32)
        self._position = np.empty((0, 3), dtype=np.float32)
        self._offset = np.empty((0, 3), dtype=np.float32)
        self._half = np.empty((0, 3), dtype=np.float32)
        self._mass = np.empty(0, dtype=np.float32)
        self._friction = np.empty(0, dtype=np.float32)
        self._damping = np.empty(0, dtype=np.float32)
        self._gravity = np.empty(0, dtype=np.float32)
        self._angular_velocity = np.empty((0, 3), dtype=np.float32)
        self._solid = np.empty(0, dtype=np.bool_)
        self._physics_enabled = np.empty(0, dtype=np.bool_)
        self._awake = np.empty(0, dtype=np.bool_)
        self._kinematic = np.empty(0, dtype=np.bool_)
        self._was_inactive = np.empty(0, dtype=np.bool_)
        self._pending_settle = np.empty(0, dtype=np.bool_)
        self._clear_rolling()
        self._static_cells.clear()
        self._static_query_cache.clear()
        self._reported_cell = None
        self._dirty = False

    def _clear_rolling(self):
        #: Turns by the distance it travels (``rolls``).
        self._rolls = np.empty(0, dtype=np.bool_)
        #: Fraction of impact speed kept on a bounce: bounciness faded by mass.
        self._bounce = np.empty(0, dtype=np.float32)
        #: Rolling bodies' orientation, (w, x, y, z); identity for the rest.
        self._orientation = np.empty((0, 4), dtype=np.float64)
        # Set at pack time: a world with nothing rolling, bouncy or heavy --
        # most maps -- never enters that code.
        self._any_rolls = False
        self._any_bounce = False
        self._any_heavy = False
        #: Set when a heavy body pins the player against a wall; the logic
        #: thread applies the damage and clears it.
        self.player_crushed = False
        #: Set by rebuild(): the first step wakes floating NEAR bodies.
        self._wake_floating_pending = False

    @staticmethod
    def _shape_from_brush(entity, brush):
        # The body is the brush's derived box (brush_placement) -- an angled
        # brush's planes, not its pos/size copy. Still a box: a true-shape
        # body is Stage 2e.
        box = world_box(brush)
        center = np.asarray(box[0:3], dtype=np.float32)
        half = np.asarray(box[3:6], dtype=np.float32)
        origin = np.asarray(
            getattr(entity, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32
        )
        return half, center - origin

    def register_body(self, entity, brush, rest_callback=None):
        """Register one body; state is packed once before the next tick."""
        entity_id = id(entity)
        body = self.bodies.get(entity_id)
        if body is None:
            body = CrudeBody(self, entity, entity_id)
            self.bodies[entity_id] = body
            self._entities.append(entity)

        props = getattr(entity, 'properties', {})
        half, offset = self._shape_from_brush(entity, brush)
        body.rest_callback = rest_callback

        # Keep registration descriptors on the handle until packing. This is
        # construction-time Python work, not physics work.
        body._initial = (
            np.asarray(getattr(entity, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32),
            half,
            offset,
            max(0.01, float(props.get('mass', 1.0))),
            max(0.0, min(1.0, float(props.get('friction', 0.55)))),
            max(0.0, float(props.get('linear_damping', 0.08))),
            1.0 if props.get('gravity', True) else 0.0,
            np.asarray(props.get('drop_angular_velocity', [0.0, 0.0, 0.0]), dtype=np.float32),
            not bool(props.get('no_collision', True)),
            bool(props.get('physics_enabled', False)),
            bool(props.get('rolls', False)),
            float(self.restitution(_float(props.get('bounciness'), 0.0),
                                   _float(props.get('mass'), 1.0)))
            if props.get('bounciness') else 0.0,
        )
        self._dirty = True
        return body

    def _pack(self):
        if not self._dirty:
            return
        n = len(self._entities)
        if n == 0:
            self._dirty = False
            return

        # A body may become active immediately before a repack. Preserve its
        # last observed state by identity so that transition is not swallowed.
        previous_inactive = {
            id(entity): bool(self._was_inactive[i])
            for i, entity in enumerate(self._entities)
            if i < len(self._was_inactive)
        }
        previous_pending_settle = {
            id(entity): bool(self._pending_settle[i])
            for i, entity in enumerate(self._entities)
            if i < len(self._pending_settle)
        }

        self._position = np.empty((n, 3), dtype=np.float32)
        self._offset = np.empty((n, 3), dtype=np.float32)
        self._half = np.empty((n, 3), dtype=np.float32)
        self._velocity = np.zeros((n, 3), dtype=np.float32)
        self._mass = np.empty(n, dtype=np.float32)
        self._friction = np.empty(n, dtype=np.float32)
        self._damping = np.empty(n, dtype=np.float32)
        self._gravity = np.empty(n, dtype=np.float32)
        self._angular_velocity = np.empty((n, 3), dtype=np.float32)
        self._solid = np.empty(n, dtype=np.bool_)
        self._physics_enabled = np.empty(n, dtype=np.bool_)
        self._awake = np.zeros(n, dtype=np.bool_)
        self._kinematic = np.zeros(n, dtype=np.bool_)
        self._was_inactive = np.empty(n, dtype=np.bool_)
        self._pending_settle = np.empty(n, dtype=np.bool_)
        self._rolls = np.zeros(n, dtype=np.bool_)
        self._bounce = np.zeros(n, dtype=np.float32)
        self._orientation = np.zeros((n, 4), dtype=np.float64)
        self._orientation[:, 0] = 1.0

        for i, entity in enumerate(self._entities):
            props = getattr(entity, 'properties', None)
            current_inactive = bool(
                isinstance(props, dict)
                and (props.get('disabled', False) or props.get('hidden', False)))
            self._was_inactive[i] = previous_inactive.get(id(entity), current_inactive)
            self._pending_settle[i] = previous_pending_settle.get(
                id(entity), False)
            body = self.bodies[id(entity)]
            (
                position, half, offset, mass, friction, damping, gravity,
                angular_velocity, solid, physics_enabled, rolls, bounce,
            ) = body._initial
            self._indices[id(entity)] = i
            self._position[i] = position
            self._half[i] = half
            self._offset[i] = offset
            self._mass[i] = mass
            self._friction[i] = friction
            self._damping[i] = damping
            self._gravity[i] = gravity
            self._angular_velocity[i] = angular_velocity
            self._solid[i] = solid
            self._physics_enabled[i] = physics_enabled
            self._rolls[i] = rolls
            self._bounce[i] = bounce
            if rolls:
                # A rolling body's turn comes from its rolling, not a fixed spin.
                self._angular_velocity[i] = 0.0
                self._orientation[i] = self._entity_orientation(entity)

        self._any_rolls = bool(self._rolls.any())
        self._any_bounce = bool(self._bounce.any())
        self._any_heavy = bool(np.any(
            self._physics_enabled & self._solid & (self._mass >= self.HEAVY_MASS)))
        self._dirty = False

    @staticmethod
    def _entity_orientation(entity):
        props = getattr(entity, 'properties', None)
        rotation = props.get('rotation') if isinstance(props, dict) else None
        try:
            return quat_from_rotation(rotation)[0]
        except (TypeError, ValueError):
            return np.array([1.0, 0.0, 0.0, 0.0])

    def get_body(self, entity):
        self._pack()
        return self.bodies.get(id(entity))

    def _set_kinematic_index(self, index, value):
        if index is None:
            return
        self._pack()

        # Kinematic bodies are positioned by the gameplay/runtime layer
        # (for example PropSession while a prop is being carried). Synchronise
        # the physics copy whenever transform ownership changes so physics
        # cannot restore a stale pre-carry position on release.
        entity = self._entities[index]
        self._position[index] = np.asarray(
            getattr(entity, "pos", self._position[index]),
            dtype=np.float32,
        )

        self._kinematic[index] = value
        if self._any_rolls and self._rolls[index]:
            # Carrying turns a model with the player; roll on from there.
            self._orientation[index] = self._entity_orientation(entity)
        if value:
            self._velocity[index] = 0.0
            self._awake[index] = False

    def set_kinematic(self, entity, value):
        self._pack()
        self._set_kinematic_index(self._indices.get(id(entity)), value)

    def _wake_index(self, index, velocity=None):
        if index is None:
            return
        self._pack()
        if velocity is not None:
            self._velocity[index] = np.asarray(velocity, dtype=np.float32)
        self._awake[index] = True
        self._kinematic[index] = False

    def wake(self, entity, velocity=None):
        self._pack()
        self._wake_index(self._indices.get(id(entity)), velocity)

    def sync_entity_position(self, entity, wake=False):
        """Synchronize a body's cached position after external movement."""
        self._pack()
        index = self._indices.get(id(entity))
        if index is None:
            return
        self._position[index] = np.asarray(
            getattr(entity, 'pos', self._position[index]),
            dtype=np.float32,
        )
        if wake:
            self._awake[index] = True
            self._kinematic[index] = False
            self._velocity[index] = 0.0

    def set_rest_callback(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.set_rest_callback(callback)

    def entities_that_changed_cell(self):
        """Bodies that crossed a cell boundary since the last call.

        PhysicsWorld owns an entity's position while it is in motion, so a
        spatial index built on those entities has to be told when to re-file
        one. This is how, and it answers the narrowest useful question: not
        "which bodies are awake" and not even "which moved", but "which are no
        longer in the cell they were in" — the only ones whose membership can
        actually be wrong.

        The detection is one vectorised comparison over the body arrays, so the
        per-frame cost does not grow a Python loop as the world fills up; what
        comes back is normally empty, because a body has to travel a whole
        512-unit column to appear in it. The returned entities are objects, so
        acting on them is per-entity by nature — which is the point of making
        this set as small as it can correctly be.
        """
        self._pack()
        if not self._entities:
            self._reported_cell = None
            return ()

        cx, cz = cells_of_points(self._position[:, 0], self._position[:, 2])
        current = np.stack((cx, cz), axis=1)
        previous = self._reported_cell

        if previous is None or previous.shape != current.shape:
            # First call, or the body set changed size and the rows no longer
            # line up. Registration filed every body from its live position, so
            # nothing is stale — take this as the new baseline.
            self._reported_cell = current
            return ()

        changed = np.flatnonzero(np.any(current != previous, axis=1))
        self._reported_cell = current
        if changed.size == 0:
            return ()
        return [self._entities[i] for i in changed]

    def reset_controls(self):
        """The world controls back to their defaults; every body woken (the
        contract's; the console used to do this itself)."""
        self.gravity = float(self.GRAVITY)
        self.time_scale = float(self.DEFAULT_TIME_SCALE)
        self.friction_scale = float(self.DEFAULT_FRICTION_SCALE)
        self.damping_scale = float(self.DEFAULT_DAMPING_SCALE)
        self.sleep_enabled = bool(self.DEFAULT_SLEEP_ENABLED)
        self.wake_all()

    def wake_all(self):
        """Wake every non-kinematic physics body for runtime console tuning."""
        self._pack()
        if not self._entities:
            return
        active = self._physics_enabled & ~self._kinematic
        self._awake[active] = True

    def _refresh_inactive_history(self):
        """Apply journalled hidden/disabled transitions to packed body rows.

        Most bodies sleep, so checking their Python property dictionaries every
        frame would defeat the sparse physics path. Flag writers already
        journal state changes; consume only those entity identities here. An
        overflow is rare and deliberately falls back to one complete refresh.
        """
        changes = JOURNAL.drain(self)
        if changes is OVERFLOW:
            indices = range(len(self._entities))
        else:
            indices = (
                self._indices.get(entity_id)
                for entity_id, flags in changes.items()
                if flags & (STATE | VISIBILITY)
            )

        for index in indices:
            if index is None:
                continue
            props = getattr(self._entities[index], 'properties', None)
            inactive = bool(
                isinstance(props, dict)
                and (props.get('disabled', False) or props.get('hidden', False)))
            if self._was_inactive[index] and not inactive:
                self._pending_settle[index] = True
            self._was_inactive[index] = inactive

    def _settle_pending_bodies(self, eligible):
        """Correct newly active placements before they can first move.

        The mask is normally empty. It tracks entities whose hidden/disabled
        state has changed from inactive to active. Dormant rows remain outside
        the pending set until a flag transition makes correction necessary.
        """
        candidates = self._pending_settle & eligible
        if not np.any(candidates):
            return
        inactive = self._inactive_mask(candidates, awake_only=False)
        ready = candidates & ~inactive
        if not np.any(ready):
            return
        self._settle_sunk_bodies(ready)
        self._pending_settle[ready] = False

    def _inactive_mask(self, candidates, awake_only=True):
        """Which candidate bodies are hidden or disabled.

        ``disabled`` is authored gameplay state; ``hidden`` is the Prop's
        I/O Hide state. Both make its physical body dormant. Ordinary
        integration only needs to inspect awake candidates, preserving the
        sparse hot path. The player-push query also checks its small set of
        overlapping bodies with ``awake_only=False``, because a sleeping body
        can still overlap the player and must not be pushed while inactive.
        """
        mask = np.zeros(len(self._entities), dtype=np.bool_)
        rows = candidates & self._awake if awake_only else candidates
        indices = np.flatnonzero(rows)
        if indices.size == 0:
            return mask
        for i in indices:
            props = getattr(self._entities[int(i)], 'properties', None)
            if (isinstance(props, dict)
                    and (props.get('disabled', False) or props.get('hidden', False))):
                mask[i] = True
        return mask

    def _integrate_rotation(self, angular, dt):
        """Advance the rotation of every spinning body.

        Rotation lives on the entity's property dict rather than in the packed
        arrays, so this is necessarily an object-boundary operation. What it
        does not have to be is scalar arithmetic: the gather, the multiply-add
        and the conversion back to lists are each one operation over all
        spinning bodies, and only the dict reads and writes stay per-entity.

        Which bodies spin is unchanged -- any body with a non-zero angular
        velocity, awake or not, exactly as before.
        """
        spinning = np.flatnonzero(np.any(angular != 0.0, axis=1))
        if spinning.size == 0:
            return

        current = np.zeros((spinning.size, 3), dtype=np.float32)
        targets = []
        for slot, index in enumerate(spinning):
            entity = self._entities[int(index)]
            props = getattr(entity, 'properties', None)
            if (not isinstance(props, dict)
                    or props.get('disabled', False)
                    or props.get('hidden', False)):
                targets.append(None)
                continue
            targets.append((entity, props))
            rotation = props.get('rotation')
            if rotation is None:
                continue
            try:
                current[slot] = rotation
            except (TypeError, ValueError):
                # A malformed authored rotation restarts from zero rather than
                # aborting the whole step for every other body.
                current[slot] = 0.0

        updated = (current + angular[spinning] * dt).tolist()
        for target, rotation in zip(targets, updated):
            if target is not None:
                entity, props = target
                props['rotation'] = rotation
                touch(entity)

    def _bodies_block_player(self, player, ppos, player_half, rows):
        """Move the player out of the bodies in *rows* they still overlap."""
        center = self._position + self._offset
        overlap = rows & np.all(
            (center + self._half > ppos - player_half)
            & (center - self._half < ppos + player_half), axis=1)
        if np.any(overlap):
            self._push_player_out(player, ppos, player_half, center,
                                  np.flatnonzero(overlap), carry=False)

    def _push_player_out(self, player, ppos, player_half, center, rows, carry=True):
        """Bodies in *rows* overlapping the player shove the player out.

        The player goes out along whichever horizontal axis overlaps least, and
        a body moving into the player carries them along at its own speed. If
        that leaves the player inside a wall, the body has pinned them there:
        :attr:`player_crushed`. Without *carry* the player is only stopped.
        """
        for i in rows:
            dx = float(ppos[0] - center[i, 0])
            dz = float(ppos[2] - center[i, 2])
            overlap_x = float(player_half[0] + self._half[i, 0]) - abs(dx)
            overlap_z = float(player_half[2] + self._half[i, 2]) - abs(dz)
            axis, delta, out = ((0, dx, overlap_x) if overlap_x <= overlap_z
                                else (2, dz, overlap_z))
            direction = 1.0 if delta >= 0.0 else -1.0
            ppos[axis] += direction * (out + 0.5)
            body_speed = float(self._velocity[i, axis])
            if carry and body_speed * direction > 0.0:
                if axis == 0:
                    player.velocity.x = body_speed
                else:
                    player.velocity.z = body_speed
                if (body_speed * direction > self.CRUSH_SPEED
                        and self._player_in_wall(ppos, player_half)):
                    self.player_crushed = True
        player.pos = glm.vec3(float(ppos[0]), float(ppos[1]), float(ppos[2]))

    def _player_in_wall(self, ppos, player_half):
        """Whether the player's box, short of its feet and head, is in a wall."""
        cell_size = float(self.spatial_grid.cell_size)
        aabbs = self._candidate_aabbs(int(math.floor(float(ppos[0]) / cell_size)),
                                      int(math.floor(float(ppos[2]) / cell_size)))
        if aabbs is None:
            return False
        # Trimmed top and bottom so the floor stood on and a low ceiling are
        # not walls.
        lo = np.array([ppos[0] - player_half[0], ppos[1] - player_half[1] + 8.0,
                       ppos[2] - player_half[2]], dtype=np.float32)
        hi = np.array([ppos[0] + player_half[0], ppos[1] + player_half[1] - 8.0,
                       ppos[2] + player_half[2]], dtype=np.float32)
        return bool(np.any(np.all((aabbs[:, 3:] > lo) & (aabbs[:, :3] < hi), axis=1)))

    def _roll(self, rows, dt):
        """Turn rolling bodies by how far they rolled this step."""
        radius = np.min(self._half[rows], axis=1)
        orientation = _rolled(
            self._orientation[rows], self._velocity[rows], radius, dt)
        self._orientation[rows] = orientation
        for i, rotation in zip(rows, rotation_from_quat(orientation).tolist()):
            entity = self._entities[int(i)]
            props = getattr(entity, 'properties', None)
            if isinstance(props, dict):
                props['rotation'] = rotation
                touch(entity)

    def _sync_entities(self, indices=None):
        # Position only. Rotation is integrated (angular * dt) in step();
        # applying angular velocity here as well spun bodies ~61x too fast.
        if indices is None:
            indices = range(len(self._entities))
        for i in indices:
            # Assigned, not written in place: assignment is what tells the
            # render projection the entity moved.
            self._entities[int(i)].pos = self._position[int(i)].tolist()

    def _rebuild_static_cells(self):
        """Cache static collision AABBs as contiguous NumPy arrays per grid cell."""
        cells = {}
        for key, brushes in self.spatial_grid.cells.items():
            if not brushes:
                continue
            rows = []
            for brush in brushes:
                if brush.get('_physics_body'):
                    continue
                rows.append(world_bounds(brush))
            if rows:
                cells[key] = np.asarray(rows, dtype=np.float32)
        self._static_cells = cells

    def rebuild(self, brushes):
        self.clear()

        for brush in brushes:
            if not brush.get('_physics_body'):
                continue
            entity = brush.get('_physics_entity')
            if entity is not None:
                self.register_body(entity, brush)

        self._pack()
        self._rebuild_static_cells()
        self._settle_sunk_bodies()
        # Waking what floats waits for the first step: simulation tiers are
        # stamped after the world is built (Big World's play_start).
        self._wake_floating_pending = True

    def _wake_floating_near_bodies(self):
        """Wake every body in the NEAR simulation tier with nothing under it.

        Bodies start asleep and wake when touched, so one placed in the air
        hung there until then. Only what floats is woken -- a body already
        resting stays asleep, rather than settling straight back and firing
        OnRest as play starts -- and only NEAR: a body further out keeps until
        it is touched. An unclassified world is all NEAR.
        """
        candidates = self._physics_enabled & self._solid & ~self._kinematic & ~self._awake
        rows = np.flatnonzero(candidates)
        if rows.size == 0:
            return
        near = np.fromiter(
            (tier_of(self._entities[int(i)]) == TIER_NEAR
             and not getattr(self._entities[int(i)], 'properties', {}).get('disabled', False)
             and not getattr(self._entities[int(i)], 'properties', {}).get('hidden', False)
             for i in rows), dtype=np.bool_, count=rows.size)
        rows = rows[near]
        if rows.size == 0:
            return
        centre = (self._position[rows] + self._offset[rows]).astype(np.float64)
        bottom = centre[:, 1] - self._half[rows, 1].astype(np.float64)
        support = self._floor_tops_at(centre[:, 0], centre[:, 2], bottom + 1.0)
        if support is None:
            return
        self._awake[rows[support < bottom - 1.0]] = True

    def _settle_sunk_bodies(self, eligible=None):
        """Lift each body that starts sunk into a floor up onto its surface.

        A body finds its floor by casting down from just above its bottom, so
        a floor it starts inside is never seen, and it drops through the
        moment it wakes. A model whose origin is its centre -- a ball placed at
        floor height -- starts half inside the floor. Once per rebuild, a body
        resting on nothing whose centre stands over a floor top between its
        bottom and its centre is raised to rest on it. A body already standing
        on a floor is left alone, even against a low wall it overlaps: in
        motion a floor top above the bottom is a wall, not a floor.
        """
        candidates = self._physics_enabled & self._solid
        if eligible is not None:
            candidates &= eligible
        rows = np.flatnonzero(
            candidates & ~self._inactive_mask(candidates, awake_only=False))
        if rows.size == 0:
            return
        centre = (self._position[rows] + self._offset[rows]).astype(np.float64)
        bottom = centre[:, 1] - self._half[rows, 1].astype(np.float64)
        # One query for every body: the floor under its centre, and whatever
        # it already stands on.
        xs = np.concatenate((centre[:, 0], centre[:, 0]))
        zs = np.concatenate((centre[:, 2], centre[:, 2]))
        ys = np.concatenate((centre[:, 1], bottom + 1.0))
        tops = self._floor_tops_at(xs, zs, ys)
        if tops is None:
            return
        floor, support = tops[:rows.size], tops[rows.size:]
        lift = (floor > bottom) & (floor <= centre[:, 1]) & (support < bottom - 1.0)
        if np.any(lift):
            lifted = rows[lift]
            self._position[lifted, 1] += (floor[lift] - bottom[lift]).astype(np.float32)
            self._sync_entities(lifted)

    def _floor_tops_at(self, xs, zs, ys):
        """The highest floor top at or below each point, or -inf; None without
        a grid that can answer. Grouped only where it pays: below the measured
        crossover (FLOOR_BATCH_MIN_BODIES) a raycast per point is cheaper than
        assembling the grouped query."""
        if xs.size >= self.FLOOR_BATCH_MIN_BODIES and self._can_group_floor_queries():
            return self._floor_tops(xs, zs, ys)
        return self._floor_tops_scalar(xs, zs, ys)

    def _floor_tops_scalar(self, xs, zs, ys):
        """:meth:`_floor_tops` one ``raycast_down`` at a time: for few points,
        or a grid that traces its own way (a test double); None without one."""
        raycast = getattr(self.spatial_grid, 'raycast_down', None)
        if raycast is None:
            return None
        tops = np.full(xs.size, -np.inf)
        for k in range(xs.size):
            try:
                floor = raycast(float(xs[k]), float(zs[k]), float(ys[k]))
            except Exception:
                floor = None
            if floor is not None:
                tops[k] = float(floor)
        return tops

    def _candidate_aabbs(self, cell_x, cell_z):
        key = (cell_x, cell_z)
        cached = self._static_query_cache.get(key)
        if cached is False:
            return None
        if cached is not None:
            return cached

        parts = []
        cells = self._static_cells
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                aabbs = cells.get((cell_x + dx, cell_z + dz))
                if aabbs is not None:
                    parts.append(aabbs)

        if not parts:
            self._static_query_cache[key] = False
            return None
        if len(parts) == 1:
            result = parts[0]
        else:
            result = np.concatenate(parts, axis=0)
        self._static_query_cache[key] = result
        return result

    def _batch_static_collision(self, axis, old_position, rows=None, pushed_from=None):
        """Resolve one axis with vectorised AABB tests per cell group.

        A body that ends up in a wall goes back to *old_position* on this axis
        and stops, or bounces off by its bounciness. *rows* limits the test to
        some bodies: the vertical axis is tested only for bodies on their way
        up, so nothing rises into a ceiling.

        *pushed_from* is where the player's push this step started each body
        from (NaN where it did not push it): a push into a wall is undone with
        the rest of the move -- unless the body was already in a wall before
        it, when it may still be pushed out.
        """
        active = (
            self._physics_enabled
            & self._awake
            & ~self._kinematic
            & self._solid
        )
        if rows is not None:
            active &= rows
        active &= ~self._inactive_mask(active)
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return

        cell_size = np.float32(self.spatial_grid.cell_size)
        cell_keys = np.column_stack((
            np.floor(self._position[indices, 0] / cell_size).astype(np.int64),
            np.floor(self._position[indices, 2] / cell_size).astype(np.int64),
        ))
        unique_cells, inverse = np.unique(
            cell_keys, axis=0, return_inverse=True
        )

        collision = np.zeros(indices.size, dtype=np.bool_)
        # Pushed bodies that were clear of every wall before the push.
        clear_before_push = np.zeros(indices.size, dtype=np.bool_)

        for group_id, key in enumerate(unique_cells):
            local_indices = np.flatnonzero(inverse == group_id)
            bi = indices[local_indices]
            aabbs = self._candidate_aabbs(int(key[0]), int(key[1]))
            if aabbs is None:
                continue

            pos = self._position[bi] + self._offset[bi]
            half = self._half[bi]

            bmin = pos - half
            bmax = pos + half

            cmin = aabbs[:, :3]
            cmax = aabbs[:, 3:]

            overlap = (
                (bmax[:, None, 0] > cmin[None, :, 0]) &
                (bmin[:, None, 0] < cmax[None, :, 0]) &
                (bmax[:, None, 1] > cmin[None, :, 1]) &
                (bmin[:, None, 1] < cmax[None, :, 1]) &
                (bmax[:, None, 2] > cmin[None, :, 2]) &
                (bmin[:, None, 2] < cmax[None, :, 2])
            )
            hit = overlap.any(axis=1)

            collision[local_indices] |= hit

            if pushed_from is not None:
                was_pushed = hit & ~np.isnan(pushed_from[bi, 0])
                if np.any(was_pushed):
                    pmin = pushed_from[bi] + self._offset[bi] - half
                    pmax = pushed_from[bi] + self._offset[bi] + half
                    before = (
                        (pmax[:, None, :] > cmin[None, :, :])
                        & (pmin[:, None, :] < cmax[None, :, :])
                    ).all(axis=2).any(axis=1)
                    clear_before_push[local_indices] |= was_pushed & ~before

        if np.any(collision):
            hit_indices = indices[collision]
            target = old_position[hit_indices, axis]
            if pushed_from is not None:
                undo = clear_before_push[collision]
                target = np.where(undo, pushed_from[hit_indices, axis], target)
            self._position[hit_indices, axis] = target
            if self._any_bounce:
                # Bounce off the wall if bouncy enough.
                hit_velocity = self._velocity[hit_indices, axis]
                self._velocity[hit_indices, axis] = -np.sign(hit_velocity) * self._bounce_speed(
                    np.abs(hit_velocity), self._bounce[hit_indices])
            else:
                self._velocity[hit_indices, axis] = 0.0

    def _batch_floor(self, previous_bottom=None):
        """Find the floor beneath each active dynamic body.

        Five support points per body -- centre plus four in-footprint samples --
        so a body straddling a seam between floor brushes cannot lose contact
        just because its centre crossed the gap.

        That is 5N downward queries per step. Done one at a time they were the
        largest remaining scalar section of the dynamic path: each
        ``raycast_down`` call re-derives the cell and then walks that cell's
        brush dicts in Python, so the cost is 5N x (brushes per cell) Python
        iterations. :meth:`_batch_floor_grouped` instead groups all 5N sample
        points by grid cell and tests every point in a cell against every brush
        in it with one broadcast, which makes the Python work proportional to
        the number of *distinct cells* rather than to the number of samples.

        Brush positions are still read live on every step. Movers and doors sit
        in the grid and move without it being repopulated, so a prop riding a
        platform depends on seeing the platform's current height; caching the
        AABBs would quietly break that.

        The grouped path is used only for a stock :class:`SpatialGrid`. Anything
        that overrides ``raycast_down`` -- a test double, or a future grid with
        its own tracing -- keeps the scalar path, because its answers are not
        derivable from ``cells`` alone.
        """
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        if (int(active.sum()) >= self.FLOOR_BATCH_MIN_BODIES
                and self._can_group_floor_queries()):
            return self._batch_floor_grouped(previous_bottom)
        return self._batch_floor_scalar(previous_bottom)

    def _can_group_floor_queries(self):
        grid = self.spatial_grid
        if type(grid).__dict__.get('raycast_down') is None:
            # Inherited from SpatialGrid rather than overridden.
            if not isinstance(grid, SpatialGrid):
                return False
        elif type(grid).raycast_down is not SpatialGrid.raycast_down:
            return False
        return (isinstance(getattr(grid, 'cells', None), dict)
                and getattr(grid, 'cell_size', None))

    def _support_offsets(self, indices):
        """Per-body support-point offsets and ray origins, in float64.

        float64 deliberately: the scalar path promotes each float32 component
        to a Python float before the offset arithmetic, so matching the width
        here keeps the two paths bit-identical rather than merely close.
        """
        centers = (self._position[indices] + self._offset[indices]).astype(np.float64)
        half = self._half[indices].astype(np.float64)
        zeros = np.zeros(indices.size, dtype=np.float64)
        hx = half[:, 0] * 0.75
        hz = half[:, 2] * 0.75
        off_x = np.stack([zeros, -hx, hx, zeros, zeros], axis=1)
        off_z = np.stack([zeros, zeros, zeros, -hz, hz], axis=1)
        px = centers[:, 0:1] + off_x
        pz = centers[:, 2:3] + off_z
        bottom = centers[:, 1] - half[:, 1]
        return px, pz, bottom

    def _batch_floor_grouped(self, previous_bottom=None):
        n = len(self._entities)
        floors = np.full(n, -np.inf, dtype=np.float32)
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return floors

        previous_bottom = self._previous_bottom(previous_bottom)
        px, pz, bottom = self._support_offsets(indices)
        ray_y = np.maximum(bottom, previous_bottom[indices].astype(np.float64)) + 1.0

        best = self._floor_tops(px.ravel(), pz.ravel(), np.repeat(ray_y, px.shape[1]))

        per_body = best.reshape(px.shape).max(axis=1)
        hit = per_body > -np.inf
        if hit.any():
            floors[indices[hit]] = per_body[hit].astype(np.float32)
        return floors

    def _floor_tops(self, flat_x, flat_z, flat_y):
        """The highest brush top at or below each point (x, z, y), or -inf.

        What ``SpatialGrid.raycast_down`` answers for one point, for many at
        once: the points are grouped by grid cell and each cell's brushes are
        tested against all its points in one broadcast, so the Python work is
        one step per distinct cell. Only for a stock grid
        (:meth:`_can_group_floor_queries`).
        """
        samples = flat_x.size
        cell_size = float(self.spatial_grid.cell_size)
        cx = np.floor(flat_x / cell_size).astype(np.int64)
        cz = np.floor(flat_z / cell_size).astype(np.int64)

        best = np.full(samples, -np.inf, dtype=np.float64)
        cells = self.spatial_grid.cells

        # Sort once, then walk contiguous runs: one pass per distinct cell
        # instead of one boolean scan of every sample per cell.
        order = np.lexsort((cz, cx))
        s_cx = cx[order]
        s_cz = cz[order]
        starts = np.flatnonzero(
            np.r_[True, (s_cx[1:] != s_cx[:-1]) | (s_cz[1:] != s_cz[:-1])]
        )
        ends = np.r_[starts[1:], samples]

        for start, end in zip(starts, ends):
            brushes = cells.get((int(s_cx[start]), int(s_cz[start])))
            if not brushes:
                continue
            bounds = self._cell_bounds(brushes)
            if bounds is None:
                continue
            lo_x, hi_x, lo_z, hi_z, top_y = bounds

            sel = order[start:end]
            X = flat_x[sel][:, None]
            Z = flat_z[sel][:, None]
            Y = flat_y[sel][:, None]
            inside = (
                (X >= lo_x) & (X <= hi_x)
                & (Z >= lo_z) & (Z <= hi_z)
                & (top_y[None, :] <= Y)
            )
            if not inside.any():
                continue
            best[sel] = np.where(inside, top_y[None, :], -np.inf).max(axis=1)
        return best

    @staticmethod
    def _cell_bounds(brushes):
        """Live XZ extents and top surface of every brush in one grid cell.

        Read from the dicts on every call, not cached: see _batch_floor.
        """
        count = len(brushes)
        lo_x = np.empty(count, dtype=np.float64)
        hi_x = np.empty(count, dtype=np.float64)
        lo_z = np.empty(count, dtype=np.float64)
        hi_z = np.empty(count, dtype=np.float64)
        top_y = np.empty(count, dtype=np.float64)
        kept = 0
        for brush in brushes:
            try:
                b = world_bounds(brush)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            lo_x[kept] = b[0]
            hi_x[kept] = b[3]
            lo_z[kept] = b[2]
            hi_z[kept] = b[5]
            top_y[kept] = b[4]
            kept += 1
        if kept == 0:
            return None
        return (lo_x[:kept], hi_x[:kept], lo_z[:kept], hi_z[:kept], top_y[:kept])

    def _previous_bottom(self, previous_bottom):
        if previous_bottom is None:
            return (self._position + self._offset)[:, 1] - self._half[:, 1]
        return np.asarray(previous_bottom, dtype=np.float32)

    def _batch_floor_scalar(self, previous_bottom=None):
        """One raycast_down per support point. Fallback for a custom grid."""
        n = len(self._entities)
        floors = np.full(n, -np.inf, dtype=np.float32)
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return floors

        raycast = getattr(self.spatial_grid, 'raycast_down', None)
        if raycast is None:
            return floors

        # Use the body's previous bottom as the ray origin while it is falling.
        # If integration has already carried the body slightly through the
        # floor this keeps the ray above the surface, allowing raycast_down()
        # to see it instead of starting underneath it.
        previous_bottom = self._previous_bottom(previous_bottom)

        for i in indices:
            i = int(i)
            center = self._position[i] + self._offset[i]
            current_bottom = float(center[1] - self._half[i, 1])
            ray_y = max(current_bottom, float(previous_bottom[i])) + 1.0

            # A pushed barrel can straddle a floor seam while its centre is
            # briefly over the seam itself.  Sample the centre plus four
            # in-footprint support points so horizontal motion cannot make a
            # grounded body lose contact just because its centre crossed a
            # small gap between floor brushes.
            hx = float(self._half[i, 0]) * 0.75
            hz = float(self._half[i, 2]) * 0.75
            support_points = (
                (float(center[0]), float(center[2])),
                (float(center[0] - hx), float(center[2])),
                (float(center[0] + hx), float(center[2])),
                (float(center[0]), float(center[2] - hz)),
                (float(center[0]), float(center[2] + hz)),
            )

            best_floor = None
            for ray_x, ray_z in support_points:
                try:
                    floor = raycast(ray_x, ray_z, ray_y)
                except Exception:
                    floor = None
                if floor is not None and (
                    best_floor is None or floor > best_floor
                ):
                    best_floor = float(floor)

            if best_floor is not None:
                floors[i] = best_floor

        return floors


    def step(self, delta, player=None):
        self._pack()
        if not self._entities:
            return

        if self._wake_floating_pending:
            self._wake_floating_pending = False
            self._wake_floating_near_bodies()

        # Flag writers journal their transitions, including Hide on sleeping
        # bodies. Keep a sparse fallback for direct writes to already-awake
        # bodies, and retain pending placement work until the body is active.
        active = self._physics_enabled & ~self._kinematic
        self._refresh_inactive_history()
        inactive = self._inactive_mask(active)
        awake_candidates = active & self._awake
        activated = self._was_inactive & awake_candidates & ~inactive
        if np.any(activated):
            self._pending_settle[activated] = True
        self._was_inactive[awake_candidates] = inactive[awake_candidates]
        self._settle_pending_bodies(active)

        base_dt = float(delta) or 1.0 / 60.0
        scale = max(0.0, float(self.time_scale))
        dt = np.float32(min(0.05, max(0.0, base_dt * scale)))
        active &= ~inactive

        # Where the player's push started each body it pushed (NaN for the
        # rest): a push into a wall is undone with the rest of the move.
        pushed_from = None
        pushed = None
        if player is not None:
            ppos = np.asarray(getattr(player, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32)
            ph = getattr(player, '_half', None)
            if ph is not None:
                player_half = np.asarray([float(ph.x), float(ph.y), float(ph.z)], dtype=np.float32)
            else:
                player_half = np.asarray([
                    float(getattr(player, 'width', 50.0)) * 0.5,
                    float(getattr(player, 'height', 100.0)) * 0.5,
                    float(getattr(player, 'depth', 50.0)) * 0.5,
                ], dtype=np.float32)

            pvel = getattr(player, 'velocity', None)
            player_velocity = np.asarray([
                float(getattr(pvel, 'x', 0.0)),
                0.0,
                float(getattr(pvel, 'z', 0.0)),
            ], dtype=np.float32)

            center = self._position + self._offset
            body_min = center - self._half
            body_max = center + self._half
            player_min = ppos - player_half
            player_max = ppos + player_half

            overlap = (
                (body_max[:, 0] > player_min[0]) &
                (body_min[:, 0] < player_max[0]) &
                (body_max[:, 1] > player_min[1]) &
                (body_min[:, 1] < player_max[1]) &
                (body_max[:, 2] > player_min[2]) &
                (body_min[:, 2] < player_max[2])
            )
            pushable = active & self._solid & ~self._kinematic & overlap
            # Sleeping bodies are excluded from the normal per-frame state
            # scan, but an overlapping sleeping body can still be pushed.
            pushable &= ~self._inactive_mask(pushable, awake_only=False)
            if self._any_heavy:
                # The player cannot push a heavy body: it pushes the player.
                heavy = pushable & (self._mass >= self.HEAVY_MASS)
                if np.any(heavy):
                    pushable &= ~heavy
                    self._push_player_out(player, ppos, player_half, center,
                                          np.flatnonzero(heavy))
            speed = float(np.hypot(player_velocity[0], player_velocity[2]))
            if speed >= 0.01 and np.any(pushable):
                dx = center[:, 0] - ppos[0]
                dz = center[:, 2] - ppos[2]
                overlap_x = player_half[0] + self._half[:, 0] - np.abs(dx)
                overlap_z = player_half[2] + self._half[:, 2] - np.abs(dz)
                use_x = overlap_x <= overlap_z

                x_indices = pushable & use_x
                z_indices = pushable & ~use_x

                x_dir = np.where(dx >= 0.0, 1.0, -1.0).astype(np.float32)
                z_dir = np.where(dz >= 0.0, 1.0, -1.0).astype(np.float32)

                pushed_from = np.full_like(self._position, np.nan)
                pushed_from[pushable] = self._position[pushable]
                self._position[x_indices, 0] += x_dir[x_indices] * (overlap_x[x_indices] + 0.5)
                self._position[z_indices, 2] += z_dir[z_indices] * (overlap_z[z_indices] + 0.5)

                target_x = np.abs(player_velocity[0]) / self._mass * 0.85
                target_z = np.abs(player_velocity[2]) / self._mass * 0.85
                self._velocity[x_indices, 0] = x_dir[x_indices] * np.maximum(
                    target_x[x_indices], np.abs(self._velocity[x_indices, 0])
                )
                self._velocity[z_indices, 2] = z_dir[z_indices] * np.maximum(
                    target_z[z_indices], np.abs(self._velocity[z_indices, 2])
                )
                self._awake[pushable] = True
                pushed = pushable

        moving = active & self._awake
        if np.any(moving):
            if np.any(moving & (self._gravity != 0.0)):
                self._velocity[moving, 1] += (
                    self._gravity[moving] * np.float32(self.gravity) * dt
                )

            damping = self._damping * np.float32(max(0.0, float(self.damping_scale)))
            self._velocity[moving, 1] *= np.maximum(
                0.0, 1.0 - damping[moving] * dt
            )

            horizontal_damp = np.maximum(
                0.0,
                1.0 - damping * dt,
            )
            self._velocity[moving, 0] *= horizontal_damp[moving]
            self._velocity[moving, 2] *= horizontal_damp[moving]

            old_position = self._position.copy()
            old_center = old_position + self._offset
            previous_bottom = old_center[:, 1] - self._half[:, 1]

            self._position[moving, 0] += self._velocity[moving, 0] * dt
            self._batch_static_collision(0, old_position, pushed_from=pushed_from)

            self._position[moving, 2] += self._velocity[moving, 2] * dt
            self._batch_static_collision(2, old_position, pushed_from=pushed_from)

            self._position[moving, 1] += self._velocity[moving, 1] * dt
            rising = moving & (self._velocity[:, 1] > 0.0)
            if np.any(rising):
                # Nothing rises into a ceiling.
                self._batch_static_collision(1, old_position, rising)
            floors = self._batch_floor(previous_bottom)
            new_center = self._position + self._offset
            new_bottom = new_center[:, 1] - self._half[:, 1]
            landed = moving & np.isfinite(floors) & (
                new_bottom <= floors + 1.0
            )
            if np.any(landed):
                self._position[landed, 1] = (
                    floors[landed]
                    - self._offset[landed, 1]
                    + self._half[landed, 1]
                )
                if self._any_bounce:
                    # Bounce back up if bouncy enough.
                    self._velocity[landed, 1] = self._bounce_speed(
                        np.maximum(-self._velocity[landed, 1], 0.0),
                        self._bounce[landed])
                else:
                    self._velocity[landed, 1] = 0.0

                # Treat friction as a surface coefficient rather than a tiny
                # per-frame damping term. Apply it only while grounded. A
                # rolling body loses only a little of its speed to the ground.
                friction_accel = (
                    self._friction[landed]
                    * np.float32(max(0.0, float(self.friction_scale)))
                    * np.float32(abs(float(self.gravity)))
                )
                if self._any_rolls:
                    friction_accel = friction_accel * np.where(
                        self._rolls[landed],
                        np.float32(self.ROLLING_FRICTION_SCALE),
                        np.float32(1.0))
                ground_speed = np.hypot(
                    self._velocity[landed, 0],
                    self._velocity[landed, 2],
                )
                friction_delta = friction_accel * dt
                scale = np.maximum(
                    0.0,
                    1.0 - friction_delta / np.maximum(ground_speed, 1e-6),
                )
                self._velocity[landed, 0] *= scale
                self._velocity[landed, 2] *= scale

            angular = self._angular_velocity
            if np.any(angular):
                self._integrate_rotation(angular, dt)

            if self._any_rolls:
                rolling_rows = np.flatnonzero(
                    moving & self._rolls
                    & ((self._velocity[:, 0] != 0.0) | (self._velocity[:, 2] != 0.0)))
                if rolling_rows.size:
                    self._roll(rolling_rows, dt)

            speed = np.max(np.abs(self._velocity), axis=1)
            sleeping = (
                self.sleep_enabled
                & moving
                & (speed < self.REST_SPEED)
            )
            if self._any_bounce:
                # A bounce is momentarily still at the top of its arc.
                sleeping &= landed
            if np.any(sleeping):
                self._velocity[sleeping] = 0.0
                rest_indices = np.flatnonzero(sleeping)
                self._awake[sleeping] = False
                for i in rest_indices:
                    body = self.bodies[id(self._entities[int(i)])]
                    callback = body.rest_callback
                    if callback is not None:
                        callback(body.entity)

            self._sync_entities(np.flatnonzero(moving))

        if pushed is not None and player is not None:
            # A body the player pushed but a wall held: the player stops at it.
            self._bodies_block_player(player, ppos, player_half, pushed)
def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

