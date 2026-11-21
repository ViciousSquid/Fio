"""Explosions: setting one off, and what it does when it ends.

An explosion is played by an Effect (engine.effect_entity). Setting one off
(:func:`explode_effect`) plays the EXPLOSION over whatever the Effect is: a
FIRE explodes and, by default (``return_after_explosion``), burns again once
the explosion has played -- the EffectStore hands the row back
(:meth:`~engine.effect_table.EffectStore.finish_explosions`, called every
tick by :func:`finish_explosions`). The authored ``effect_type`` is not
rewritten, so a playtest never leaves a FIRE saved as an EXPLOSION. With
``return_after_explosion`` off it becomes an EXPLOSION for good, as Explode
always used to do.

The editor's Effect panel plays the same explosion as a preview
(:func:`preview_explosion`): the store row alone, back to what was authored
when it ends, with no outputs fired.
"""

from __future__ import annotations

import math
import time

from .effect_entity import EFFECT_EXPLOSION

#: The sound every explosion makes, from where it is, at its volume.
EXPLOSION_SOUND = 'assets/sounds/explode.mp3'


def _flag(value, default=True):
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return bool(value)


def returns_after_explosion(props) -> bool:
    """Whether an Effect that is not an EXPLOSION goes back to its own type
    once it has exploded (the default) rather than staying one."""
    return _flag(props.get('return_after_explosion', True))


def _turn(rotation):
    """The rotation ``[x, y, z]`` degrees a model is drawn with, as three
    rows (Y, then X, then Z, as the renderer applies them), or None for none."""
    try:
        rx, ry, rz = (float(rotation[0]), float(rotation[1]), float(rotation[2]))
    except (TypeError, ValueError, IndexError):
        return None
    if not (rx or ry or rz):
        return None
    cx, sx = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, sy = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, sz = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    y = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    x = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
    z = ((cz, -sz, 0.0), (sz, cz, 0.0), (0.0, 0.0, 1.0))

    def mul(a, b):
        return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                     for i in range(3))
    return mul(mul(y, x), z)


def _rotate(turn, v):
    return tuple(turn[i][0] * v[0] + turn[i][1] * v[1] + turn[i][2] * v[2]
                 for i in range(3))


def _rotate_back(turn, v):
    return tuple(turn[0][i] * v[0] + turn[1][i] * v[1] + turn[2][i] * v[2]
                 for i in range(3))


def _position(thing):
    pos = thing.pos
    try:
        return [float(pos.x), float(pos.y), float(pos.z)]
    except AttributeError:
        return [float(pos[0]), float(pos[1]), float(pos[2])]


def explosion_volume(props) -> float:
    """How loud an explosion is, 0..1 (``explosion_volume``; default 1).
    A map saved before the volume existed says ``silent``: that is 0."""
    raw = props.get('explosion_volume')
    if raw is None:
        return 0.0 if _flag(props.get('silent', False), False) else 1.0
    try:
        volume = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if volume != volume:                    # NaN
        return 1.0
    return max(0.0, min(1.0, volume))


def queue_explosion_sound(game_state, effect):
    """Play the explosion's sound from where it is (positional: panned and
    fading with distance), at its ``explosion_volume``."""
    volume = explosion_volume(effect.properties)
    if game_state is None or volume <= 0.0:
        return
    game_state.queue_sound({
        'action': 'play',
        'file': EXPLOSION_SOUND,
        'volume': volume,
        'position': _position(effect),
    })


#: An Effect's blast: what its explosion does to the world around it.
#:
#: * ``explosion_damage`` -- damage dealt to everything within the radius
#:   that the blast can see (walls stop it): the player, monsters, and
#:   explosive Props, which may go off in turn;
#: * ``explosion_radius`` -- world units, from the explosion's centre;
#: * ``explosion_force`` -- the push at the centre (world units a second for
#:   a body of mass 1), falling to nothing at the radius. Monsters, the
#:   player and Props with physics on are shoved away; a Prop with physics
#:   off is never moved, nor is anything when physics is off for the session.
#:
#: An Effect does no damage and pushes nothing unless it is given some: an
#: explosion is harmless until it is told otherwise.
BLAST_DEFAULTS = {
    'explosion_damage': 0,
    'explosion_radius': 192.0,
    'explosion_force': 0.0,
}

#: The most a blast setting can be, whatever is authored. A value that is not
#: a finite number (NaN, ``"inf"``) is the default instead; one above its
#: maximum is the maximum. 4096 units of radius is eight grid cells -- far past
#: any room -- and keeps the radius queries bounded; 20000 units a second at
#: the centre already throws a body of mass 1 clean across a large map, so
#: more only feeds the physics broad phase velocities it cannot use (the cost
#: of a swept box grows with the square of speed); and a million damage kills
#: anything while staying a number ``int()`` can round.
BLAST_MAXIMUMS = {
    'explosion_damage': 1.0e6,
    'explosion_radius': 4096.0,
    'explosion_force': 20000.0,
}


def _finite(value, default):
    """*value* as a float, or *default* when it is not a finite number."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _blast_number(props, key):
    value = _finite(props.get(key, BLAST_DEFAULTS[key]), BLAST_DEFAULTS[key])
    return min(max(value, 0.0), BLAST_MAXIMUMS[key])


def blast_settings(props):
    """``(damage, radius, force)`` of an Effect's blast."""
    return (_blast_number(props, 'explosion_damage'),
            _blast_number(props, 'explosion_radius'),
            _blast_number(props, 'explosion_force'))


def blast_centre(effect):
    """Where an Effect's blast comes from: the middle of its fireball. An
    EXPLOSION stands on its position (its fireball rises from it); any other
    type is centred on it."""
    x, y, z = _position(effect)
    props = effect.properties
    if str(props.get('effect_type', 'FIRE')).strip().upper() == EFFECT_EXPLOSION:
        try:
            y += float(props.get('height', 46.0)) * 0.5
        except (TypeError, ValueError):
            pass
    return (x, y, z)


def explode_effect(effect, logic, now=None):
    """Set *effect* off in a running game: play its explosion and sound,
    fire ``OnExplode`` (and ``OnChanged`` if it became an EXPLOSION for
    good), and apply its blast (:data:`BLAST_DEFAULTS`) -- the one way
    anything explodes, barrels included. Returns the type it will go back
    to when the explosion ends."""
    now = time.perf_counter() if now is None else now
    props = effect.properties
    current = str(props.get('effect_type', 'FIRE') or 'FIRE').strip().upper()
    became_explosion = False
    if current != EFFECT_EXPLOSION and returns_after_explosion(props):
        return_to = current
    else:
        if current != EFFECT_EXPLOSION:
            props['effect_type'] = EFFECT_EXPLOSION
            became_explosion = True
        props['preview'] = False
        return_to = EFFECT_EXPLOSION          # dormant again once it has played
    logic.session_runtime.effect_store.trigger_explosion(
        effect, now, return_to=return_to)
    queue_explosion_sound(logic.game_state, effect)
    io = logic.io_manager
    if io is not None:
        io.fire_output(effect, 'OnExplode')
        if became_explosion:
            io.fire_output(effect, 'OnChanged', value=EFFECT_EXPLOSION)
    damage, radius, force = blast_settings(props)
    if radius > 0.0 and (damage > 0.0 or force > 0.0):
        runtime = getattr(logic, 'explosion_runtime', None)
        if runtime is not None:
            runtime.blast(blast_centre(effect), radius, damage, force, now)
    return return_to


def preview_explosion(effect, effect_store, game_state=None, now=None):
    """Play *effect*'s explosion in the editor: the store row only, back to
    the authored type when it has played; nothing authored changes and no
    output fires. The caller holds the logic thread's tick lock."""
    now = time.perf_counter() if now is None else now
    authored = str(effect.properties.get('effect_type', 'FIRE') or 'FIRE')
    effect_store.trigger_explosion(effect, now, return_to=authored)
    queue_explosion_sound(game_state, effect)


def finish_explosions(logic, now=None):
    """Hand every explosion that has played back to its type (once a tick).

    In a game each fires ``OnExplodeFinished``. Nothing to do -- the usual
    case -- costs one dict check."""
    store = logic.session_runtime.effect_store
    finished = store.finish_explosions(time.perf_counter() if now is None else now)
    if finished and logic.session_runtime.play_mode and logic.io_manager is not None:
        for effect, _type in finished:
            logic.io_manager.fire_output(effect, 'OnExplodeFinished')
    return finished


# ---------------------------------------------------------------------------
# Explosive Props (barrels)
# ---------------------------------------------------------------------------

#: Authored settings of an explosive Prop, and their defaults:
#:
#: * ``explosive`` -- whether damage can set it off at all;
#: * ``explosive_health`` -- damage it takes, in total, before it explodes;
#: * ``explosion_damage``, ``explosion_radius``, ``explosion_force`` and
#:   ``explosion_volume`` -- its explosion's. When the barrel explodes its
#:   model is removed and, at that same moment, an EXPLOSION Effect is
#:   spawned where the model was, carrying these, and set off: the Effect's
#:   explosion (:func:`explode_effect`) is the fireball, light, sound and
#:   sparks, and the blast (:data:`BLAST_DEFAULTS`). The barrel has no
#:   explosion of its own.
EXPLOSIVE_DEFAULTS = {
    'explosive': False,
    'explosive_health': 20,
    'explosion_damage': 60,
    'explosion_radius': 192.0,
    'explosion_force': 400.0,
    'explosion_volume': 1.0,
}

#: How the Effect a barrel spawns looks: a burst of sparks and a flash of
#: light. Its size follows the blast radius (BARREL_FIREBALL_FRACTION of it;
#: the fireball grows to three times that while it plays).
BARREL_EFFECT_LOOK = {
    'effect_type': EFFECT_EXPLOSION,
    'preview': False,
    'lifetime': 0.9,
    'emitter': 'SPARKS',
    'light_enabled': True,
    'light_intensity': 3.0,
}
BARREL_FIREBALL_FRACTION = 0.3
#: Hiding the model as it goes: the fireball is at least this many times the
#: barrel's own size, and opens BARREL_SKIP of the way into its playback --
#: already a solid core, at about twice its brightness from the flash -- so
#: from its first frame it covers the place where the model was.
BARREL_COVER = 1.5
BARREL_SKIP = 0.12


def barrel_effect(prop, base, model_size=(0.0, 0.0)):
    """The EXPLOSION Effect *prop* spawns when it goes, standing at *base*
    (where its model was), with the barrel's explosion settings; sized to
    cover the model (*model_size*: its width and height)."""
    from .effect_entity import Effect
    props = prop.properties
    radius = _number(props, 'explosion_radius')
    cover_w, cover_h = (BARREL_COVER * float(v) for v in model_size)
    settings = dict(BARREL_EFFECT_LOOK)
    settings.update({
        'name': '%s_explosion' % (props.get('name') or 'barrel'),
        'width': max(radius * BARREL_FIREBALL_FRACTION, cover_w, 16.0),
        'height': max(radius * BARREL_FIREBALL_FRACTION, cover_h, 16.0),
        'explosion_skip': BARREL_SKIP,
        'light_radius': max(radius * 2.0, 128.0),
        'explosion_damage': _number(props, 'explosion_damage'),
        'explosion_radius': radius,
        'explosion_force': _number(props, 'explosion_force'),
        'explosion_volume': explosion_volume(
            {'explosion_volume': props.get('explosion_volume', 1.0)}),
    })
    return Effect(pos=[float(v) for v in base], properties=settings)


def _number(props, key):
    value = _finite(props.get(key, EXPLOSIVE_DEFAULTS[key]), EXPLOSIVE_DEFAULTS[key])
    if key in BLAST_MAXIMUMS:
        return min(max(value, 0.0), BLAST_MAXIMUMS[key])
    return value


def is_explosive(thing) -> bool:
    props = getattr(thing, 'properties', None)
    return (isinstance(props, dict) and props.get('type') == 'prop'
            and _flag(props.get('explosive', False), False))


class ExplosionRuntime:
    """Explosive Props in a running game: the damage they have taken, the
    explosions waiting to go off, and what Play must put back when it stops;
    and the blast every exploding Effect applies (:meth:`blast`).

    Damage reaches a barrel from a shot (:meth:`trace` / :meth:`point_hit`,
    asked by the hitscan and the projectiles), the ``Damage`` input, or a
    blast. Once a barrel has taken ``explosive_health`` it explodes -- at
    once when shot, :data:`CHAIN_DELAY` later when caught in a neighbour's
    blast, so a row of barrels goes up one after another. Exploding removes
    the model (hidden, its collision gone) and, at that same moment, spawns
    an EXPLOSION Effect where the model was and sets it off
    (:func:`barrel_effect`, :func:`explode_effect`): the Effect does
    everything else.

    Everything here is per session: :meth:`stop` shows the barrels again,
    and the Effects they spawned are session Things, removed when Play
    stops -- the map is as authored.
    Nothing costs anything on a map without explosive Props: every entry
    point returns at its first check.
    """

    #: Seconds between a barrel caught in a blast and its own explosion.
    CHAIN_DELAY = 0.15
    #: Monsters and the player are pushed by this fraction of the force;
    #: physics bodies take all of it.
    ACTOR_PUSH = 0.8
    #: Upward share of the player's push: a blast lifts as it shoves.
    PLAYER_LIFT = 0.35

    def __init__(self, logic):
        self.logic = logic
        self.damage_taken = {}          # id(prop) -> damage so far
        self.pending = []               # [(when, prop)]
        self.exploded = {}              # id(prop) -> (prop, hidden before)
        self._bounds = {}               # model path -> local (min, max) or None
        self._explosives = None         # (key, [props]) cache
        self.scans = 0                  # thing-list scans by explosives()
        self.sight_tests = 0            # chain-reaction line-of-sight tests

    # -- session ----------------------------------------------------------

    def start(self):
        self.damage_taken.clear()
        self.pending.clear()
        self.exploded.clear()
        self._explosives = None

    def stop(self):
        """Put the barrels back as authored (the Effects they spawned are
        session Things: the session removes them). Returns whether any
        changed."""
        from .spatial import set_authored_flag
        changed = bool(self.exploded)
        for prop, was_hidden in self.exploded.values():
            set_authored_flag(prop, 'hidden', was_hidden)
        self.start()
        return changed

    # -- which Props -------------------------------------------------------

    def explosives(self):
        """The explosive Props still standing (cached per thing list).

        The thing list is scanned only when it is a different list or its
        length changed; a barrel that explodes leaves the cached list itself
        (:meth:`explode_prop`), so a chain of explosions never rescans the
        world."""
        things = self.logic.editor_state.things
        key = (id(things), len(things))
        cached = self._explosives
        if cached is None or cached[0] != key:
            self.scans += 1
            exploded = self.exploded
            standing = [t for t in things
                        if is_explosive(t) and id(t) not in exploded]
            cached = self._explosives = (key, standing)
        return cached[1]

    def _drop_explosive(self, prop):
        """Take *prop* out of the cached standing list, if it is there."""
        cached = self._explosives
        if cached is None:
            return
        standing = cached[1]
        for i, other in enumerate(standing):
            if other is prop:
                del standing[i]
                return

    def _model_box(self, prop):
        """*prop*'s model as the renderer places it: ``(lo, hi, scale, pos,
        turn)`` -- its bounds in model space, its scale, its position, and its
        rotation as three rows (None when it is not turned). The renderer's
        order: turn by Y, then X, then Z (degrees ``rotation`` [x, y, z]),
        after scaling; a toppled barrel's ``rotation`` is written by physics."""
        props = prop.properties
        path = props.get('model_path', '')
        if path not in self._bounds:
            self._bounds[path] = self.logic.collision_runtime.compute_model_bounds(path)
        bounds = self._bounds[path]
        scale = props.get('scale', 1.0)
        if isinstance(scale, (int, float)):
            scale = (scale, scale, scale)
        scale = tuple(float(v) or 1.0 for v in scale[:3])
        if bounds:
            lo, hi = (tuple(float(v) for v in bounds[0]),
                      tuple(float(v) for v in bounds[1]))
        else:
            lo, hi = (-32.0, 0.0, -32.0), (32.0, 64.0, 32.0)
        return lo, hi, scale, _position(prop), _turn(props.get('rotation'))

    def box(self, prop):
        """*prop*'s world box ``(lo, hi)``: around its model's bounds, scaled
        and turned as it is drawn (a barrel lying on its side is a long low
        box, not an upright one)."""
        lo, hi, scale, (x, y, z), turn = self._model_box(prop)
        if turn is None:
            return ((x + lo[0] * scale[0], y + lo[1] * scale[1], z + lo[2] * scale[2]),
                    (x + hi[0] * scale[0], y + hi[1] * scale[1], z + hi[2] * scale[2]))
        corners = [_rotate(turn, (cx * scale[0], cy * scale[1], cz * scale[2]))
                   for cx in (lo[0], hi[0]) for cy in (lo[1], hi[1]) for cz in (lo[2], hi[2])]
        return (tuple(min(c[i] for c in corners) + (x, y, z)[i] for i in range(3)),
                tuple(max(c[i] for c in corners) + (x, y, z)[i] for i in range(3)))

    def _to_model(self, prop, point, direction=None):
        """*point* (and *direction*) in *prop*'s model space, with its bounds:
        ``(lo, hi, point, direction)``. A distance along a ray is the same in
        both spaces."""
        lo, hi, scale, pos, turn = self._model_box(prop)
        rel = tuple(float(point[i]) - pos[i] for i in range(3))
        if turn is not None:
            rel = _rotate_back(turn, rel)
        local = tuple(rel[i] / scale[i] for i in range(3))
        if direction is None:
            return lo, hi, local, None
        d = tuple(float(direction[i]) for i in range(3))
        if turn is not None:
            d = _rotate_back(turn, d)
        return lo, hi, local, tuple(d[i] / scale[i] for i in range(3))

    def centre(self, prop):
        lo, hi = self.box(prop)
        return tuple((a + b) * 0.5 for a, b in zip(lo, hi))

    # -- shots ------------------------------------------------------------

    def trace(self, origin, direction, reach):
        """The nearest explosive Prop a ray meets within *reach*:
        ``(prop, distance)``, or None. Against each model's own box, turned
        as it is drawn: the whole barrel is hit, standing or lying."""
        standing = self.explosives()
        if not standing:
            return None
        best = None
        for prop in standing:
            lo, hi, o, d = self._to_model(prop, origin, direction)
            t0, t1 = 0.0, float(reach)
            for axis in range(3):
                if abs(d[axis]) < 1e-12:
                    if o[axis] < lo[axis] or o[axis] > hi[axis]:
                        t0 = t1 + 1.0
                        break
                    continue
                n = (lo[axis] - o[axis]) / d[axis]
                f = (hi[axis] - o[axis]) / d[axis]
                if n > f:
                    n, f = f, n
                t0, t1 = max(t0, n), min(t1, f)
                if t0 > t1:
                    break
            if t0 <= t1 and (best is None or t0 < best[1]):
                best = (prop, t0)
        return best

    def point_hit(self, x, y, z):
        """The explosive Prop whose model box (turned as it is drawn) holds
        the point, or None."""
        standing = self.explosives()
        if not standing:
            return None
        for prop in standing:
            lo, hi, p, _d = self._to_model(prop, (x, y, z))
            if all(lo[i] <= p[i] <= hi[i] for i in range(3)):
                return prop
        return None

    # -- damage -----------------------------------------------------------

    def damage_prop(self, prop, amount, delay=0.0, now=None):
        """Add *amount* of damage to an explosive Prop; once it has taken its
        ``explosive_health`` it explodes, *delay* seconds from now. Cheap
        and lock-free: the explosion itself runs in :meth:`update`."""
        key = id(prop)
        if key in self.exploded or not is_explosive(prop):
            return False
        taken = self.damage_taken.get(key, 0.0) + max(float(amount), 0.0)
        self.damage_taken[key] = taken
        if taken < _number(prop.properties, 'explosive_health'):
            return False
        if any(p is prop for _when, p in self.pending):
            return True
        now = time.perf_counter() if now is None else now
        self.pending.append((now + max(float(delay), 0.0), prop))
        return True

    def update(self, now=None):
        """Set off every explosion that is due (once a tick)."""
        if not self.pending:
            return
        now = time.perf_counter() if now is None else now
        due = [entry for entry in self.pending if entry[0] <= now]
        if not due:
            return
        self.pending = [entry for entry in self.pending if entry[0] > now]
        for _when, prop in due:
            if id(prop) not in self.exploded:
                self.explode_prop(prop, now)

    def explode_prop(self, prop, now=None):
        """Blow *prop* up now: the model is removed and, at the same moment,
        an EXPLOSION Effect is spawned where it was and explodes."""
        from .change_journal import touch
        from .spatial import set_authored_flag
        logic = self.logic
        now = time.perf_counter() if now is None else now
        props = prop.properties
        centre = self.centre(prop)
        lo, _hi = self.box(prop)

        # The model goes: hidden, and its collision with it.
        self.exploded[id(prop)] = (prop, bool(props.get('hidden', False)))
        self.damage_taken.pop(id(prop), None)
        self._drop_explosive(prop)
        set_authored_flag(prop, 'hidden', True)
        touch(prop)
        sync = getattr(logic.collision_runtime, 'sync_bodies', None)
        if sync is not None:
            sync()

        # The Effect, where the model was (standing on its base), explodes.
        hi = self.box(prop)[1]
        effect = barrel_effect(prop, (centre[0], lo[1], centre[2]),
                               (max(hi[0] - lo[0], hi[2] - lo[2]), hi[1] - lo[1]))
        things = logic.editor_state.things
        before = (id(things), len(things))
        logic.session_runtime.add_session_thing(effect)
        cached = self._explosives
        if (cached is not None and cached[0] == before
                and len(things) == before[1] + 1 and things[-1] is effect):
            # The fireball is an Effect, not an explosive Prop: the standing
            # list is still right for the longer thing list.
            self._explosives = ((id(things), len(things)), cached[1])
        if logic.io_manager is not None:
            logic.io_manager.fire_output(prop, 'OnExplode')
        explode_effect(effect, logic, now)
        return effect

    def blast(self, centre, radius, damage, force, now=None):
        """Damage and push everything within *radius* of *centre* that the
        blast can see (walls stop it)."""
        logic = self.logic
        cx, cy, cz = centre
        grid = logic.session_runtime.spatial_grid

        def seen(x, y, z):
            if grid is None:
                return True
            import glm
            return grid.has_line_of_sight(glm.vec3(cx, cy, cz), glm.vec3(x, y, z))

        def falloff(d):
            return max(0.0, 1.0 - d / radius)

        # The player.
        runtime = logic.player_runtime
        player = runtime.player
        if player is not None and not runtime.player_dead:
            px, py, pz = float(player.pos.x), float(player.pos.y), float(player.pos.z)
            half = float(getattr(player, 'height', 0.0)) * 0.5
            # The nearest point of the player's height to the blast.
            ny = min(max(cy, py - half), py + half)
            d = ((px - cx) ** 2 + (ny - cy) ** 2 + (pz - cz) ** 2) ** 0.5
            if d <= radius and seen(px, ny, pz):
                if damage > 0.0:
                    runtime.damage_source = (cx, cy, cz)
                    logic.trigger_runtime._apply_player_damage(damage)
                push = force * self.ACTOR_PUSH * falloff(d)
                if push > 0.0:
                    hx, hz = px - cx, pz - cz
                    flat = (hx * hx + hz * hz) ** 0.5
                    if flat > 1e-3:
                        player.velocity.x += hx / flat * push
                        player.velocity.z += hz / flat * push
                    player.velocity.y = max(float(player.velocity.y),
                                            push * self.PLAYER_LIFT)

        # Monsters.
        hit_sphere = logic.combat_runtime.hit_sphere
        monster_ai = logic.monster_ai
        with logic.session_runtime.monster_lock:
            for monster in list(logic.world_runtime.monster_things):
                mp = monster.properties
                if mp.get('dead') or mp.get('hidden') or mp.get('disabled'):
                    continue
                (mx, my, mz), r = hit_sphere(monster.pos, mp)
                d = max(((mx - cx) ** 2 + (my - cy) ** 2 + (mz - cz) ** 2) ** 0.5 - r, 0.0)
                if d > radius or not seen(mx, my, mz):
                    continue
                if damage > 0.0:
                    monster_ai._apply_monster_damage(monster, int(round(damage)))
                push = force * self.ACTOR_PUSH * falloff(d)
                if push > 0.0:
                    monster_ai.knock_back(monster, mx - cx, mz - cz, push)

        # Other explosive Props: they go off in turn. One already waiting to
        # go off needs nothing more from this blast -- no box, no line of
        # sight -- so a chain reaction tests each barrel's sight until it is
        # caught, not once per blast that reaches it.
        pending = {id(p) for _when, p in self.pending}
        for other in tuple(self.explosives()):
            if id(other) in pending:
                continue
            lo, hi = self.box(other)
            ox, oy, oz = ((a + b) * 0.5 for a, b in zip(lo, hi))
            reach = max(hi[0] - lo[0], hi[2] - lo[2]) * 0.5
            d = max(((ox - cx) ** 2 + (oy - cy) ** 2 + (oz - cz) ** 2) ** 0.5 - reach, 0.0)
            if d > radius:
                continue
            # Its own box would hide its centre: look at the near side.
            if d > 0.0:
                k = reach / max(d + reach, 1e-6)
                tx, ty, tz = ox + (cx - ox) * k, oy + (cy - oy) * k, oz + (cz - oz) * k
                self.sight_tests += 1
                if not seen(tx, ty, tz):
                    continue
            self.damage_prop(other, damage, delay=self.CHAIN_DELAY, now=now)

        # Physics Props are shoved -- solid or not (the physics engine's own
        # radial push reaches only the solid ones). Only those: a Prop with
        # physics off stays exactly where it is, and with physics switched off
        # for the session (Settings) nothing is pushed at all -- a blast never
        # turns physics on. The Props near the blast come from the Prop
        # runtime's cell index: the cost is what is near.
        session = logic.session_runtime
        physics = session.physics_world if session.physics_enabled else None
        if physics is not None and force > 0.0:
            for prop in logic.prop_runtime.props_within(cx, cz, radius):
                pp = prop.properties
                if not _flag(pp.get('physics_enabled', False), False):
                    continue
                if id(prop) in self.exploded or pp.get('hidden'):
                    continue
                bx, by, bz = self.centre(prop)
                hx, hy, hz = bx - cx, by - cy, bz - cz
                d = (hx * hx + hy * hy + hz * hz) ** 0.5
                if d >= radius:
                    continue
                push = force * falloff(d)
                if d > 1e-3:
                    vec = (hx / d * push, max(hy / d, 0.25) * push, hz / d * push)
                else:
                    vec = (0.0, push, 0.0)
                physics.impulse(prop, vec)
