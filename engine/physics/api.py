"""The public physics contract.

Fio simulates dynamic bodies -- Props whose ``physics_enabled`` is set --
through whichever physics is active. A physics is any object that satisfies
:class:`Physics`; it does not inherit from anything in Fio. The built-in
implementation is :class:`engine.physics.soma.Soma` (registered as
``"Soma"``).

What crosses the boundary, and in which direction::

    LogicCollision -> body brushes -> Physics.rebuild
    SpatialGrid    -> walls and floors, asked live -> Physics.step
    Physics.step   -> entity.pos / properties['rotation'] -> ChangeJournal

**The world is the grid's.** A physics is created with the session's
:class:`engine.spatial_grid.SpatialGrid` and asks it where the world is
(:data:`WORLD_QUERIES`): its solid brushes, its movers and doors where they
are now, its water. It keeps no copy of that world: the grid is the single
source of truth, and a physics that
snapshots it is wrong the moment a door opens. The engine never hands a
physics the editor's brushes, the mover table or another system's internals.

A solver fed shapes up front (Bullet and its kind) asks the same grid in
those terms instead: :meth:`solid_shapes` once, rebuilding its static
proxies whenever ``world_version`` changes, and each step :meth:`shape_pose`
for the shapes that move (movers and doors). That is not a snapshot: the
grid still says, every step, where every moving thing is.

**Bodies are the engine's entities.** :meth:`Physics.rebuild` receives the
body brushes the collision runtime derived (:data:`BODY_BRUSH`); the entity
each one names stays the engine's object. A physics owns an awake body's
position for the length of :meth:`Physics.step` and hands it back by
*assigning* ``entity.pos`` (a new list; never an in-place write), which is
what journals the move for the render projection and every index built on
positions. A rotation it changes is written to ``properties['rotation']``
and journalled with :func:`engine.change_journal.touch`. Authored properties
(:data:`BODY_PROPERTIES`) are read, never rewritten.

**Threading.** Every member is called on the logic thread, inside the fixed
60 Hz tick. A physics may not block, and may not touch Qt or OpenGL.

**Gameplay rules are the engine's.** What "heavy" means and what crushing
costs are part of the game, not of a solver, so they are values here
(:data:`HEAVY_MASS`, :data:`CRUSH_DAMAGE`, :data:`PUSH_SCALE`,
:func:`restitution`): a physics honours them, the engine applies them, and
neither asks the other's class for a number. :data:`GAME_RULES` names each
rule; a physics says which it honours with a ``rules`` attribute
(:func:`physics_rules`), so one can be built a rule at a time. Soma honours
them all.

**Bookkeeping a solver does not keep.** Who rests on whom, waking what
rests on a body that moves, and which bodies crossed a grid column are the
contract's, not a solver's: :mod:`engine.physics.bookkeeping` keeps them
for an implementation that feeds it its contacts and positions.

Everything here is stdlib-only, so the contract can be imported, implemented
and tested without NumPy, PyGLM or a running session.
"""

from typing import (Any, Callable, Iterable, Mapping, Optional, Protocol,
                    Sequence, Tuple, runtime_checkable)

#: The version of this contract, apart from the plugin API's (which is
#: plugins.api.API_VERSION). A physics may say which it was written against
#: with a ``contract_version`` attribute; the registry refuses one written
#: for a newer contract or another major version, and -- while the major is
#: 0, when a minor version may change what there is -- another minor
#: version (contract_compatible). 0.9 is the contract with the world as
#: shapes (solid_shapes, shape_pose, world_version, prepare_mesh,
#: mesh_columns), the mesh keys of BODY_BRUSH, PUSH_SCALE, restitution,
#: GAME_RULES / physics_rules and engine.physics.bookkeeping; 1.0 is to
#: follow with fuller support for an adapter over a general solver (Bullet).
PHYSICS_CONTRACT_VERSION = "0.9.0"


def _version_info(value) -> Tuple[int, ...]:
    parts = []
    for piece in str(value).strip().split('.')[:3]:
        digits = ''.join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


PHYSICS_CONTRACT_VERSION_INFO = _version_info(PHYSICS_CONTRACT_VERSION)

#: Quake 2's sv_gravity, in units per second squared, pulling down: the
#: world's gravity until something changes it.
SV_GRAVITY = 800.0

#: The share of the player's speed a body it walks into is given (divided
#: by the body's mass).
PUSH_SCALE = 0.85

#: Mass at and above which a body is heavy: the player cannot push or carry
#: it -- it pushes the player instead, and crushes a player it drives into a
#: wall -- and it does not bounce.
HEAVY_MASS = 10.0

#: Damage the engine applies when :attr:`Physics.player_crushed` is raised:
#: enough to kill through any armour. God mode still stops it.
CRUSH_DAMAGE = 100000


#: The keys of a body brush, as :meth:`Physics.rebuild` receives them. A body
#: brush is the collision runtime's box for one physics Prop; a physics may
#: ignore any key it has no use for and must not require others.
BODY_BRUSH = {
    '_physics_body': 'True: this brush is a dynamic body, not a wall (the '
                     'spatial grid does not file it)',
    '_physics_entity': 'the entity it simulates: its ``pos`` is the body\'s '
                       'origin and ``properties`` its authored material',
    'pos': '[x, y, z]: the collision box\'s centre in the world',
    'size': '[x, y, z]: the collision box\'s full extents',
    '_collision_mode': "optional: how the box was derived ('aabb', ...); "
                       "'mesh' for Collision Shape: Mesh",
    '_mesh_triangles': "'mesh' only: the model's triangles in the world, "
                       "[((v0, v1, v2), normal), ...], where the entity's "
                       "``pos`` is now: the surface other bodies meet",
    '_mesh_bounds': "'mesh' only: ([lo x, y, z], [hi x, y, z]) of those "
                    "triangles -- the collision box, in place of pos/size",
}

#: The authored entity properties a physics reads, with the default each has
#: when absent. ``hidden`` and ``disabled`` are live state, not authored
#: material: a body with either set is dormant -- it neither moves nor is
#: pushed -- and both change in play (I/O, a streaming layer parking it),
#: journalled as STATE or VISIBILITY.
BODY_PROPERTIES = {
    'physics_enabled': '(False) simulate this body at all',
    'no_collision': '(True) pass through walls and the player',
    'mass': '(1.0) scales pushes and bounce; HEAVY_MASS and above is heavy',
    'gravity': '(True) falls by the world\'s gravity',
    'friction': '(0.55) 0..1 surface friction while grounded',
    'linear_damping': '(0.08) velocity lost per second, grounded or not',
    'bounciness': '(0.0) 0..1 share of impact speed kept, faded by mass',
    'rolls': '(False) turns by the distance it travels, as a ball does',
    'topples': '(False) falls over when knocked hard enough, and then lies '
               'on its side, rolling across its length: a barrel. Picking '
               'it up stands it up again',
    'drop_angular_velocity': '([0, 0, 0]) degrees per second of spin',
    'rotation': '([0, 0, 0]) Euler degrees; written back when it changes',
    'hidden': '(False) live: dormant while set',
    'disabled': '(False) live: dormant while set',
    '_sim_tier': '(absent: NEAR) live: the simulation tier a streaming layer '
                 'stamps (engine.spatial.tier_of). DISTANT or beyond is past '
                 'live simulation: the body is held as it is, and carries on '
                 'when its cell is near again',
}

#: What a physics may ask of the :class:`engine.spatial_grid.SpatialGrid` it
#: was created with -- the whole of its view of the world.
WORLD_QUERIES = {
    'cell_size': 'float: the edge of a grid column (engine.spatial.CELL_SIZE)',
    'box_pairs(lo, hi)': '(query, row, rows): every filed brush sharing a cell '
                         'with each box; rows.lo / rows.hi are their float64 '
                         'world boxes, movers and doors where they are now',
    'box_pairs(...) rows.shaped': 'per row: the brush may not be its box (an '
                                  'angled brush, a turning mover)',
    'columns(rows, row, x, z)': '(bottom, top): the solid column of each brush '
                                'over each point -- its true surfaces, where it '
                                'is now; NaN where the point misses it. What a '
                                'body stands on, on a slope',
    'terrain_heights(x, z)': 'the terrain\'s built surface at each point (NaN '
                             'outside it), or None where the map has no solid '
                             'terrain',
    'raycast_down(x, z, start_y)': 'the highest solid top at or below a point, '
                                   'or None',
    'raycast_down_batch(x, z, start_y)': 'the same for many points; NaN where '
                                         'there is none',
    'overlaps_wall(x, y, z, margin)': 'whether a monster-sized box is in a wall',
    'has_line_of_sight(start, end)': 'whether a segment is clear of the world',
    'water_brushes': 'the water volumes: brushes, not solid, never among the '
                     'rows above',
    'world_version': 'changes when the set of solid shapes, or a shape\'s '
                     'authored form, changes: ask solid_shapes again',
    'solid_shapes()': '[(key, kind, data, centre, moves)]: every solid brush '
                      'once. kind \'box\' (data: half extents), \'convex\' '
                      '(its vertices) or \'mesh\' (its triangles), about its '
                      'authored centre; moves: a mover or door',
    'shape_pose(key)': '(centre, axis, angle_degrees): where that shape is '
                       'now; None for a key the world no longer has',
    'prepare_mesh(triangles)': 'a body\'s _mesh_triangles (BODY_BRUSH) made '
                               'ready for mesh_columns, or None when none is '
                               'usable (the body is then its box)',
    'mesh_columns(mesh, x, z, shift)': '(bottom, top): that surface over each '
                                       'point, moved by *shift* since it was '
                                       'prepared; NaN where the point misses it',
}


#: The game's rules a physics honours, by name. A physics lists the ones it
#: does in its ``rules`` attribute (:func:`physics_rules`).
GAME_RULES = {
    'heavy': 'a body of HEAVY_MASS or more is not pushed by the player; it '
             'pushes the player instead, and does not bounce',
    'push': 'a body the player walks into takes PUSH_SCALE of the player\'s '
            'speed over its mass',
    'restitution': 'a bounce keeps restitution(bounciness, mass) of the impact '
                   'speed',
    'crush': 'a heavy body that pins the player against a wall sets '
             'player_crushed',
    'rolls': 'a body that rolls turns by the distance it travels',
    'topples': 'a body that topples goes over when knocked, then lies and '
               'rolls across its length (physics.toppled)',
    'resting_on': 'Body.resting_on names the body under it, and a body '
                  'resting on one that moves is woken',
    'events': 'drain_events reports PHYSICS_EVENTS while events_wanted',
}


def restitution(bounciness: float, mass: float) -> float:
    """The share of impact speed a body keeps when it bounces: *bounciness*
    (0..1) faded linearly by *mass*, whole at 1 or less, nothing at
    :data:`HEAVY_MASS`."""
    bounciness = min(1.0, max(0.0, float(bounciness)))
    weight = min(1.0, max(0.0, (HEAVY_MASS - float(mass)) / (HEAVY_MASS - 1.0)))
    return bounciness * weight


def physics_rules(physics: Any) -> frozenset:
    """The :data:`GAME_RULES` *physics* honours: its ``rules``, or all of
    them for one that does not say (the contract asked for every one)."""
    if not physics:
        return frozenset()
    rules = getattr(physics, 'rules', None)
    return frozenset(GAME_RULES) if rules is None else frozenset(rules)


#: What a physics reports through :meth:`Physics.drain_events`, as the engine
#: publishes it on the plugin event bus: the event name -> its payload. The
#: engine asks for events only while something is subscribed to one of
#: these, so they cost nothing unused.
PHYSICS_EVENTS = {
    'physics.rest': 'entity: a body came to rest',
    'physics.landed': 'entity, speed: a body came down onto the ground',
    'physics.contact': 'entity, other (the entity it met, or None for the '
                       'world), axis (0, 1 or 2), speed: a body ran into '
                       'something',
    'physics.toppled': 'entity, axis, sign: a body that topples went over',
}


@runtime_checkable
class Body(Protocol):
    """One simulated body, as :meth:`Physics.get_body` returns it.

    A handle, not a snapshot: its members read and write the live state.
    """

    #: The engine entity this body simulates.
    entity: Any
    #: ``[x, y, z]`` units per second; assignable.
    velocity: Sequence[float]
    mass: float
    #: Half the collision box, ``(x, y, z)``.
    half_extents: Tuple[float, float, float]
    #: Simulated this tick. A body at rest sleeps until something wakes it.
    awake: bool
    #: Moved by something else (carried, scripted): not simulated, not pushed.
    kinematic: bool
    #: The body's origin -- its entity's ``pos`` -- as the physics has it.
    position: Sequence[float]
    #: Whether it was on the ground when it was last stepped.
    grounded: bool
    #: The entity whose body it stands on, or None (the world, or nothing).
    resting_on: Any
    #: 0..1 surface friction; assignable, as ``mass`` is.
    friction: float


@runtime_checkable
class Physics(Protocol):
    """The physics contract the engine's logic thread drives.

    A physics is created by a factory registered with
    :func:`engine.physics.register_physics`, called as ``factory(spatial_grid)``
    once per play session, after the grid is populated.
    """

    # -- world controls (the console's phys_* commands) ------------------------
    #: Signed vertical gravity in units per second squared (negative is down).
    #: The one gravity in the game: the player, monsters and dropped Props
    #: fall by it too (:func:`world_gravity`).
    gravity: float
    #: Multiplier on the step's delta; 0 freezes every body.
    time_scale: float
    #: Multipliers on every body's authored friction and damping.
    friction_scale: float
    damping_scale: float
    #: Whether bodies at rest stop being simulated.
    sleep_enabled: bool

    #: Raised by :meth:`step` when a heavy body pinned the player against a
    #: wall. The engine applies :data:`CRUSH_DAMAGE` and clears it.
    player_crushed: bool

    # -- lifecycle -------------------------------------------------------------
    def rebuild(self, brushes: Iterable[Mapping[str, Any]]) -> None:
        """Replace every body with those in *brushes* (:data:`BODY_BRUSH`).

        Called when a session starts and again whenever the set of body
        brushes changes. Bodies start asleep, except those left floating.
        """

    def clear(self) -> None:
        """Forget every body and callback; the session is ending. The object
        may be rebuilt afterwards."""

    def reset_controls(self) -> None:
        """Put the world controls back to their defaults and wake every body."""

    # -- the tick --------------------------------------------------------------
    def step(self, delta: float, player: Any = None) -> None:
        """Advance every awake body by *delta* seconds.

        *player* is the primary :class:`engine.player.Player` or None: bodies
        it walks into are pushed (or, if heavy, push it), read from its
        ``pos``, ``velocity`` and half-extents and written back to them.
        Moved entities have their ``pos`` assigned before this returns.
        """

    # -- bodies ----------------------------------------------------------------
    def get_body(self, entity: Any) -> Optional[Body]:
        """The body simulating *entity*, or None if it has none."""

    def wake(self, entity: Any, velocity: Optional[Sequence[float]] = None) -> None:
        """Simulate *entity*'s body again, optionally at *velocity*."""

    def wake_all(self) -> None:
        """Wake every body that can move (after a control changes)."""

    def set_kinematic(self, entity: Any, value: bool) -> None:
        """Hand *entity*'s body to the engine (True: carried, scripted) or
        back to the simulation (False)."""

    def sync_entity_position(self, entity: Any, wake: bool = False) -> None:
        """The engine moved *entity* (a teleport, a cutscene): take its
        ``pos`` as the body's, at rest, optionally waking it."""

    def set_rest_callback(self, entity: Any,
                          callback: Optional[Callable[[Any], None]]) -> None:
        """Call ``callback(entity)`` each time *entity*'s body comes to rest
        (the Prop's OnRest output); None removes it. One slot, and the
        engine's: anything else listens with :meth:`on_rest`."""

    def entities_that_changed_cell(self) -> Sequence[Any]:
        """The entities whose body crossed a grid-column boundary since the
        last call, for indexes filed by cell. Usually empty."""

    # -- bodies that come and go -------------------------------------------------
    def add_body(self, brush: Mapping[str, Any]) -> Optional[Body]:
        """Add one body while the session runs (:data:`BODY_BRUSH`): a
        physics Prop spawned in play. The others carry on as they are."""

    def remove_body(self, entity: Any) -> bool:
        """Remove *entity*'s body (it was despawned). True if it had one."""

    # -- pushing ----------------------------------------------------------------
    def impulse(self, entity: Any, impulse: Sequence[float]) -> bool:
        """Push *entity*'s body: its velocity changes by ``impulse / mass``,
        and it wakes. True if it had a body to push."""

    def impulse_radial(self, centre: Sequence[float], radius: float,
                       strength: float) -> Sequence[Any]:
        """Push every body within *radius* of *centre* away from it, by
        *strength* at the centre falling to nothing at the radius: an
        explosion. Returns the entities pushed."""

    # -- being told -------------------------------------------------------------
    def on_rest(self, entity: Any, callback: Callable[[Any], None]) -> None:
        """Also call ``callback(entity)`` each time *entity*'s body comes to
        rest. Any number may listen; none replaces another, nor the engine's
        own (:meth:`set_rest_callback`)."""

    def off_rest(self, entity: Any, callback: Callable[[Any], None]) -> None:
        """Stop a listener :meth:`on_rest` added."""

    #: Set by the engine while something is subscribed to a physics event
    #: (:data:`PHYSICS_EVENTS`): record what happens for :meth:`drain_events`.
    #: While it is False a physics records nothing.
    events_wanted: bool

    def drain_events(self) -> Sequence[Tuple[str, Any, Mapping[str, Any]]]:
        """What happened since the last call: ``(kind, entity, data)``, where
        ``'physics.' + kind`` is one of :data:`PHYSICS_EVENTS` and *data* its
        payload beyond the entity."""

    # -- asking -----------------------------------------------------------------
    def bodies_in_box(self, lo: Sequence[float], hi: Sequence[float]) -> Sequence[Any]:
        """The entities whose solid, simulated body overlaps the box."""

    def bodies_near(self, point: Sequence[float], radius: float) -> Sequence[Any]:
        """The entities whose body's centre is within *radius* of *point*."""

    def raycast(self, origin: Sequence[float], direction: Sequence[float],
                reach: float) -> Optional[Tuple[Any, float]]:
        """The first body a ray meets within *reach*: ``(entity, distance)``,
        or None. Bodies only: the world is the spatial grid's to ask."""


class NoPhysics:
    """The physics engine there is when there is none: physics switched off,
    or no play session.

    Every member of the contract is here and does nothing, quietly: a query
    finds no bodies, a push pushes nothing, a control is accepted and
    forgotten. So code that talks to the physics engine -- a plugin, most of
    all -- does not have to ask first whether there is one, and cannot fail
    for want of asking. It is falsy, for code that does want to know::

        if api.physics:            # an engine is running
            ...

    Anything else asked of it (a member that is one engine's own) answers
    with a function that does nothing and returns None.
    """

    __slots__ = ()

    gravity = -SV_GRAVITY
    rules = frozenset()
    time_scale = 1.0
    friction_scale = 1.0
    damping_scale = 1.0
    sleep_enabled = True
    player_crushed = False
    events_wanted = False

    def __bool__(self) -> bool:
        return False

    def __setattr__(self, name, value) -> None:
        pass                                    # accepted and forgotten

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return _nothing

    def rebuild(self, brushes) -> None: pass
    def clear(self) -> None: pass
    def reset_controls(self) -> None: pass
    def step(self, delta, player=None) -> None: pass
    def get_body(self, entity): return None
    def wake(self, entity, velocity=None) -> None: pass
    def wake_all(self) -> None: pass
    def set_kinematic(self, entity, value) -> None: pass
    def sync_entity_position(self, entity, wake=False) -> None: pass
    def set_rest_callback(self, entity, callback) -> None: pass
    def entities_that_changed_cell(self): return ()
    def add_body(self, brush): return None
    def remove_body(self, entity) -> bool: return False
    def impulse(self, entity, impulse) -> bool: return False
    def impulse_radial(self, centre, radius, strength): return []
    def on_rest(self, entity, callback) -> None: pass
    def off_rest(self, entity, callback) -> None: pass
    def drain_events(self): return []
    def bodies_in_box(self, lo, hi): return []
    def bodies_near(self, point, radius): return []
    def raycast(self, origin, direction, reach): return None

    def __repr__(self) -> str:
        return '<NoPhysics>'


def _nothing(*args, **kwargs):
    return None


#: The one :class:`NoPhysics`.
NO_PHYSICS = NoPhysics()


def missing_members(physics: Any) -> list:
    """The members of :class:`Physics` *physics* lacks, sorted: an attribute
    it does not have, or a method it has that cannot be called. Empty for
    an object that satisfies the contract."""
    import typing
    names = (typing.get_protocol_members(Physics)
             if hasattr(typing, 'get_protocol_members')
             else [n for n in vars(Physics) if not n.startswith('_')]
             + list(getattr(Physics, '__annotations__', {})))
    missing = []
    for name in sorted(set(names)):
        try:
            value = getattr(physics, name)
        except AttributeError:
            missing.append(name)
            continue
        except Exception:
            continue                    # a property that raised: it is there
        if callable(getattr(Physics, name, None)) and not callable(value):
            missing.append(name)
    return missing


def contract_compatible(physics: Any) -> Optional[str]:
    """Why *physics* cannot run on this contract, or None when it can.

    One that does not say (no ``contract_version``) is taken as written for
    this one. One written for a newer version, or another major version --
    or, while the major is 0, another minor version -- is refused: it may
    rely on what is not here, or on what has changed."""
    declared = getattr(physics, 'contract_version', None)
    if declared is None or callable(declared):
        return None
    wanted = _version_info(declared)
    have = PHYSICS_CONTRACT_VERSION_INFO
    if (wanted[0] != have[0] or wanted > have
            or (have[0] == 0 and wanted[1] != have[1])):
        return ("written for physics contract %s; this Fio has %s"
                % (declared, PHYSICS_CONTRACT_VERSION))
    return None


def world_gravity(world: Optional[Physics] = None) -> float:
    """Signed vertical gravity (negative is down) from the live physics.

    *world* is the session's physics, or None outside a play session (or
    when the caller has none), which gives the default, Quake 2's 800.
    """
    if world:
        return float(world.gravity)
    return -SV_GRAVITY
