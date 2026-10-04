"""Small, intentionally narrow test doubles and instrumentation helpers.

These helpers do not stand in for production owners such as ``LogicThread``,
``EditorState``, ``ThreadedGameState``, render tables, or the editor window.
Machinery tests must construct those real owners. The doubles below are limited
to deterministic clocks and recording sinks where replacing the downstream
side effect is the thing being tested.
"""

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
