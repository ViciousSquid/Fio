"""Soma (Greek: "body") simulates bodies.

That is the whole of what it does. The world is not Soma's: walls, floors,
slopes and the terrain are asked of the
:class:`engine.spatial_grid.SpatialGrid` it is created with, and it keeps no
copy. The game's rules are not Soma's either: what is heavy and what
crushing costs are the contract's. Soma holds the bodies -- their state as
NumPy rows -- and moves the awake ones.

Fio's physics engine: a subsystem of the engine, registered as ``"Soma"``
(:mod:`engine.physics.registry`) and reached by the rest of the engine only
through the contract (:class:`engine.physics.Physics`).
"""
import math

import numpy as np
import glm

from ..change_journal import JOURNAL, OVERFLOW, STATE, VISIBILITY, touch
from ..spatial import SIM_TIER_KEY, TIER_DISTANT, TIER_NEAR, cells_of_points
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
        #: The engine's own rest handler (a Prop's OnRest), and anyone else's.
        self.rest_callback = None
        self.rest_listeners = []
        #: Registered or re-registered since the rows were last packed: its
        #: descriptor is taken as it is, not carried over.
        self._fresh = True
        #: A body whose Collision Shape is Mesh: its model's triangles as the
        #: world's prepare_mesh made them ready (WORLD_QUERIES), and where the
        #: body was when they were taken. None for a box (AABB), which is every other body.
        self._mesh = None
        self._mesh_origin = (0.0, 0.0, 0.0)

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

    @mass.setter
    def mass(self, value):
        i = self._index
        if i is not None:
            world = self.world
            world._mass[i] = max(0.01, float(value))
            world._any_heavy = bool(np.any(
                world._physics_enabled & world._solid
                & (world._mass >= world.HEAVY_MASS)))

    @property
    def position(self):
        """The body's origin (its entity's ``pos``), as physics has it."""
        i = self._index
        if i is None:
            return [0.0, 0.0, 0.0]
        return self.world._position[i].tolist()

    @property
    def grounded(self):
        """Whether it was on the ground when it was last stepped."""
        i = self._index
        return bool(self.world._grounded[i]) if i is not None else False

    @property
    def resting_on(self):
        """The entity whose body this one stands on, or None (the world, or
        nothing)."""
        i = self._index
        if i is None:
            return None
        support = int(self.world._support[i])
        return self.world._entities[support] if support >= 0 else None

    @property
    def toppled(self):
        """Whether it is lying on its side (a body that ``topples``)."""
        i = self._index
        return bool(self.world._lying[i]) if i is not None else False

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
        return tuple(self.world._half[i].tolist())

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

    @gravity.setter
    def gravity(self, value):
        i = self._index
        if i is not None:
            self.world._gravity[i] = 1.0 if value else 0.0
            self.world._wake_index(i)

    @property
    def friction(self):
        i = self._index
        return float(self.world._friction[i]) if i is not None else 0.55

    @friction.setter
    def friction(self, value):
        i = self._index
        if i is not None:
            self.world._friction[i] = max(0.0, min(1.0, float(value)))

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
        self.rest_listeners = []


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
    #: A body that ``topples`` goes over when it is moving this fast along
    #: the ground (units per second). The player's push gives a body 0.85 of
    #: the player's speed: a walk (200) slides a barrel along upright, a run
    #: (300) knocks it over.
    TOPPLE_SPEED = 200.0
    #: ...or when it lands this hard while moving at all...
    TOPPLE_IMPACT = 400.0
    TOPPLE_NUDGE = 40.0
    #: ...or stands on ground this steep (rise over run): about where a
    #: barrel's weight is past its rim.
    TOPPLE_SLOPE = 0.7
    #: How long going over takes, in seconds: what is seen. The body's box is
    #: the lying one from the moment it starts.
    TOPPLE_TIME = 0.25
    #: The share of its ``friction`` a body lying on its side loses across
    #: its length, where it rolls. Along its length it slides, by all of it.
    LYING_ROLL_FRICTION = 0.12

    #: Steps between looks at the awake bodies for a ``hidden``/``disabled``
    #: or tier change the change journal never heard of
    #: (:meth:`_audit_flags`): about every two seconds. 0 never looks.
    AUDIT_FLAGS_EVERY = 120

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
    #: The fastest a body moves, in units a second: one 512-unit grid column
    #: a 60 Hz tick (three in the longest step, MAX_STEP). Quake's
    #: sv_maxvelocity is 2000, a run is 300 and a default blast (force 400)
    #: throws a body of mass 1 at 400, so play never comes near it; what
    #: does is a blast or a push on a body of the least mass (0.01), a
    #: hundred times faster. Past it the move is a blur either way, and the
    #: broad phase, which asks about every column a move crosses, would pay
    #: for each of them: two steps at ten million units a second took five
    #: seconds.
    MAX_SPEED = np.float32(512.0 * 60.0)

    #: The share of its ``friction`` a body that ``rolls`` loses to the ground:
    #: rolling loses far less speed than sliding.
    ROLLING_FRICTION_SCALE = 0.05
    #: The contract's (:data:`engine.physics.HEAVY_MASS`): a heavy body does
    #: not bounce, and the player cannot push it -- it pushes the player
    #: instead, and crushes a player it drives into a wall. A body of mass 1
    #: or less bounces by its full ``bounciness``; between the two the bounce
    #: fades linearly.
    HEAVY_MASS = api.HEAVY_MASS
    #: The contract's (:data:`engine.physics.PUSH_SCALE`).
    PUSH_SCALE = api.PUSH_SCALE
    #: Every one of the contract's game rules (:data:`engine.physics.GAME_RULES`).
    rules = frozenset(api.GAME_RULES)
    #: The physics contract Soma is written against.
    contract_version = api.PHYSICS_CONTRACT_VERSION
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
        to nothing at :data:`HEAVY_MASS`. Scalars or arrays: the contract's
        :func:`engine.physics.restitution`, vectorised.
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
        #: The entities as last packed, by row (see _pack).
        self._entities_before = []
        #: Whether anyone wants to be told what happens (drain_events).
        self.events_wanted = False
        self._events = []
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
        # Hidden or disabled, and the simulation tier: each entity's flags as
        # the change journal last reported them (see _refresh_flags). Soma
        # never reads them from an entity while it steps.
        self._inactive = np.empty(0, dtype=np.bool_)
        self._tier = np.empty(0, dtype=np.int8)
        self._audit_in = self.AUDIT_FLAGS_EVERY
        self._audit_reported = set()
        # Bodies initially dormant (or reactivated by a flag change) need the
        # one-time placement correction before their first active movement.
        self._pending_settle = np.empty(0, dtype=np.bool_)
        self._clear_rolling()
        self._clear_body_index()
        self._dirty = False
        #: Bodies removed since the last repack, by entity id: still listed in
        #: ``_entities`` (their rows are as last packed) until _pack drops them.
        self._removed = set()
        #: The columns each row's registration set (_AUTHORED), as it set
        #: them, by row as last packed: a repack takes a carried-over row's
        #: from here rather than reading its descriptor again.
        self._authored = {}
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
        self._indices = {}
        self._entities.clear()
        self._entities_before = []
        self._events = []
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
        self._inactive = np.empty(0, dtype=np.bool_)
        self._tier = np.empty(0, dtype=np.int8)
        self._audit_reported = set()
        self._pending_settle = np.empty(0, dtype=np.bool_)
        self._clear_rolling()
        self._clear_body_index()
        self._reported_cell = None
        self._dirty = False
        self._removed = set()
        self._authored = {}

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
        #: Falls over and lies on its side (``topples``): a barrel.
        self._topples = np.empty(0, dtype=np.bool_)
        #: 0 upright; 1 lying along x; 2 lying along z.
        self._lying = np.empty(0, dtype=np.int8)
        #: The upright box of a body that is lying, to stand it up again.
        self._stand_half = np.empty((0, 3), dtype=np.float32)
        self._stand_offset = np.empty((0, 3), dtype=np.float32)
        #: Going over: seconds left of it, the way it fell (+1/-1 along the
        #: axis it lies on) and how it was turned when it began.
        self._tip_left = np.empty(0, dtype=np.float32)
        self._tip_sign = np.empty(0, dtype=np.float32)
        self._tip_from = np.empty((0, 4), dtype=np.float64)
        self._any_topples = False
        # Set at pack time: a world with nothing rolling, bouncy or heavy --
        # most maps -- never enters that code.
        self._any_rolls = False
        #: Whether any body's Collision Shape is Mesh; while none is, nothing
        #: here looks at a mesh.
        self._any_mesh = False
        self._meshed = np.zeros(0, dtype=np.bool_)
        self._steep_bodies = None
        self._any_spin = False
        self._any_bounce = False
        self._any_heavy = False
        #: Set when a heavy body pins the player against a wall; the logic
        #: thread applies the damage and clears it.
        self.player_crushed = False
        #: Set by rebuild(): the first step wakes floating NEAR bodies.
        self._wake_floating_pending = False

    #: The columns the simulation writes: carried over a repack for every
    #: body that was not registered afresh.
    _CARRIED = ('_position', '_velocity', '_awake', '_kinematic', '_orientation',
                '_half', '_offset', '_lying', '_stand_half', '_stand_offset',
                '_tip_left', '_tip_sign', '_tip_from', '_grounded')
    #: The columns a body's registration sets (its ``_initial`` descriptor).
    #: A repack puts them back as registered -- as it always has -- but takes
    #: a carried-over row's from :attr:`_authored`, so only the bodies
    #: registered since are read one at a time: adding one body to thousands
    #: is not a pass of Python over all of them.
    _AUTHORED = ('_mass', '_friction', '_damping', '_gravity', '_angular_velocity',
                 '_solid', '_physics_enabled', '_rolls', '_meshed', '_bounce',
                 '_topples')

    @staticmethod
    def _shape_from_brush(entity, brush):
        # The body is its box, from the body brush alone (BODY_BRUSH): a
        # mesh's triangles' bounds, otherwise pos and size. Still a box: a
        # true-shape body is Stage 2e.
        bounds = brush.get('_mesh_bounds') if brush.get('_collision_mode') == 'mesh' else None
        if bounds:
            lo = np.asarray(bounds[0], dtype=np.float32)
            hi = np.asarray(bounds[1], dtype=np.float32)
            center, half = (lo + hi) * 0.5, (hi - lo) * 0.5
        else:
            center = np.asarray(brush.get('pos') or (0.0, 0.0, 0.0), dtype=np.float32)
            half = np.asarray(brush.get('size') or (64.0, 64.0, 64.0), dtype=np.float32) * 0.5
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
            if entity_id in self._removed:
                # Removed and back before the repack: still listed, and taken
                # afresh -- its old row went with the removal.
                self._removed.discard(entity_id)
            else:
                self._entities.append(entity)

        props = getattr(entity, 'properties', {})
        half, offset = self._shape_from_brush(entity, brush)
        body.rest_callback = rest_callback
        body._fresh = True
        body._mesh = None
        if brush.get('_collision_mode') == 'mesh':
            # Collision Shape: Mesh. Other bodies meet this one's actual
            # surface (see _pair_columns), not the box around it.
            body._mesh = self.spatial_grid.prepare_mesh(brush.get('_mesh_triangles'))
            origin = getattr(entity, 'pos', (0.0, 0.0, 0.0))
            body._mesh_origin = (float(origin[0]), float(origin[1]), float(origin[2]))

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
            bool(props.get('topples', False)) and not bool(props.get('rolls', False)),
        )
        self._dirty = True
        return body

    def _pack(self):
        if not self._dirty:
            return
        if self._removed:
            # The bodies removed since the last repack leave the list now, all
            # at once (see remove_body).
            removed = self._removed
            self._entities[:] = [e for e in self._entities if id(e) not in removed]
            removed.clear()
        entities = self._entities
        n = len(entities)
        if n == 0:
            self._dirty = False
            return

        bodies = self.bodies
        ids = [id(entity) for entity in entities]
        # Each entity's row as last packed, or -1: never packed, or removed
        # and registered again.
        old_index = self._indices
        was = np.fromiter((old_index.get(i, -1) for i in ids), dtype=np.int64, count=n)
        packed = was >= 0
        before = was[packed]

        # Hidden or disabled, and the tier, read afresh. A body may become
        # active immediately before a repack: its last observed state, taken
        # by identity, keeps that transition from being swallowed.
        props = [getattr(entity, 'properties', None) for entity in entities]
        inactive = np.fromiter(
            (isinstance(p, dict) and bool(p.get('disabled', False) or p.get('hidden', False))
             for p in props), dtype=np.bool_, count=n)
        tier = np.fromiter(
            ((p.get(SIM_TIER_KEY) or TIER_NEAR) if isinstance(p, dict) else 0
             for p in props), dtype=np.int8, count=n)
        previous_inactive = inactive.copy()
        previous_inactive[packed] = self._inactive[before]
        pending = np.zeros(n, dtype=np.bool_)
        pending[packed] = self._pending_settle[before]
        # Made active again across a repack: its placement is corrected
        # before it first moves, as for any other reactivation.
        pending |= previous_inactive & ~inactive

        # Bodies that were packed before and have not been registered again
        # keep everything about them that the simulation has changed: adding
        # or removing one body must not stop the others where they are.
        fresh = np.fromiter((bodies[i]._fresh for i in ids), dtype=np.bool_, count=n)
        kept = packed & ~fresh
        kept_new = np.flatnonzero(kept)
        kept_old = was[kept_new]
        carried = authored = None
        if kept_new.size:
            carried = {name: getattr(self, name)[kept_old] for name in self._CARRIED}
            authored = {name: self._authored[name][kept_old] for name in self._AUTHORED}
            # What each stood on, by entity: rows are renumbered below.
            stood_on = [self._entities_before[row] if row >= 0 else None
                        for row in self._support[kept_old].tolist()]

        self._indices = dict(zip(ids, range(n)))
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
        self._inactive = inactive
        self._tier = tier
        self._pending_settle = pending
        self._rolls = np.zeros(n, dtype=np.bool_)
        self._meshed = np.zeros(n, dtype=np.bool_)
        self._bounce = np.zeros(n, dtype=np.float32)
        self._orientation = np.zeros((n, 4), dtype=np.float64)
        self._orientation[:, 0] = 1.0
        self._topples = np.zeros(n, dtype=np.bool_)
        self._lying = np.zeros(n, dtype=np.int8)
        self._stand_half = np.zeros((n, 3), dtype=np.float32)
        self._stand_offset = np.zeros((n, 3), dtype=np.float32)
        self._tip_left = np.zeros(n, dtype=np.float32)
        self._tip_sign = np.ones(n, dtype=np.float32)
        self._tip_from = np.zeros((n, 4), dtype=np.float64)
        self._support = np.full(n, -1, dtype=np.int64)
        self._grounded = np.zeros(n, dtype=np.bool_)
        self._any_support = False
        self._bodies_dirty = True

        if authored is not None:
            for name, values in authored.items():
                getattr(self, name)[kept_new] = values
        # The rest -- new, or registered again -- from their descriptors.
        for i in np.flatnonzero(~kept).tolist():
            entity = entities[i]
            body = bodies[ids[i]]
            (
                position, half, offset, mass, friction, damping, gravity,
                angular_velocity, solid, physics_enabled, rolls, bounce, topples,
            ) = body._initial
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
            self._meshed[i] = body._mesh is not None
            self._bounce[i] = bounce
            if rolls:
                # A rolling body's turn comes from its rolling, not a fixed spin.
                self._angular_velocity[i] = 0.0
                self._orientation[i] = self._entity_orientation(entity)
            elif topples:
                self._topples[i] = True
                self._angular_velocity[i] = 0.0
                self._orientation[i] = self._entity_orientation(entity)
        # As registered: what the next repack carries over for these rows.
        self._authored = {name: getattr(self, name).copy() for name in self._AUTHORED}

        self._any_topples = bool(self._topples.any())
        self._any_mesh = bool(self._meshed.any())
        if self._any_topples:
            self._lie_as_placed()
        if carried is not None:
            for name, values in carried.items():
                getattr(self, name)[kept_new] = values
            for row, entity in zip(kept_new.tolist(), stood_on):
                support = self._indices.get(id(entity), -1) if entity is not None else -1
                self._support[row] = support
                if support >= 0:
                    self._any_support = True
        for body in bodies.values():
            body._fresh = False
        self._entities_before = list(entities)
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
        if value and self._any_topples and self._lying[index]:
            # Picked up: stood up again, in the hands.
            self._stand(index)
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
        """The engine's own rest handler for *entity* (a Prop's OnRest). One
        slot, and the engine's: anything else listens with :meth:`on_rest`."""
        body = self.get_body(entity)
        if body is not None:
            body.set_rest_callback(callback)

    def on_rest(self, entity, callback):
        """Also call ``callback(entity)`` each time *entity*'s body comes to
        rest. Any number may listen; none replaces another."""
        body = self.get_body(entity)
        if body is not None and callback not in body.rest_listeners:
            body.rest_listeners.append(callback)

    def off_rest(self, entity, callback):
        body = self.get_body(entity)
        if body is not None and callback in body.rest_listeners:
            body.rest_listeners.remove(callback)

    def add_body(self, brush):
        """Add one body while the session runs (a physics Prop spawned in
        play). The others carry on as they are. Returns its body."""
        entity = brush.get('_physics_entity')
        if entity is None:
            return None
        body = self.register_body(entity, brush)
        self._bodies_dirty = True
        return body

    def remove_body(self, entity):
        """Remove *entity*'s body (it was despawned). What stood on it falls.
        Returns whether it had one.

        Its row goes at the next repack, with every other body added or
        removed since: a level despawning a crowd (the collision runtime's
        ``sync_bodies`` removes one at a time) repacks once, not once a body.
        Until then the rows are as last packed, and everything that reads
        them repacks first."""
        body = self.bodies.pop(id(entity), None)
        if body is None:
            return False
        # Its row as last packed; none for a body registered since, which
        # nothing has stood on yet.
        index = self._indices.pop(id(entity), None)
        if index is not None:
            self._wake_dependents(index)
        body.clear()
        if not self.bodies:
            self.clear()
            return True
        self._removed.add(id(entity))
        self._dirty = True
        return True

    def impulse(self, entity, impulse):
        """Push *entity*'s body: its velocity changes by ``impulse / mass``,
        so the same push moves a heavy body less. It wakes, with whatever
        rests on it. Returns whether it had a body to push."""
        self._pack()
        index = self._indices.get(id(entity))
        if index is None or self._kinematic[index] or self._inactive[index]:
            return False
        push = np.asarray(impulse, dtype=np.float32).reshape(3)
        if not np.isfinite(push).all():
            return False
        self._velocity[index] += push / self._mass[index]
        self._wake_index(index)
        return True

    def impulse_radial(self, centre, radius, strength):
        """Push every body within *radius* of *centre* away from it: an
        explosion. *strength* is the impulse at the centre, falling to
        nothing at the radius. Returns the entities pushed."""
        self._pack()
        centre = np.asarray(centre, dtype=np.float32).reshape(3)
        radius = float(radius)
        if radius <= 0.0 or not self._entities:
            return []
        rows = self._rows_in_box(centre - radius, centre + radius)
        if not rows.size:
            return []
        away = self._position[rows] + self._offset[rows] - centre
        distance = np.sqrt(away[:, 0] * away[:, 0] + away[:, 1] * away[:, 1]
                           + away[:, 2] * away[:, 2])
        inside = distance < radius
        if not inside.all():
            rows, away, distance = rows[inside], away[inside], distance[inside]
            if not rows.size:
                return []
        # Straight up for one exactly at the centre.
        direction = np.where(distance[:, None] > 1e-6,
                             away / np.maximum(distance[:, None], 1e-6),
                             np.array([0.0, 1.0, 0.0], dtype=np.float32))
        push = float(strength) * (1.0 - distance / radius)
        self._velocity[rows] += (direction * (push / self._mass[rows])[:, None]).astype(np.float32)
        asleep = rows[~self._awake[rows]]
        self._awake[rows] = True
        if asleep.size:
            self._wake_dependents(asleep)
        return [self._entities[i] for i in rows.tolist()]

    def topple(self, entity, direction=(1.0, 0.0, 0.0)):
        """Knock *entity*'s body over toward *direction* (its larger
        horizontal part decides), if it is one that ``topples``, is upright
        and has room to lie. Returns whether it went over."""
        self._pack()
        index = self._indices.get(id(entity))
        if index is None or not self._topples[index] or self._lying[index]:
            return False
        along_x = abs(float(direction[0])) >= abs(float(direction[2]))
        way = float(direction[0] if along_x else direction[2])
        self._topple(np.array([index]), np.array([0 if along_x else 2]),
                     np.array([1.0 if way >= 0.0 else -1.0]))
        if not self._lying[index]:
            return False
        self._wake_index(index)
        return True

    def stand(self, entity):
        """Stand *entity*'s body up again if it is lying. Returns whether it
        was."""
        self._pack()
        index = self._indices.get(id(entity))
        if index is None or not self._lying[index]:
            return False
        self._stand(index)
        self._bodies_dirty = True
        self._wake_index(index)
        return True

    # -- queries ---------------------------------------------------------------

    def _rows_in_box(self, lo, hi):
        """The simulated, solid, undormant body rows whose box overlaps the
        box ``lo..hi``, in row order."""
        rows = self._bodies_at(np.asarray(lo, dtype=np.float32),
                               np.asarray(hi, dtype=np.float32))
        return rows[~self._inactive[rows]] if rows.size else rows

    def bodies_in_box(self, lo, hi):
        """The entities whose (solid, simulated) body overlaps the box
        ``lo..hi``. Found through the body index: the cost is what is near,
        not how many bodies there are."""
        self._pack()
        if not self._entities:
            return []
        return [self._entities[i] for i in self._rows_in_box(lo, hi).tolist()]

    def bodies_near(self, point, radius):
        """The entities whose body's centre is within *radius* of *point*."""
        self._pack()
        if not self._entities:
            return []
        point = np.asarray(point, dtype=np.float32).reshape(3)
        rows = self._rows_in_box(point - float(radius), point + float(radius))
        if not rows.size:
            return []
        away = self._position[rows] + self._offset[rows] - point
        near = (away[:, 0] * away[:, 0] + away[:, 1] * away[:, 1]
                + away[:, 2] * away[:, 2]) <= float(radius) ** 2
        entities = self._entities
        return [entities[i] for i in rows[near].tolist()]

    def raycast(self, origin, direction, reach):
        """The first body a ray meets: ``(entity, distance)`` or None.

        Against the boxes of the bodies filed along the ray -- found through
        the body index, then tested all at once (no loop over bodies). The
        world's brushes are the spatial grid's to ask about, not physics'.
        """
        self._pack()
        if not self._entities:
            return None
        try:
            origin = [float(v) for v in origin]
            ray = [float(v) for v in direction]
            reach = float(reach)
        except (TypeError, ValueError):
            return None
        if len(origin) != 3 or len(ray) != 3:
            return None
        length = math.sqrt(ray[0] * ray[0] + ray[1] * ray[1] + ray[2] * ray[2])
        if not (0.0 < length < math.inf) or not all(math.isfinite(v) for v in origin):
            return None
        if not reach >= 0.0:
            return None
        lo = np.empty(3, dtype=np.float32)
        hi = np.empty(3, dtype=np.float32)
        for axis in range(3):
            step = ray[axis] / length
            end = origin[axis] + step * reach if step else origin[axis]
            lo[axis] = min(origin[axis], end) - 1.0
            hi[axis] = max(origin[axis], end) + 1.0
        rows = self._rows_in_box(lo, hi)
        if not rows.size:
            return None
        centre = (self._position[rows] + self._offset[rows]).astype(np.float64)
        half = self._half[rows].astype(np.float64)
        # Slab by slab. A ray along an axis is inside or outside that slab
        # for good, and never divides by it.
        enter = leave = hit = None
        for axis in range(3):
            step = ray[axis] / length
            low = centre[:, axis] - half[:, axis] - origin[axis]
            high = centre[:, axis] + half[:, axis] - origin[axis]
            if step == 0.0:
                inside = (low <= 0.0) & (high >= 0.0)
                hit = inside if hit is None else hit & inside
                continue
            if step > 0.0:
                near, far = low / step, high / step
            else:
                near, far = high / step, low / step
            enter = near if enter is None else np.maximum(enter, near)
            leave = far if leave is None else np.minimum(leave, far)
        met = (enter <= leave) & (leave >= 0.0) & (enter <= reach)
        if hit is not None:
            met &= hit
        which = np.flatnonzero(met)
        if not which.size:
            return None
        distance = np.maximum(enter[which], 0.0)
        first = int(distance.argmin())
        return self._entities[int(rows[which[first]])], float(distance[first])

    # -- events ----------------------------------------------------------------

    def drain_events(self):
        """What happened since the last call, for whoever set
        :attr:`events_wanted`: ``(kind, entity, data)`` tuples --

        * ``'rest'``: a body came to rest;
        * ``'landed'``: it came down onto the ground (``speed``);
        * ``'contact'``: it ran into something (``other``: the entity it
          met, or None for the world; ``axis``; ``speed``);
        * ``'toppled'``: it went over (``axis``, ``sign``).

        Nothing is recorded, at any cost, while nobody wants it.
        """
        events, self._events = self._events, []
        return events

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

    def _refresh_flags(self):
        """Take the hidden/disabled flags and simulation tiers the change
        journal says have changed.

        The journal is the only way Soma hears of them. Whatever changes an
        entity's ``hidden``, ``disabled`` or simulation tier records it
        (:func:`engine.spatial.set_live_flag`, ``set_authored_flag``, a
        streaming layer's ``touch``), and only the entities it names are
        looked at here -- none, on almost every step. A write that goes
        straight to the property dict is not heard: that is a bug in the
        writer, and :meth:`_audit_flags` says so. An overflowing journal is
        rare and falls back to one complete refresh.
        """
        changes = JOURNAL.drain(self)
        if not changes:
            return
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
            if not isinstance(props, dict):
                continue
            inactive = bool(props.get('disabled') or props.get('hidden'))
            if self._inactive[index] and not inactive:
                self._pending_settle[index] = True
            elif inactive and not self._inactive[index]:
                # Hidden or disabled: gone from under whatever rested on it.
                self._wake_dependents(index)
            self._inactive[index] = inactive
            self._tier[index] = props.get(SIM_TIER_KEY) or TIER_NEAR

    def _audit_flags(self, rows):
        """Report awake bodies whose flags were changed behind the journal's
        back: every :data:`AUDIT_FLAGS_EVERY` steps, a look at what is awake.

        It reports; it does not repair. The body goes on being simulated as
        the journal last described it, so the mistake is seen where it was
        made rather than papered over at a cost every body pays every step.
        """
        entities = self._entities
        for i in rows.tolist():
            entity = entities[i]
            props = getattr(entity, 'properties', None)
            if not isinstance(props, dict) or id(entity) in self._audit_reported:
                continue
            inactive = bool(props.get('disabled') or props.get('hidden'))
            tier = props.get(SIM_TIER_KEY) or TIER_NEAR
            if inactive != bool(self._inactive[i]) or tier != int(self._tier[i]):
                self._audit_reported.add(id(entity))
                print("[Soma] %r: 'hidden', 'disabled' or its simulation tier was "
                      "changed without the change journal, so physics has not "
                      "heard of it. Write them with engine.spatial.set_live_flag "
                      "(plugins: api.set_flag), or call "
                      "engine.change_journal.touch(entity) after writing."
                      % (getattr(entity, 'name', None) or type(entity).__name__))

    def _settle_pending_bodies(self, eligible):
        """Correct newly active placements before they can first move.

        The mask is normally empty. It tracks entities whose hidden/disabled
        state has changed from inactive to active. Dormant rows remain outside
        the pending set until a flag transition makes correction necessary.
        """
        candidates = self._pending_settle & eligible
        if not np.any(candidates):
            return
        ready = candidates & ~self._inactive
        if not np.any(ready):
            return
        self._settle_sunk_bodies(ready)
        self._pending_settle[ready] = False

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
            if not isinstance(props, dict) or self._inactive[index]:
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
        rows = rows[(self._tier[rows] == TIER_NEAR) & ~self._inactive[rows]]
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
        rows = np.flatnonzero(candidates & ~self._inactive)
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
    # Falling over
    #
    # A body that ``topples`` -- a barrel -- is a box standing up until
    # something knocks it over, and the same box on its side after: it rolls
    # across its length and slides along it. Nothing about how it tips is
    # simulated. Going over is a decision (it is moving too fast, it landed
    # too hard, the ground is too steep), a swap of the box's two dimensions,
    # and a quarter turn played over a few frames; picking it up stands it
    # up again. A body upright or lying costs what any other body costs.
    # ------------------------------------------------------------------

    def _lay(self, rows, axis, sign, lift=True):
        """Put the body rows *rows* on their side along *axis* (0: x, 2: z),
        fallen toward ``sign`` of it: the box that was upright, turned a
        quarter about its base, and lifted so it lies on what it stood on."""
        half = self._half[rows].copy()
        offset = self._offset[rows].copy()
        self._stand_half[rows] = half
        self._stand_offset[rows] = offset
        bottom = offset[:, 1] - half[:, 1]
        lying_half = half.copy()
        lying_half[:, axis] = half[:, 1]
        lying_half[:, 1] = half[:, axis]
        lying_offset = offset.copy()
        lying_offset[:, axis] = sign * offset[:, 1]
        lying_offset[:, 1] = -sign * offset[:, axis]
        self._half[rows] = lying_half
        self._offset[rows] = lying_offset
        if lift:
            self._position[rows, 1] += bottom - (lying_offset[:, 1] - lying_half[:, 1])
        self._lying[rows] = 1 if axis == 0 else 2
        self._tip_sign[rows] = sign

    def _lie_as_placed(self):
        """Bodies the map (or a saved game) has lying on their side already:
        told by how the entity is turned, so a barrel knocked over before a
        save is still one after it."""
        rows = np.flatnonzero(self._topples)
        q = self._orientation[rows]
        # Where the body's own up points, in the world.
        up_x = 2.0 * (q[:, 1] * q[:, 2] - q[:, 0] * q[:, 3])
        up_y = 1.0 - 2.0 * (q[:, 1] ** 2 + q[:, 3] ** 2)
        up_z = 2.0 * (q[:, 2] * q[:, 3] + q[:, 0] * q[:, 1])
        down = np.abs(up_y) < 0.5
        for axis, along, across in ((0, up_x, up_z), (2, up_z, up_x)):
            lying = down & (np.abs(along) >= np.abs(across) if axis == 0
                            else np.abs(along) > np.abs(across))
            if not lying.any():
                continue
            chosen = rows[lying]
            sign = np.where(along[lying] >= 0.0, 1.0, -1.0).astype(np.float32)
            # A box derived from the turned model is lying already: only
            # what it is upright has to be worked out. One derived from the
            # model as authored is laid down here.
            shaped = self._half[chosen, axis] > self._half[chosen, 1]
            if (~shaped).any():
                self._lay(chosen[~shaped], axis, sign[~shaped], lift=False)
            if shaped.any():
                done = chosen[shaped]
                half = self._half[done].copy()
                offset = self._offset[done].copy()
                stand_half = half.copy()
                stand_half[:, 1] = half[:, axis]
                stand_half[:, axis] = half[:, 1]
                stand_offset = offset.copy()
                stand_offset[:, 1] = sign[shaped] * offset[:, axis]
                stand_offset[:, axis] = -sign[shaped] * offset[:, 1]
                self._stand_half[done] = stand_half
                self._stand_offset[done] = stand_offset
                self._lying[done] = 1 if axis == 0 else 2
                self._tip_sign[done] = sign[shaped]

    def _topple(self, rows, axis, sign):
        """Knock the upright body rows *rows* over, each along ``axis[k]``
        (0 or 2) toward ``sign[k]`` of it -- those with room to lie there."""
        for along in (0, 2):
            chosen = axis == along
            if not chosen.any():
                continue
            group = rows[chosen]
            self._lay(group, along, sign[chosen].astype(np.float32))
            # Room to lie: asked of the grid, of the few going over. The
            # floor it lies on is not in its way.
            centre = (self._position[group] + self._offset[group]).astype(np.float64)
            half = self._half[group].astype(np.float64) - self.CONTACT_SKIN
            lo = centre - half
            hi = centre + half
            lo[:, 1] += 1.0
            query, row, boxes = self.spatial_grid.box_pairs(lo, hi)
            if len(query):
                # Against boxes: an angled brush's box is not where it is,
                # and what it stands on is often one.
                blocked = np.zeros(group.size, dtype=np.bool_)
                blocked[query[np.all((hi[query] > boxes.lo[row])
                                     & (lo[query] < boxes.hi[row]), axis=1)
                              & ~boxes.shaped[row]]] = True
                if blocked.any():
                    self._stand_rows(group[blocked])
                    group = group[~blocked]
            if not group.size:
                continue
            self._tip_left[group] = self.TOPPLE_TIME
            self._tip_from[group] = self._orientation[group]
            self._bodies_dirty = True
            if self.events_wanted:
                for i, way in zip(group.tolist(), self._tip_sign[group].tolist()):
                    self._events.append(('toppled', self._entities[i],
                                         {'axis': along, 'sign': way}))
            # What stood on it has nothing under it now.
            self._wake_dependents(group)

    def _stand_rows(self, rows):
        """The upright box back, for body rows that are lying."""
        bottom = self._offset[rows, 1] - self._half[rows, 1]
        self._half[rows] = self._stand_half[rows]
        self._offset[rows] = self._stand_offset[rows]
        self._position[rows, 1] += bottom - (self._offset[rows, 1] - self._half[rows, 1])
        self._lying[rows] = 0
        self._tip_left[rows] = 0.0

    def _stand(self, index):
        """Stand one lying body up: its box, and how it is turned."""
        self._stand_rows(np.array([index]))
        self._orientation[index] = (1.0, 0.0, 0.0, 0.0)
        entity = self._entities[index]
        props = getattr(entity, 'properties', None)
        if isinstance(props, dict):
            props['rotation'] = [0.0, 0.0, 0.0]
            touch(entity)

    def _turn_lying(self, rows, velocity, dt):
        """Turn the body rows *rows* that are going over or lying: the
        quarter turn of the fall, then the roll across their length.
        *velocity* is their rows of this step's working velocity."""
        lying = self._lying[rows]
        sign = self._tip_sign[rows].astype(np.float64)
        left = self._tip_left[rows]
        tipping = left > 0.0
        orientation = self._orientation[rows]
        changed = tipping.copy()
        if tipping.any():
            left = np.maximum(left - dt, 0.0)
            self._tip_left[rows] = left
            fallen = (0.5 * np.pi) * (1.0 - left / self.TOPPLE_TIME)
            # Up goes toward the way it fell: about z for a fall along x,
            # about x for one along z.
            for along, about, way in ((1, 2, -1.0), (2, 0, 1.0)):
                group = np.flatnonzero(tipping & (lying == along))
                if group.size:
                    turn = _axis_quat(about, way * sign[group] * fallen[group])
                    orientation[group] = _quat_multiply(turn, self._tip_from[rows[group]])
        rolling = ~tipping
        if rolling.any():
            # Across its length only: along it, it slides.
            across = velocity[rolling].copy()
            across[:, 1] = 0.0
            across[lying[rolling] == 1, 0] = 0.0
            across[lying[rolling] == 2, 2] = 0.0
            moving = (across[:, 0] != 0.0) | (across[:, 2] != 0.0)
            if moving.any():
                which = np.flatnonzero(rolling)[moving]
                orientation[which] = _rolled(
                    orientation[which], across[moving],
                    self._half[rows[which], 1], dt)
                changed[which] = True
        if not changed.any():
            return
        self._orientation[rows[changed]] = orientation[changed]
        turned = rotation_from_quat(orientation[changed]).tolist()
        for i, rotation in zip(rows[changed].tolist(), turned):
            entity = self._entities[i]
            props = getattr(entity, 'properties', None)
            if isinstance(props, dict):
                props['rotation'] = rotation
                touch(entity)

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

    #: The most grid cells a query box of :meth:`_bodies_near` is walked
    #: cell by cell: eight columns square, where a body's move at MAX_SPEED
    #: in the longest step spans five. A box past it -- a vast body's
    #: reach, say -- has every obstacle's column tested against it instead,
    #: which costs as the bodies do rather than as the square of its width.
    _WALK_CELLS = 64

    def _bodies_near(self, lo, hi):
        """``(query, other)``: for each box ``lo[i]..hi[i]`` (float64), every
        obstacle row filed in a cell the box, grown by the reach, touches.
        The broad phase only; boxes are read live by the caller."""
        keys = self._body_keys
        size = float(self.spatial_grid.cell_size)
        reach = self._body_reach
        x0 = np.floor((lo[:, 0] - reach) / size)
        z0 = np.floor((lo[:, 2] - reach) / size)
        more_x = np.floor((hi[:, 0] + reach) / size) - x0
        more_z = np.floor((hi[:, 2] + reach) / size) - z0
        queries, others = [], []
        side = np.maximum(more_x, more_z) + 1.0
        wide = side * side > self._WALK_CELLS
        if wide.any():
            wide = np.flatnonzero(wide)
            self._bodies_spanned(wide, x0, z0, x0 + more_x, z0 + more_z,
                                 queries, others)
            # Their own cell is walked with the rest's; no other.
            more_x = more_x.copy()
            more_z = more_z.copy()
            more_x[wide] = 0.0
            more_z[wide] = 0.0
        limit = float(self._KEY_BIAS - 1)
        cx0 = np.clip(x0, -limit, limit).astype(np.int64)
        cz0 = np.clip(z0, -limit, limit).astype(np.int64)
        more_x = more_x.astype(np.int64)
        more_z = more_z.astype(np.int64)
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

    def _bodies_spanned(self, wide, x0, z0, x1, z1, queries, others):
        """For :meth:`_bodies_near`: each query of *wide* with every obstacle
        filed in a cell of ``x0..x1, z0..z1`` but its first (``x0, z0``, which
        the walk finds) -- each obstacle's column tested against the range,
        a bounded slice of queries at a time. Appended to *queries*/*others*."""
        keys = self._body_keys
        if not keys.size:
            return
        # The column each obstacle is filed under: _cell_key, undone.
        cx = keys // self._KEY_STRIDE
        cz = keys - cx * self._KEY_STRIDE - self._KEY_BIAS
        chunk = max(1, (1 << 20) // keys.size)
        for start in range(0, wide.size, chunk):
            which = wide[start:start + chunk]
            ax, az = x0[which, None], z0[which, None]
            inside = ((cx >= ax) & (cx <= x1[which, None])
                      & (cz >= az) & (cz <= z1[which, None])
                      & ~((cx == ax) & (cz == az)))
            query, at = np.nonzero(inside)
            if query.size:
                queries.append(which[query])
                others.append(self._body_rows[at])

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
        # A dormant body is in nobody's way.
        dormant = self._inactive[other]
        if dormant.any():
            keep = ~dormant
            if not keep.any():
                return None
            query, other = query[keep], other[keep]
            other_lo, other_hi = other_lo[keep], other_hi[keep]
        return query, other_lo, other_hi, other

    #: Past this many grid columns across, a query box is the whole world
    #: and every body is simply read.
    _WIDE_QUERY_COLUMNS = 64

    def _bodies_at(self, lo, hi):
        """The obstacle rows whose box overlaps the box ``lo..hi`` (float32
        triples), in row order: what the player is touching, and what a
        plugin's query finds.

        One box, so no pair lists: the index is sorted by column and then by
        row along it, so the cells the box touches are one run of the index
        for each column across -- two binary searches for all of them.
        """
        self._index_bodies()
        rows = self._body_rows
        if not rows.size:
            return rows
        size = float(self.spatial_grid.cell_size)
        reach = self._body_reach
        limit = float(self._KEY_BIAS - 1)
        x0, x1 = float(lo[0]) - reach, float(hi[0]) + reach
        z0, z1 = float(lo[2]) - reach, float(hi[2]) + reach
        if not (x0 <= x1 and z0 <= z1):     # empty, or not a number
            return rows[:0]
        cx0 = math.floor(min(max(x0 / size, -limit), limit))
        cx1 = math.floor(min(max(x1 / size, -limit), limit))
        cz0 = math.floor(min(max(z0 / size, -limit), limit)) + self._KEY_BIAS
        cz1 = math.floor(min(max(z1 / size, -limit), limit)) + self._KEY_BIAS
        keys = self._body_keys
        if cx1 == cx0:
            other = rows[keys.searchsorted(cx0 * self._KEY_STRIDE + cz0, 'left'):
                         keys.searchsorted(cx0 * self._KEY_STRIDE + cz1, 'right')]
        elif cx1 - cx0 >= self._WIDE_QUERY_COLUMNS:
            other = rows
        else:
            column = np.arange(cx0, cx1 + 1, dtype=np.int64) * self._KEY_STRIDE
            first = keys.searchsorted(column + cz0, 'left').tolist()
            last = keys.searchsorted(column + cz1, 'right').tolist()
            runs = [rows[f:l] for f, l in zip(first, last) if l > f]
            if not runs:
                return rows[:0]
            other = runs[0] if len(runs) == 1 else np.concatenate(runs)
        if not other.size:
            return other
        centre = self._position[other] + self._offset[other]
        half = self._half[other]
        over = (centre + half > lo) & (centre - half < hi)
        other = other[over[:, 0] & over[:, 1] & over[:, 2]]
        other.sort()
        return other

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
                if self._any_mesh:
                    # A body whose Collision Shape is Mesh is met as its
                    # model's surface -- while it stands as it was made: one
                    # that has been knocked over, or rolls, is its box.
                    them = bodies[3]
                    meshed = self._meshed[them] & (self._lying[them] == 0)
                    if self._any_rolls:
                        meshed &= ~self._rolls[them]
                else:
                    meshed = np.zeros(count, dtype=np.bool_)
                shaped = np.concatenate((shaped, meshed))
        return _Pairs(query, pair_lo, pair_hi, offset, half, other, row, shaped, boxes)

    def _pair_columns(self, pairs, mask, x, z):
        """``(bottom, top)``: the solid column of each shaped pair in *mask*
        over its points ``(x[k, j], z[k, j])`` -- the world's brushes and
        Mesh props from the grid (:meth:`SpatialGrid.columns`), another
        body's from its own mesh, where that body is now."""
        row = pairs.row[mask]
        if not self._any_mesh:
            return self.spatial_grid.columns(pairs.rows, row, x, z)
        world = row >= 0
        if world.all():
            return self.spatial_grid.columns(pairs.rows, row, x, z)
        x = np.asarray(x, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        bottom = np.full(x.shape, np.nan)
        top = np.full(x.shape, np.nan)
        if world.any():
            b, t = self.spatial_grid.columns(pairs.rows, row[world], x[world], z[world])
            bottom[world], top[world] = b, t
        others = pairs.other[mask]
        entities = self._entities
        for them in np.unique(others[~world]).tolist():
            body = self.bodies.get(id(entities[them]))
            if body is None or body._mesh is None:
                continue
            at = np.flatnonzero(~world & (others == them))
            now = self._position[them]
            origin = body._mesh_origin
            shift = (float(now[0]) - origin[0], float(now[1]) - origin[1],
                     float(now[2]) - origin[2])
            b, t = self.spatial_grid.mesh_columns(body._mesh, x[at], z[at], shift)
            bottom[at], top[at] = b, t
        return bottom, top

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
        if self.events_wanted:
            self._report_contacts(axis, hit_indices, velocity[hit, axis],
                                  indices, query, other, ahead, behind, lo, hi,
                                  stop, back_stop)
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
        steep_bodies = self._steep_bodies if steep is not None else None
        if steep_bodies is not None:
            # Read before the wall's answer below changes its speed.
            steep_incoming = velocity[steep_bodies[0], axis].copy()
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
        if steep_bodies is not None:
            # Stopped by a Mesh body's surface rather than a box's face: the
            # same sharing of momentum, with the body whose surface it was.
            mine, others = steep_bodies
            moving = steep_incoming != 0.0
            if moving.any():
                mine, others, incoming = mine[moving], others[moving], steep_incoming[moving]
                answered = velocity[mine, axis].copy()
                velocity[mine, axis] = incoming
                knocked, closing = self._knock(axis, incoming > 0.0, mine, others,
                                               velocity, indices)
                walled = ~knocked & (closing > 0.0)
                velocity[mine[walled], axis] = answered[walled]

    def _report_contacts(self, axis, hit_rows, speeds, indices, query, other,
                         ahead, behind, lo, hi, stop, back_stop):
        """Record a ``contact`` for each body row that ran into something on
        *axis* -- only ever called while somebody wants events."""
        met = {}
        soft = other >= 0
        if soft.any():
            front = ahead & soft & (lo == stop[query])
            back = behind & soft & (hi == back_stop[query])
            for k in np.flatnonzero(front | back).tolist():
                met[int(indices[query[k]])] = int(other[k])
        entities = self._entities
        for row, speed in zip(hit_rows.tolist(), speeds.tolist()):
            if speed == 0.0:
                continue                    # stood against it, not run into it
            against = met.get(row)
            self._events.append(('contact', entities[row], {
                'other': entities[against] if against is not None else None,
                'axis': axis, 'speed': abs(speed)}))

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
        self._steep_bodies = None
        if shaped is not None:
            query = pairs.query[shaped]
            bottom, top = self._pair_columns(pairs, shaped, x[query], z[query])
            in_way = ((top > limit[query, None])
                      & (bottom < bmax[query, 1:2] - self.CONTACT_SKIN)).any(axis=1)
            blocked[query[in_way]] = True
            if self._any_mesh and in_way.any():
                # Stopped by another body's surface: that body is knocked
                # on, as one met at its box's face is (_sweep_axis).
                others = pairs.other[shaped][in_way]
                struck = others >= 0
                if struck.any():
                    self._steep_bodies = (query[in_way][struck], others[struck])
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
        body_top = body_under = None
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
                _base, top = self._pair_columns(pairs, shaped, px[q], pz[q])
                top = np.where(top <= reach[q, None], top, -np.inf)     # NaN: a miss
                if self._any_mesh:
                    # The highest of it that is another body's surface: what
                    # stands there stands on that body (below).
                    on = other[shaped] >= 0
                    if on.any():
                        body_top = np.full(count, -np.inf)
                        body_under = np.full(count, -1, dtype=np.int64)
                        tops = np.nan_to_num(top[on], nan=-np.inf).max(axis=1)
                        for k, row_top, them in zip(q[on].tolist(), tops.tolist(),
                                                    other[shaped][on].tolist()):
                            if row_top > body_top[k]:
                                body_top[k], body_under[k] = row_top, them
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
            if body_under is not None:
                # Unless that ground is a Mesh body's own surface.
                on_body = ((ground > best) & (body_under >= 0)
                           & (body_top >= ground - 1e-6) & np.isfinite(ground))
                support[on_body] = body_under[on_body]
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

    def _limit_speed(self, velocity):
        """No row of *velocity* faster than :data:`MAX_SPEED`: one that is
        keeps its direction at that speed, and one that is not a number (or
        is infinite) stops. One comparison for a step with neither, which is
        every step."""
        limit = float(self.MAX_SPEED)
        squared = np.einsum('ij,ij->i', velocity, velocity)
        over = ~(squared <= np.float32(limit * limit))     # NaN is over, too
        if not over.any():
            return
        over = np.flatnonzero(over)
        moving = velocity[over].astype(np.float64)
        speed = np.sqrt(np.einsum('ij,ij->i', moving, moving))
        finite = np.isfinite(speed)
        scale = np.where(finite, limit / np.maximum(speed, limit), 0.0)
        moving[~finite] = 0.0
        velocity[over] = (moving * scale[:, None]).astype(np.float32)

    def _stop_lost(self, rows):
        """*rows* without the bodies whose position is not a number -- an
        entity moved to NaN, say, and synchronised. Those are not stepped:
        they sleep, stopped, where they are, until something puts them
        somewhere (one would take the whole step's broad phase down)."""
        lost = ~np.isfinite(self._position[rows]).all(axis=1)
        gone = rows[lost]
        self._velocity[gone] = 0.0
        self._awake[gone] = False
        return rows[~lost]

    def step(self, delta, player=None):
        self._pack()
        if not self._entities:
            return

        # Hidden, disabled and the simulation tier come from the change
        # journal and nowhere else: usually nothing to do. A body in a cell
        # that is DISTANT or beyond is past live simulation -- it keeps its
        # state, awake and moving, and carries on when the cell is near
        # again; an unclassified world is all NEAR.
        self._refresh_flags()

        if self._wake_floating_pending:
            self._wake_floating_pending = False
            self._wake_floating_near_bodies()

        active = self._physics_enabled & ~self._kinematic
        self._settle_pending_bodies(active)
        active &= ~self._inactive
        active &= self._tier < TIER_DISTANT

        base_dt = float(delta) or 1.0 / 60.0
        scale = max(0.0, float(self.time_scale))
        dt = np.float32(min(0.05, max(0.0, base_dt * scale)))

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
                touched = touched[~self._inactive[touched]]
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

                target_x = np.abs(player_velocity[0]) / self._mass[x_rows] * self.PUSH_SCALE
                target_z = np.abs(player_velocity[2]) / self._mass[z_rows] * self.PUSH_SCALE
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
        if rows.size and not np.isfinite(self._position[rows]).all():
            rows = self._stop_lost(rows)
        if rows.size:
            if self.AUDIT_FLAGS_EVERY:
                self._audit_in -= 1
                if self._audit_in <= 0:
                    self._audit_in = self.AUDIT_FLAGS_EVERY
                    self._audit_flags(rows)
            velocity = self._velocity[rows]
            position = self._position[rows]
            weight = self._gravity[rows]
            if weight.any():
                velocity[:, 1] += weight * np.float32(self.gravity) * dt

            damping = self._damping[rows] * np.float32(max(0.0, float(self.damping_scale)))
            velocity *= np.maximum(0.0, 1.0 - damping * dt)[:, None]
            self._limit_speed(velocity)

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
            if self.events_wanted:
                touched_down = np.flatnonzero(landed & ~self._grounded[rows])
                entities = self._entities
                for k in touched_down.tolist():
                    self._events.append(('landed', entities[int(rows[k])],
                                         {'speed': max(0.0, -float(velocity[k, 1]))}))
            self._grounded[rows] = landed
            toppling = None
            if landed.any():
                on = np.flatnonzero(landed)
                on_rows = rows[on]
                position[on, 1] = (
                    floors[on] - self._offset[on_rows, 1] + self._half[on_rows, 1])
                impact = -velocity[on, 1]            # how hard it came down
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
                if self._any_topples:
                    lying = self._lying[on_rows]
                    down = np.flatnonzero(lying != 0)
                    if down.size:
                        # On its side a body rolls across its length and
                        # slides along it: friction by the axis, not by the
                        # speed.
                        k = on[down]
                        grip = friction_delta[down]
                        across = np.where(lying[down] == 1, 2, 0)
                        along = 2 - across
                        for column, share in ((along, 1.0), (across, self.LYING_ROLL_FRICTION)):
                            speed = velocity[k, column]
                            velocity[k, column] = np.sign(speed) * np.maximum(
                                np.abs(speed) - grip * np.float32(share), 0.0)
                        scale[down] = 1.0
                    upright = self._topples[on_rows] & (lying == 0)
                    if upright.any():
                        # Going over: moving too fast, landed too hard, or
                        # stood on ground too steep. Along whichever of x
                        # and z it is mostly going (or the ground mostly
                        # falls), for the box it will lie in.
                        speed_x = velocity[on, 0] * scale
                        speed_z = velocity[on, 2] * scale
                        speed = np.hypot(speed_x, speed_z)
                        knocked = upright & (
                            (speed >= self.TOPPLE_SPEED)
                            | ((impact >= self.TOPPLE_IMPACT) & (speed >= self.TOPPLE_NUDGE)))
                        tilted = (upright & ~knocked
                                  & (np.abs(lean).max(axis=1) > self.TOPPLE_SLOPE))
                        over = knocked | tilted
                        if over.any():
                            # Downhill is against the rise.
                            way_x = np.where(tilted, -lean[:, 0], speed_x)[over]
                            way_z = np.where(tilted, -lean[:, 1], speed_z)[over]
                            along_x = np.abs(way_x) >= np.abs(way_z)
                            toppling = (
                                on_rows[over],
                                np.where(along_x, 0, 2),
                                np.where(np.where(along_x, way_x, way_z) >= 0.0, 1.0, -1.0))
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
                    if self._any_topples:
                        # And not half way over.
                        sleeping &= self._tip_left[rows] <= 0.0
                        if toppling is not None:
                            sleeping &= ~np.isin(rows, toppling[0])
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

            if self._any_topples:
                if toppling is not None:
                    self._topple(*toppling)
                down = np.flatnonzero(self._lying[rows] != 0)
                if down.size:
                    self._turn_lying(rows[down], velocity[down], dt)

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
                wanted = self.events_wanted
                for i in rest.tolist():
                    body = bodies[id(entities[i])]
                    callback = body.rest_callback
                    if callback is not None:
                        callback(body.entity)
                    for listener in tuple(body.rest_listeners):
                        listener(body.entity)
                    if wanted:
                        self._events.append(('rest', body.entity, {}))

        if pushed is not None and player is not None:
            # A body the player pushed but a wall held: the player stops at it.
            self._bodies_block_player(player, ppos, player_half, pushed)



def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

