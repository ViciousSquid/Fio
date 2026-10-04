"""Fixtures shared by the MonsterAI suite.

MonsterAI reaches into editor.things for the Monster and PathNode classes,
which are PyQt-backed, so the whole area is marked qt. The behavioural
fixtures use the production LogicThread, player, spatial grid and IOManager.
"""

import glm
import pytest

pytest.importorskip("PyQt5", reason="editor.things (Monster/PathNode) needs PyQt5")

from editor.editor_state import EditorState
from editor.io_system import IOManager
from editor.things import Monster, PathNode
from engine.io_handlers import register_all_input_handlers
from engine.logic_thread import LogicThread
from engine.monster_ai import MonsterAI
from engine.physics import SpatialGrid
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState
from tests.helpers.worlds import make_thing

pytestmark = pytest.mark.qt


@pytest.fixture
def monster_factory():
    """Build a Monster with a fixed name and known gameplay properties."""
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
def ai_world(request):
    """Build MonsterAI against a complete production LogicThread."""
    created = []

    def _build(brushes=(), things=(), player_pos=(0.0, 0.0, 0.0)):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = list(things)
        logic = LogicThread(ThreadedGameState(), state)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.player_runtime.player.pos = glm.vec3(*player_pos)
        logic.player_runtime.player_health = 100
        logic.player_runtime.player_max_health = 100
        logic.world_runtime.build_entity_caches()
        ai = MonsterAI(logic)
        logic.monster_ai = ai
        grid = SpatialGrid()
        grid.populate(state.brushes)
        ai.set_spatial_grid(grid)
        created.append(logic)
        return ai, logic

    def cleanup():
        for logic in created:
            logic.stop()

    request.addfinalizer(cleanup)
    return _build


@pytest.fixture
def io_manager():
    """A real IOManager with a recording wrapper around its real dispatcher."""
    manager = IOManager()
    register_all_input_handlers(manager)
    manager.fired = []
    real_fire_output = manager.fire_output

    def record_and_dispatch(entity, output_name, value=None):
        manager.fired.append((entity, output_name, value))
        return real_fire_output(entity, output_name, value)

    manager.fire_output = record_and_dispatch
    return manager


@pytest.fixture
def flat_ground():
    """A large floor brush so ground monsters have something to stand on."""
    from tests.helpers.worlds import box_brush
    return [box_brush("ground", (0, -16, 0), (8192, 32, 8192))