"""``terrain_stream``: stream an authored terrain's chunks inside its own bounds.

'Fill world with terrain' re-bounds the terrain to the world's extent, which
moves anything laid over the authored bounds (a heightmap overlay). With
``terrain_stream`` the chunks around the camera stream in and out, but the
terrain keeps the bounds it was authored with, and gets its streaming state
back when the session stops.
"""

import pytest

from plugins.bigworld.config import config_from_properties
from plugins.bigworld.runtime import BigWorldSession

from .test_bigworld_tiers import make_thing

pytest.importorskip("PyQt5", reason="Big World sessions run on a real LogicThread")

pytestmark = pytest.mark.qt

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState


class _Terrain:
    chunk_size = 2048.0
    min_chunk_x, max_chunk_x, min_chunk_z, max_chunk_z = -20, 19, -20, 19
    streaming = False
    stream_radius = 1536.0
    stream_evict_padding = 512.0

    def set_streaming(self, on, radius=None):
        self.streaming = on
        if radius:
            self.stream_radius = radius

    def set_bounds(self, *a, **k):
        raise AssertionError("terrain_stream must not re-bound the terrain")

    def set_world_extent(self, *a, **k):
        raise AssertionError("terrain_stream must not re-bound the terrain")


def _logic(terrain):
    state = EditorState()
    state.things = [make_thing(0.0, 0.0)]
    state.brushes = []
    logic = LogicThread(ThreadedGameState(), state)
    from engine.player import Player
    logic.player_runtime.player = Player(0.0, 0.0)
    logic.world_runtime.terrain = terrain
    logic.world_runtime.build_entity_caches()
    return logic


def test_the_setting_defaults_off():
    assert config_from_properties({})["terrain_stream"] is False


def test_terrain_streams_inside_its_own_bounds():
    terrain = _Terrain()
    session = BigWorldSession(_logic(terrain), activation_radius=2048.0,
                              terrain_stream=True)
    session.start()
    assert terrain.streaming is True
    # A derived radius: the activation radius plus one chunk.
    assert terrain.stream_radius == pytest.approx(2048.0 + 2048.0)
    assert (terrain.min_chunk_x, terrain.max_chunk_x) == (-20, 19)
    terrain.set_bounds = lambda *a, **k: None     # restoring may re-apply them
    session.stop()
    assert terrain.streaming is False
    assert terrain.stream_radius == pytest.approx(1536.0)


def test_off_by_default_the_terrain_is_left_alone():
    terrain = _Terrain()
    session = BigWorldSession(_logic(terrain), activation_radius=2048.0)
    session.start()
    assert terrain.streaming is False
    session.stop()
