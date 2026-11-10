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

import math
import threading
import weakref
from collections.abc import MutableMapping
from collections import deque
from itertools import compress, repeat
from operator import itemgetter, not_, setitem

import numpy as np

from engine import brush_placement, change_detection
from engine.change_journal import JOURNAL, OVERFLOW, VISIBILITY
from engine.spatial import PARKED_HIDDEN_KEY


#: Publications the mover table remembers the published rows of (M4): a
#: render table last written further back than this is written whole.
POSE_LOG = 32

#: Marks a group keeps between publications before it gives up listing rows
#: and says "every row" (ticks walked with no frame published: a headless
#: run, a stalled renderer).
MARKS_LIMIT = 512

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


#: An empty row list.
_NO_ROWS = np.zeros(0, dtype=np.intp)


def _no_logic():
    """A table not yet linked to a logic thread's runtime."""
    return None

_consume = deque(maxlen=0).extend

#: The runtime-only slots a published row writes (constants.AABB_RUNTIME_KEYS).
_PUBLISHED_SLOTS = ('_aabb_sig', '_aabb_bounds', '_aabb_runtime', '_world_bounds',
                    '_aabb_turn')


def travel_range(brush):
    """``(lo, hi, turns, reach)``: every centre a mover's or door's travel can
    give it -- *lo*/*hi* bound its authored centre and the far end of its
    authored travel (``direction`` x ``distance`` for a mover; a door's
    ``door_direction`` x ``door_distance`` less its lip), with a margin for
    rounding -- whether it can turn, and how far its box reaches from that
    centre: its half-extents, or for a row that can turn the radius of its
    box in every axis. The rules :class:`MoverTable` moves rows by, from the
    authored parameters alone, so the spatial grid can file a brush for all
    of its travel before the table has a row for it."""
    from engine.logic_movers import DOOR_DIRECTION_MAP
    box = brush_placement.authored_box(brush)
    origin = tuple(float(v) for v in box[:3])
    half = box[3:]
    ends = [origin]
    if brush.get('is_mover'):
        direction = _unit_direction(brush.get('direction', [0, 1, 0]))
        distance = float(brush.get('distance', 128.0))
        ends.append(tuple(origin[i] + direction[i] * distance for i in range(3)))
    if brush.get('is_door'):
        distance = float(brush.get('door_distance', brush.get('distance', 128.0)))
        if 'door_lip' in brush:
            distance = max(1.0, distance - float(brush.get('door_lip', 0.0)))
        direction = _unit_direction(DOOR_DIRECTION_MAP.get(brush.get('door_direction', ''),
                                                           [0, 1, 0]))
        ends.append(tuple(origin[i] + direction[i] * distance for i in range(3)))
    margin = 1.0 + 1e-6 * max(abs(v) for end in ends for v in end)
    lo = tuple(min(end[i] for end in ends) - margin for i in range(3))
    hi = tuple(max(end[i] for end in ends) + margin for i in range(3))
    turns = bool(brush.get('is_mover') and brush.get('rotate'))
    if turns:
        radius = math.sqrt(half[0] * half[0] + half[1] * half[1] + half[2] * half[2])
        reach = (radius, radius, radius)
    else:
        reach = (float(half[0]), float(half[1]), float(half[2]))
    return lo, hi, turns, reach


def _publish_slot(brush, transform):
    """Publish one changed row's transform into its brush's runtime-only
    slot and drop its derived key -- nothing else. The first reader that
    needs its world box, float64 bounds or rotation works them out once from
    this transform (:func:`engine.constants.mover_box`) and every other
    reader shares them; a row that has not changed keeps what was derived
    for it. A brush another source answers for is left to the resolver."""
    overridden = brush_placement.overridden
    if overridden and id(brush) in overridden:
        brush.pop('_aabb_runtime', None)
    else:
        brush['_aabb_runtime'] = transform          # transform, then key
    brush.pop('_aabb_sig', None)


def _publish_slots(chosen, transforms, live):
    """:func:`_publish_slot` for many rows, iterated in C (*live*: which
    rows have a transform, or None when all do)."""
    _store_slot(chosen, '_aabb_runtime', transforms)
    if live is not None:
        # The rows back where they were authored: no transform.
        _store_slot(compress(chosen, map(not_, live)), '_aabb_runtime', repeat(None))
    _consume(map(dict.pop, chosen, repeat('_aabb_sig'), repeat(None)))
    overridden = brush_placement.overridden
    if overridden:
        for brush in chosen:
            if id(brush) in overridden:
                brush.pop('_aabb_runtime', None)


def _withdraw_slots(brushes):
    """Drop every slot a mover table publishes from *brushes*."""
    for brush in brushes:
        for key in _PUBLISHED_SLOTS:
            brush.pop(key, None)


def _withdraw(brushes):
    """Drop every slot a mover table publishes from *brushes*, and their
    marks as off their filed travel (``brush_placement.off_range``) or
    parked (``brush_placement.asleep``)."""
    _withdraw_slots(brushes)
    off_range = brush_placement.off_range
    if off_range:
        count = len(off_range)
        for brush in brushes:
            off_range.pop(id(brush), None)
        if len(off_range) != count:
            brush_placement.note_off_range()
    asleep = brush_placement.asleep
    if asleep:
        gone = False
        for brush in brushes:
            held = asleep.get(id(brush))
            if held is not None and held[0] is brush:
                del asleep[id(brush)]
                gone = True
        if gone:
            brush_placement.note_asleep()


def _withdraw_held(held):
    for brushes in held:
        _withdraw(brushes)


def _store_slot(brushes, key, values):
    """``brush[key] = value`` for each pair, iterated in C."""
    _consume(map(setitem, brushes, repeat(key), values))


def _sequence_points(parts):
    """The sorted, distinct rows of *parts* (row arrays) as intp: where the
    pass must stop and run a row in order. Usually there are none -- no path
    movers, no rows to set up, no events this tick -- and then no NumPy
    sort runs."""
    if not any(len(part) for part in parts):
        return _NO_ROWS
    return np.unique(np.concatenate(parts)).astype(np.intp)


def _eased(t):
    """The movers' ease-in-out, elementwise; the same arithmetic as the loop's.

    ``np.float_power`` and not ``np.power``: the scalar ease calls ``pow``,
    which is libm's, and ``float_power`` evaluates with it. ``np.power`` takes
    a SIMD path that differs from libm in the last bit for about a quarter of
    inputs, and a mover would then drift from where it used to be.
    """
    return np.where(t < 0.5, 4 * t * t * t, 1 - np.float_power(-2 * t + 2, 3) / 2)


def _ticks_to(gap, step):
    """How many ticks a value moving *step* a tick (> 0) takes to cover
    *gap*: the smallest k >= 1 with ``k * step >= gap``. A ratio within
    float error of a whole number counts as that number (what exact
    arithmetic says; a sum stepped tick by tick can land either side)."""
    if gap <= 0.0:
        return 1
    ratio = gap / step
    k = math.ceil(ratio - 1e-9 * ratio)
    return k if k > 1 else 1


#: Legs longer than this many ticks are not counted step by step (see
#: :func:`_leg`): worked out instead.
LEG_COUNT_LIMIT = 20000

#: ``(step, forward) -> ticks``: a whole leg's length as the walk counts it.
_WHOLE_LEGS = {}


def _leg(p, step, forward):
    """How many ticks the walk takes to carry a progress value from *p* to
    its end (1 going forward, 0 going back) at *step* a tick (> 0): counted
    with the walk's own additions, so an end lands on the tick it would --
    a whole number of steps a leg in exact arithmetic can take one more as
    a sum. Bounded by one leg, whatever time has passed; a whole leg (from
    an end) is counted once per step. A leg longer than
    :data:`LEG_COUNT_LIMIT` ticks is worked out (:func:`_ticks_to`)."""
    estimate = _ticks_to(1.0 - p if forward else p, step)
    if estimate > LEG_COUNT_LIMIT:
        return estimate
    key = None
    if p == (0.0 if forward else 1.0):
        key = (step, forward)
        known = _WHOLE_LEGS.get(key)
        if known is not None:
            return known
    k = 0
    if forward:
        while True:
            p += step
            k += 1
            if p >= 1.0:
                break
    else:
        while True:
            p -= step
            k += 1
            if p <= 0.0:
                break
    if key is not None:
        if len(_WHOLE_LEGS) > 4096:
            _WHOLE_LEGS.clear()
        _WHOLE_LEGS[key] = k
    return k


def _countdown(timer, dt):
    """How many ticks a timer at *timer* takes to run out, *dt* a tick: the
    walk's own subtractions (bounded by the timer), or worked out past
    :data:`LEG_COUNT_LIMIT` ticks."""
    estimate = _ticks_to(timer, dt)
    if estimate > LEG_COUNT_LIMIT:
        return estimate
    k = 0
    while True:
        timer -= dt
        k += 1
        if timer <= 0:
            return k


def _bounce(progress, forward, step, n):
    """A mover's ``(progress, forward)`` *n* ticks of *step* on: the per-tick
    rule (:meth:`LinearMovers._one` -- on towards the end it faces, clamped
    there, where it turns), its ends on the ticks the walk reaches them
    (:func:`_leg`), whole round trips skipped."""
    p, f = progress, forward
    if step <= 0.0:
        # Nothing moves; an end it is already at still turns it, once.
        if n > 0:
            if f and p >= 1.0:
                return 1.0, False
            if not f and p <= 0.0:
                return 0.0, True
        return p, f
    lap = None
    while n > 0:
        k = _leg(p, step, f)
        if n < k:
            return (p + n * step, True) if f else (p - n * step, False)
        p, f = (1.0, False) if f else (0.0, True)
        n -= k
        if lap is None:
            # At an end now: a round trip from here is the two whole legs.
            lap = _leg(1.0, step, False) + _leg(0.0, step, True)
            n %= lap
    return p, f


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

    # A parked row's state is brought to now before it is read or written
    # (_Group.settle): what an input or a saved game sees is where it has
    # got to, and what it writes takes effect from now.

    def __getitem__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None:
            if group.n_parked and group.parked[row]:
                group.settle(row)
            if group.has_state[row]:
                return RowState(group, row)
        return group.fallback[index]

    def __setitem__(self, index, state):
        group = self._group
        row = group.row_of_index.get(index)
        if row is None:
            group.fallback[index] = state
        else:
            if group.n_parked and group.parked[row]:
                group.settle(row)
            group.load_state(row, state)
            group.dirty.add(row)

    def __delitem__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None and group.n_parked and group.parked[row]:
            group.settle(row)
        if row is not None and group.has_state[row]:
            group.has_state[row] = False
            group.dirty.add(row)
        else:
            del group.fallback[index]

    def __contains__(self, index):
        group = self._group
        row = group.row_of_index.get(index)
        if row is not None:
            if group.n_parked and group.parked[row]:
                group.settle(row)
            if group.has_state[row]:
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
        #: Ticks this group has walked, and the step of the last: the clock
        #: a parked row is resolved against (see :meth:`settle`). Kept
        #: across rebuilds.
        self.ticks = 0
        self.delta = 1.0 / 60.0
        #: Rows whose runtime transform was published since the mover table
        #: last took them (MoverTable.publish): its change record (M4) --
        #: single rows as ints, batches as arrays; None once there are too
        #: many to list ("every row").
        self._marked_rows = []
        self._marked_arrays = []
        self._build([])

    # -- rows ----------------------------------------------------------------

    def _build(self, entries):
        n = len(entries)
        # What was published for the rows being replaced is withdrawn (the
        # new rows publish their own when the table adopts them).
        _withdraw(getattr(self, 'brushes', ()))
        # Row numbers of the rows being replaced: the rebuild is written whole.
        self._marked_rows = []
        self._marked_arrays = []
        self.index = [i for i, _ in entries]
        self.brushes = [brush for _, brush in entries]
        self.row_of_index = {i: row for row, i in enumerate(self.index)}
        self.row_of_obj = {id(brush): row for row, brush in enumerate(self.brushes)}
        self.pos = np.zeros((n, 3))
        self.original = np.zeros((n, 3))
        #: The authored box's half-extents (brush_placement.authored_box),
        #: and the half-extents of its AABB as the row is turned now: what
        #: brush_placement.world_box answers, kept here so readers gather it
        #: by row (MoverTable.fill_boxes) instead of asking per brush.
        self.half = np.zeros((n, 3))
        self.turned = np.zeros((n, 3))
        #: Whether the row has a runtime entry (displaced or turned).
        self.live = np.zeros(n, dtype=bool)
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
        #: Rows a streaming layer has parked (PARKED_HIDDEN_KEY): not walked
        #: or published; each is resolved on demand from its state and the
        #: ticks since :attr:`since` (see :meth:`settle`).
        self.parked = np.zeros(n, dtype=bool)
        self.since = np.zeros(n, dtype=np.int64)
        self.n_parked = 0
        self._build_columns(n)
        for row in range(n):
            # A brush cached as still before it had a mover's flag
            # (constants.brush_aabb_bounds keys that by pos/size alone) is
            # one now: drop the key once, here.
            self.brushes[row].pop('_aabb_sig', None)
            self.resync(row)
            self.note_parking(row)
        if self.table is not None:
            self.table._rows_held[self.held_as] = self.brushes
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
            self.quiet(moved)
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
        unchanged one leaves its position exactly as it is. A parked row is
        first brought to now under the parameters it had."""
        if self.n_parked and self.parked[row]:
            self.settle(row)
        brush = self.brushes[row]
        box = brush_placement.authored_box(brush)
        authored = box[:3]
        self.half[row] = box[3:]
        self.turned[row] = box[3:]
        if tuple(self.original[row].tolist()) != authored:
            travelled = self.pos[row] - self.original[row]
            self.original[row] = authored
            self.pos[row] = self.original[row] + travelled

    # -- parked rows ---------------------------------------------------------

    def note_parking(self, row, publish=True):
        """Park or wake row *row* if its brush's parking mark
        (``engine.spatial.PARKED_HIDDEN_KEY``, set by a streaming layer) no
        longer says what the row is: ``'parked'``, ``'woken'`` or None. A
        brush in both groups is never parked: its two rows move it
        together. With *publish* false the caller publishes a woken row and
        stamps the change (a crossing parks and wakes many at once)."""
        table = self.table
        brush = self.brushes[row]
        parked = (table is not None and PARKED_HIDDEN_KEY in brush
                  and self.twin_row[row] < 0)
        if parked == bool(self.parked[row]):
            return None
        if parked:
            self.parked[row] = True
            self.since[row] = self.ticks
            self.n_parked += 1
            brush_placement.asleep[id(brush)] = (brush, table.ref)
            brush_placement.note_asleep()
            # Every reader asks the resolver now (runtime_now), from any
            # thread; nothing is published until it wakes.
            _withdraw_slots((brush,))
            if publish:
                brush_placement.note_runtime_change()
            return 'parked'
        self.wake(row, publish)
        return 'woken'

    def wake(self, row, publish=True):
        """Unpark row *row*: brought to now once, then walked and published
        as any other row (by the caller, when *publish* is false)."""
        self.settle(row, publish)
        brush = self.brushes[row]
        self.parked[row] = False
        self.n_parked -= 1
        held = brush_placement.asleep.get(id(brush))
        if held is not None and held[0] is brush:
            del brush_placement.asleep[id(brush)]
            brush_placement.note_asleep()
        if publish and self.table is not None:
            self.table._publish(self, [row])

    def _mark_row(self, row):
        """Record that row *row* had its runtime transform published."""
        rows = self._marked_rows
        if rows is not None:
            rows.append(row)
            if len(rows) > MARKS_LIMIT * 8:
                self._marked_rows = None        # "every row"

    def _mark_rows(self, rows):
        """Record that *rows* (an array) had their runtime transform
        published."""
        arrays = self._marked_arrays
        if arrays is not None:
            arrays.append(rows)
            if len(arrays) > MARKS_LIMIT:
                self._marked_arrays = None      # "every row"

    def take_marks(self):
        """The rows whose runtime transform was published since the last
        call (M4's change record for the publication; a row may appear more
        than once), or None for every row (a rebuild, or more than could be
        listed)."""
        rows, arrays = self._marked_rows, self._marked_arrays
        self._marked_rows, self._marked_arrays = [], []
        if rows is None or arrays is None:
            return None
        if not arrays:
            if not rows:
                return _NO_ROWS
            marked = np.fromiter(set(rows), dtype=np.intp)
        else:
            if rows:
                arrays.append(np.fromiter(set(rows), dtype=np.intp))
            marked = arrays[0] if len(arrays) == 1 else np.concatenate(arrays)
        # Every row (the usual tick of a busy level): say so, and the
        # publication writes them as it always did.
        return None if len(marked) >= len(self.index) else marked

    def quiet(self, rows):
        """Withdraw what was just published for the parked rows among
        *rows*: a parked row is answered by :meth:`runtime_now`."""
        if self.n_parked:
            _withdraw_slots([self.brushes[row] for row in rows if self.parked[row]])

    def settle(self, row, publish=True):
        """Bring parked row *row* to now and store it: its state as the
        ticks since it was last resolved leave it (:meth:`_after`, worked
        out, not stepped), firing no output -- a transition it passed while
        parked is not replayed. Only the logic thread, which owns the
        columns, stores; another thread's readers use :meth:`runtime_now`.
        With *publish* false the caller publishes it (it is being woken)."""
        n = self.ticks - int(self.since[row])
        if n <= 0:
            return
        table = self.table
        if table is not None and table.owner not in (None, threading.get_ident()):
            return
        self.since[row] = self.ticks
        self._catch_up(row, n, self.delta)
        if table is not None and publish:
            brush = self.brushes[row]
            table._publish_row(self, row, table.runtime_entries)
            if self.parked[row]:
                _withdraw_slots((brush,))
            brush_placement.note_runtime_change()

    def runtime_now(self, row):
        """Parked row *row*'s runtime transform now, worked out from its
        state and the ticks since, storing nothing (any thread)."""
        n = self.ticks - int(self.since[row])
        if n <= 0:
            centre = self.pos[row].tolist()
            angle = self._angle(row)
        else:
            out = self._after(row, n, self.delta)
            centre, angle = out['centre'], out.get('angle', 0.0)
        if angle:
            ax, ay, az = self.rot_axis[row].tolist()
            return (centre[0], centre[1], centre[2], ax, ay, az, angle)
        rest = self.original[row].tolist()
        if centre[0] == rest[0] and centre[1] == rest[1] and centre[2] == rest[2]:
            return None
        return (centre[0], centre[1], centre[2], 0.0, 1.0, 0.0, 0.0)

    def _angle(self, row):
        return 0.0

    def check_range(self, row, centre=None, angle=None):
        """Whether row *row* can now be anywhere the spatial grid did not
        file its brush for (``_travel_filed``, :func:`travel_range`): a path
        step or a restored placement at *centre*, a restored spin *angle*,
        or a travel segment an edit or a loaded state changed. If so the
        brush is *off range* (``brush_placement.off_range``) until play
        stops, and every reader of the grid adds it wherever it looks.

        Linear travel stays on its segment, which is checked whenever the
        columns it is worked out from change, so a tick checks nothing."""
        brush = self.brushes[row]
        filed = brush.get('_travel_filed')
        if filed is None or id(brush) in brush_placement.off_range:
            return
        lo, hi, turns = filed[0], filed[1], filed[2]
        if angle and not turns:
            brush_placement.off_range[id(brush)] = brush
            brush_placement.note_off_range()
            return
        if centre is not None:
            points = (centre,)
        else:
            origin = self.original[row].tolist()
            distance = float(self.distance[row])
            if self.dir_valid[row]:
                direction = self.direction[row].tolist()
            else:
                direction = self._authored_direction(row)
            points = (origin, [origin[i] + direction[i] * distance for i in range(3)])
        for point in points:
            if not all(lo[i] <= point[i] <= hi[i] for i in range(3)):
                brush_placement.off_range[id(brush)] = brush
                brush_placement.note_off_range()
                return

    def _authored_direction(self, row):
        return _unit_direction(self.brushes[row].get('direction', [0, 1, 0]))

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
        # A saved game may put it anywhere.
        self.check_range(row, centre=position, angle=angle)

    def _write_positions(self, rows, new_pos, ride, spin=None):
        """Store *new_pos* for the *rows* that moved, and publish them and
        the rows *spin* marks (a mask over *rows*: their angle the caller has
        just stored) together.

        *ride* is ``(player, ground row)`` or None: the player standing on a
        processed row is carried by its movement, as the loop did.
        """
        if ride is not None:
            player, ground = ride
            hit = np.flatnonzero(rows == ground)
            if len(hit):
                self._carry(player, ground, new_pos[hit[0]])
        changed = (new_pos != self.pos[rows]).any(axis=1)
        if changed.all():
            # Every row moved (the usual tick): no picking.
            self._store(rows, new_pos, publish=False)
            if self.table is not None:
                self.table._publish(self, rows, here=new_pos)
            return
        any_changed = changed.any()
        if any_changed:
            self._store(rows[changed], new_pos[changed], publish=False)
        if self.table is None:
            return
        if spin is not None:
            # The moved and the spun rows, in row order, with where they are.
            changed |= spin
            any_changed = True
        if any_changed:
            self.table._publish(self, rows[changed], here=new_pos[changed])

    def _store_one(self, row, new, rest):
        """:meth:`_store` for one row: *new* its position and *rest* its
        authored centre, in Python floats. In scalars: the per-row walk calls
        this for every row it moves."""
        self.pos[row] = new
        if self.any_twin:
            twin = int(self.twin_row[row])
            if twin >= 0:
                mirror = self.twin.pos          # an array, not an entity
                mirror[twin] = new
        table = self.table
        if table is not None:
            table._publish_row(self, row, table.runtime_entries, new, rest)
            brush_placement.note_runtime_change()

    def _store(self, rows, values, publish=True):
        """Where *rows* are now, kept for both groups a brush is in, and
        published to the resolver (:meth:`MoverTable.runtime_of`)."""
        self.pos[rows] = values
        if self.any_twin:
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
        self.ticks += 1
        self.delta = delta
        if not self.index or (self.n_parked and self.n_parked == len(self.index)):
            # Most maps have no movers or no doors (and a streamed one may
            # have every row parked). With no rows to walk the plan is empty
            # and the pass below commits nothing, but building it cost some
            # thirty NumPy calls per group per tick.
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

    #: Below this many stale rows settle_turns goes row by row.
    SETTLE_ROWS_BELOW = 8

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
        #: The spin angle each row's ``turned`` half-extents were last worked
        #: out for (NaN: not yet): see :meth:`settle_turns`.
        self.turn_angle = np.full(n, np.nan)
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
        # Its half-extents and axis are new: its turned box is worked out
        # again when next gathered.
        self.turn_angle[row] = np.nan
        # An edit in play may take its travel past where the grid filed it.
        self.check_range(row)

    def settle_turns(self, rows):
        """Bring the turned half-extents of *rows* up to date: worked out
        -- by the resolver's own scalar rule, so what a reader gathers is
        world_box to the bit -- only for the rows whose spin angle has
        changed since they last were, and only when a reader gathers boxes.
        The publication records the angle and nothing more: a spinner's
        angle is new every tick, and most ticks nothing asks for its box.

        Each result is stored with the angle it was worked out for, after
        it (a reader on another thread that sees the angle sees its box);
        the angles are read once, so a change that lands meanwhile leaves
        the row stale for the next reader."""
        angle = self.rot_angle[rows]
        stale = self.turn_angle[rows] != angle         # NaN: never matches
        if not stale.any():
            return
        rows = rows[stale]
        angle = angle[stale]
        if len(rows) < self.SETTLE_ROWS_BELOW:
            # A few rows: in scalars (each NumPy call costs more than a row).
            turn = brush_placement.turned_half_now
            self.turned[rows] = [
                turn(h[0], h[1], h[2], a[0], a[1], a[2], g)
                for h, a, g in zip(self.half[rows].tolist(),
                                   self.rot_axis[rows].tolist(), angle.tolist())]
            self.turn_angle[rows] = angle
            return
        turned = self.half[rows]
        spin = np.flatnonzero(angle != 0.0)
        if len(spin):
            turn = brush_placement.turned_half_now
            turned[spin] = [
                turn(h[0], h[1], h[2], a[0], a[1], a[2], g)
                for h, a, g in zip(turned[spin].tolist(),
                                   self.rot_axis[rows[spin]].tolist(),
                                   angle[spin].tolist())]
        self.turned[rows] = turned
        self.turn_angle[rows] = angle

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
            self.check_range(row)

    def _unset(self, row, key):
        if key == '_direction_np':
            self.dir_valid[row] = False

    # -- planning ------------------------------------------------------------

    def _takes_part(self, row):
        """Whether *row* moves this tick: what :meth:`_plan` calls active."""
        return (bool(self.start_on[row]) and not self.move_once[row]
                and not (self.n_parked and self.parked[row]))

    def _plan(self, logic, start, delta, io):
        rows = np.arange(start, len(self.index))
        moving = ~self.move_once[start:] & self.start_on[start:]
        if self.n_parked:
            moving &= ~self.parked[start:]
        active = rows[moving]
        paths = logic.mover_runtime.mover_path_states
        if paths:
            on_path = np.zeros(len(self.index), dtype=bool)
            for i in paths:
                row = self.row_of_index.get(i)
                if row is not None:
                    on_path[row] = True
            path_rows = active[on_path[active]]
            rest = active[~on_path[active]]
        else:
            path_rows = _NO_ROWS
            rest = active
        ready = self.has_state[rest] & self.dir_valid[rest]
        special = rest[~ready]
        simple = rest[ready]
        step = self._step(simple, delta)
        seq = [path_rows, special]
        if io:
            seq.append(simple[step['event'] != _NO_EVENT])
        step['seq'] = _sequence_points(seq)
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
        event = np.full(len(rows), _NO_EVENT, dtype=np.int8)
        # An end of travel is rare: its stores run only when one happens.
        if hit_end.any():
            new[hit_end] = 1.0
            event[hit_end & ~was_end] = _FULLY_OPEN
        if hit_start.any():
            new[hit_start] = 0.0
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
            self.rot_angle[rows[spin]] = plan['angle'][pick][spin]
        else:
            spin = None
        self.progress[rows] = plan['progress'][pick]
        self.forward[rows] = plan['forward'][pick]
        # The spun rows are published with the moved ones, once.
        self._write_positions(rows, plan['pos'][pick], ride, spin)

    # -- a parked row, worked out ---------------------------------------------

    def _angle(self, row):
        return float(self.rot_angle[row])

    def _after(self, row, n, dt):
        """Row *row*'s state *n* ticks of *dt* on, as the walk would leave
        it: ``{'progress', 'forward', 'angle', 'centre', 'path'}`` (*path*: a
        path mover's ``(state or None, ended)``). Worked out, not stepped;
        nothing is stored or fired."""
        progress = float(self.progress[row])
        forward = bool(self.forward[row])
        angle = float(self.rot_angle[row])
        out = {'progress': progress, 'forward': forward, 'angle': angle,
               'centre': self.pos[row].tolist(), 'path': None}
        if n <= 0 or not self.start_on[row] or self.move_once[row]:
            return out
        table = self.table
        logic = table.logic() if table is not None else None
        paths = logic.mover_runtime.mover_path_states if logic is not None else None
        i = self.index[row]
        has_state = bool(self.has_state[row])
        if paths and i in paths:
            # A path follower does not spin (the walk hands it to the path
            # step before its spin).
            state, centre, n = self._path_after(row, n, dt, paths[i], logic)
            out['centre'] = centre
            out['path'] = state
            if state[0] is not None or state[1] or n <= 0:
                return out
            # Off the end of a path with no wait (or a node gone): the walk
            # drops the path and the row travels on its own from the next
            # tick, from a fresh state (the path took its old one).
            has_state = False
        if self.rotate[row]:
            out['angle'] = (angle + float(self.spin_speed[row]) * dt * n) % 360.0
        if not has_state:
            progress, forward = 0.0, True
        speed = float(self.speed[row])
        distance = float(self.distance[row])
        if self.dir_valid[row]:
            direction = self.direction[row].tolist()
        else:
            direction = list(self._authored_direction(row))
        step = (speed * dt) / distance if distance > 0 else 0
        progress, forward = _bounce(progress, forward, step, n)
        out['progress'], out['forward'] = progress, forward
        t = progress
        eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
        original = self.original[row].tolist()
        out['centre'] = [original[0] + (direction[0] * distance) * eased,
                      original[1] + (direction[1] * distance) * eased,
                      original[2] + (direction[2] * distance) * eased]
        return out

    #: Path segments and waits a parked path follower is worked through at
    #: most when brought to now (a looped path is skipped by whole laps).
    PATH_STEPS = 10000

    def _path_after(self, row, n, dt, state, logic):
        """A parked path follower *n* ticks of *dt* on: ``((state, ended),
        centre, left)`` -- the path state it would have (None once it has
        left the path), whether it stopped at the end of its path (after a
        wait, the walk clears its ``start_on``), and the ticks left after it
        left the path -- by ``LogicMovers._update_mover_path``'s rule, a
        segment or a wait at a time, whole laps of a looped path skipped. No
        output is fired."""
        brush = self.brushes[row]
        find = logic.world_runtime.find_path_node_by_name
        st = dict(state)
        centre = self.pos[row].tolist()
        speed = brush.get("speed", 64.0)
        laps = {}
        for _ in range(self.PATH_STEPS):
            if n <= 0:
                break
            name = st.get("current_node")
            if not name:
                break
            node = find(name)
            if node is None:
                return (None, False), centre, n - 1     # the walk drops the path
            if st.get("waiting"):
                k = _countdown(st["wait_remaining"], dt)
                if n < k:
                    st["wait_remaining"] -= n * dt
                    break
                n -= k
                st["wait_remaining"] -= k * dt
                st["waiting"] = False
                next_name = node.get_next_node_name()
                if not next_name:
                    return (None, True), centre, 0
                st["origin"] = list(centre)
                st["current_node"] = next_name
                st["lerp_t"] = 0.0
                continue
            origin = [float(v) for v in st["origin"]]
            if st["lerp_t"] == 0.0 and laps is not None:
                # The start of a segment: seen here before, the path loops.
                key = (name, tuple(origin))
                seen = laps.get(key)
                if seen is not None and seen > n:
                    n %= seen - n
                    laps = None
                    continue
                laps[key] = n
            target = [float(v) for v in node.pos]
            segment = [target[0] - origin[0], target[1] - origin[1], target[2] - origin[2]]
            length = math.sqrt(segment[0] ** 2 + segment[1] ** 2 + segment[2] ** 2)
            if length < 1.0:
                k = 1
            else:
                step = (speed * node.get_speed() * dt) / length
                if step <= 0.0:
                    break                           # it never gets there
                k = _leg(st["lerp_t"], step, True)
                if n < k:
                    t = st["lerp_t"] = st["lerp_t"] + n * step
                    eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
                    centre = [origin[0] + segment[0] * eased,
                              origin[1] + segment[1] * eased,
                              origin[2] + segment[2] * eased]
                    break
            n -= k
            st["lerp_t"] = 1.0
            centre = target
            wait = node.get_wait_time()
            if wait > 0.0:
                st["waiting"] = True
                st["wait_remaining"] = wait
            else:
                next_name = node.get_next_node_name()
                if not next_name:
                    return (None, False), centre, n
                st["origin"] = list(centre)
                st["current_node"] = next_name
                st["lerp_t"] = 0.0
        return (st, False), centre, 0

    def _catch_up(self, row, n, dt):
        """:meth:`_after`, stored in the row (and a path follower's state)."""
        out = self._after(row, n, dt)
        path = out['path']
        if path is not None:
            state, ended = path
            logic = self.table.logic()
            paths = logic.mover_runtime.mover_path_states
            i = self.index[row]
            if state is None:
                paths.pop(i, None)
                if ended:
                    brush = self.brushes[row]
                    brush["start_on"] = False
                    self.start_on[row] = False
            else:
                held = paths[i]
                held.clear()
                held.update(state)
            self.pos[row] = out['centre']
            self.check_range(row, centre=out['centre'])
            if state is not None or ended:
                return
            # It left the path and travelled on its own (see _after).
            self.load_state(row, {'progress': out['progress'], 'forward': out['forward']})
            if not self.dir_valid[row]:
                self._set_direction(row, self._authored_direction(row))
            self.rot_angle[row] = out['angle']
            return
        if self.start_on[row] and not self.move_once[row]:
            if not self.has_state[row]:
                self.load_state(row, {'progress': 0.0, 'forward': True})
            if not self.dir_valid[row]:
                self._set_direction(row, self._authored_direction(row))
        self.progress[row] = out['progress']
        self.forward[row] = out['forward']
        self.rot_angle[row] = out['angle']
        self.pos[row] = out['centre']

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
        direction = self.direction[row].tolist()
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
        if self.dir_valid[row]:
            self.check_range(row)

    def _authored_direction(self, row):
        raw = self.raw_direction[row]
        return _unit_direction([0, 1, 0] if raw is None else raw)

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
            if self.dir_valid[row]:
                self.check_range(row)
        elif key == 'direction':
            self.raw_direction[row] = value
        else:
            self.direction[row] = value
            self.dir_valid[row] = True
            self.check_range(row)

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
            self.code[row] == CLOSED and self.progress[row] == 0.0) and not (
                self.n_parked and self.parked[row])

    def _plan(self, logic, start, delta, io):
        rows = np.arange(start, len(self.index))
        idle = (self.code[start:] == CLOSED) & (self.progress[start:] == 0.0)
        if self.n_parked:
            idle |= self.parked[start:]
        live = rows[self.has_state[start:] & ~idle]
        special = live[~self.dir_valid[live]]
        simple = live[self.dir_valid[live]]
        step = self._step(simple, delta)
        seq = [special]
        if io:
            seq.append(simple[step['event'] != _NO_EVENT])
        step['seq'] = _sequence_points(seq)
        step['rows'] = simple
        return step

    def _step(self, rows, delta):
        distance = self.distance[rows]
        step_size = np.zeros(len(rows))
        np.divide(self.speed[rows] * delta, distance, out=step_size,
                  where=distance > 0)
        # Fancy indexing already copies: these are the tentative new state.
        was = self.code[rows]
        code = was.copy()
        progress = self.progress[rows]
        timer = self.open_timer[rows]
        event = np.full(len(rows), _NO_EVENT, dtype=np.int8)

        # Each phase reads the codes as they were (a door that just finished
        # opening does not also start waiting this tick). A transition is
        # rare, so its stores run only when one happens.
        opening = was == OPENING
        progress[opening] += step_size[opening]
        done = opening & (progress >= 1.0)
        if done.any():
            progress[done] = 1.0
            code[done] = OPEN
            timer[done] = self.open_time[rows][done]
            event[done] = _FULLY_OPEN

        waiting = was == OPEN
        timer[waiting] -= delta
        done = waiting & (timer <= 0)
        if done.any():
            code[done] = CLOSING
            event[done] = _CLOSE

        closing = was == CLOSING
        progress[closing] -= step_size[closing]
        done = closing & (progress <= 0.0)
        if done.any():
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

    # -- a parked row, worked out ---------------------------------------------

    def _after(self, row, n, dt):
        """Row *row*'s state *n* ticks of *dt* on, as the walk would leave
        it: ``{'code', 'progress', 'timer', 'centre'}``. Opening, open (its
        timer running down), closing, closed: at most one pass through the
        cycle, worked out, not stepped; nothing is stored or fired."""
        code = int(self.code[row])
        progress = float(self.progress[row])
        timer = float(self.open_timer[row])
        out = {'code': code, 'progress': progress, 'timer': timer,
               'centre': self.pos[row].tolist()}
        if n <= 0 or not self.has_state[row] or code not in (OPENING, OPEN, CLOSING):
            return out
        distance = float(self.distance[row])
        step = (float(self.speed[row]) * dt) / distance if distance > 0 else 0
        open_time = float(self.open_time[row])
        while n > 0:
            if code == OPENING:
                if step <= 0.0 and progress < 1.0:
                    break                           # it never gets there
                k = _leg(progress, step, True) if step > 0.0 else 1
                if n < k:
                    progress += n * step
                    break
                n -= k
                progress, code, timer = 1.0, OPEN, open_time
            elif code == OPEN:
                k = _countdown(timer, dt)
                if n < k:
                    timer -= n * dt
                    break
                n -= k
                timer -= k * dt
                code = CLOSING
            elif code == CLOSING:
                if step <= 0.0 and progress > 0.0:
                    break
                k = _leg(progress, step, False) if step > 0.0 else 1
                if n < k:
                    progress -= n * step
                    break
                n -= k
                progress, code = 0.0, CLOSED
            else:
                break
        out['code'], out['progress'], out['timer'] = code, progress, timer
        if self.dir_valid[row]:
            direction = self.direction[row].tolist()
        else:
            direction = list(self._authored_direction(row))
        original = self.original[row].tolist()
        out['centre'] = [original[0] + (direction[0] * distance) * progress,
                      original[1] + (direction[1] * distance) * progress,
                      original[2] + (direction[2] * distance) * progress]
        return out

    def _catch_up(self, row, n, dt):
        """:meth:`_after`, stored in the row."""
        out = self._after(row, n, dt)
        if out['code'] != int(self.code[row]) or out['progress'] != float(self.progress[row]):
            if not self.dir_valid[row]:
                self._set_direction(row, self._authored_direction(row))
        self.code[row] = out['code']
        self.progress[row] = out['progress']
        self.open_timer[row] = out['timer']
        self.pos[row] = out['centre']

    def _one(self, logic, row, delta, io, ride):
        brush = self.brushes[row]
        speed = float(self.speed[row])
        distance = float(self.distance[row])
        open_time = brush.get('open_time', 3.0)
        if not self.dir_valid[row]:
            raw = self.raw_direction[row]
            self._set_direction(row, _unit_direction([0, 1, 0] if raw is None else raw))
        direction = self.direction[row].tolist()
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
        self.movers.held_as, self.doors.held_as = 0, 1
        #: The two groups' current brush lists, for the finalizer below
        #: (which must not hold the table itself): a table collected
        #: without a reset (a logic thread stopped mid-play) withdraws what
        #: it published, so no brush is left with a transform no source has.
        self._rows_held = [self.movers.brushes, self.doors.brushes]
        weakref.finalize(self, _withdraw_held, self._rows_held)
        # render table -> ((its generation, mover version, door version),
        # slots). One entry per table: frames alternate between the two render
        # buffers' tables, so a single entry would miss on every frame.
        self._slot_cache = weakref.WeakKeyDictionary()
        #: The rows published since each publication, logged (M4): version ->
        #: (mover rows, door rows). A render table is written only the rows
        #: logged since it was last written (the two render buffers' tables
        #: take turns, so each catches up on what moved during the other's
        #: frame); one further back than the log is written whole.
        self._pose_version = 0
        self._pose_log = deque(maxlen=POSE_LOG)
        #: id(brush) -> (brush, (cx, cy, cz, ax, ay, az, angle)): the runtime
        #: transform of every row that is displaced or turned -- its position
        #: column as it is, and its spin -- refreshed for the rows that move
        #: (:meth:`_publish`). What the resolver reads.
        self.runtime_entries = {}
        #: id(BulkBoxes) -> (weak ref to that query, row versions, row maps,
        #: twins): see fill_boxes.
        self._box_maps = {}
        #: id(brush) -> (brush, position, angle): saved-game placements
        #: restored before the brush's row exists (:meth:`restore_placement`).
        self._pending = {}
        #: A weak reference to the table: what brush_placement.asleep holds
        #: (a parked row must not keep a stopped world's table alive).
        self.ref = weakref.ref(self)
        #: The logic thread (the one that walks the rows), once it has: only
        #: it stores a parked row's catch-up (_Group.settle).
        self.owner = None
        #: The logic thread's runtime, held weakly (set by its mover
        #: runtime): a parked path follower is worked out along its nodes.
        self.logic = _no_logic
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

    # -- parked rows (brush_placement.asleep) ----------------------------------

    def _parked_row(self, brush):
        for group in (self.movers, self.doors):
            if not group.n_parked:
                continue
            row = group.row_of_obj.get(id(brush))
            if row is not None and group.brushes[row] is brush and group.parked[row]:
                return group, row
        return None, None

    def runtime_now(self, brush):
        """A parked brush's runtime transform now, worked out from its row's
        state and the ticks since, storing nothing (any thread)."""
        group, row = self._parked_row(brush)
        if group is None:
            return self.runtime_of(brush)
        return group.runtime_now(row)

    def settle_brush(self, brush):
        """Bring a parked brush's row to now (an input is about to act on
        it): see :meth:`_Group.settle`."""
        group, row = self._parked_row(brush)
        if group is not None:
            group.settle(row)

    def settle_parked(self):
        """Bring every parked row to now (a saved game is about to be
        taken): its state, position and path as they are now."""
        for group in (self.movers, self.doors):
            if group.n_parked:
                for row in np.flatnonzero(group.parked).tolist():
                    group.settle(row)

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
            # _publish_row marks each row.
            centres = (None if here is None
                       else np.asarray(here, dtype=np.float64).tolist())
            for k, row in enumerate(rows):
                self._publish_row(group, int(row), runtime,
                                  None if centres is None else centres[k])
            return
        pick = np.array(rows, dtype=np.intp)
        group._mark_rows(pick)
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
            axes = movers.rot_axis[spin_at]
            axes[~turned] = _UP
            ax, ay, az = axes.T.tolist()
            values = zip(x, y, z, ax, ay, az, angle.tolist())
        group.live[pick] = live
        # Tuples built and stored in C: no bytecode per row (count >= 2, so
        # the getter hands back a tuple).
        chosen = itemgetter(*pick.tolist())(group.brushes)
        values = list(values)
        if live.all():
            runtime.update(zip(map(id, chosen), zip(chosen, values)))
            live_list = None
        else:
            live_list = live.tolist()
            runtime.update(compress(zip(map(id, chosen), zip(chosen, values)), live_list))
            # The rows that are back where they were authored, unturned: no
            # entry.
            for brush in compress(chosen, (~live).tolist()):
                runtime.pop(id(brush), None)
        _publish_slots(chosen, values, live_list)

    def _publish_row(self, group, row, runtime, centre=None, rest=None):
        """:meth:`_publish` for one row, in scalars (*centre* and *rest*:
        its position and authored centre, when the caller holds them)."""
        group._mark_row(row)
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
                group.live[row] = False
                runtime.pop(id(brush), None)
                _publish_slot(brush, None)
                return
            ax, ay, az = 0.0, 1.0, 0.0
        group.live[row] = True
        transform = (cx, cy, cz, ax, ay, az, angle)
        runtime[id(brush)] = (brush, transform)
        _publish_slot(brush, transform)

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
        # The other group's twin rows name this group's old rows (and this
        # group's are unset): link them to the rows as they are now before
        # either group is published -- stopping play empties one group
        # before the other.
        self._link_twins()
        # Built aside and swapped in whole: a reader on another thread never
        # sees a half-built dict (every mover momentarily at home).
        entries = {}
        for each in (self.movers, self.doors):
            if each.brushes:
                self._publish(each, list(range(len(each.brushes))), entries)
                each.quiet(range(len(each.brushes)))
        self.runtime_entries = entries
        brush_placement.note_runtime_change()

    def fill_boxes(self, bulk, centre, half, claimed):
        """Fill a :class:`engine.brush_placement.BulkBoxes` query from the
        columns: for each of its brushes this table holds a live row for
        (one with a runtime entry) and no earlier source has claimed, its
        world box -- the row's position and turned half-extents, gathered by
        index. A brush in both groups (rare) is answered from its runtime
        entry instead. The row map is rebuilt only when the rows are."""
        key = (self.movers.version, self.doors.version, getattr(self, '_linked', None))
        entry = self._box_maps.get(id(bulk))
        if entry is None or entry[0]() is not bulk or entry[1] != key:
            maps, twins = [], []
            for group in (self.movers, self.doors):
                at, rows = [], []
                for i, brush in enumerate(bulk.brushes):
                    row = group.row_of_obj.get(id(brush))
                    if row is None or group.brushes[row] is not brush:
                        continue
                    if group.twin_row[row] >= 0:
                        if group is self.movers:
                            twins.append(i)
                        continue
                    at.append(i)
                    rows.append(row)
                maps.append((group, np.asarray(at, dtype=np.intp),
                             np.asarray(rows, dtype=np.intp)))
            if len(self._box_maps) > 8:
                self._box_maps.clear()
            # The query is held weakly: the table must not keep a collision
            # structure's brushes alive (after Stop, or a rebuild).
            entry = self._box_maps[id(bulk)] = (weakref.ref(bulk), key, maps, twins)
        # Nothing claimed yet (the usual case: the editor preview, asked
        # first, is empty): the claimed mask need not be consulted.
        fresh = not claimed.any()
        for group, at, rows in entry[2]:
            if not len(at):
                continue
            take = group.live[rows]
            if not fresh:
                take &= ~claimed[at]
            if take.all():
                at_t, rows_t = at, rows             # every row moving: no picks
            elif take.any():
                at_t, rows_t = at[take], rows[take]
            else:
                continue
            centre[at_t] = group.pos[rows_t]
            if group is self.movers:
                group.settle_turns(rows_t)
            half[at_t] = group.turned[rows_t]
            claimed[at_t] = True
        for i in entry[3]:
            if not claimed[i]:
                bulk.claim(i, self.runtime_entries.get(id(bulk.brushes[i])), centre, half, claimed)

    def position_of(self, brush):
        """Where *brush* is now (its centre): its row's position, or its
        authored centre when it has no row."""
        for group in (self.doors, self.movers):
            row = group.row_of_obj.get(id(brush))
            if row is not None and group.brushes[row] is brush:
                if group.n_parked and group.parked[row]:
                    runtime = group.runtime_now(row)
                    if runtime is None:
                        return tuple(group.original[row].tolist())
                    return (runtime[0], runtime[1], runtime[2])
                return tuple(group.pos[row].tolist())
        return tuple(brush_placement.authored_box(brush)[:3])

    def move_to(self, brush, position):
        """Put *brush*'s row (a path mover) at *position*, its centre."""
        movers = self.movers
        row = movers.row_of_obj.get(id(brush))
        if row is None or movers.brushes[row] is not brush:
            return False
        if movers.n_parked and movers.parked[row]:
            movers.settle(row)
        # One row, in scalars (_store_one), as the per-row walk stores it.
        centre = [float(position[0]), float(position[1]), float(position[2])]
        movers.check_range(row, centre=centre)        # a path goes anywhere
        movers._store_one(row, centre, movers.original[row].tolist())
        movers.quiet((row,))
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
            if group.n_parked and group.parked[row]:
                group.settle(row)
            if group is self.movers:
                group.rot_angle[row] = float(angle)
            group.check_range(row, centre=position, angle=float(angle))
            group._store(np.array([row]), np.array([position]))
            group.quiet((row,))
        if not found:
            self._pending[id(brush)] = (brush, position, float(angle))

    def reset(self):
        """Play stopped: no row is displaced or turned any more, and what
        was published for the rows is withdrawn (the readers' caches go back
        to their own rules)."""
        _withdraw_held(self._rows_held)
        self.runtime_entries = {}
        self._pending = {}
        self._slot_cache = weakref.WeakKeyDictionary()     # written whole next
        self.movers.live[:] = False
        self.doors.live[:] = False
        for group in (self.movers, self.doors):
            group.parked[:] = False
            group.n_parked = 0
        self._box_maps = {}
        brush_placement.note_runtime_change()

    def sync(self):
        """Re-read the brush parameters of every row the journal names."""
        changes = JOURNAL.drain(self)
        groups = (self.movers, self.doors)
        if changes is OVERFLOW:
            for group in groups:
                rows = list(range(len(group.index)))
                for row in rows:
                    group.resync(row)
                    group.dirty.add(row)
                # An authored edit can rebase a row (resync): its runtime
                # transform is republished, not left at the old position.
                self._publish(group, rows)
                group.quiet(rows)
                for row in rows:
                    group.note_parking(row)
            return
        if not changes:
            return
        parked = False
        for group in groups:
            rows = []
            woken = []
            for oid, flags in changes.items():
                row = group.row_of_obj.get(oid)
                if row is None:
                    continue
                group.dirty.add(row)
                if flags == VISIBILITY:
                    # Only its live hidden flag: a streaming layer parking
                    # or unparking it (a crossing parks thousands at once:
                    # the woken rows are published together, one stamp).
                    change = group.note_parking(row, publish=False)
                    if change == 'woken':
                        woken.append(row)
                    elif change == 'parked':
                        parked = True
                    continue
                group.resync(row)
                rows.append(row)
            if woken:
                self._publish(group, woken)
            if rows:
                self._publish(group, rows)
                group.quiet(rows)
                for row in rows:
                    group.note_parking(row)
        if parked:
            brush_placement.note_runtime_change()

    def tick_movers(self, logic, delta):
        if self.owner is None:
            self.owner = threading.get_ident()
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
        self._link_twins()

    def _link_twins(self):
        """Which row of the other group holds the same brush (a brush that
        is both a mover and a door), worked out from both groups' rows as
        they are now."""
        movers, doors = self.movers, self.doors
        movers.twin, doors.twin = doors, movers
        movers.twin_row[:] = -1
        doors.twin_row[:] = -1
        for row, brush in enumerate(movers.brushes):
            other = doors.row_of_obj.get(id(brush))
            if other is not None:
                movers.twin_row[row] = other
                doors.twin_row[other] = row
        movers.any_twin = doors.any_twin = bool((movers.twin_row >= 0).any())
        if movers.any_twin:
            # A brush in both groups is never parked (note_parking).
            for group in (movers, doors):
                if group.n_parked:
                    for row in np.flatnonzero(group.parked & (group.twin_row >= 0)).tolist():
                        group.wake(row)

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
        fresh = cached is None or cached[0] != key
        if fresh:
            slot_of = table._slot_of_obj
            maps = []
            for group in (movers, doors):
                slots = [slot_of.get(id(brush), -1) for brush in group.brushes]
                slots = np.asarray(slots, dtype=np.intp)
                ok = slots >= 0
                maps.append((np.flatnonzero(ok), slots[ok], slots))
            # [key, row/slot maps, the pose version the table holds]
            cached = [key, tuple(maps), None]
            self._slot_cache[table] = cached
        started = change_detection.now()
        # What was published since the last publication -- the tick's own
        # record of the rows it moved (M4) -- logged for both render tables.
        # A group whose every row was published is "every row" (None).
        marks_m = movers.take_marks()
        marks_d = doors.take_marks()
        if marks_m is None or marks_d is None or len(marks_m) or len(marks_d):
            self._pose_version += 1
            self._pose_log.append((self._pose_version, (marks_m, marks_d)))
        rows_m, rows_d = (None, None) if fresh else self._rows_since(cached[2])
        cached[2] = self._pose_version
        (all_m, slots_m, slot_of_m), (all_d, slots_d, slot_of_d) = cached[1]
        writes = []
        if rows_m is None or len(rows_m) >= len(slot_of_m):
            rows_m = all_m
        elif len(rows_m):
            picked = slot_of_m[rows_m]
            held = picked >= 0
            rows_m, slots_m = rows_m[held], picked[held]
        if len(rows_m):
            angle = movers.rot_angle[rows_m]
            rot = np.zeros((len(rows_m), 4), dtype=table.rot.dtype)
            spun = angle != 0.0
            if spun.any():
                rot[spun, :3] = movers.rot_axis[rows_m][spun]
                rot[spun, 3] = angle[spun]
            writes.append((slots_m, movers.pos[rows_m], rot))
        if rows_d is None or len(rows_d) >= len(slot_of_d):
            rows_d = all_d
        elif len(rows_d):
            picked = slot_of_d[rows_d]
            held = picked >= 0
            rows_d, slots_d = rows_d[held], picked[held]
        if len(rows_d):
            # Doors last: a brush that is both is left as the door put it.
            writes.append((slots_d, doors.pos[rows_d], None))
        if writes:
            table.write_transforms_many(writes)
        # The rows written: those published since this table was last
        # written, or every row when it was not (or a rebuild came between).
        change_detection.DETECT.add('mover.publish', len(rows_m) + len(rows_d),
                                    change_detection.now() - started)
        if change_detection.ORACLE:
            self._check_published(table, cached[1])

    def _rows_since(self, since):
        """``(mover rows, door rows)`` published since *table* was last
        written (None: every row; a row may appear more than once) -- a brush
        in both groups counts in both. *since*: the pose version the table
        holds (None: none)."""
        if since is None:
            return None, None
        if since == self._pose_version:
            return _NO_ROWS, _NO_ROWS
        log = self._pose_log
        if not log or log[0][0] > since + 1:
            return None, None
        movers, doors = self.movers, self.doors
        if since == self._pose_version - 1 and not movers.any_twin:
            return log[-1][1]               # one publication behind: its rows
        parts = ([], [])
        for version, marks in log:
            if version <= since:
                continue
            for k in (0, 1):
                if marks[k] is None:
                    parts[k].append(None)
                elif len(marks[k]):
                    parts[k].append(marks[k])
        rows = []
        for k in (0, 1):
            if any(p is None for p in parts[k]):
                rows.append(None)
            elif not parts[k]:
                rows.append(_NO_ROWS)
            else:
                rows.append(parts[k][0] if len(parts[k]) == 1
                            else np.concatenate(parts[k]))
        if movers.any_twin and rows[0] is not None and rows[1] is not None:
            # The twin of a row that moved moved with it.
            twins_d = movers.twin_row[rows[0]]
            twins_m = doors.twin_row[rows[1]]
            rows = [np.concatenate((rows[0], twins_m[twins_m >= 0])),
                    np.concatenate((rows[1], twins_d[twins_d >= 0]))]
        return rows[0], rows[1]

    def _check_published(self, table, maps):
        """Oracle (M4): every awake mover's and door's row in *table* holds
        what the group holds. A row that differs was moved without being
        published (marked): a writer outside the change record. Parked rows
        are hidden, and written when they wake."""
        movers, doors = self.movers, self.doors
        problems = []
        for group, (rows, slots, _slot_of_row) in zip((movers, doors), maps):
            awake = ~group.parked[rows]
            rows, slots = rows[awake], slots[awake]
            if not len(rows):
                continue
            differ = (table.bounds[slots, :3] != group.pos[rows]).any(axis=1)
            if group is movers:
                angle = movers.rot_angle[rows].astype(table.rot.dtype)
                differ |= table.rot[slots, 3] != angle
            if differ.any():
                problems.append((type(group).__name__, rows[differ].tolist()[:8],
                                 slots[differ].tolist()[:8]))
        if problems:
            change_detection.missed(
                "mover rows changed without being published: %r" % (problems,))
