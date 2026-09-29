"""Runtime change notification for the dense render projections.

The render tables (:class:`engine.render_table.RenderTable`,
:class:`engine.entity_table.EntityTable`) are a numeric copy of the scene.
Copying is only cheap if it happens when something changes, not every frame
for everything that *might* have: a frame that re-reads every entity's
position, sprite state and light settings to find the handful that moved
spends almost all of its time confirming that nothing happened.

So the objects say when they change. There are two kinds of change:

* :data:`MOVED` -- only the transform. ``Thing.pos`` records this itself on
  assignment (see :class:`TrackedPosition`), so the AI, physics, movers and
  plugins that move entities by assigning ``pos`` need do nothing more.
* :data:`STATE` -- anything else a row is resolved from: a property the
  renderer reads (``hidden``, ``dead``, a light's ``state``, a brush's tint),
  or runtime render state (a carried prop's yaw, a respawn fade, an effect
  being triggered). Whoever writes such a value calls :func:`touch`.

Editor transactions still go through
:meth:`editor.editor_state.EditorState.mark_world_changed`; this journal is
for what changes while the world is running, where there is no undo
checkpoint to hang a notification on.

Each table subscribes itself when built and :meth:`ChangeJournal.drain`\\ s its
own pending set once per frame, so the two double-buffered tables each see every
change, whichever of them the next frame is built in. A subscriber that stops
draining (a table no frame uses any more) is capped at
:data:`PENDING_LIMIT` entries and then told to refresh everything, so it
cannot grow without bound.
"""

from __future__ import annotations

import threading
import weakref

#: The object's transform changed.
MOVED = 1
#: Something other than the transform that a row is resolved from changed.
STATE = 2

#: Pending entries a subscriber may accumulate before it is told to refresh
#: every row instead.
PENDING_LIMIT = 8192


class _Overflow:
    """Returned by :meth:`ChangeJournal.drain` when the precise set was lost."""

    __slots__ = ()

    def __repr__(self):
        return 'OVERFLOW'


#: Drain result meaning "refresh every row"; see :data:`PENDING_LIMIT`.
OVERFLOW = _Overflow()


class _Sink:
    """One subscriber's pending changes, held apart from the subscriber.

    ``record`` walks a plain tuple of these rather than a
    ``WeakKeyDictionary``: iterating the weak mapping costs an iteration
    guard and a removal commit per call, and ``record`` is called for every
    relay an I/O chain fires -- 24,000 times in one tick when 1000 monsters
    shoot at once, 0.35 s of it in the mapping. The subscriber is still held
    weakly: when it is collected, its finaliser marks the sink dead and the
    next subscribe or drain drops it.
    """

    __slots__ = ('pending', 'alive', '__weakref__')

    def __init__(self):
        self.pending = {}
        self.alive = True

    def _die(self):
        # Runs from the garbage collector, possibly inside ``record`` on this
        # very thread, so it must not take the journal lock: one attribute
        # store, and the sink is skipped from then on.
        self.alive = False


class ChangeJournal:
    """Per-subscriber sets of ``id(obj) -> MOVED|STATE`` since the last drain."""

    def __init__(self):
        self._lock = threading.Lock()
        # subscriber -> _Sink; a sink's pending is a dict, or OVERFLOW once it
        # outgrew PENDING_LIMIT.
        self._sinks = weakref.WeakKeyDictionary()
        # The live sinks, for record() to walk.
        self._live = ()

    def _prune(self):
        """Drop dead sinks from the walk list (caller holds the lock)."""
        if not all(sink.alive for sink in self._live):
            self._live = tuple(sink for sink in self._live if sink.alive)

    def subscribe(self, subscriber) -> None:
        """Start collecting changes for *subscriber* (idempotent).

        Tables subscribe when they are built, empty: a table with no rows has
        nothing a change could have made stale, and its first reconcile reads
        every row it takes on.
        """
        with self._lock:
            if subscriber not in self._sinks:
                self._add(subscriber)

    def _add(self, subscriber):
        sink = _Sink()
        self._sinks[subscriber] = sink
        weakref.finalize(subscriber, sink._die)
        self._prune()
        self._live = self._live + (sink,)
        return sink

    def record(self, obj, flags: int) -> None:
        oid = id(obj)
        with self._lock:
            for sink in self._live:
                pending = sink.pending
                if pending is OVERFLOW or not sink.alive:
                    continue
                pending[oid] = pending.get(oid, 0) | flags
                if len(pending) > PENDING_LIMIT:
                    sink.pending = OVERFLOW

    def record_many(self, objs, flags: int) -> None:
        """:meth:`record` for a batch: one lock, one pass per subscriber.

        What the dense monster pass uses to journal every monster it moved in
        a tick, rather than taking the lock once per monster.
        """
        oids = [id(obj) for obj in objs]
        if not oids:
            return
        with self._lock:
            for sink in self._live:
                pending = sink.pending
                if pending is OVERFLOW or not sink.alive:
                    continue
                get = pending.get
                for oid in oids:
                    pending[oid] = get(oid, 0) | flags
                if len(pending) > PENDING_LIMIT:
                    sink.pending = OVERFLOW

    def drain(self, subscriber):
        """``{id(obj): flags}`` recorded since the last drain, or OVERFLOW.

        A subscriber the journal does not know has missed everything, so it is
        told to refresh everything.
        """
        with self._lock:
            sink = self._sinks.get(subscriber)
            if sink is None:
                self._add(subscriber)
                return OVERFLOW
            pending = sink.pending
            sink.pending = {}
            self._prune()
            return pending


#: The process-wide journal. Objects notify it; tables drain it.
JOURNAL = ChangeJournal()


def touch(obj, flags: int = STATE) -> None:
    """Tell the render projections that *obj* changed in a way they resolve."""
    JOURNAL.record(obj, flags)


def moved(obj) -> None:
    """Tell the render projections that *obj*'s transform changed."""
    JOURNAL.record(obj, MOVED)


def set_positions(objs, positions) -> None:
    """Assign ``pos`` on many entities and journal them as one batch.

    The same result as ``obj.pos = [x, y, z]`` for each -- the stored value is
    a new list of three Python floats -- with one journal lock instead of one
    per entity. *positions* is any ``(N, 3)`` sequence aligned with *objs*.
    """
    for obj, (x, y, z) in zip(objs, positions):
        obj.__dict__['pos'] = [float(x), float(y), float(z)]
    JOURNAL.record_many(objs, MOVED)


class TrackedPosition:
    """``pos`` for entity classes: an ordinary attribute, journalled on write.

    Only ``__set__`` is defined, so reads never call into Python: the value is
    stored in the instance ``__dict__`` under ``pos`` itself, and attribute
    lookup finds it there as it would any plain attribute. Writes record
    :data:`MOVED`. Mutating the list in place (``thing.pos[1] = y``) bypasses
    this, so the engine never does it; assign a new list instead.

    A position is a list of three floats wherever it came from: a ``glm``
    vector or a tuple assigned here is stored as one, so serialisers and the
    projection see one shape.
    """

    __slots__ = ()

    def __set__(self, obj, value):
        if type(value) is not list:
            value = [float(value[0]), float(value[1]), float(value[2])]
        obj.__dict__['pos'] = value
        JOURNAL.record(obj, MOVED)


class TrackedAttribute:
    """A runtime attribute the renderer resolves, journalled on write.

    For state that is not authored (so no editor gesture covers it) but that a
    table row is resolved from -- a respawn fade, a carried sprite's yaw, a
    portal's fade, an effect's playback clock. Unlike :class:`TrackedPosition`
    it has a getter, so an instance that never assigned it reads *default*;
    these are read rarely, and only when a row is resolved.
    """

    __slots__ = ('name', 'default', 'flags')

    def __init__(self, default=None, flags: int = STATE):
        self.name = None
        self.default = default
        self.flags = flags

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return obj.__dict__.get(self.name, self.default)

    def __set__(self, obj, value):
        obj.__dict__[self.name] = value
        JOURNAL.record(obj, self.flags)


def is_tracked(obj) -> bool:
    """Whether *obj* journals its own position changes."""
    return isinstance(getattr(type(obj), 'pos', None), TrackedPosition)
