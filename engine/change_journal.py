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


class ChangeJournal:
    """Per-subscriber sets of ``id(obj) -> MOVED|STATE`` since the last drain."""

    def __init__(self):
        self._lock = threading.Lock()
        # subscriber -> dict, or OVERFLOW once it outgrew PENDING_LIMIT.
        self._pending = weakref.WeakKeyDictionary()

    def subscribe(self, subscriber) -> None:
        """Start collecting changes for *subscriber* (idempotent).

        Tables subscribe when they are built, empty: a table with no rows has
        nothing a change could have made stale, and its first reconcile reads
        every row it takes on.
        """
        with self._lock:
            if subscriber not in self._pending:
                self._pending[subscriber] = {}

    def record(self, obj, flags: int) -> None:
        oid = id(obj)
        with self._lock:
            for subscriber, pending in self._pending.items():
                if pending is OVERFLOW:
                    continue
                pending[oid] = pending.get(oid, 0) | flags
                if len(pending) > PENDING_LIMIT:
                    self._pending[subscriber] = OVERFLOW

    def drain(self, subscriber):
        """``{id(obj): flags}`` recorded since the last drain, or OVERFLOW.

        A subscriber the journal does not know has missed everything, so it is
        told to refresh everything.
        """
        with self._lock:
            pending = self._pending.get(subscriber)
            if pending is None:
                self._pending[subscriber] = {}
                return OVERFLOW
            self._pending[subscriber] = {}
            return pending


#: The process-wide journal. Objects notify it; tables drain it.
JOURNAL = ChangeJournal()


def touch(obj, flags: int = STATE) -> None:
    """Tell the render projections that *obj* changed in a way they resolve."""
    JOURNAL.record(obj, flags)


def moved(obj) -> None:
    """Tell the render projections that *obj*'s transform changed."""
    JOURNAL.record(obj, MOVED)


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
