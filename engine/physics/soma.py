"""Soma (Greek: "body") simulates bodies.

That is the whole of what it does. The world is not Soma's: walls and
floors are asked of the :class:`engine.spatial_grid.SpatialGrid` it is
created with, and it keeps no copy. The game's rules are not Soma's either:
what is heavy and what crushing costs are the contract's. Soma holds the
bodies -- their state as NumPy rows -- and moves the awake ones.

Fio's built-in physics, registered as ``"Soma"``
(:mod:`engine.physics.registry`) and reached by the engine only through the
contract (:class:`engine.physics.Physics`).
"""
import numpy as np
import glm

from ..change_journal import JOURNAL, OVERFLOW, STATE, VISIBILITY, touch
from ..brush_placement import world_box
from ..spatial import SIM_TIER_KEY, TIER_DISTANT, TIER_NEAR, cells_of_points, tier_of
from ..spatial_grid import SpatialGrid
from . import api
from .api import SV_GRAVITY


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


class _Pairs:
    """A step's one broad phase: each awake body with everything near its move.

    Body ``query[k]`` (a position among the step's rows) is paired with a box
    ``lo[k]..hi[k]`` -- float64, in the world. Which kind of thing the box
    belongs to decides how it is tested:

    * a brush of the spatial grid (``row[k]`` its row in ``rows``, the grid's
      rows object). ``shaped[k]`` says its box is only what it is filed by --
      an angled brush, a turning one -- and its true surfaces are the grid's
      to give (:meth:`SpatialGrid.columns`);
    * another body (``other[k]`` its row; -1 for a brush).

    ``offset`` and ``half`` are the bodies' own boxes, per body, which every
    test of the pairs needs.
    """

    __slots__ = ('query', 'lo', 'hi', 'offset', 'half', 'other', 'row',
                 'shaped', 'rows', 'any_shaped')

    def __init__(self, query, lo, hi, offset, half, other, row, shaped, rows):
        self.query, self.lo, self.hi = query, lo, hi
        self.offset, self.half = offset, half
        self.other, self.row, self.shaped, self.rows = other, row, shaped, rows
        self.any_shaped = bool(shaped.any())

    def of(self, chosen, count):
        """These pairs for the bodies in *chosen* only (positions among
        *count*), renumbered to index them."""
        renumber = np.full(count, -1, dtype=np.int64)
        renumber[chosen] = np.arange(len(chosen))
        to = renumber[self.query]
        keep = to >= 0
        return _Pairs(to[keep], self.lo[keep], self.hi[keep],
                      self.offset[chosen], self.half[chosen], self.other[keep],
                      self.row[keep], self.shaped[keep], self.rows)


class PhysicsBody:
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
            if value and not self.world._awake[i]:
                self.world._wake_dependents(i)
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


class Soma:
    """Soma simulates bodies: batched, over NumPy rows.

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
    GRAVITY = np.float32(-SV_GRAVITY)
    #: Active-body count from which one batched floor query
    #: (:meth:`SpatialGrid.raycast_down_batch`) is cheaper than a
    #: ``raycast_down`` per support point. The batched query is a fixed fee
    #: of a few dozen NumPy calls however many points it answers; a raycast
    #: costs per point, and per brush in the point's cell. Measured with
    #: ``tools/bench_physics.py`` (about ten brushes a cell) the two cross at
    #: around 16 bodies -- 190 against 250 microseconds a step for one body,
    #: 440 against 440 for sixteen, 560 against 500 for thirty-two. Both
    #: answer identically (tests/physics/test_dynamic_bodies.py), so this is
    #: purely a cost choice and never a behavioural one.
    FLOOR_BATCH_MIN_BODIES = 16
    #: The same crossover in query points: a body asks about five.
    FLOOR_BATCH_MIN_POINTS = 5 * FLOOR_BATCH_MIN_BODIES
    MAX_STEP = np.float32(0.05)
    #: The steepest ground a body moves up: rise over run. 1 is 45 degrees,
    #: Quake's walkable limit near enough (a floor normal of 0.7). Steeper
    #: ground is in the way, as a wall is.
    WALKABLE_SLOPE = 1.0
    #: How far short of a wall's face a body that meets it is put: Quake's
    #: DIST_EPSILON. Positions are float32, so a body placed exactly on a
    #: face may round into it; a body this far off cannot, out to a couple
    #: of hundred thousand units from the origin.
    CONTACT_SKIN = 0.03125
    REST_SPEED = np.float32(1.0)

    #: The share of its ``friction`` a body that ``rolls`` loses to the ground:
    #: rolling loses far less speed than sliding.
    ROLLING_FRICTION_SCALE = 0.05
    #: The contract's (:data:`engine.physics.HEAVY_MASS`): a heavy body does
    #: not bounce, and the player cannot push it -- it pushes the player
    #: instead, and crushes a player it drives into a wall. A body of mass 1
    #: or less bounces by its full ``bounciness``; between the two the bounce
    #: fades linearly.
    HEAVY_MASS = api.HEAVY_MASS
    #: A rebound slower than this (units per second) is not a bounce: the
    #: body settles instead of chattering on the floor.
    MIN_BOUNCE_SPEED = 60.0
    #: A heavy body moving into the player faster than this (units per
    #: second) crushes them if it pins them against a wall.
    CRUSH_SPEED = 5.0
    #: The contract's (:data:`engine.physics.CRUSH_DAMAGE`).
    CRUSH_DAMAGE = api.CRUSH_DAMAGE

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
        self._clear_body_index()
        self._dirty = False
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
        self._clear_body_index()
        self._reported_cell = None
        self._dirty = False

    def _clear_body_index(self):
        """Where the bodies are, for bodies meeting bodies (see
        :meth:`_index_bodies`), and what each rests on."""
        #: Row of the body each body rests on, or -1 (the world, or nothing).
        self._support = np.empty(0, dtype=np.int64)
        #: Whether each body was on the ground when it was last stepped.
        self._grounded = np.empty(0, dtype=np.bool_)
        #: Whether any body has ever rested on another: until one does, no
        #: wake has anything to pass on.
        self._any_support = False
        #: The cell each body was filed under when the index was last built.
        self._body_key = np.empty(0, dtype=np.int64)
        #: The index: obstacle rows sorted by cell key, and their keys.
        self._body_rows = np.empty(0, dtype=np.int64)
        self._body_keys = np.empty(0, dtype=np.int64)
        #: The widest obstacle's horizontal half-extent: how far outside its
        #: own cell a body filed by its centre can reach.
        self._body_reach = 0.0
        self._bodies_dirty = True
        #: This step's knocks: ``(rows, axis, velocity)`` for bodies another
        #: body ran into, applied when the step's rows are put back.
        self._knocks = []

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
        self._any_spin = False
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
            body = PhysicsBody(self, entity, entity_id)
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
        self._support = np.full(n, -1, dtype=np.int64)
        self._grounded = np.zeros(n, dtype=np.bool_)
        self._any_support = False
        self._bodies_dirty = True

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
        self._any_spin = bool(self._angular_velocity.any())
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
        # In or out of what other bodies meet; and no longer under anything.
        self._bodies_dirty = True
        if value:
            self._wake_dependents(index)
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
        was_awake = bool(self._awake[index])
        if self._kinematic[index]:
            self._bodies_dirty = True
        self._awake[index] = True
        self._kinematic[index] = False
        if not was_awake:
            self._wake_dependents(index)

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
        self._bodies_dirty = True
        self._grounded[index] = False
        self._wake_dependents(index)
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

        Soma owns an entity's position while it is in motion, so a
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
        """The world controls back to their defaults; every body woken."""
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
            elif inactive and not self._was_inactive[index]:
                # Hidden or disabled: gone from under whatever rested on it.
                self._wake_dependents(index)
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

    def _inactive_mask(self, candidates, awake_only=True, distant=None):
        """Which candidate bodies are hidden or disabled.

        ``disabled`` is authored gameplay state; ``hidden`` is the Prop's
        I/O Hide state. Both make its physical body dormant. Ordinary
        integration only needs to inspect awake candidates, preserving the
        sparse hot path. The player-push query also checks its small set of
        overlapping bodies with ``awake_only=False``, because a sleeping body
        can still overlap the player and must not be pushed while inactive.

        *distant*, a mask the size of the world, is filled where a candidate
        is beyond live simulation (:meth:`step`): the same look at the same
        property dict, so knowing costs nothing more.
        """
        mask = np.zeros(len(self._entities), dtype=np.bool_)
        rows = candidates & self._awake if awake_only else candidates
        indices = np.flatnonzero(rows)
        if indices.size == 0:
            return mask
        entities = self._entities
        for i in indices.tolist():
            props = getattr(entities[i], 'properties', None)
            if not isinstance(props, dict):
                continue
            if props.get('disabled') or props.get('hidden'):
                mask[i] = True
            elif distant is not None:
                tier = props.get(SIM_TIER_KEY)
                if tier is not None and tier >= TIER_DISTANT:
                    distant[i] = True
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
        """Move the player out of the body rows *rows* they still overlap."""
        centre = self._position[rows] + self._offset[rows]
        half = self._half[rows]
        overlap = np.all(
            (centre + half > ppos - player_half)
            & (centre - half < ppos + player_half), axis=1)
        if overlap.any():
            self._push_player_out(player, ppos, player_half, rows[overlap], carry=False)

    def _push_player_out(self, player, ppos, player_half, rows, carry=True):
        """Bodies in *rows* overlapping the player shove the player out.

        The player goes out along whichever horizontal axis overlaps least, and
        a body moving into the player carries them along at its own speed. If
        that leaves the player inside a wall, the body has pinned them there:
        :attr:`player_crushed`. Without *carry* the player is only stopped.
        """
        centres = (self._position[rows] + self._offset[rows]).tolist()
        halves = self._half[rows].tolist()
        for i, centre, half in zip(rows.tolist(), centres, halves):
            dx = float(ppos[0]) - centre[0]
            dz = float(ppos[2]) - centre[2]
            overlap_x = float(player_half[0]) + half[0] - abs(dx)
            overlap_z = float(player_half[2]) + half[2] - abs(dz)
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
        # Trimmed top and bottom so the floor stood on and a low ceiling are
        # not walls.
        lo = np.array([[ppos[0] - player_half[0], ppos[1] - player_half[1] + 8.0,
                        ppos[2] - player_half[2]]], dtype=np.float64)
        hi = np.array([[ppos[0] + player_half[0], ppos[1] + player_half[1] - 8.0,
                        ppos[2] + player_half[2]]], dtype=np.float64)
        _query, row, rows = self.spatial_grid.box_pairs(lo, hi)
        return bool(np.any(np.all((rows.hi[row] > lo) & (rows.lo[row] < hi), axis=1)))

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
        entities = self._entities
        if indices is None:
            rows, positions = range(len(entities)), self._position.tolist()
        else:
            indices = np.asarray(indices)
            rows, positions = indices.tolist(), self._position[indices].tolist()
        for i, position in zip(rows, positions):
            # Assigned, not written in place: assignment is what tells the
            # render projection the entity moved.
            entities[i].pos = position

    def rebuild(self, brushes):
        self.clear()

        for brush in brushes:
            if not brush.get('_physics_body'):
                continue
            entity = brush.get('_physics_entity')
            if entity is not None:
                self.register_body(entity, brush)

        self._pack()
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
        if self._can_group_floor_queries():
            # What the step itself would stand it on: brushes by their true
            # surfaces, the terrain, other bodies. Standing on another body
            # is not floating -- it is what holds it up, and what wakes it
            # if it goes.
            where = self._position[rows]
            pairs = self._world_pairs(rows, where, where)
            floors, held, _slope = self._floors_and_support(
                rows, bottom.astype(np.float32), pairs)
            resting = floors >= bottom - 1.0
            self._support[rows[resting]] = held[resting]
            self._grounded[rows[resting]] = True
            if (held[resting] >= 0).any():
                self._any_support = True
            self._awake[rows[~resting]] = True
            return
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
        a grid that can answer. Batched only where it pays: below the measured
        crossover (FLOOR_BATCH_MIN_POINTS) a raycast per point is cheaper than
        the batched query."""
        if xs.size >= self.FLOOR_BATCH_MIN_POINTS and self._can_group_floor_queries():
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

    # ------------------------------------------------------------------
    # Bodies meeting bodies
    #
    # To an awake body every other solid body is one more box in its way:
    # something to stop at, to bounce off, to come to rest on. So the other
    # bodies near it join the step's pairs beside the world's brushes, and
    # the same tests answer for both. What is different about a body is
    # that it can be moved: one that is run into is knocked on, by the
    # momentum the two share, and one that is rested on wakes what rests on
    # it when it goes.
    #
    # Sleeping bodies cost nothing here. The index of where bodies are is
    # rebuilt only when an awake body crosses into another cell (or a body
    # is added, carried or put down), and it is asked only about the awake.
    # ------------------------------------------------------------------

    _KEY_STRIDE = 1 << 32
    _KEY_BIAS = 1 << 31

    def _cell_key(self, rows):
        """The grid column each body row's centre is in, as one int64."""
        size = float(self.spatial_grid.cell_size)
        centre = np.nan_to_num(
            (self._position[rows] + self._offset[rows]).astype(np.float64),
            nan=0.0, posinf=0.0, neginf=0.0)
        limit = float(self._KEY_BIAS - 1)
        cx = np.clip(np.floor(centre[:, 0] / size), -limit, limit).astype(np.int64)
        cz = np.clip(np.floor(centre[:, 2] / size), -limit, limit).astype(np.int64)
        return cx * self._KEY_STRIDE + (cz + self._KEY_BIAS)

    def _index_bodies(self):
        """Bring the body index up to date; whether there are two bodies to
        meet at all.

        The obstacles are the solid, simulated bodies nothing else is
        carrying. Each is filed under the one cell its centre is in -- the
        spatial grid's columns (:mod:`engine.spatial`) -- and a query looks
        :attr:`_body_reach` further than its box, which is as far outside
        its cell as any of them reaches.
        """
        if self._bodies_dirty:
            self._bodies_dirty = False
            count = len(self._entities)
            self._body_key = self._cell_key(slice(None)) if count else np.empty(0, np.int64)
            rows = np.flatnonzero(self._physics_enabled & self._solid & ~self._kinematic)
            if rows.size:
                keys = self._body_key[rows]
                order = np.argsort(keys, kind='stable')
                self._body_rows = rows[order]
                self._body_keys = keys[order]
                self._body_reach = float(self._half[rows][:, (0, 2)].max())
            else:
                self._body_rows = np.empty(0, dtype=np.int64)
                self._body_keys = np.empty(0, dtype=np.int64)
                self._body_reach = 0.0
        return self._body_rows.size > 1

    def _bodies_near(self, lo, hi):
        """``(query, other)``: for each box ``lo[i]..hi[i]`` (float64), every
        obstacle row filed in a cell the box, grown by the reach, touches.
        The broad phase only; boxes are read live by the caller."""
        keys = self._body_keys
        size = float(self.spatial_grid.cell_size)
        reach = self._body_reach
        cx0 = np.floor((lo[:, 0] - reach) / size).astype(np.int64)
        cz0 = np.floor((lo[:, 2] - reach) / size).astype(np.int64)
        more_x = np.floor((hi[:, 0] + reach) / size).astype(np.int64) - cx0
        more_z = np.floor((hi[:, 2] + reach) / size).astype(np.int64) - cz0
        queries, others = [], []
        for dx in range(int(more_x.max()) + 1):
            for dz in range(int(more_z.max()) + 1):
                if dx or dz:
                    which = np.flatnonzero((more_x >= dx) & (more_z >= dz))
                    if not which.size:
                        continue
                    key = ((cx0[which] + dx) * self._KEY_STRIDE
                           + (cz0[which] + dz + self._KEY_BIAS))
                else:
                    which = None
                    key = cx0 * self._KEY_STRIDE + (cz0 + self._KEY_BIAS)
                first = np.searchsorted(keys, key, side='left')
                counts = np.searchsorted(keys, key, side='right') - first
                total = int(counts.sum())
                if not total:
                    continue
                within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
                others.append(self._body_rows[np.repeat(first, counts) + within])
                query = np.repeat(np.arange(len(key)), counts)
                queries.append(query if which is None else which[query])
        if not queries:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty
        if len(queries) == 1:
            return queries[0], others[0]
        return np.concatenate(queries), np.concatenate(others)

    def _body_pairs(self, indices, lo, hi):
        """The other bodies near each body row's move ``lo..hi``:
        ``(query, lo, hi, other)`` -- their boxes where they are now, and
        their rows. Never itself, never one that is hidden or disabled."""
        query, other = self._bodies_near(lo, hi)
        if not len(query):
            return None
        keep = other != indices[query]
        if not keep.any():
            return None                     # alone in its cells: the usual case
        query, other = query[keep], other[keep]
        keep = np.ones(len(query), dtype=np.bool_)
        centre = self._position[other].astype(np.float64) + self._offset[other]
        half = self._half[other].astype(np.float64)
        other_lo = centre - half
        other_hi = centre + half
        # Beside or under it: a column's width apart is not near.
        keep &= ((hi[query, 0] > other_lo[:, 0]) & (lo[query, 0] < other_hi[:, 0])
                 & (hi[query, 2] > other_lo[:, 2]) & (lo[query, 2] < other_hi[:, 2]))
        if not keep.any():
            return None
        query, other = query[keep], other[keep]
        other_lo, other_hi = other_lo[keep], other_hi[keep]
        # A dormant body is in nobody's way. Asked of the few bodies that are
        # near something awake, asleep or not: their flags are not journalled
        # by every writer.
        dormant = self._dormant(other)
        if dormant.any():
            keep = ~dormant
            if not keep.any():
                return None
            query, other = query[keep], other[keep]
            other_lo, other_hi = other_lo[keep], other_hi[keep]
        return query, other_lo, other_hi, other

    def _dormant(self, rows):
        """Which of the body rows *rows* are hidden or disabled.

        An awake body's flags were read when this step began, and a dormant
        one is not awake; so only the sleeping rows are asked, each once,
        from their entities -- not every writer of those flags journals.
        """
        out = np.zeros(len(rows), dtype=np.bool_)
        asleep = np.flatnonzero(~self._awake[rows])
        if asleep.size:
            unique, where = np.unique(rows[asleep], return_inverse=True)
            entities = self._entities
            flags = np.zeros(unique.size, dtype=np.bool_)
            for k, i in enumerate(unique.tolist()):
                props = getattr(entities[i], 'properties', None)
                if isinstance(props, dict) and (props.get('disabled') or props.get('hidden')):
                    flags[k] = True
            out[asleep] = flags[where]
        return out

    def _bodies_at(self, lo, hi):
        """The obstacle rows whose box overlaps the box ``lo..hi`` (float32
        triples), in row order: what the player is touching."""
        self._index_bodies()
        if not self._body_rows.size:
            return self._body_rows
        _query, other = self._bodies_near(
            lo[None].astype(np.float64), hi[None].astype(np.float64))
        if not other.size:
            return other
        other = np.unique(other)
        centre = self._position[other] + self._offset[other]
        half = self._half[other]
        return other[np.all((centre + half > lo) & (centre - half < hi), axis=1)]

    def _wake_dependents(self, rows):
        """Wake whatever rests on the body rows *rows*, and whatever rests on
        that: what held them up is moving, or gone."""
        if not self._any_support:
            return
        rows = np.atleast_1d(np.asarray(rows, dtype=np.int64))
        while rows.size:
            rows = np.flatnonzero(
                np.isin(self._support, rows) & ~self._awake
                & self._physics_enabled & ~self._kinematic)
            self._awake[rows] = True

    def _knock(self, axis, forward, mine, others, velocity, into):
        """Share momentum between bodies and the bodies they ran into.

        *mine* are positions in this step's working rows, *others* the body
        rows each met along *axis* (*forward*: in the positive direction),
        *into* its rows. The two leave at the velocity their momentum gives
        them together -- so a heavy body barely notices a light one -- and
        apart again by their bounciness. A body that would not be moved to
        speak of is a wall, and is left asleep. Returns which pairs knocked, and
        how fast each pair was closing.
        """
        mine_rows = into[mine]
        v_mine = velocity[mine, axis].astype(np.float64)
        v_other = self._velocity[others, axis].astype(np.float64)
        closing = (v_mine - v_other) * np.where(forward, 1.0, -1.0)
        m_mine = self._mass[mine_rows].astype(np.float64)
        m_other = self._mass[others].astype(np.float64)
        total = m_mine + m_other
        shared = (m_mine * v_mine + m_other * v_other) / total
        if self._any_bounce:
            spring = np.maximum(self._bounce[mine_rows], self._bounce[others]).astype(np.float64)
            spring = np.where(closing * spring > self.MIN_BOUNCE_SPEED, spring, 0.0)
        else:
            spring = 0.0
        apart = spring * (v_mine - v_other) / total
        new_mine = shared - m_other * apart
        new_other = shared + m_mine * apart
        knocked = (closing > 0.0) & (self._awake[others]
                                     | (np.abs(new_other) >= self.REST_SPEED))
        if knocked.any():
            velocity[mine[knocked], axis] = new_mine[knocked]
            self._knocks.append((others[knocked], axis,
                                 new_other[knocked].astype(np.float32)))
        return knocked, closing

    def _world_pairs(self, indices, old, ahead, pushed_from=None):
        """This step's one broad phase (:class:`_Pairs`): each body row of
        *indices* with every brush filed in a cell its move touches, from
        *old* to *ahead* (where it would be with nothing in its way), and
        every other body near it.

        The grid's cells are columns, so one footprint answers for all three
        axes' walls and for the floor under the body wherever in that move it
        ends up: the grid is asked once a step, not once a question.
        """
        offset = self._offset[indices].astype(np.float64)
        half = self._half[indices].astype(np.float64)
        was = old.astype(np.float64) + offset
        now = ahead.astype(np.float64) + offset
        lo = np.minimum(was, now) - half
        hi = np.maximum(was, now) + half
        if pushed_from is not None:
            # A push that is undone puts the body back where it began.
            start = pushed_from[indices].astype(np.float64) + offset
            pushed = ~np.isnan(start[:, 0])
            if pushed.any():
                lo[pushed] = np.minimum(lo[pushed], start[pushed] - half[pushed])
                hi[pushed] = np.maximum(hi[pushed], start[pushed] + half[pushed])
        query, row, boxes = self.spatial_grid.box_pairs(lo, hi)
        pair_lo, pair_hi = boxes.lo[row], boxes.hi[row]
        shaped = boxes.shaped[row]
        # Which body each pair is with: none, for the world's brushes.
        other = np.full(len(query), -1, dtype=np.int64)
        if self._index_bodies():
            bodies = self._body_pairs(indices, lo, hi)
            if bodies is not None:
                count = len(bodies[0])
                query = np.concatenate((query, bodies[0]))
                pair_lo = np.concatenate((pair_lo, bodies[1]))
                pair_hi = np.concatenate((pair_hi, bodies[2]))
                other = np.concatenate((other, bodies[3]))
                row = np.concatenate((row, np.full(count, -1, dtype=np.int64)))
                shaped = np.concatenate((shaped, np.zeros(count, dtype=np.bool_)))
        return _Pairs(query, pair_lo, pair_hi, offset, half, other, row, shaped, boxes)

    def _support_points(self, centre, half):
        """The five points a body stands on -- its centre and four inside its
        footprint -- as ``(x, z)``, each ``(n, 5)``. One definition, for the
        floor under a body and for what is in its way."""
        zeros = np.zeros(len(centre))
        hx = half[:, 0] * 0.75
        hz = half[:, 2] * 0.75
        x = centre[:, 0:1] + np.stack([zeros, -hx, hx, zeros, zeros], axis=1)
        z = centre[:, 2:3] + np.stack([zeros, zeros, zeros, -hz, hz], axis=1)
        return x, z

    def _collide_axis(self, axis, indices, position, velocity, old, pairs,
                      start=None):
        """Resolve one axis against what is in the way, for every body at once.

        The world is the spatial grid's -- its boxes, its movers and doors
        where they are now; nothing of it is kept here. *pairs* is this
        step's one broad phase (:meth:`_world_pairs`): every body with every
        brush near its move, and every other body.

        *indices* are the body rows to test, and *position*, *velocity* and
        *old* their rows of working state -- where each is now, how fast, and
        where it was before this step -- changed in place. Only awake rows
        reach here, and nothing here is the size of the world.

        The test is swept: each body is asked about the whole of this axis's
        move, from *old* to where it is now, so a fast body cannot
        step over a thin wall, and one that meets a wall stops *at* it --
        :data:`CONTACT_SKIN` short of the face it first reaches -- not back
        where the step began, which left a gap the size of the step. It then
        stops on this axis, or bounces off by its bounciness. A body that is
        inside a wall both before and after the move (it started there) has
        no face to stop at: it goes back to *old*, as it always did.

        *start* is where the move is swept from, where that is not *old*: a
        body the player pushed this step was moved by the push before the
        step began, and the push is part of what must not carry it through a
        wall or into another body. Pushed into a wall, it stops at the wall.
        One that was already in a wall keeps the push (it may be pushed out)
        and loses only the rest of the move.
        """
        if indices.size == 0:
            return
        if start is None:
            start = old
        query, lo, hi, offset, half, other = (
            pairs.query, pairs.lo, pairs.hi, pairs.offset, pairs.half, pairs.other)
        terrain = self.spatial_grid.terrain if axis != 1 else None
        if ((not len(query) and terrain is None)
                or not (position[:, axis] != start[:, axis]).any()):
            # Nothing near, or nothing moved on this axis: a body that has
            # not moved has met nothing new, and one inside a wall stays put.
            return
        centre = position.astype(np.float64) + offset
        bmin = centre - half
        bmax = centre + half
        # This axis only: where the body's two faces were before the move,
        # and where it stays if it has no face to stop at.
        was = start[:, axis].astype(np.float64) + offset[:, axis]
        stay = old[:, axis].astype(np.float64) + offset[:, axis]
        old_min = was - half[:, axis]
        old_max = was + half[:, axis]
        swept_min = bmin.copy()
        swept_max = bmax.copy()
        swept_min[:, axis] = np.minimum(bmin[:, axis], old_min)
        swept_max[:, axis] = np.maximum(bmax[:, axis], old_max)
        # Across the move the box is the skin smaller: what a body rests on
        # or stands against (a float32 rounding into it, at most) is not in
        # its way.
        skin = self.CONTACT_SKIN
        for across in range(3):
            if across != axis:
                swept_min[:, across] += skin
                swept_max[:, across] -= skin

        touching = np.all((swept_max[query] > lo) & (swept_min[query] < hi), axis=1)
        # What is not its box -- an angled brush, the terrain -- is in the
        # way only where its true surface is: asked of the grid, below.
        steep = self._blocked_by_surfaces(
            axis, position, old, centre, half, bmin, bmax, pairs, touching, terrain)
        if pairs.any_shaped:
            touching &= ~pairs.shaped
        if not touching.any() and steep is None:
            return
        query, lo, hi = query[touching], lo[touching, axis], hi[touching, axis]
        other = other[touching]
        soft = other >= 0

        # A wall ahead is one whose near face the body's leading face had not
        # passed before the move and has passed now. The nearest decides.
        forward = centre[query, axis] > was[query]
        backward = centre[query, axis] < was[query]
        ahead = forward & (lo >= old_max[query])
        behind = backward & (hi <= old_min[query])
        stop = np.full(indices.size, np.inf)
        np.minimum.at(stop, query[ahead], lo[ahead])
        back_stop = np.full(indices.size, -np.inf)
        np.maximum.at(back_stop, query[behind], hi[behind])
        # Anything else it touches now, it was already in: no face to stop at.
        # (Another body it is inside is not a wall: bodies placed
        # overlapping stay free of each other, as they always were.)
        inside = (~(ahead | behind) & ~soft
                  & (bmax[query, axis] > lo) & (bmin[query, axis] < hi))
        embedded = np.zeros(indices.size, dtype=np.bool_)
        embedded[query[inside]] = True
        if steep is not None:
            # A surface too steep to climb has no face to stop at either: the
            # body stays where this axis's move began.
            embedded |= steep

        met_ahead = np.isfinite(stop)
        met_behind = np.isfinite(back_stop)
        collision = met_ahead | met_behind | embedded
        if not collision.any():
            return

        # Never past where the move began: a body closer to the wall than
        # the skin stays where it is.
        target = np.where(
            met_ahead, np.maximum(stop - skin - half[:, axis], was),
            np.where(met_behind, np.minimum(back_stop + skin + half[:, axis], was),
                     was))
        target = np.where(embedded, stay, target) - offset[:, axis]

        hit = np.flatnonzero(collision)
        hit_indices = indices[hit]
        target = target[hit].astype(np.float32)
        position[hit, axis] = target
        # What it met, if that was another body: the ones at the face it
        # stopped at. Read before the wall's answer below changes its speed.
        met = None
        if soft.any():
            front = ahead & soft & (lo == stop[query])
            back = behind & soft & (hi == back_stop[query])
            met = np.flatnonzero(front | back)
            if met.size:
                incoming = velocity[query[met], axis].copy()
            else:
                met = None
        if self._any_bounce:
            # Bounce off the wall if bouncy enough.
            hit_velocity = velocity[hit, axis]
            velocity[hit, axis] = -np.sign(hit_velocity) * self._bounce_speed(
                np.abs(hit_velocity), self._bounce[hit_indices])
        else:
            velocity[hit, axis] = 0.0
        if met is not None:
            # A body run into is knocked on rather than stood against; one
            # too heavy to notice stays the wall it was treated as above.
            mine = query[met]
            answered = velocity[mine, axis].copy()
            velocity[mine, axis] = incoming
            knocked, closing = self._knock(axis, front[met], mine, other[met],
                                           velocity, indices)
            # One that is not closing on what it met is following it: it
            # keeps its speed (and its distance). The rest met a wall.
            walled = ~knocked & (closing > 0.0)
            velocity[mine[walled], axis] = answered[walled]

    def _blocked_by_surfaces(self, axis, position, old, centre, half, bmin, bmax,
                             pairs, touching, terrain):
        """Which bodies this axis's move has carried into ground too steep to
        climb -- an angled brush's true surface, or the terrain -- or None
        when there is neither near.

        A slope is not a wall and not a floor until it is asked which. Under
        each of the body's support points the grid gives the height of the
        solid there; ground that has risen no more than a walkable slope
        would over the distance moved (:data:`WALKABLE_SLOPE`) is climbed --
        the floor pass lifts the body onto it -- and ground higher than that
        is in the way. An angled brush overhead, whose solid begins above the
        body, is neither.
        """
        shaped = touching & pairs.shaped if pairs.any_shaped else None
        if shaped is not None and not shaped.any():
            shaped = None
        if shaped is None and terrain is None:
            return None
        # How far ground may have risen and still be climbed: a walkable
        # slope's rise over what has been moved across it so far this step.
        moved = (np.abs(position[:, 0] - old[:, 0])
                 + np.abs(position[:, 2] - old[:, 2])).astype(np.float64)
        limit = bmin[:, 1] + 1.0 + self.WALKABLE_SLOPE * moved
        x, z = self._support_points(centre, half)
        blocked = np.zeros(len(centre), dtype=np.bool_)
        grid = self.spatial_grid
        if shaped is not None:
            query = pairs.query[shaped]
            bottom, top = grid.columns(pairs.rows, pairs.row[shaped], x[query], z[query])
            in_way = ((top > limit[query, None])
                      & (bottom < bmax[query, 1:2] - self.CONTACT_SKIN)).any(axis=1)
            blocked[query[in_way]] = True
        if terrain is not None:
            heights = grid.terrain_heights(x, z)
            blocked |= (heights > limit[:, None]).any(axis=1)
        return blocked if blocked.any() else None

    def _floors_under(self, indices, previous_bottom):
        """The floor beneath each body row in *indices*: float32, -inf where
        there is none.

        Five support points per body -- centre plus four in-footprint samples --
        so a body straddling a seam between floor brushes cannot lose contact
        just because its centre crossed the gap.

        That is five downward queries a body, and the spatial grid answers
        them: one :meth:`SpatialGrid.raycast_down` each for a few bodies, one
        :meth:`SpatialGrid.raycast_down_batch` for many (the same brushes, the
        same boxes and the same comparisons, so the answers are identical and
        the choice is only a cost one: :data:`FLOOR_BATCH_MIN_BODIES`).
        Either way a mover or door is where it is now: a prop riding a
        platform sees the platform's current height.

        *previous_bottom* is each body's bottom before this step: the ray
        starts from the higher of that and its bottom now, so a body that
        integration has already carried through a floor still sees it.

        The batched query is used only for a stock :class:`SpatialGrid`.
        Anything that overrides ``raycast_down`` -- a test double, or a future
        grid with its own tracing -- is asked point by point, because its
        answers are its own.
        """
        if (indices.size >= self.FLOOR_BATCH_MIN_BODIES
                and self._can_group_floor_queries()):
            return self._floors_batched(indices, previous_bottom)
        return self._floors_scalar(indices, previous_bottom)

    def _can_group_floor_queries(self):
        """Whether the grid's batched floor query answers as its
        ``raycast_down`` does: true of a stock grid, not of one that traces
        its own way."""
        grid = self.spatial_grid
        return (isinstance(grid, SpatialGrid)
                and type(grid).raycast_down is SpatialGrid.raycast_down
                and type(grid).raycast_down_batch is SpatialGrid.raycast_down_batch)

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

    def _floors_batched(self, indices, previous_bottom):
        floors = np.full(indices.size, -np.inf, dtype=np.float32)
        if indices.size == 0:
            return floors
        px, pz, bottom = self._support_offsets(indices)
        ray_y = np.maximum(bottom, previous_bottom.astype(np.float64)) + 1.0
        best = self._floor_tops(px.ravel(), pz.ravel(), np.repeat(ray_y, px.shape[1]))
        per_body = best.reshape(px.shape).max(axis=1)
        hit = per_body > -np.inf
        if hit.any():
            floors[hit] = per_body[hit].astype(np.float32)
        return floors

    def _floors_from_pairs(self, indices, previous_bottom, pairs):
        """:meth:`_floors_batched` from this step's broad phase rather than a
        query of its own: a support point is inside its body's footprint, so
        the brushes filed in the point's cell are among the body's pairs.
        The same brushes, boxes and comparisons; the same answers."""
        return self._floors_and_support(indices, previous_bottom, pairs)[0]

    def _floors_and_support(self, indices, previous_bottom, pairs, follow=None):
        """What each body stands on: ``(floors, support, slope)``.

        *floors* is the highest ground under its support points, float32,
        -inf where there is none. *support* is the row of the body whose top
        that is -- -1 where it is the world's, or there is none: another
        body's top is a floor like any other. *slope* is the ground's
        gradient there, ``(n, 2)``: how much it rises along x and along z.

        A box's top is a floor where it is at or below the body's bottom. An
        angled brush's surface and the terrain are asked of the grid, point
        by point, and are floors up to *follow* higher than that: the rise a
        walkable slope makes over the distance the body has moved, so a body
        moving uphill is lifted with the ground rather than stopped by it.
        """
        count = indices.size
        floors = np.full(count, -np.inf, dtype=np.float32)
        support = np.full(count, -1, dtype=np.int64)
        slope = np.zeros((count, 2))
        grid = self.spatial_grid
        terrain = grid.terrain
        query, lo, hi, other = pairs.query, pairs.lo, pairs.hi, pairs.other
        if not len(query) and terrain is None:
            return floors, support, slope
        px, pz, bottom = self._support_offsets(indices)
        ray_y = np.maximum(bottom, previous_bottom.astype(np.float64)) + 1.0
        uneven = pairs.any_shaped or terrain is not None
        # The ground under each support point, where anything uneven is near;
        # for boxes alone the highest top is all there is to know.
        under_point = np.full(px.shape, -np.inf) if uneven else None
        best = np.full(count, -np.inf)

        boxes = hi[:, 1] <= ray_y[query]
        if pairs.any_shaped:
            boxes &= ~pairs.shaped
        if boxes.any():
            q, box_lo, box_hi, box_other = query[boxes], lo[boxes], hi[boxes], other[boxes]
            x = px[q]
            z = pz[q]
            inside = ((box_lo[:, 0:1] <= x) & (x <= box_hi[:, 0:1])
                      & (box_lo[:, 2:3] <= z) & (z <= box_hi[:, 2:3]))
            under = inside.any(axis=1)
            if under.any():
                q, top, box_other = q[under], box_hi[under, 1], box_other[under]
                np.maximum.at(best, q, top)
                if uneven:
                    tops = np.where(inside[under], top[:, None], -np.inf)
                    np.maximum.at(under_point.reshape(-1),
                                  (q[:, None] * px.shape[1] + np.arange(px.shape[1])).reshape(-1),
                                  tops.reshape(-1))
                highest = top == best[q]
                on_body = highest & (box_other >= 0)
                if on_body.any():
                    support[q[on_body]] = box_other[on_body]
                    # Level with the world's floor, it is the world it stands on.
                    support[q[highest & (box_other < 0)]] = -1

        if uneven:
            reach = ray_y if follow is None else ray_y + follow
            if pairs.any_shaped:
                shaped = pairs.shaped
                q = query[shaped]
                _base, top = grid.columns(pairs.rows, pairs.row[shaped], px[q], pz[q])
                top = np.where(top <= reach[q, None], top, -np.inf)     # NaN: a miss
                np.maximum.at(under_point.reshape(-1),
                              (q[:, None] * px.shape[1] + np.arange(px.shape[1])).reshape(-1),
                              top.reshape(-1))
            if terrain is not None:
                heights = grid.terrain_heights(px, pz)
                heights = np.where(heights <= reach[:, None], heights, -np.inf)
                np.maximum(under_point, heights, out=under_point)
            ground = under_point.max(axis=1)
            # Uneven ground higher than a body's top is the world's.
            support[ground > best] = -1
            best = np.maximum(best, ground)
            slope = self._slope_under(under_point, pairs.half)
        floors[:] = best
        return floors, support, slope

    @staticmethod
    def _slope_under(under_point, half):
        """The ground's gradient under each body, from the heights under its
        support points: rise along x, rise along z.

        Only where the ground is one surface: the two points either side of
        the centre and the centre itself in a line. A body overhanging a
        ledge has ground under one side and a drop under the other, which is
        an edge, not a slope, and gives it no push.
        """
        centre = under_point[:, 0]
        slope = np.zeros((len(under_point), 2))
        for column, (low, high, extent) in enumerate(((1, 2, 0), (3, 4, 2))):
            a = under_point[:, low]
            b = under_point[:, high]
            rise = b - a
            with np.errstate(invalid='ignore'):
                even = (np.isfinite(a) & np.isfinite(b) & np.isfinite(centre)
                        & (np.abs(a + b - 2.0 * centre)
                           <= np.maximum(1.0, 0.5 * np.abs(rise))))
            run = 1.5 * half[:, extent]
            slope[even, column] = rise[even] / np.maximum(run[even], 1e-6)
        return slope

    def _floor_tops(self, flat_x, flat_z, flat_y):
        """The highest brush top at or below each point (x, z, y), or -inf:
        the grid's batched floor query (:meth:`_can_group_floor_queries`)."""
        tops = self.spatial_grid.raycast_down_batch(flat_x, flat_z, flat_y)
        return np.where(np.isnan(tops), -np.inf, tops)

    def _floors_scalar(self, indices, previous_bottom):
        """One raycast_down per support point: for few bodies, or a grid that
        traces its own way."""
        floors = np.full(indices.size, -np.inf, dtype=np.float32)
        raycast = getattr(self.spatial_grid, 'raycast_down', None)
        if raycast is None or indices.size == 0:
            return floors

        centres = (self._position[indices] + self._offset[indices]).tolist()
        halves = self._half[indices].tolist()
        previous = previous_bottom.tolist()
        for k, (centre, half) in enumerate(zip(centres, halves)):
            cx, cy, cz = centre
            # From the body's previous bottom while it is falling: if
            # integration has already carried it slightly through the floor
            # this keeps the ray above the surface.
            ray_y = max(cy - half[1], previous[k]) + 1.0
            # A pushed barrel can straddle a floor seam while its centre is
            # briefly over the seam itself: the centre plus four in-footprint
            # support points.
            hx = half[0] * 0.75
            hz = half[2] * 0.75
            best_floor = None
            for ray_x, ray_z in ((cx, cz), (cx - hx, cz), (cx + hx, cz),
                                 (cx, cz - hz), (cx, cz + hz)):
                try:
                    floor = raycast(ray_x, ray_z, ray_y)
                except Exception:
                    floor = None
                if floor is not None and (best_floor is None or floor > best_floor):
                    best_floor = float(floor)
            if best_floor is not None:
                floors[k] = best_floor
        return floors

    # The same queries for every active body at once, as arrays the size of
    # the world: what tests and tools ask. The step asks for its awake rows.

    def _floor_rows(self, previous_bottom):
        indices = np.flatnonzero(
            self._physics_enabled & self._awake & ~self._kinematic & self._solid)
        if previous_bottom is None:
            previous = ((self._position[indices] + self._offset[indices])[:, 1]
                        - self._half[indices, 1])
        else:
            previous = np.asarray(previous_bottom, dtype=np.float32)[indices]
        return indices, previous

    def _spread(self, indices, floors):
        spread = np.full(len(self._entities), -np.inf, dtype=np.float32)
        spread[indices] = floors
        return spread

    def _batch_floor(self, previous_bottom=None):
        indices, previous = self._floor_rows(previous_bottom)
        return self._spread(indices, self._floors_under(indices, previous))

    def _batch_floor_grouped(self, previous_bottom=None):
        indices, previous = self._floor_rows(previous_bottom)
        return self._spread(indices, self._floors_batched(indices, previous))

    def _batch_floor_scalar(self, previous_bottom=None):
        indices, previous = self._floor_rows(previous_bottom)
        return self._spread(indices, self._floors_scalar(indices, previous))

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
        # Simulation tiers (engine.spatial): a body in a cell that is DISTANT
        # or beyond is past live simulation. It keeps its state -- awake,
        # moving -- and carries on from it when the cell is near again. An
        # unclassified world is all NEAR: nothing is ever held.
        distant = np.zeros(len(self._entities), dtype=np.bool_)
        inactive = self._inactive_mask(active, distant=distant)
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
        active &= ~distant

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

            # The bodies the player is touching, by the body index: nothing
            # here is the size of the world either. Dormant ones are not
            # touched; a sleeping one still is, and can be pushed awake.
            touched = self._bodies_at(ppos - player_half, ppos + player_half)
            if touched.size:
                touched = touched[~self._dormant(touched)]
            if touched.size and self._any_heavy:
                # The player cannot push a heavy body: it pushes the player.
                heavy = self._mass[touched] >= self.HEAVY_MASS
                if heavy.any():
                    self._push_player_out(player, ppos, player_half, touched[heavy])
                    touched = touched[~heavy]
            speed = float(np.hypot(player_velocity[0], player_velocity[2]))
            if speed >= 0.01 and touched.size:
                centre = self._position[touched] + self._offset[touched]
                half = self._half[touched]
                dx = centre[:, 0] - ppos[0]
                dz = centre[:, 2] - ppos[2]
                overlap_x = player_half[0] + half[:, 0] - np.abs(dx)
                overlap_z = player_half[2] + half[:, 2] - np.abs(dz)
                use_x = overlap_x <= overlap_z
                use_z = ~use_x
                x_rows = touched[use_x]
                z_rows = touched[use_z]
                x_dir = np.where(dx >= 0.0, 1.0, -1.0).astype(np.float32)[use_x]
                z_dir = np.where(dz >= 0.0, 1.0, -1.0).astype(np.float32)[use_z]

                pushed_from = np.full_like(self._position, np.nan)
                pushed_from[touched] = self._position[touched]
                self._position[x_rows, 0] += x_dir * (overlap_x[use_x] + 0.5)
                self._position[z_rows, 2] += z_dir * (overlap_z[use_z] + 0.5)

                target_x = np.abs(player_velocity[0]) / self._mass[x_rows] * 0.85
                target_z = np.abs(player_velocity[2]) / self._mass[z_rows] * 0.85
                self._velocity[x_rows, 0] = x_dir * np.maximum(
                    target_x, np.abs(self._velocity[x_rows, 0]))
                self._velocity[z_rows, 2] = z_dir * np.maximum(
                    target_z, np.abs(self._velocity[z_rows, 2]))
                asleep = touched[~self._awake[touched]]
                self._awake[touched] = True
                if asleep.size:
                    self._wake_dependents(asleep)
                pushed = touched

        # Everything below is the size of what is awake, not of the world:
        # the awake rows' state is gathered once, worked on, and put back.
        rows = np.flatnonzero(active & self._awake)
        if rows.size:
            velocity = self._velocity[rows]
            position = self._position[rows]
            weight = self._gravity[rows]
            if weight.any():
                velocity[:, 1] += weight * np.float32(self.gravity) * dt

            damping = self._damping[rows] * np.float32(max(0.0, float(self.damping_scale)))
            velocity *= np.maximum(0.0, 1.0 - damping * dt)[:, None]

            old = position.copy()
            previous_bottom = old[:, 1] + self._offset[rows, 1] - self._half[rows, 1]

            # Walls and floors are for solid bodies; all of them, usually.
            solid = self._solid[rows]
            everything = bool(solid.all())
            if everything:
                hard = None
                hard_rows, hard_pos, hard_vel, hard_old = rows, position, velocity, old
            else:
                hard = np.flatnonzero(solid)
                hard_rows = rows[hard]
                hard_pos, hard_vel, hard_old = position[hard], velocity[hard], old[hard]

            # The grid is asked once, about each body's whole move.
            ahead = hard_old.copy()
            ahead[:, 0] += hard_vel[:, 0] * dt
            ahead[:, 2] += hard_vel[:, 2] * dt
            pairs = self._world_pairs(hard_rows, hard_old, ahead, pushed_from)
            start = None
            if pushed_from is not None:
                # A pushed body's move began where the push found it.
                began = pushed_from[hard_rows]
                was_pushed = ~np.isnan(began[:, 0])
                if was_pushed.any():
                    start = hard_old.copy()
                    start[was_pushed] = began[was_pushed]

            hard_pos[:, 0] += hard_vel[:, 0] * dt
            self._collide_axis(0, hard_rows, hard_pos, hard_vel, hard_old, pairs, start)
            hard_pos[:, 2] += hard_vel[:, 2] * dt
            self._collide_axis(2, hard_rows, hard_pos, hard_vel, hard_old, pairs, start)
            if not everything:
                position[hard] = hard_pos
                velocity[hard] = hard_vel
                ghost = np.flatnonzero(~solid)
                position[ghost, 0] += velocity[ghost, 0] * dt
                position[ghost, 2] += velocity[ghost, 2] * dt

            position[:, 1] += velocity[:, 1] * dt
            rising = solid & (velocity[:, 1] > 0.0)
            if rising.any():
                # Nothing rises into a ceiling.
                up = np.flatnonzero(rising)
                up_pos, up_vel = position[up], velocity[up]
                among = up if everything else np.searchsorted(hard, up)
                self._collide_axis(1, rows[up], up_pos, up_vel, old[up],
                                   pairs.of(among, hard_rows.size))
                position[up] = up_pos
                velocity[up] = up_vel

            # The floor queries read the packed rows: put the moved ones back.
            self._position[rows] = position
            if not everything:
                previous_hard = previous_bottom[hard]
            else:
                previous_hard = previous_bottom
            # How far the ground may have risen or fallen away under a body
            # and still be the ground it is on: a walkable slope's rise over
            # the distance it has moved.
            follow = self.WALKABLE_SLOPE * (
                np.abs(position[:, 0] - old[:, 0])
                + np.abs(position[:, 2] - old[:, 2])).astype(np.float64)
            if self._can_group_floor_queries():
                under, held, lean = self._floors_and_support(
                    hard_rows, previous_hard, pairs,
                    follow if everything else follow[hard])
            else:
                under = self._floors_scalar(hard_rows, previous_hard)
                held = np.full(hard_rows.size, -1, dtype=np.int64)
                lean = np.zeros((hard_rows.size, 2))
            if everything:
                floors, support, slope = under, held, lean
            else:
                floors = np.full(rows.size, -np.inf, dtype=np.float32)
                floors[hard] = under
                support = np.full(rows.size, -1, dtype=np.int64)
                support[hard] = held
                slope = np.zeros((rows.size, 2))
                slope[hard] = lean
            new_bottom = position[:, 1] + self._offset[rows, 1] - self._half[rows, 1]
            # A body that was on the ground and is not on its way up stays on
            # ground that falls away no faster than it can be walked down:
            # going downhill is not a series of small falls.
            stick = np.where(self._grounded[rows] & (velocity[:, 1] <= 0.0), follow, 0.0)
            landed = np.isfinite(floors) & (new_bottom <= floors + 1.0 + stick)
            self._grounded[rows] = landed
            if landed.any():
                on = np.flatnonzero(landed)
                on_rows = rows[on]
                position[on, 1] = (
                    floors[on] - self._offset[on_rows, 1] + self._half[on_rows, 1])
                if self._any_bounce:
                    # Bounce back up if bouncy enough.
                    velocity[on, 1] = self._bounce_speed(
                        np.maximum(-velocity[on, 1], 0.0), self._bounce[on_rows])
                else:
                    velocity[on, 1] = 0.0

                # Treat friction as a surface coefficient rather than a tiny
                # per-frame damping term. Apply it only while grounded. A
                # rolling body loses only a little of its speed to the ground.
                friction_accel = (
                    self._friction[on_rows]
                    * np.float32(max(0.0, float(self.friction_scale)))
                    * np.float32(abs(float(self.gravity)))
                )
                if self._any_rolls:
                    friction_accel = friction_accel * np.where(
                        self._rolls[on_rows],
                        np.float32(self.ROLLING_FRICTION_SCALE),
                        np.float32(1.0))
                lean = slope[on]
                if lean.any():
                    # On a slope, gravity pulls along it and the ground
                    # presses less: downhill by g sin cos, friction by cos^2
                    # -- so a body slides where the slope is steeper than its
                    # friction (tan > mu), and a rolling one rolls almost
                    # anywhere.
                    flatness = 1.0 / (1.0 + lean[:, 0] ** 2 + lean[:, 1] ** 2)
                    pull = (abs(float(self.gravity)) * weight[on] * flatness * dt)
                    velocity[on, 0] -= (pull * lean[:, 0]).astype(np.float32)
                    velocity[on, 2] -= (pull * lean[:, 1]).astype(np.float32)
                    friction_accel = friction_accel * flatness.astype(np.float32)
                ground_speed = np.hypot(velocity[on, 0], velocity[on, 2])
                friction_delta = friction_accel * dt
                scale = np.maximum(
                    0.0,
                    1.0 - friction_delta / np.maximum(ground_speed, 1e-6),
                )
                velocity[on, 0] *= scale
                velocity[on, 2] *= scale
                self._position[on_rows, 1] = position[on, 1]

            # What each stands on, for whoever moves that next.
            support[~landed] = -1
            self._support[rows] = support
            on_body = support >= 0
            stacked = bool(on_body.any())
            if stacked:
                self._any_support = True

            rest = None
            if self.sleep_enabled:
                sleeping = np.max(np.abs(velocity), axis=1) < self.REST_SPEED
                if sleeping.any():
                    # At rest means on something. A body that gravity pulls
                    # is also momentarily still at the top of an arc -- a
                    # throw, a bounce -- and one put to sleep there hung in
                    # the air.
                    if self.gravity != 0.0:
                        sleeping &= landed | (weight == 0.0)
                    if stacked:
                        # And on something that is itself at rest: a body
                        # asleep on one still moving would be left behind.
                        sleeping &= ~(on_body & self._awake[np.maximum(support, 0)])
                    rest = rows[sleeping]
                    velocity[sleeping] = 0.0
            self._velocity[rows] = velocity

            if self._knocks:
                # The bodies that were run into: moving now, at the velocity
                # their share of the momentum gives them, and awake -- with
                # whatever rests on them.
                knocks, self._knocks = self._knocks, []
                for struck, axis, speed in knocks:
                    self._velocity[struck, axis] = speed
                    asleep = struck[~self._awake[struck]]
                    self._awake[struck] = True
                    if asleep.size:
                        self._wake_dependents(asleep)
                    if rest is not None and rest.size:
                        rest = rest[~np.isin(rest, struck)]

            # A body that crossed into another column is filed under the old
            # one: the index is rebuilt before it is next asked.
            if not self._bodies_dirty and self._body_key.size:
                if (self._cell_key(rows) != self._body_key[rows]).any():
                    self._bodies_dirty = True

            if self._any_spin:
                self._integrate_rotation(self._angular_velocity, dt)

            if self._any_rolls:
                rolling_rows = rows[self._rolls[rows]
                                    & ((velocity[:, 0] != 0.0) | (velocity[:, 2] != 0.0))]
                if rolling_rows.size:
                    self._roll(rolling_rows, dt)

            self._sync_entities(rows)

            if rest is not None and rest.size:
                self._awake[rest] = False
                # Last, with every row put back and every entity where its
                # body is: a callback may ask anything of this world.
                bodies = self.bodies
                entities = self._entities
                for i in rest.tolist():
                    body = bodies[id(entities[i])]
                    callback = body.rest_callback
                    if callback is not None:
                        callback(body.entity)

        if pushed is not None and player is not None:
            # A body the player pushed but a wall held: the player stops at it.
            self._bodies_block_player(player, ppos, player_half, pushed)



def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


#: The name this class had before it was Soma; existing code imports it.
PhysicsWorld = Soma
