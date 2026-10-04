"""The Debug Tables instrument must not hold the renderer's frame.

Publication is double-buffered: while anything borrows the published frame,
the logic thread cannot swap. The instrument used to keep the snapshot it
sampled until its next refresh, a quarter of a second later, and so held a
borrow permanently -- with it open, the renderer never saw another frame.
"""

import os
import random
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the instrument is a Qt window")

from engine.threaded_game_state import ThreadedGameState      # noqa: E402
from tests.helpers.worlds import box_brush                    # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def window(qt_app):
    from tools.debug_tables import DebugTablesWindow
    from editor.editor_state import EditorState
    from editor.main_window import MainWindow
    from engine.logic_thread import LogicThread

    game_state = ThreadedGameState()
    logic = LogicThread(game_state, EditorState())
    write = game_state.get_write_state()
    write.render_table.sync([box_brush("wall")], 1)
    write.visible_brush_slots = np.array([0], dtype=np.int32)
    write.prepare_ms = 1.5
    assert game_state.request_swap() is True

    host = MainWindow(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    host.view_3d.logic_thread = logic
    instrument = DebugTablesWindow(host)
    instrument.timer.stop()
    yield instrument, game_state
    instrument.close()
    host.close()
    host.deleteLater()
    qt_app.processEvents()


def test_sampling_does_not_hold_the_published_frame(window):
    instrument, game_state = window
    instrument.refresh()

    assert game_state.request_swap() is True, (
        "the instrument is still borrowing the frame it sampled, so the "
        "logic thread can never publish another")


def test_what_it_shows_is_a_copy_of_the_sampled_frame(window):
    instrument, game_state = window
    instrument.refresh()
    shown = instrument.render

    game_state.request_swap()                 # both buffers change hands
    game_state.get_write_state().render_table.center[0] = 999.0

    assert shown.count == 1
    assert shown.center[0].tolist() != [999.0, 999.0, 999.0]
    assert "prepare (logic thread)" in instrument.dashboard.toPlainText()
    assert "1.500 ms" in instrument.dashboard.toPlainText()


@pytest.fixture
def monster_window(window):
    """The instrument attached to the real MonsterAI after a dense tick."""
    from editor.editor_state import EditorState
    from editor.things import Monster
    from engine.logic_thread import LogicThread
    from engine.monster_ai import MonsterAIThread
    from engine.physics import SpatialGrid
    from engine.player import Player
    from engine.threaded_game_state import ThreadedGameState
    from tests.helpers.worlds import box_brush, make_thing

    instrument, game_state = window
    state = EditorState()
    state.brushes = [
        box_brush("ground", (0, -16, 0), (8192, 32, 8192))
    ]
    state.things = [
        *[
            make_thing(
                Monster, "m%d" % i, (300.0 * (i + 1), 96, 0),
                monster_type="human", awake=True, team="red",
            )
            for i in range(3)
        ],
        make_thing(Monster, "corpse", (0, 96, 900), dead=True),
    ]

    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player = Player(0.0, 0.0)
    logic.player_runtime.player.pos.y = 0.0
    logic.world_runtime.build_entity_caches()
    logic.session_runtime.spatial_grid = SpatialGrid(cell_size=512.0)
    logic.session_runtime.spatial_grid.populate(state.brushes)
    logic.monster_ai.set_spatial_grid(logic.session_runtime.spatial_grid)
    logic.monster_ai.update(1.0 / 30.0)

    ai_thread = MonsterAIThread(
        logic, logic.monster_ai, logic.session_runtime.monster_lock
    )
    ai_thread.update_ms = 2.0
    ai_thread.lock_wait_ms = 0.5
    logic.session_runtime.monster_ai_thread = ai_thread

    view = instrument.main_window.view_3d
    view.logic_thread = logic
    return instrument, logic.monster_ai, view.logic_thread.session_runtime.monster_lock


def test_the_monster_table_is_shown_after_a_dense_tick(monster_window):
    instrument, ai, _ = monster_window
    instrument.refresh()

    shown = instrument.monsters
    assert shown is not None and shown.count == 4
    assert shown.path == "dense"
    assert shown.mode[3] == 1                     # the corpse: MODE_DEAD
    text = instrument.dashboard.toPlainText()
    assert "MONSTER AI (MonsterTable)" in text
    assert "pass                 dense" in text
    assert "dead 1" in text
    assert "waiting for the monster lock    0.500 ms" in text
    assert instrument.monster_raw.selector.count() > 0
    assert "MonsterTable" in instrument.memory_text.toPlainText()


def test_a_busy_monster_lock_is_never_waited_on(monster_window):
    import threading

    instrument, ai, lock = monster_window
    instrument.refresh()
    before = instrument.monsters

    held, release = threading.Event(), threading.Event()

    def ai_tick():
        with lock:
            held.set()
            release.wait(5.0)

    worker = threading.Thread(target=ai_tick)
    worker.start()
    held.wait(5.0)
    try:
        instrument.refresh()                      # returns: does not block
        assert instrument.monsters is before      # the last copy is kept
    finally:
        release.set()
        worker.join()


def test_the_export_includes_the_monster_table(monster_window, tmp_path):
    import json
    import zipfile

    instrument, ai, _ = monster_window
    instrument.refresh()
    path = tmp_path / "snapshot.zip"
    from unittest import mock
    with mock.patch("tools.debug_tables.QFileDialog.getSaveFileName",
                    return_value=(str(path), "")):
        instrument.export_snapshot()
    assert "EXPORT FAILED" not in instrument.status.text(), instrument.status.text()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        pipeline = json.loads(archive.read("pipeline.json"))
    assert "MonsterTable/mode.npy" in names
    assert pipeline["monster_rows"] == 4
    assert pipeline["monster_pass"] == "dense"


def test_export_writes_every_table_of_a_real_frame(window, tmp_path):
    """The tables' ``refs`` columns hold objects, which ``np.save`` refuses;
    they used to abort the export after the first table."""
    import zipfile
    from unittest import mock

    instrument, _ = window
    instrument.refresh()
    path = tmp_path / "snapshot.zip"
    with mock.patch("tools.debug_tables.QFileDialog.getSaveFileName",
                    return_value=(str(path), "")):
        instrument.export_snapshot()
    assert "EXPORT FAILED" not in instrument.status.text(), instrument.status.text()
    with zipfile.ZipFile(path) as archive:
        tables = {n.split("/")[0] for n in archive.namelist() if n.endswith(".npy")}
    assert {"RenderTable", "EntityTable"} <= tables


def test_follow_selection_names_the_render_row_key_and_run(window):
    instrument, game_state = window
    brush = game_state._read_state.render_table.brushes[0]
    instrument.main_window.state.selected_objects = [brush]
    instrument.refresh()
    status = instrument.status.text()
    assert "FOLLOW id=%s" % brush["id"] in status
    assert "render-row=0" in status and "key=0x" in status and "run=0" in status


# ---------------------------------------------------------------------------
# TerrainTable
# ---------------------------------------------------------------------------

def _terrain_host(instrument):
    """Give the instrument's host a terrain with a few built chunks."""
    from engine.terrain import Terrain
    terrain = Terrain()
    for cx in range(3):
        slot = terrain.table.ensure(
            cx, 0, terrain.chunk_size, terrain.offset_x, terrain.offset_z
        )
        if cx < 2:
            terrain.table.store(
                slot, 48, 0,
                np.full((51, 51), 10.0 * cx, dtype=np.float32),
            )
    terrain.table.release([terrain.table.slot_of_coord[(2, 0)]])
    terrain.enabled = True
    terrain.streaming = True
    terrain.stream_radius = 2048.0
    terrain.drawn_slots = np.array([0, 1], dtype=np.intp)
    terrain.culled_chunks = 0
    terrain.total_triangles = 9216
    terrain._height_pages = [7]
    terrain._page_layers = 512
    terrain.use_textures = True
    terrain.grass_enabled = False
    terrain.UPDATE_BUDGET_MS = 4.0
    terrain.MAX_UPDATES_PER_FRAME = 2
    instrument.main_window.terrain = terrain
    return terrain


def test_the_terrain_table_is_shown(window):
    instrument, _ = window
    terrain = _terrain_host(instrument)
    instrument.refresh()

    assert instrument.terrain is not None
    assert ("TerrainTable", instrument.terrain) in instrument._tables()
    fields = [instrument.terrain_raw.selector.itemText(i)
              for i in range(instrument.terrain_raw.selector.count())]
    assert {"coord", "live", "built", "lod", "heights"} <= set(fields)
    # Rows are shown up to the allocated extent, the freed slot included.
    assert instrument.terrain.count == 3 and instrument.terrain.live_count == 2
    text = instrument.dashboard.toPlainText()
    assert "TERRAIN (TerrainTable)" in text
    assert "resident 2" in text and "built 2" in text
    assert "48x48: 2" in text
    assert "TerrainTable" in instrument.memory_text.toPlainText()

    terrain.table.heights[0, 0, 0] = 999.0         # the live table moves on
    assert instrument.terrain.heights[0, 0, 0] == 0.0, "the copy tracked the live table"


def test_a_map_without_terrain_has_no_terrain_table(window):
    instrument, _ = window
    instrument.main_window.terrain = None
    instrument.refresh()
    assert instrument.terrain is None
    assert all(label != "TerrainTable" for label, _ in instrument._tables())
    assert "terrain_table is None" in instrument.dashboard.toPlainText()


def test_the_export_includes_the_terrain_table(window, tmp_path, monkeypatch):
    import json
    import zipfile
    from PyQt5.QtWidgets import QFileDialog
    instrument, _ = window
    _terrain_host(instrument)
    instrument.refresh()
    path = tmp_path / "snapshot.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    instrument.export_snapshot()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        pipeline = json.loads(archive.read("pipeline.json"))
    assert "TerrainTable/heights.npy" in names
    assert pipeline["terrain_resident"] == 2



@pytest.fixture
def real_world_window(qt_app):
    """Attach Debug Tables to a real generated map and real LogicThread."""
    from editor.editor_state import EditorState
    from editor.main_window import MainWindow
    from editor.procedural_generator import create_map_data
    from editor.things import Light, Sprite
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState
    from tests.helpers.worlds import make_thing

    random.seed(0xF10)
    data = create_map_data({
        "world_width": 1024,
        "world_height": 1024,
        "room_count": 8,
        "min_room": 192,
        "max_room": 256,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "random_wall_texture": False,
        "random_floor_texture": False,
        "enable_floors": False,
        "spawn_monsters": False,
        "spawn_health": False,
    })

    state = EditorState()
    state.load_from_data(data, save_undo=False)

    generated_lights = [thing for thing in state.things if isinstance(thing, Light)]
    assert generated_lights, "procedural map must contain its generated room lights"
    light = generated_lights[0]
    light.properties["casts_shadows"] = True

    sprite = make_thing(Sprite, "audit_sprite", light.pos,
                        sprite="Dev/monster.png")
    state.things.append(sprite)
    state.mark_world_changed([light, sprite])

    game_state = ThreadedGameState()
    logic = LogicThread(game_state, state)
    logic._prepare_render_state()
    assert game_state.request_swap() is True
    logic._prepare_render_state()
    assert game_state.request_swap() is True

    host = MainWindow(os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", ".."
    )))
    host.state = state
    host.view_3d.logic_thread = logic

    from tools.debug_tables import DebugTablesWindow
    instrument = DebugTablesWindow(host)
    instrument.timer.stop()
    yield instrument, state, logic

    instrument.close()
    host.close()
    host.deleteLater()
    qt_app.processEvents()


def test_debug_tables_is_an_oracle_for_a_real_authored_world(real_world_window):
    """Debug Tables must expose the dense rows produced by the real world path."""
    instrument, state, logic = real_world_window
    instrument.refresh()

    assert instrument.render is not None
    assert instrument.entities is not None
    assert instrument.render.count == len(state.brushes)
    assert instrument.entities.count == len(state.things)

    for brush in state.brushes:
        slot = instrument.render.slot_of_id[brush["id"]]
        assert instrument.render.center[slot].tolist() == pytest.approx(brush["pos"])
        assert instrument.render.bounds[slot, 3:6].tolist() == pytest.approx(
            [brush["size"][i] * 0.5 for i in range(3)]
        )

    for thing in state.things:
        ident = thing.properties["id"]
        slot = instrument.entities.slot_of_id[ident]
        assert instrument.entities.pos[slot].tolist() == pytest.approx(
            thing.pos.tolist()
        )

    light = next(thing for thing in state.things if isinstance(thing, Light))
    light_slot = instrument.entities.slot_of_id[light.properties["id"]]
    assert instrument.entities.pos[light_slot].tolist() == pytest.approx(
        light.pos.tolist()
    )
    assert bool(instrument.entities.light_casts_shadows[light_slot])
    assert instrument.entities.light_params[light_slot, 0] == pytest.approx(2.0)\n    assert instrument.entities.light_params[light_slot, 1] == pytest.approx(1000.0)

    sprite = next(thing for thing in state.things if thing.properties["id"] == "audit_sprite")
    sprite_slot = instrument.entities.slot_of_id[sprite.properties["id"]]
    assert int(instrument.entities.sprite_key_id[sprite_slot]) >= 0


def test_debug_tables_tracks_a_real_light_move_in_the_dense_entity_row(
    real_world_window,
):
    """A real authoring mutation must become a new EntityTable row value."""
    instrument, state, logic = real_world_window

    instrument.refresh()
    light = next(thing for thing in state.things if isinstance(thing, Light))
    ident = light.properties["id"]
    before = instrument.entities.slot_of_id[ident]
    old = instrument.entities.pos[before].copy()

    light.pos = [320.0, 256.0, 192.0]
    state.mark_world_changed([light])
    logic._prepare_render_state()
    assert logic.game_state.request_swap() is True

    instrument.refresh()
    slot = instrument.entities.slot_of_id[ident]
    assert slot == before
    assert instrument.entities.pos[slot].tolist() == pytest.approx(
        [320.0, 256.0, 192.0]
    )
    assert not np.array_equal(old, instrument.entities.pos[slot])


def test_debug_tables_detects_the_real_brush_set_change(real_world_window):
    """Adding authored geometry must appear as a new dense RenderTable row."""
    instrument, state, logic = real_world_window
    instrument.refresh()
    before = instrument.render.count

    added = box_brush("late_wall", (0, 128, -320), (128, 256, 64))
    state.brushes.append(added)
    state.mark_world_changed([added])
    logic._prepare_render_state()
    assert logic.game_state.request_swap() is True

    instrument.refresh()
    assert instrument.render.count == before + 1
    slot = instrument.render.slot_of_id[added["id"]]
    assert instrument.render.center[slot].tolist() == pytest.approx(added["pos"])
