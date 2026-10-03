"""Fixtures shared by the MonsterAI suite.

MonsterAI reaches into ``editor.things`` for the ``Monster`` and ``PathNode``
classes, which are PyQt-backed, so the whole area is marked ``qt`` — it runs
head-less against the offscreen platform plugin, but PyQt5 has to be installed.
Nothing here needs a display, a GL context or a running thread.
"""

import pytest

pytest.importorskip("PyQt5", reason="editor.things (Monster/PathNode) needs PyQt5")

from editor.things import Monster, PathNode  # noqa: E402

from engine.monster_ai import MonsterAI  # noqa: E402
from tests.helpers.fakes import (FakeLogicThread, FakePlayer,  # noqa: E402
                                 RecordingIOManager)
from tests.helpers.worlds import make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def monster_factory():
    """Builds a ``Monster`` with a fixed name, id and known properties."""
    counter = {"n": 0}

    def _make(name=None, pos=(0, 0, 0), **props):
        counter["n"] += 1
        name = name or "monster_%d" % counter["n"]
        props.setdefault("awake", True)
        props.setdefault("wake_on_sight", True)
        props.setdefault("health", 100)
        props.setdefault("damage", 20)
        props.setdefault("monster_type", "human")
        return make_thing(Monster, name, pos, **props)

    return _make


@pytest.fixture
def path_node_factory():
    def _make(name, pos=(0, 0, 0), **props):
        return make_thing(PathNode, name, pos, **props)
    return _make


@pytest.fixture
def ai_world():
    """``(ai, logic)`` over an empty world with a player at the origin.

    The spatial grid is real (``engine.physics.SpatialGrid``), not a stub: the
    AI's sight, ground and wall queries all go through it, and a fake that
    always answered "clear" would make every behavioural test vacuous.
    """
    def _build(brushes=(), things=(), player_pos=(0.0, 0.0, 0.0)):
        logic = FakeLogicThread(brushes=brushes, things=things,
                                player=FakePlayer(player_pos))
        logic._monster_things = [t for t in logic.things if isinstance(t, Monster)]
        ai = MonsterAI(logic)
        ai.set_spatial_grid(logic.build_spatial_grid())
        return ai, logic
    return _build


@pytest.fixture
def io_manager():
    """A recording stand-in for the I/O manager the AI fires outputs into."""
    return RecordingIOManager()


@pytest.fixture
def flat_ground():
    """A large floor brush so ground monsters have something to stand on."""
    from tests.helpers.worlds import box_brush
    return [box_brush("ground", (0, -16, 0), (8192, 32, 8192))]
