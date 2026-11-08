"""Movers and doors as dense arrays: their runtime state, advanced per tick.

A mover's motion is closed-form. Its position is its original position plus
its direction times its distance, eased by one progress value that its speed
advances each tick. A door is the same with no easing and a four-state
cycle. So every mover and every door on a map fits in a handful of NumPy
columns, and one vectorised pass moves them all. The renderer then takes
their positions in a single fancy store (:meth:`MoverTable.publish`) instead of
re-reading every mover's brush dict every frame.

The Python loop this replaces had to be reproduced exactly, not just
approximately, and that is most of what is here:

* **I/O is synchronous.** When a mover reaches its end, ``fire_output`` runs
  the connected inputs *during* the loop. An input that starts, stops or
  reverses a later mover affects it on this same tick; one that reaches an
  earlier mover affects it on the next. So the vectorised pass is only
  *tentative*. A row that fires an event is a *sequence point*: rows before it
  are committed, its event runs, and rows after it are recomputed if the event
  changed any of them.
* **Some rows are always taken one at a time**, in list order: path-following
  movers (``LogicMovers._update_mover_path``, which walks named nodes), rows
  with no state yet (created lazily, as the loop did), and rows whose direction
  has not been resolved yet (resolved on first use, as the loop did).
* **The state dicts stay the interface.** I/O handlers and saved games read and
  write ``logic.mover_runtime.mover_states[i]`` /
  ``logic.mover_runtime.door_states[i]`` as dicts.
  :class:`StateView` and :class:`RowState` are mappings over the columns, so
  that code is unchanged. A state set for an index that is not a row (a door
  opened outside play mode) is kept as the plain dict it was given.
* **Brush parameters stay in the brush.** ``start_on``, ``speed``,
  ``distance``, ``open_time`` and the rest are copied into columns when the
  rows are built. They are copied again only for rows the change journal says
  were touched, and every I/O input journals its target.

**The table owns the runtime transform** (unified brush model, Stage 2b). A
row's ``original`` is its brush's authored centre, from
:mod:`engine.brush_placement`; ``pos`` is where it is now; a mover's spin
lives in ``rot_angle``. Nothing is written into the brush dict: ``pos`` stays
the authored placement, and ``original_pos``, ``_rot_angle`` and
``rotation_yaw`` are not written at all. The table is a registered runtime
source (:func:`engine.brush_placement.register_runtime`): every reader --
rendering, collision, picking, parented lights and portals, saved games,
plugins -- asks the resolver, which answers from the per-brush
``(centre, spin)`` the table refreshes for the rows that moved
(:meth:`MoverTable.runtime_of`).
"""

from __future__ import annotations

import weakref
from collections.abc import MutableMapping
from itertools import repeat

import numpy as np

from engine import brush_placement
from engine.change_journal import JOURNAL, OVERFLOW

# Door states. OTHER keeps any unrecognised string as-is.
CLOSED, OPENING, OPEN, CLOSING, STOPPED, OTHER = range(6)
_DOOR_CODES = {'closed': CLOSED, 'opening': OPENING, 'open': OPEN,
               'closing': CLOSING, 'stopped': STOPPED}
_DOOR_NAMES = {code: name for name, code in _DOOR_CODES.items()}

# Events a tentative step can produce.
_NO_EVENT, _FULLY_OPEN, _FULLY_CLOSED, _CLOSE = range(4)

#: The axis a runtime transform names when it does not turn.
_UP = np.array([0.0, 1.0, 0.0])

_EVENT_NAMES = {_FULLY_OPEN: 'OnFullyOpen', _FULLY_CLOSED: 'OnFullyClosed',
                _CLOSE: 'OnClose'}


def _eased(t):
    """The movers' ease-in-out, elementwise; the same arithmetic as the loop's.

    ``np.float_power`` and not ``np.power``: the scalar ease calls ``pow``,
    which is libm's, and ``float_power`` evaluates with it. ``np.power`` takes
    a SIMD path that differs from libm in the last bit for about a quarter of
    inputs, and a mover would then drift from where it used to be.
    """
    return np.where(t < 0.5, 4 * t * t * t, 1 - np.float_power(-2 * t + 2, 3) / 2)


def _unit_direction(raw):
    """A direction normalised exactly as the loop did, as a float tuple."""
    d = np.array(raw, dtype=float)
    length = np.linalg.norm(d)
    if length > 0:
        d = d / length
    return (float(d[0]), float(d[1]), float(d[2]))


class RowState(MutableMapping):
    """One row's state, read and written as the dict it used to be."""

    __slots__ = ('_group', '_row')

    def __init__(self, group, row):
        self._group = group
        self._row = row

    def __getitem__(self, key):
        return self._group.get_field(self._row, key)

    def __setitem__(self, key, value):
        self._group.set_field(self._row, key, value)
        self._group.dirty.add(self._row)

    def __delitem__(self, key):
        self._group.del_field(self._row, key)
        self._group.dirty.add(self._row)

    def __iter__(self):
        return iter(self._group.keys(self._row))

    def __len__(self):
        return len(self._group.keys(self._row))

    def __repr__(self):
        return repr(dict(self))


class StateView(MutableMapping):
    """``brush index -> state`` over a group's rows (and any stray dicts)."""

    __slots__ = ('_group',)

    def __init__(self, group):
        self._group = group

    def __getitem__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None and group.has_state[row]:
            return RowState(group, row)
        return group.fallback[index]

    def __setitem__(self, index, state):
        group = self._group
        row = group.row_of_index.get(index)
        if row is None:
            group.fallback[index] = state
        else:
            group.load_state(row, state)
            group.dirty.add(row)

    def __delitem__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None and group.has_state[row]:
            group.has_state[row] = False
            group.dirty.add(row)
        else:
            del group.fallback[index]

    def __contains__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None and group.has_state[row]:
            return True
        return index in group.fallback

    def __iter__(self):
        group = self._group
        for row in np.flatnonzero(group.has_state).tolist():
            yield group.index[row]
        yield from list(group.fallback)

    def __len__(self):
        return int(self._group.has_state.sum()) + len(self._group.fallback)

    def __repr__(self):
        return repr(dict(self.items()))


class _Group:
    """Rows shared by movers and doors: identity, position, state bookkeeping."""

    #: Public state keys, in the order the engine creates them.
    KEYS: tuple = ()

    #: Below this many rows a group walks its rows one by one through
    #: :meth:`_one` -- the loop's own arithmetic, which the vectorised pass
    #: reproduces bit for bit -- instead of planning: the plan is some
    #: forty NumPy calls whatever the row count, which with a handful of
    #: rows costs several times the rows themselves. Set per group from
    #: ``tests/performance/test_mover_threshold_benchmark.py``.
    WALK_ROWS_BELOW = 0

    def __init__(self):
        self.source = None
        self.fallback = {}
        self.dirty = set()
        self.states = StateView(self)
        #: The MoverTable this group belongs to (its runtime source).
        self.table = None
        self._build([])

    # -- rows ----------------------------------------------------------------

    def _build(self, entries):
        n = len(entries)
        self.index = [i for i, _ in entries]
        self.brushes = [brush for _, brush in entries]
        self.row_of_index = {i: row for row, i in enumerate(self.index)}
        self.row_of_obj = {id(brush): row for row, brush in enumerate(self.brushes)}
        self.pos = np.zeros((n, 3))
        self.original = np.zeros((n, 3))
        self.has_state = np.zeros(n, dtype=bool)
        self.progress = np.zeros(n)
        self.direction = np.zeros((n, 3))
        self.dir_valid = np.zeros(n, dtype=bool)
        self.present = np.zeros(n, dtype=np.int32)
        self.extras = [None] * n
        #: The row holding the same brush in the other group (a brush that is
        #: both a mover and a door), or -1; see MoverTable._link.
        self.twin_row = np.full(n, -1, dtype=np.intp)
        self.twin = None
        #: Whether any row has a twin (almost never): the publish skips the
        #: twin gather without it.
        self.any_twin = False
        self._build_columns(n)
        for row in range(n):
            self.resync(row)
        if self.table is not None:
            self.table._adopt(self)
        #: Moves each time the rows are rebuilt, so a renderer slot map built
        #: against the old rows is recognised as stale.
        self.version = getattr(self, 'version', 0) + 1

    def ensure(self, entries):
        """Rows for *entries* (``[(brush index, brush), ...]``), states kept.

        Rebuilt only when handed a different list, never when that list is
        merely the same one as before.
        """
        if entries is self.source and len(entries) == len(self.index):
            return
        kept = {i: dict(state) for i, state in self.states.items()}
        # Where each surviving brush has got to: a rebuild must not snap a
        # path mover (whose position is no function of its state) home.
        placed = {id(brush): (brush, self.pos[row] - self.original[row])
                  for row, brush in enumerate(self.brushes)}
        self.fallback = {}
        self._build(list(entries))
        self.source = entries
        for i, state in kept.items():
            self.states[i] = state
        moved = []
        for row, brush in enumerate(self.brushes):
            entry = placed.get(id(brush))
            if entry is not None and entry[0] is brush and entry[1].any():
                self.pos[row] = self.original[row] + entry[1]
                moved.append(row)
        if moved and self.table is not None:
            self.table._publish(self, moved)
        self.dirty.clear()

    def replace_states(self, entries, states):
        """``logic.X_states = {...}``: these states, and no others."""
        self.ensure(entries)
        self.has_state[:] = False
        self.fallback = {}
        for index, state in dict(states).items():
            self.states[index] = state
        self.dirty.clear()

    def resync(self, row):
        """Copy row *row*'s brush parameters and authored centre into the
        columns. Only an authored centre that has changed (an edit in play)
        rebases the row, keeping the displacement it has travelled; an
        unchanged one leaves its position exactly as it is."""
        brush = self.brushes[row]
        authored = brush_placement.authored_box(brush)[:3]
        if tuple(self.original[row].tolist()) != authored:
            travelled = self.pos[row] - self.original[row]
            self.original[row] = authored
            self.pos[row] = self.original[row] + travelled

    # -- state as a mapping ----------------------------------------------------

    def load_state(self, row, state):
        self.has_state[row] = True
        self.present[row] = 0
        self.extras[row] = None
        self._reset_fields(row)
        for key, value in dict(state).items():
            self.set_field(row, key, value)

    def keys(self, row):
        bits = int(self.present[row])
        out = [key for bit, key in enumerate(self.KEYS) if bits & (1 << bit)]
        extras = self.extras[row]
        if extras:
            out.extend(extras)
        return out

    def get_field(self, row, key):
        bit = self._bit(key)
        if bit is None:
            extras = self.extras[row]
            if extras is None or key not in extras:
                raise KeyError(key)
            return extras[key]
        if not self.present[row] & (1 << bit):
            raise KeyError(key)
        return self._get(row, key)

    def set_field(self, row, key, value):
        bit = self._bit(key)
        if bit is None:
            if self.extras[row] is None:
                self.extras[row] = {}
            self.extras[row][key] = value
            return
        self._set(row, key, value)
        self.present[row] |= 1 << bit

    def del_field(self, row, key):
        bit = self._bit(key)
        if bit is None:
            extras = self.extras[row]
            if extras is None or key not in extras:
                raise KeyError(key)
            del extras[key]
            return
        if not self.present[row] & (1 << bit):
            raise KeyError(key)
        self.present[row] &= ~(1 << bit)
        self._unset(row, key)

    def _bit(self, key):
        try:
            return self.KEYS.index(key)
        except ValueError:
            return None

    def _set_direction(self, row, value):
        self.direction[row] = value
        self.dir_valid[row] = True
        self.present[row] |= 1 << self.KEYS.index('_direction_np')

    # -- the tick ------------------------------------------------------------

    def take_dirty(self):
        dirty, self.dirty = self.dirty, set()
        return dirty

    def _place(self, row, position, angle):
        """Put a freshly built row where a saved game left it (its centre;
        *angle* is a mover's spin)."""
        self.pos[row] = position
        if hasattr(self, 'rot_angle'):
            self.rot_angle[row] = angle

    def _write_positions(self, rows, new_pos, ride, spun=None):
        """Store *new_pos* for the *rows* that moved, and publish them and
        the *spun* rows (whose angle the caller has just stored) together.

        *ride* is ``(player, ground row)`` or None: the player standing on a
        processed row is carried by its movement, as the loop did.
        """
        if ride is not None:
            player, ground = ride
            hit = np.flatnonzero(rows == ground)
            if len(hit):
                self._carry(player, ground, new_pos[hit[0]])
        changed = np.flatnonzero((new_pos != self.pos[rows]).any(axis=1))
        moved = rows[changed]
        values = new_pos[changed]
        if len(changed):
            self._store(moved, values, publish=False)
        if self.table is None:
            return
        if spun is not None and len(spun):
            moved = np.union1d(moved, spun) if len(moved) else spun
            if len(moved):
                self.table._publish(self, moved)
        elif len(moved):
            self.table._publish(self, moved, here=values)

    def _store_one(self, row, new, rest):
        """:meth:`_store` for one row: *new* its position and *rest* its
        authored centre, in Python floats."""
        self._store(np.array([row]), np.array([new]), publish=False)
        table = self.table
        if table is not None:
            table._publish_row_now(self, row, new, rest)

    def _store(self, rows, values, publish=True):
        """Where *rows* are now, kept for both groups a brush is in, and
        published to the resolver (:meth:`MoverTable.runtime_of`)."""
        self.pos[rows] = values
        twins = self.twin_row[rows]
        shared = twins >= 0
        if shared.any():
            mirror = self.twin.pos          # an array, not an entity
            mirror[twins[shared]] = np.asarray(values)[shared]
        if publish and self.table is not None:
            self.table._publish(self, np.atleast_1d(rows).tolist())

    def _carry(self, player, row, new):
        """Carry *player* by the row's move from where it is to *new*."""
        import glm
        cur = self.pos[row]
        player.pos += glm.vec3(float(new[0]) - float(cur[0]),
                               float(new[1]) - float(cur[1]),
                               float(new[2]) - float(cur[2]))

    def _ride(self, logic):
        player = logic.player_runtime.player
        if not player:
            return None
        ground = getattr(player, 'ground_object', None)
        row = self.row_of_obj.get(id(ground))
        if row is None or self.brushes[row] is not ground:
            return None
        return player, row

    def _walk(self, logic, delta, table):
        """One pass in list order: vectorised between sequence points."""
        self.dirty.clear()
        if not self.index:
            # Most maps have no movers or no doors. With no rows the plan is
            # empty and the pass below commits nothing, but building it cost
            # some thirty NumPy calls per group per tick.
            return
        io = logic.io_manager
        ride = self._ride(logic)
        version = self.version
        if len(self.index) < self.WALK_ROWS_BELOW:
            self._walk_rows(logic, delta, table, io, ride, version)
            return
        cursor = 0
        plan = self._plan(logic, cursor, delta, io)
        while True:
            seq_rows = plan['seq']
            at = np.searchsorted(seq_rows, cursor)
            stop = int(seq_rows[at]) if at < len(seq_rows) else len(self.index)
            self._commit(plan, cursor, stop, ride)
            if stop >= len(self.index):
                return
            ran = self._one(logic, stop, delta, io, ride)
            cursor = stop + 1
            if self.version != version:
                return          # an input rebuilt the rows (a level change)
            if ran:
                table.sync()
                # An input fired at the sequence point reaches this group's
                # dirty set through table.sync() (the change journal) or the
                # state views, so only a later dirty row can change the plan
                # for the rest of the pass. Re-planning after every sequence
                # point cost ~0.3 ms a tick on the showcase map and, checked
                # against a fresh plan at 115k skipped re-plans in the
                # equivalence oracle's worlds, never changed one.
                changed = self.take_dirty()
                if any(row >= cursor for row in changed):
                    plan = self._plan(logic, cursor, delta, io)


    def _walk_rows(self, logic, delta, table, io, ride, version):
        """The pass for a small group: every taking-part row through
        :meth:`_one`, in list order -- the loop the plan reproduces, so an
        input a row fires reaches the later rows on this tick as it does
        there (each row's part is decided when its turn comes)."""
        for row in range(len(self.index)):
            if not self._takes_part(row):
                continue
            ran = self._one(logic, row, delta, io, ride)
            if self.version != version:
                return          # an input rebuilt the rows (a level change)
            if ran:
                table.sync()
                self.take_dirty()


class LinearMovers(_Group):
    """Movers that travel back and forth along their direction (and spin)."""

    KEYS = ('progress', 'forward', '_direction_np')

    #: Measured (CPython 3.13, NumPy 2.5.3, quiet cores): one tick of 1 / 2 /
    #: 4 / 8 / 12 moving rows costs 10 / 18 / 31 / 63 / 92 us walked and 77 /
    #: 82 / 86 / 80 / 82 us planned; the plan wins from 12 rows (32: 233 vs
    #: 84 us).
    WALK_ROWS_BELOW = 12

    def _build_columns(self, n):
        self.move_once = np.zeros(n, dtype=bool)
        self.start_on = np.zeros(n, dtype=bool)
        self.rotate = np.zeros(n, dtype=bool)
        self.speed = np.zeros(n)
        self.spin_speed = np.zeros(n)
        self.distance = np.zeros(n)
        self.rot_angle = np.zeros(n)
        self.rot_axis = np.zeros((n, 3))
        self.forward = np.ones(n, dtype=bool)

    def resync(self, row):
        super().resync(row)
        brush = self.brushes[row]
        self.move_once[row] = bool(brush.get('move_once', False))
        self.start_on[row] = bool(brush.get('start_on', False))
        self.rotate[row] = bool(brush.get('rotate', False))
        self.speed[row] = brush.get('speed', 64.0)
        self.spin_speed[row] = brush.get('speed', 45.0)
        self.distance[row] = brush.get('distance', 128.0)
        # The spin angle is runtime state, kept in the column (a rebuild
        # starts it at zero, a saved game restores it); the axis is authored.
        self.rot_axis[row] = brush.get('rot_axis') or (0.0, 1.0, 0.0)

    def _reset_fields(self, row):
        self.progress[row] = 0.0
        self.forward[row] = True
        self.dir_valid[row] = False

    def _get(self, row, key):
        if key == 'progress':
            return float(self.progress[row])
        if key == 'forward':
            return bool(self.forward[row])
        return tuple(float(v) for v in self.direction[row])

    def _set(self, row, key, value):
        if key == 'progress':
            self.progress[row] = value
        elif key == 'forward':
            self.forward[row] = bool(value)
        else:
            self.direction[row] = value
            self.dir_valid[row] = True

    def _unset(self, row, key):
        if key == '_direction_np':
            self.dir_valid[row] = False

    # -- planning ------------------------------------------------------------

    def _takes_part(self, row):
        """Whether *row* moves this tick: what :meth:`_plan` calls active."""
        return bool(self.start_on[row]) and not self.move_once[row]

    def _plan(self, logic, start, delta, io):
        rows = np.arange(start, len(self.index))
        active = rows[~self.move_once[start:] & self.start_on[start:]]
        on_path = np.zeros(len(self.index), dtype=bool)
        for i in logic.mover_runtime.mover_path_states:
            row = self.row_of_index.get(i)
            if row is not None:
                on_path[row] = True
        path_rows = active[on_path[active]]
        rest = active[~on_path[active]]
        special = rest[~(self.has_state[rest] & self.dir_valid[rest])]
        simple = rest[self.has_state[rest] & self.dir_valid[rest]]
        step = self._step(simple, delta)
        seq = [path_rows, special]
        if io:
            seq.append(simple[step['event'] != _NO_EVENT])
        step['seq'] = np.unique(np.concatenate(seq)).astype(np.intp)
        step['rows'] = simple
        return step

    def _step(self, rows, delta):
        """Tentative new state for *rows*, every one of them at once."""
        spin = self.rotate[rows]
        new_angle = np.mod(self.rot_angle[rows] + self.spin_speed[rows] * delta, 360.0)
        distance = self.distance[rows]
        step_size = np.zeros(len(rows))
        np.divide(self.speed[rows] * delta, distance, out=step_size,
                  where=distance > 0)
        progress = self.progress[rows]
        forward = self.forward[rows]
        was_end = progress >= 1.0
        was_start = progress <= 0.0
        new = np.where(forward, progress + step_size, progress - step_size)
        hit_end = forward & (new >= 1.0)
        hit_start = ~forward & (new <= 0.0)
        new[hit_end] = 1.0
        new[hit_start] = 0.0
        event = np.full(len(rows), _NO_EVENT, dtype=np.int8)
        event[hit_end & ~was_end] = _FULLY_OPEN
        event[hit_start & ~was_start] = _FULLY_CLOSED
        eased = _eased(new)
        pos = self.original[rows] + (self.direction[rows] * distance[:, None]) * eased[:, None]
        return {'spin': spin, 'angle': new_angle, 'progress': new,
                'forward': (forward & ~hit_end) | hit_start,
                'event': event, 'pos': pos}

    def _commit(self, plan, start, stop, ride):
        rows = plan['rows']
        lo, hi = np.searchsorted(rows, [start, stop])
        if lo == hi:
            return
        pick = slice(lo, hi)
        rows = rows[pick]
        spin = plan['spin'][pick]
        spun = None
        if spin.any():
            spun = rows[spin]
            self.rot_angle[spun] = plan['angle'][pick][spin]
        self.progress[rows] = plan['progress'][pick]
        self.forward[rows] = plan['forward'][pick]
        # The spun rows are published with the moved ones, once.
        self._write_positions(rows, plan['pos'][pick], ride, spun)

    # -- one row, in order ---------------------------------------------------

    def _one(self, logic, row, delta, io, ride):
        """Advance one row exactly as the loop did. Returns whether it ran code
        that could have changed another row (I/O, or a path step)."""
        brush = self.brushes[row]
        i = self.index[row]
        if i in logic.mover_runtime.mover_path_states:
            # Moves the row itself (MoverTable.move_to).
            logic.mover_runtime._update_mover_path(i, brush, delta)
            return True

        # A new spin is published with the new position below, or before an
        # output fires if one does first (an input may read the brush).
        spun = False
        if self.rotate[row]:
            angle = (float(self.rot_angle[row]) + float(self.spin_speed[row]) * delta) % 360.0
            self.rot_angle[row] = angle
            spun = self.table is not None
        if not self.has_state[row]:
            self.load_state(row, {'progress': 0.0, 'forward': True})
        speed = float(self.speed[row])
        distance = float(self.distance[row])
        if not self.dir_valid[row]:
            self._set_direction(row, _unit_direction(brush.get('direction', [0, 1, 0])))
        direction = tuple(float(v) for v in self.direction[row])
        step = (speed * delta) / distance if distance > 0 else 0
        progress = float(self.progress[row])
        was_end = progress >= 1.0
        was_start = progress <= 0.0
        fired = False
        if self.forward[row]:
            progress += step
            self.progress[row] = progress
            if progress >= 1.0:
                self.progress[row] = 1.0
                self.forward[row] = False
                if not was_end and io:
                    if spun:
                        self.table._publish(self, [row])
                    io.fire_output(brush, 'OnFullyOpen')
                    fired = True
        else:
            progress -= step
            self.progress[row] = progress
            if progress <= 0.0:
                self.progress[row] = 0.0
                self.forward[row] = True
                if not was_start and io:
                    if spun:
                        self.table._publish(self, [row])
                    io.fire_output(brush, 'OnFullyClosed')
                    fired = True
        t = float(self.progress[row])
        eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
        original = self.original[row].tolist()
        new = (original[0] + (direction[0] * distance) * eased,
               original[1] + (direction[1] * distance) * eased,
               original[2] + (direction[2] * distance) * eased)
        if ride is not None and ride[1] == row:
            self._carry(ride[0], row, new)
        self._store_one(row, new, original)
        return fired


class Doors(_Group):
    """Doors: open, wait, close, on a progress value from 0 to 1."""

    KEYS = ('progress', 'state', 'open_timer', 'speed', 'distance', 'direction',
            '_direction_np')

    #: Measured as for LinearMovers: 1 / 2 / 4 / 8 / 12 cycling doors cost 14
    #: / 22 / 37 / 69 / 109 us walked and 65 / 67 / 75 / 78 / 86 us planned;
    #: the plan wins from 12 rows (32: 252 vs 109 us).
    WALK_ROWS_BELOW = 12

    def _build_columns(self, n):
        self.code = np.zeros(n, dtype=np.int8)
        self.other_state = [None] * n
        self.open_timer = np.zeros(n)
        self.speed = np.full(n, 128.0)
        self.distance = np.full(n, 128.0)
        self.raw_direction = [None] * n
        self.open_time = np.zeros(n)

    def resync(self, row):
        super().resync(row)
        self.open_time[row] = self.brushes[row].get('open_time', 3.0)

    def _reset_fields(self, row):
        self.progress[row] = 0.0
        self.code[row] = CLOSED
        self.other_state[row] = None
        self.open_timer[row] = 0.0
        self.speed[row] = 128.0
        self.distance[row] = 128.0
        self.raw_direction[row] = None
        self.dir_valid[row] = False

    def _get(self, row, key):
        if key == 'progress':
            return float(self.progress[row])
        if key == 'state':
            code = int(self.code[row])
            return self.other_state[row] if code == OTHER else _DOOR_NAMES[code]
        if key == 'open_timer':
            return float(self.open_timer[row])
        if key == 'speed':
            return float(self.speed[row])
        if key == 'distance':
            return float(self.distance[row])
        if key == 'direction':
            return self.raw_direction[row]
        return tuple(float(v) for v in self.direction[row])

    def _set(self, row, key, value):
        if key == 'progress':
            self.progress[row] = value
        elif key == 'state':
            code = _DOOR_CODES.get(value, OTHER) if isinstance(value, str) else OTHER
            self.code[row] = code
            self.other_state[row] = value if code == OTHER else None
        elif key == 'open_timer':
            self.open_timer[row] = value
        elif key == 'speed':
            self.speed[row] = value
        elif key == 'distance':
            self.distance[row] = value
        elif key == 'direction':
            self.raw_direction[row] = value
        else:
            self.direction[row] = value
            self.dir_valid[row] = True

    def _unset(self, row, key):
        if key == 'speed':
            self.speed[row] = 128.0
        elif key == 'distance':
            self.distance[row] = 128.0
        elif key == 'direction':
            self.raw_direction[row] = None
        elif key == '_direction_np':
            self.dir_valid[row] = False

    def _takes_part(self, row):
        """Whether *row* moves this tick: what :meth:`_plan` calls live."""
        return bool(self.has_state[row]) and not (
            self.code[row] == CLOSED and self.progress[row] == 0.0)

    def _plan(self, logic, start, delta, io):
        rows = np.arange(start, len(self.index))
        idle = (self.code[start:] == CLOSED) & (self.progress[start:] == 0.0)
        live = rows[self.has_state[start:] & ~idle]
        special = live[~self.dir_valid[live]]
        simple = live[self.dir_valid[live]]
        step = self._step(simple, delta)
        seq = [special]
        if io:
            seq.append(simple[step['event'] != _NO_EVENT])
        step['seq'] = np.unique(np.concatenate(seq)).astype(np.intp)
        step['rows'] = simple
        return step

    def _step(self, rows, delta):
        distance = self.distance[rows]
        step_size = np.zeros(len(rows))
        np.divide(self.speed[rows] * delta, distance, out=step_size,
                  where=distance > 0)
        code = self.code[rows].copy()
        progress = self.progress[rows].copy()
        timer = self.open_timer[rows].copy()
        event = np.full(len(rows), _NO_EVENT, dtype=np.int8)

        opening = code == OPENING
        progress[opening] += step_size[opening]
        done = opening & (progress >= 1.0)
        progress[done] = 1.0
        code[done] = OPEN
        timer[done] = self.open_time[rows][done]
        event[done] = _FULLY_OPEN

        waiting = self.code[rows] == OPEN
        timer[waiting] -= delta
        done = waiting & (timer <= 0)
        code[done] = CLOSING
        event[done] = _CLOSE

        closing = self.code[rows] == CLOSING
        progress[closing] -= step_size[closing]
        done = closing & (progress <= 0.0)
        progress[done] = 0.0
        code[done] = CLOSED
        event[done] = _FULLY_CLOSED

        pos = self.original[rows] + (self.direction[rows] * distance[:, None]) * progress[:, None]
        return {'code': code, 'progress': progress, 'timer': timer,
                'event': event, 'pos': pos}

    def _commit(self, plan, start, stop, ride):
        rows = plan['rows']
        lo, hi = np.searchsorted(rows, [start, stop])
        if lo == hi:
            return
        pick = slice(lo, hi)
        rows = rows[pick]
        self.code[rows] = plan['code'][pick]
        self.progress[rows] = plan['progress'][pick]
        self.open_timer[rows] = plan['timer'][pick]
        self._write_positions(rows, plan['pos'][pick], ride)

    def _one(self, logic, row, delta, io, ride):
        brush = self.brushes[row]
        speed = float(self.speed[row])
        distance = float(self.distance[row])
        open_time = brush.get('open_time', 3.0)
        if not self.dir_valid[row]:
            raw = self.raw_direction[row]
            self._set_direction(row, _unit_direction([0, 1, 0] if raw is None else raw))
        direction = tuple(float(v) for v in self.direction[row])
        step = (speed * delta) / distance if distance > 0 else 0
        code = int(self.code[row])
        event = None
        if code == OPENING:
            self.progress[row] = float(self.progress[row]) + step
            if self.progress[row] >= 1.0:
                self.progress[row] = 1.0
                self.code[row] = OPEN
                self.open_timer[row] = open_time
                event = 'OnFullyOpen'
        elif code == OPEN:
            self.open_timer[row] = float(self.open_timer[row]) - delta
            if self.open_timer[row] <= 0:
                self.code[row] = CLOSING
                event = 'OnClose'
        elif code == CLOSING:
            self.progress[row] = float(self.progress[row]) - step
            if self.progress[row] <= 0.0:
                self.progress[row] = 0.0
                self.code[row] = CLOSED
                event = 'OnFullyClosed'
        if event and io:
            io.fire_output(brush, event)
        progress = float(self.progress[row])
        original = self.original[row].tolist()
        new = (original[0] + (direction[0] * distance) * progress,
               original[1] + (direction[1] * distance) * progress,
               original[2] + (direction[2] * distance) * progress)
        if ride is not None and ride[1] == row:
            self._carry(ride[0], row, new)
        self._store_one(row, new, original)
        return bool(event and io)


class MoverTable:
    """Every mover and door in play, as dense columns the tick advances.

    Owned by the logic thread. :meth:`publish` hands the renderer their
    positions and spins as array stores.
    """

    def __init__(self):
        self.movers = LinearMovers()
        self.doors = Doors()
        self.movers.table = self
        self.doors.table = self
        # render table -> ((its generation, mover version, door version),
        # slots). One entry per table: frames alternate between the two render
        # buffers' tables, so a single entry would miss on every frame.
        self._slot_cache = weakref.WeakKeyDictionary()
        #: id(brush) -> (brush, (cx, cy, cz, ax, ay, az, angle)): the runtime
        #: transform of every row that is displaced or turned -- its position
        #: column as it is, and its spin -- refreshed for the rows that move
        #: (:meth:`_publish`). What the resolver reads.
        self.runtime_entries = {}
        #: id(brush) -> (brush, position, angle): saved-game placements
        #: restored before the brush's row exists (:meth:`restore_placement`).
        self._pending = {}
        JOURNAL.subscribe(self)
        brush_placement.register_runtime(self)

    # -- the runtime transform -----------------------------------------------

    def runtime_of(self, brush):
        """``(cx, cy, cz, ax, ay, az, angle)`` -- the row's position column
        as it is, and its spin -- for a displaced or turned row, or ``None``:
        the runtime source contract of :mod:`engine.brush_placement`."""
        entry = self.runtime_entries.get(id(brush))
        if entry is None or entry[0] is not brush:
            return None
        return entry[1]

    #: Below this many rows :meth:`_publish` goes row by row in scalars: the
    #: NumPy pack costs more than the few reads it saves.
    PUBLISH_ROWS_BELOW = 4

    def _publish(self, group, rows, entries=None, here=None):
        """:meth:`_publish_into`, then the resolver's stamp
        (:func:`engine.brush_placement.note_runtime_change`), so a reader
        that caches derived bounds knows to look again."""
        self._publish_into(group, rows, entries, here)
        if entries is None:
            brush_placement.note_runtime_change()

    def _publish_into(self, group, rows, entries=None, here=None):
        """Refresh the runtime transform of *group*'s *rows*, in
        :attr:`runtime_entries` or the dict *entries* being built. *here* is
        the rows' positions when the caller holds them.

        This runs on every tick for every moving row, so the per-row work --
        one ``(brush, transform)`` entry each -- is iterated in C
        (``dict.update`` over ``zip``/``map``) after one NumPy pack."""
        count = len(rows)
        if not count:
            return
        runtime = self.runtime_entries if entries is None else entries
        if count < self.PUBLISH_ROWS_BELOW:
            centres = (None if here is None
                       else np.asarray(here, dtype=np.float64).tolist())
            for k, row in enumerate(rows):
                self._publish_row(group, int(row), runtime,
                                  None if centres is None else centres[k])
            return
        pick = np.asarray(rows, dtype=np.intp)
        here = group.pos[pick] if here is None else np.asarray(here, dtype=np.float64)
        movers = self.movers
        angle = spin_at = None
        if group is movers:
            angle, spin_at = movers.rot_angle[pick], pick
        elif group.any_twin:
            twins = group.twin_row[pick]
            spin_at = np.where(twins >= 0, twins, 0)
            angle = np.where(twins >= 0, movers.rot_angle[spin_at], 0.0)
        live = (here != group.original[pick]).any(axis=1)
        x, y, z = here.T.tolist()
        turned = None if angle is None else angle != 0.0
        if turned is None or not turned.any():
            # Nothing turns: the default axis, and each row's angle (0).
            values = zip(x, y, z, repeat(0.0), repeat(1.0), repeat(0.0),
                         repeat(0.0) if angle is None else angle.tolist())
        else:
            live |= turned
            ax, ay, az = np.where(turned[:, None], movers.rot_axis[spin_at], _UP).T.tolist()
            values = zip(x, y, z, ax, ay, az, angle.tolist())
        brushes = group.brushes
        chosen = [brushes[row] for row in pick.tolist()]
        if live.all():
            # Tuples built and stored in C: no bytecode per row.
            runtime.update(zip(map(id, chosen), zip(chosen, values)))
            return
        for brush, transform, keep in zip(chosen, values, live.tolist()):
            if keep:
                runtime[id(brush)] = (brush, transform)
            else:
                runtime.pop(id(brush), None)

    def _publish_row(self, group, row, runtime, centre=None, rest=None):
        """:meth:`_publish` for one row, in scalars (*centre* and *rest*:
        its position and authored centre, when the caller holds them)."""
        brush = group.brushes[row]
        if centre is None:
            cx, cy, cz = group.pos[row].tolist()
        else:
            cx, cy, cz = centre
        movers = self.movers
        if group is movers:
            spin_row = row
        elif group.any_twin:
            spin_row = int(group.twin_row[row])
        else:
            spin_row = -1
        angle = float(movers.rot_angle[spin_row]) if spin_row >= 0 else 0.0
        if angle:
            ax, ay, az = movers.rot_axis[spin_row].tolist()
        else:
            rx, ry, rz = group.original[row].tolist() if rest is None else rest
            if cx == rx and cy == ry and cz == rz:
                runtime.pop(id(brush), None)
                return
            ax, ay, az = 0.0, 1.0, 0.0
        runtime[id(brush)] = (brush, (cx, cy, cz, ax, ay, az, angle))

    def _publish_row_now(self, group, row, centre=None, rest=None):
        """:meth:`_publish_row` into :attr:`runtime_entries`, stamped."""
        self._publish_row(group, row, self.runtime_entries, centre, rest)
        brush_placement.note_runtime_change()

    def _adopt(self, group):
        """*group* was rebuilt: apply any pending restores to its rows and
        re-derive the runtime transforms from both groups' rows."""
        if self._pending:
            for row, brush in enumerate(group.brushes):
                entry = self._pending.get(id(brush))
                if entry is None or entry[0] is not brush:
                    continue
                group._place(row, entry[1], entry[2])
        # Built aside and swapped in whole: a reader on another thread never
        # sees a half-built dict (every mover momentarily at home).
        entries = {}
        for each in (self.movers, self.doors):
            if each.brushes:
                self._publish(each, list(range(len(each.brushes))), entries)
        self.runtime_entries = entries
        brush_placement.note_runtime_change()

    def position_of(self, brush):
        """Where *brush* is now (its centre): its row's position, or its
        authored centre when it has no row."""
        for group in (self.doors, self.movers):
            row = group.row_of_obj.get(id(brush))
            if row is not None and group.brushes[row] is brush:
                return tuple(group.pos[row].tolist())
        return tuple(brush_placement.authored_box(brush)[:3])

    def move_to(self, brush, position):
        """Put *brush*'s row (a path mover) at *position*, its centre."""
        movers = self.movers
        row = movers.row_of_obj.get(id(brush))
        if row is None or movers.brushes[row] is not brush:
            return False
        movers._store(np.array([row]), np.array([position], dtype=np.float64))
        return True

    def restore_placement(self, brush, position, angle=0.0):
        """A saved game's placement for *brush*: its centre and spin angle.

        Applied to its row now, or -- when play has not built the rows yet --
        when the row is built."""
        position = [float(position[0]), float(position[1]), float(position[2])]
        found = False
        for group in (self.movers, self.doors):
            row = group.row_of_obj.get(id(brush))
            if row is None or group.brushes[row] is not brush:
                continue
            found = True
            if group is self.movers:
                group.rot_angle[row] = float(angle)
            group._store(np.array([row]), np.array([position]))
        if not found:
            self._pending[id(brush)] = (brush, position, float(angle))

    def reset(self):
        """Play stopped: no row is displaced or turned any more."""
        self.runtime_entries = {}
        self._pending = {}
        brush_placement.note_runtime_change()

    def sync(self):
        """Re-read the brush parameters of every row the journal names."""
        changes = JOURNAL.drain(self)
        groups = (self.movers, self.doors)
        if changes is OVERFLOW:
            for group in groups:
                for row in range(len(group.index)):
                    group.resync(row)
                    group.dirty.add(row)
            return
        if not changes:
            return
        for oid in changes:
            for group in groups:
                row = group.row_of_obj.get(oid)
                if row is not None:
                    group.resync(row)
                    group.dirty.add(row)

    def tick_movers(self, logic, delta):
        self._link(logic)
        self.sync()
        self.movers._walk(logic, delta, self)

    def tick_doors(self, logic, delta):
        self._link(logic)
        self.sync()
        self.doors._walk(logic, delta, self)

    def _link(self, logic):
        """Rows for the live lists, and the rows the two groups share."""
        movers, doors = self.movers, self.doors
        movers.ensure(logic.mover_runtime.movers)
        doors.ensure(logic.mover_runtime.doors)
        self._pending = {}
        key = (movers.version, doors.version)
        if getattr(self, '_linked', None) == key:
            return
        self._linked = key
        movers.twin, doors.twin = doors, movers
        movers.twin_row[:] = -1
        doors.twin_row[:] = -1
        for row, brush in enumerate(movers.brushes):
            other = doors.row_of_obj.get(id(brush))
            if other is not None:
                movers.twin_row[row] = other
                doors.twin_row[other] = row
        movers.any_twin = doors.any_twin = bool((movers.twin_row >= 0).any())

    def publish(self, logic, table):
        """Store every mover's and door's position (and a mover's spin) in
        *table*, the render table of the buffer being built.

        Rows map to the table's slots by object identity, re-derived only when
        either side's rows change. A mover that is also a door is written by
        both, doors last, as the tick moved it.
        """
        self._link(logic)
        self.sync()             # a journalled move made outside the tick
        movers, doors = self.movers, self.doors
        if not (len(movers.index) or len(doors.index)):
            return
        key = (table.generation, movers.version, doors.version)
        cached = self._slot_cache.get(table)
        if cached is None or cached[0] != key:
            slot_of = table._slot_of_obj
            maps = []
            for group in (movers, doors):
                slots = [slot_of.get(id(brush), -1) for brush in group.brushes]
                slots = np.asarray(slots, dtype=np.intp)
                ok = slots >= 0
                maps.append((np.flatnonzero(ok), slots[ok]))
            cached = (key, tuple(maps))
            self._slot_cache[table] = cached
        (mover_rows, mover_slots), (door_rows, door_slots) = cached[1]
        if len(mover_rows):
            table.bounds[mover_slots, :3] = movers.pos[mover_rows]
            angle = movers.rot_angle[mover_rows]
            rot = np.zeros((len(mover_rows), 4), dtype=table.rot.dtype)
            spun = angle != 0.0
            rot[spun, :3] = movers.rot_axis[mover_rows][spun]
            rot[spun, 3] = angle[spun]
            table.rot[mover_slots] = rot
        if len(door_rows):
            table.bounds[door_slots, :3] = doors.pos[door_rows]
