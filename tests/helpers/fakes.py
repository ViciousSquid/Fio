"""Lightweight stand-ins for the live editor and logic thread.

These are *not* mocks of whole subsystems.  Each one implements exactly the
surface the code under test actually reads — the attributes MonsterAI touches
on its parent thread, the two methods the property editor calls on its host —
so a behavioural test can be written without a display, a GL context or a
running thread, while the integration tiers still drive the real objects.

Where a fake stands in for something with real behaviour (ray/AABB
intersection, the entity name cache) it delegates to or reproduces the engine's
own implementation rather than a simplified one, so a test cannot pass against
a fake that is kinder than the real thing.
"""

import glm

from engine.physics import SpatialGrid


class ManualClock:
    """A clock that only moves when a test moves it.

    Anything in Fio that ages state does it from a ``delta`` handed in by its
    caller (``IOManager.update``, ``MonsterAI.update``, ``LogicThread._tick``),
    so a deterministic test drives time by calling those with fixed deltas.
    This tracks the total for the few places that want an absolute ``now``.
    """

    def __init__(self, start=0.0):
        self.now = float(start)

    def advance(self, delta):
        self.now += float(delta)
        return self.now

    def __call__(self):
        return self.now


class FakePlayer:
    """The slice of :class:`engine.player.Player` the AI and I/O paths read."""

    def __init__(self, pos=(0.0, 0.0, 0.0), angle=0.0, pitch=0.0):
        self.pos = glm.vec3(*pos)
        self.angle = float(angle)
        self.pitch = float(pitch)
        self.camera_height = 50.0
        self.eye_underwater = False
        self.water_tint = [0.0, 0.4, 0.6]
        self.damage_taken = []

    def take_damage(self, amount):
        self.damage_taken.append(amount)


class FakeGameState:
    """The slice of :class:`engine.threaded_game_state.ThreadedGameState` the
    AI and I/O paths use: a place to queue sounds and console commands."""

    def __init__(self):
        self.sounds = []
        self.console_commands = []

    def queue_sound(self, request):
        self.sounds.append(dict(request))

    def queue_console_command(self, command):
        self.console_commands.append(command)


class FakeLogicThread:
    """The parent object :class:`engine.monster_ai.MonsterAI` talks to.

    MonsterAI reads a well-defined set of attributes off its logic thread (see
    ``grep 'self\\.lt\\.' engine/monster_ai.py``); every one of them is here,
    with the same meaning.  ``intersect_ray_aabb`` is the engine's own slab
    test, copied rather than approximated, because line-of-sight results would
    otherwise depend on which implementation the test happened to get.
    """

    def __init__(self, brushes=(), things=(), player=None, io_manager=None):
        self.brushes = list(brushes)
        self.things = list(things)
        self.player = player
        self.player_dead = False
        self.player_health = 100
        self.notarget = False
        self.io_manager = io_manager
        self.game_state = FakeGameState()
        self._monster_projectiles = []
        self._noise_events = []
        self._name_cache = {}
        self._monster_things = None
        self.damage_applied = []
        self.rebuild_name_cache()

    # -- entity lookup ----------------------------------------------------
    def rebuild_name_cache(self):
        """Mirror ``LogicThread._build_entity_caches``' name index."""
        self._name_cache = {}
        for thing in self.things:
            name = thing.properties.get("name")
            if name:
                self._name_cache[name] = thing
        for brush in self.brushes:
            name = brush.get("name")
            if name:
                self._name_cache.setdefault(name, brush)
        return self._name_cache

    def _find_entity_by_name(self, name):
        return self._name_cache.get(name)

    # -- geometry ---------------------------------------------------------
    @staticmethod
    def intersect_ray_aabb(origin, direction, box_min, box_max):
        t_min = 0.0
        t_max = 10000.0
        for i in range(3):
            if abs(direction[i]) < 1e-6:
                if origin[i] < box_min[i] or origin[i] > box_max[i]:
                    return False, 0
            else:
                inv_d = 1.0 / direction[i]
                t1 = (box_min[i] - origin[i]) * inv_d
                t2 = (box_max[i] - origin[i]) * inv_d
                t_near = min(t1, t2)
                t_far = max(t1, t2)
                t_min = max(t_min, t_near)
                t_max = min(t_max, t_far)
                if t_min > t_max:
                    return False, 0
        return True, t_min

    # -- noise ------------------------------------------------------------
    def emit_noise(self, pos, source="test", loudness=1.0, when=0.0):
        self._noise_events.append({
            "pos": [float(pos[0]), float(pos[1]), float(pos[2])],
            "time": float(when),
            "source": source,
            "loudness": float(loudness),
        })

    def get_recent_noise_events(self, max_age=3.0):
        return list(self._noise_events)

    def get_recent_gunfire_events(self, max_age=3.0):
        return self.get_recent_noise_events(max_age)

    # -- damage -----------------------------------------------------------
    def _apply_player_damage(self, damage):
        self.damage_applied.append(damage)
        self.player_health -= damage
        if self.player_health <= 0:
            self.player_dead = True

    # -- convenience ------------------------------------------------------
    def build_spatial_grid(self):
        """Populate a real :class:`SpatialGrid` from this world's brushes."""
        grid = SpatialGrid()
        grid.populate(self.brushes)
        return grid


class RecordingIOManager:
    """Records ``fire_output`` calls instead of dispatching them.

    For tests that care *whether* a subsystem announced something (the AI
    firing ``OnSeePlayer``), not what the I/O system then did with it — the
    execution path has its own suite under ``tests/logic``.
    """

    def __init__(self):
        self.fired = []          # [(entity, output_name, value), ...]

    def fire_output(self, entity, output_name, value=None):
        self.fired.append((entity, output_name, value))

    def outputs_for(self, entity):
        return [name for ent, name, _ in self.fired if ent is entity]

    def names(self):
        return [name for _, name, _ in self.fired]


class RecordingHandler:
    """A callable that records every call, for event/dispatch assertions."""

    def __init__(self, name="handler", raises=None, returns=None):
        self.name = name
        self.calls = []
        self.raises = raises
        self.returns = returns

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.returns

    @property
    def call_count(self):
        return len(self.calls)


class OrderRecordingDict(dict):
    """A ``dict`` that remembers the order keys were assigned.

    Used to assert the publication order Fio's lock-free caches depend on: a
    reader on another thread may observe the dict between two stores, so which
    store lands first is load-bearing (see
    :func:`engine.constants.brush_aabb_bounds`).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_log = []

    def __setitem__(self, key, value):
        self.write_log.append(key)
        super().__setitem__(key, value)
