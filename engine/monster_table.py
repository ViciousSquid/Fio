"""The dense monster projection: what the AI computes over, as columns.

:mod:`engine.render_table` and :mod:`engine.entity_table` gave the renderer
dense rows so it stopped asking objects what they were every frame. The monster
AI was the last per-object hot path: a Python body per monster per tick that
built ``glm`` vectors, walked grid buckets for its ground and its walls, and
searched the other monsters for crossfire -- 34 ms per tick at 1000 monsters,
more than a core at 30 Hz.

This table is the AI's execution representation, in the same shape as the
others:

* **Monster objects stay authoritative.** Positions and flags are gathered
  from them at the start of a tick (one list comprehension per column, never a
  per-monster body) and results are scattered back to them at the end, so
  I/O, savegames, the editor and the renderer see exactly the objects they
  always saw. ``MonsterAI.monster_states`` stays the per-monster state record
  (savegames and patrol read it); its four numeric fields are gathered and
  scattered with the rest.
* **Rows are monsters in ``LogicThread._monster_things`` order**, which is also
  the order the enemy-search batch and the old per-monster loop used, so
  "first in order" means the same monster everywhere.
* **Per-tick columns are what the kernels produce** -- the resolved target,
  squared distance, ground height, the mode each row took this tick, whether a
  line-of-sight ray was cast for it -- kept rather than thrown away so Debug
  Tables can show them.

Nothing here decides behaviour; :meth:`engine.monster_ai.MonsterAI.update` does.
"""

from __future__ import annotations

import numpy as np

#: What each row did this tick (``mode`` column; Debug Tables names them).
MODE_SKIPPED = 0      # hidden or disabled
MODE_DEAD = 1         # dead: falls to the ground
MODE_ASLEEP = 2       # not awake, and nothing woke it
MODE_CHASE = 3        # target in sight range: moves, may fire
MODE_SEARCH = 4       # target out of range: patrol / investigate (Python)
MODE_SCALAR = 5       # an irregular case run through the per-monster path
MODE_WAITING = 6      # a scripted ambush waiting for its I/O trigger

MODE_NAMES = ('skipped', 'dead', 'asleep', 'chase', 'search', 'scalar',
              'waiting')

#: No target resolved / the target is the player / the target is a monster row.
TARGET_NONE = -2
TARGET_PLAYER = -1

#: Every column: ``(name, trailing shape, dtype, fill)``.
_COLUMNS = (
    ('pos', (3,), np.float64, 0.0),
    ('team', (), np.int32, -1),
    ('hidden', (), bool, False),
    ('disabled', (), bool, False),
    ('dead', (), bool, False),
    ('awake', (), bool, False),
    ('triggered', (), bool, False),
    ('wake_on_sight', (), bool, True),
    ('can_hear', (), bool, False),
    ('flying', (), bool, False),
    ('sprite_height', (), np.float64, 128.0),
    ('shoot_timer', (), np.float64, 0.0),
    ('anim_timer', (), np.float64, 0.0),
    ('in_sight', (), bool, False),
    ('vel_y', (), np.float64, 0.0),
    ('target', (), np.int32, TARGET_NONE),
    ('target_pos', (3,), np.float32, 0.0),
    ('dist_sq', (), np.float32, 0.0),
    ('ground_y', (), np.float64, np.nan),
    ('mode', (), np.uint8, MODE_SKIPPED),
    ('ray_cast', (), bool, False),
    ('fired', (), bool, False),
)


class MonsterTable:
    """Dense columns over the live monster list, rebuilt as it changes."""

    __slots__ = tuple(name for name, *_ in _COLUMNS) + (
        'count', 'generation', 'monsters', 'props', 'row_of', 'slot_of_id',
        'team_names', 'rows_read', 'rays_cast', 'python_rows', 'phase_ms',
        'path', '_live')

    def __init__(self):
        self.count = 0
        self.generation = 0
        self.monsters = []
        self.props = []
        #: ``id(monster) -> row``: how an ``_aggro_target`` id becomes a row.
        self.row_of = {}
        #: ``properties['id'] -> row``, for Debug Tables' follow-selection.
        self.slot_of_id = {}
        self.team_names = []
        #: Monsters read into the table last tick (Debug Tables).
        self.rows_read = 0
        #: Line-of-sight rays cast last tick, and rows that needed Python.
        self.rays_cast = 0
        self.python_rows = 0
        #: Wall-clock milliseconds per phase of the last dense tick (Debug
        #: Tables): gather, fall, wake, classify, chase, events.
        self.phase_ms = {}
        #: Which pass ran last tick: ``'dense'``, or why it fell back to the
        #: per-monster path (then the columns are last dense tick's).
        self.path = 'not run'
        #: Who crossfire may hit this tick; read on the first shot of a tick.
        self._live = None
        for name, shape, dtype, fill in _COLUMNS:
            setattr(self, name, np.full((0,) + shape, fill, dtype=dtype))

    # -- rows ----------------------------------------------------------------

    def _resize(self, n):
        if n <= len(self.pos):
            return
        capacity = max(n, 16, len(self.pos) * 2)
        for name, shape, dtype, fill in _COLUMNS:
            old = getattr(self, name)
            new = np.full((capacity,) + shape, fill, dtype=dtype)
            new[:len(old)] = old
            setattr(self, name, new)

    def _reconcile(self, monsters):
        """New row set: the list changed identity or length."""
        n = len(monsters)
        self._resize(n)
        self.monsters = list(monsters)
        self.row_of = {id(m): row for row, m in enumerate(self.monsters)}
        self.slot_of_id = {}
        for row, m in enumerate(self.monsters):
            ident = m.properties.get('id')
            if ident:
                self.slot_of_id[ident] = row
        self.count = n
        self.generation += 1

    # -- per tick ------------------------------------------------------------

    def gather(self, monsters):
        """Read this tick's positions and flags from the live monsters.

        One comprehension per column: the per-monster work is a dict read,
        not a behaviour. Everything the AI's decisions depend on that can
        change between ticks -- flags written by I/O, teams set by a plugin,
        positions moved by physics or the editor -- is read here, fresh.
        """
        # The rows are compared by identity, element by element (one C loop:
        # Monster defines no __eq__). Keying on (id(list), len) was not enough:
        # the logic thread rebuilds its monster list as a new list, CPython
        # readily gives a new list a freed one's address, and two rebuilds
        # between ticks with the same count left the AI driving -- and
        # scattering results onto -- the previous monster objects.
        if type(monsters) is not list:
            monsters = list(monsters)
        if len(monsters) != self.count or monsters != self.monsters:
            self._reconcile(monsters)
        n = self.count
        monsters = self.monsters
        props = self.props = [m.properties for m in monsters]
        self.rows_read = n
        if not n:
            return
        self.pos[:n] = [m.pos for m in monsters]

        def flag(column, key, default=False):
            column[:n] = np.fromiter((bool(p.get(key, default)) for p in props),
                                     dtype=bool, count=n)

        flag(self.hidden, 'hidden')
        flag(self.disabled, 'disabled')
        flag(self.dead, 'dead')
        flag(self.awake, 'awake')
        flag(self.triggered, 'triggered')
        flag(self.wake_on_sight, 'wake_on_sight', True)
        flag(self.can_hear, 'can_hear')
        self.flying[:n] = np.fromiter(
            (p.get('monster_type', 'human') == 'flying' for p in props),
            dtype=bool, count=n)
        self.sprite_height[:n] = [p.get('sprite_height', 128) for p in props]
        codes = {}
        names = []
        team = self.team[:n]
        for row, p in enumerate(props):
            name = p.get('team', '')
            if name:
                code = codes.get(name)
                if code is None:
                    code = codes[name] = len(names)
                    names.append(name)
                team[row] = code
            else:
                team[row] = -1
        self.team_names = names
        self.target[:n] = TARGET_NONE
        self.dist_sq[:n] = 0.0
        self.ground_y[:n] = np.nan
        self.mode[:n] = MODE_SKIPPED
        self.ray_cast[:n] = False
        self.fired[:n] = False
        self.rays_cast = 0
        self.python_rows = 0
        self._live = None

    def gather_states(self, rows, states):
        """The numeric AI state of *rows*, from their ``monster_states`` dicts."""
        if not len(rows):
            return
        self.shoot_timer[rows] = [s['shoot_timer'] for s in states]
        self.anim_timer[rows] = [s['anim_timer'] for s in states]
        self.in_sight[rows] = [bool(s['in_sight']) for s in states]
        self.vel_y[rows] = [s.get('vel_y', 0.0) for s in states]

    def crossfire(self, shooter, ray_start, ray_end, radius=80.0):
        """The first monster a shot from *shooter* at *ray_end* passes through.

        The per-monster walk this replaces, as one expression over the rows:
        same sphere (radius 80, 64 above the monster's origin), same
        exclusions (the shooter, dead or hidden monsters, the shooter's team)
        and the same answer -- the nearest entry point, the earliest row on a
        tie.

        Who is alive is read once per tick, not once per shot: when shooters'
        timers line up, hundreds of shots land in one tick, and reading every
        monster's flags for every shot was 300 ms of a single tick at 1000
        monsters. A shot earlier in the tick can still kill a monster, so the
        monster a shot would hit is re-read before it is returned, and one
        that has died since is struck off for the rest of the tick. (A monster
        that I/O *un*-hides mid-tick joins the crossfire test on the next
        tick.)
        """
        n = self.count
        start = np.asarray(ray_start, dtype=np.float64)
        direction = np.asarray(ray_end, dtype=np.float64) - start
        length = float(np.sqrt(direction @ direction))
        if length < 1.0 or not n:
            return None
        direction /= length
        props = self.props
        live = self._live
        if live is None:
            live = self._live = np.fromiter(
                (not (p.get('dead', False) or p.get('hidden', False)) for p in props),
                dtype=bool, count=n)
        candidates = live.copy()
        candidates[shooter] = False
        shooter_team = self.team[shooter]
        if shooter_team >= 0:
            candidates &= self.team[:n] != shooter_team
        rows = np.flatnonzero(candidates)
        if not len(rows):
            return None
        centre = self.pos[rows].copy()
        centre[:, 1] += 64.0
        oc = start - centre
        b = 2.0 * (oc @ direction)
        c = np.einsum('ij,ij->i', oc, oc) - radius * radius
        disc = b * b - 4.0 * c
        t = np.full(len(rows), np.inf)
        hit = disc >= 0.0
        t[hit] = (-b[hit] - np.sqrt(disc[hit])) / 2.0
        entered = np.flatnonzero((t > 0.0) & (t < length))
        # Nearest entry first, earliest row on a tie; almost always the first
        # candidate is still alive and the loop runs once.
        for i in entered[np.lexsort((rows[entered], t[entered]))]:
            row = int(rows[i])
            p = props[row]
            if p.get('dead', False) or p.get('hidden', False):
                live[row] = False
                continue
            return self.monsters[row]
        return None
