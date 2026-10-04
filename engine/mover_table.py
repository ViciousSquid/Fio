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

Brush dicts are still written, but only for rows that actually moved: collision,
parented lights and portals, and saved games all read ``brush['pos']``.
"""

from __future__ import annotations

import weakref
from collections.abc import MutableMapping

import numpy as np

from engine.change_journal import JOURNAL, OVERFLOW

# Door states. OTHER keeps any unrecognised string as-is.
CLOSED, OPENING, OPEN, CLOSING, STOPPED, OTHER = range(6)
_DOOR_CODES = {'closed': CLOSED, 'opening': OPENING, 'open': OPEN,
               'closing': CLOSING, 'stopped': STOPPED}
_DOOR_NAMES = {code: name for name, code in _DOOR_CODES.items()}

# Events a tentative step can produce.
_NO_EVENT, _FULLY_OPEN, _FULLY_CLOSED, _CLOSE = range(4)
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

    def __init__(self):
        self.source = None
        self.fallback = {}
        self.dirty = set()
        self.states = StateView(self)
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
        self._build_columns(n)
        for row in range(n):
            self.resync(row)
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
        self.fallback = {}
        self._build(list(entries))
        self.source = entries
        for i, state in kept.items():
            self.states[i] = state
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
        """Copy row *row*'s brush parameters and position into the columns."""
        brush = self.brushes[row]
        self.pos[row] = brush['pos']
        original = brush.get('original_pos')
        if original is not None:
            self.original[row] = original

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

    def _write_positions(self, rows, new_pos, ride):
        """Store *new_pos* for *rows*, and in each brush dict that moved.

        *ride* is ``(player, ground row)`` or None: the player standing on a
        processed row is carried by its movement, as the loop did.
        """
        brushes = self.brushes
        if ride is not None:
            player, ground = ride
            hit = np.flatnonzero(rows == ground)
            if len(hit):
                self._carry(player, ground, new_pos[hit[0]])
        changed = np.flatnonzero((new_pos != self.pos[rows]).any(axis=1))
        if len(changed):
            moved = rows[changed]
            values = new_pos[changed]
            self._store(moved, values)
            for row, value in zip(moved.tolist(), values.tolist()):
                brushes[row]['pos'] = value

    def _store(self, rows, values):
        """The mirror of ``brush['pos']``, kept for both groups a brush is in."""
        self.pos[rows] = values
        twins = self.twin_row[rows]
        shared = twins >= 0
        if shared.any():
            mirror = self.twin.pos          # an array, not an entity
            mirror[twins[shared]] = np.asarray(values)[shared]

    def _carry(self, player, row, new):
        import glm
        cur = self.brushes[row]['pos']
        player.pos += glm.vec3(float(new[0]) - cur[0], float(new[1]) - cur[1],
                               float(new[2]) - cur[2])

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
        io = logic.io_manager
        ride = self._ride(logic)
        version = self.version
        self.dirty.clear()
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
                self.take_dirty()
                # A synchronous I/O sequence point can change later state
                # through a path/state/cache side effect. Re-plan the remaining
                # suffix unconditionally so the next vectorised span sees the
                # same state as the ordered legacy loop.
                plan = self._plan(logic, cursor, delta, io)



class LinearMovers(_Group):
    """Movers that travel back and forth along their direction (and spin)."""

    KEYS = ('progress', 'forward', '_direction_np')

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
        self.rot_angle[row] = brush.get('_rot_angle', 0.0) or 0.0
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
        if spin.any():
            spun = rows[spin]
            angles = plan['angle'][pick][spin]
            self.rot_angle[spun] = angles
            for row, angle in zip(spun.tolist(), angles.tolist()):
                brush = self.brushes[row]
                brush['_rot_angle'] = angle
                brush['rotation_yaw'] = angle
        self.progress[rows] = plan['progress'][pick]
        self.forward[rows] = plan['forward'][pick]
        self._write_positions(rows, plan['pos'][pick], ride)

    # -- one row, in order ---------------------------------------------------

    def _one(self, logic, row, delta, io, ride):
        """Advance one row exactly as the loop did. Returns whether it ran code
        that could have changed another row (I/O, or a path step)."""
        brush = self.brushes[row]
        i = self.index[row]
        if i in logic.mover_runtime.mover_path_states:
            logic.mover_runtime._update_mover_path(i, brush, delta)
            self.resync(row)
            self._store(np.array([row]), self.pos[[row]])
            return True

        if self.rotate[row]:
            angle = (float(self.rot_angle[row]) + float(self.spin_speed[row]) * delta) % 360.0
            self.rot_angle[row] = angle
            brush['_rot_angle'] = angle
            brush['rotation_yaw'] = angle
        if not self.has_state[row]:
            if 'original_pos' not in brush:
                brush['original_pos'] = list(brush['pos'])
                self.original[row] = brush['original_pos']
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
                    io.fire_output(brush, 'OnFullyOpen')
                    fired = True
        else:
            progress -= step
            self.progress[row] = progress
            if progress <= 0.0:
                self.progress[row] = 0.0
                self.forward[row] = True
                if not was_start and io:
                    io.fire_output(brush, 'OnFullyClosed')
                    fired = True
        t = float(self.progress[row])
        eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
        original = brush['original_pos']
        new = (original[0] + (direction[0] * distance) * eased,
               original[1] + (direction[1] * distance) * eased,
               original[2] + (direction[2] * distance) * eased)
        if ride is not None and ride[1] == row:
            self._carry(ride[0], row, new)
        brush['pos'] = list(new)
        self._store(np.array([row]), np.array([new]))
        return fired


class Doors(_Group):
    """Doors: open, wait, close, on a progress value from 0 to 1."""

    KEYS = ('progress', 'state', 'open_timer', 'speed', 'distance', 'direction',
            '_direction_np')

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
        original = brush['original_pos']
        new = (original[0] + (direction[0] * distance) * progress,
               original[1] + (direction[1] * distance) * progress,
               original[2] + (direction[2] * distance) * progress)
        if ride is not None and ride[1] == row:
            self._carry(ride[0], row, new)
        brush['pos'] = list(new)
        self._store(np.array([row]), np.array([new]))
        return bool(event and io)


class MoverTable:
    """Every mover and door in play, as dense columns the tick advances.

    Owned by the logic thread. :meth:`publish` hands the renderer their
    positions and spins as array stores.
    """

    def __init__(self):
        self.movers = LinearMovers()
        self.doors = Doors()
        # render table -> ((its generation, mover version, door version),
        # slots). One entry per table: frames alternate between the two render
        # buffers' tables, so a single entry would miss on every frame.
        self._slot_cache = weakref.WeakKeyDictionary()
        JOURNAL.subscribe(self)

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
