"""The bundled reel-to-reel pack and the map that shows it off.

``assets/bundled_reel_to_reel.fiopak`` is a user pack shipped with Fio: two
mainframes and three tape reels (GLB). ``maps/Reel_To_Reel_Test.json``
(built by ``tools/make_reel_to_reel_test_map.py``) mounts two reels on each
mainframe's hubs; one machine's reels run from the start of Play, a button
toggles the other's and a second button reverses the first's.
"""

import json
import os

import pytest

from tests.helpers.paths import REPO_ROOT

PACK = os.path.join(REPO_ROOT, "assets", "bundled_reel_to_reel.fiopak")
MAP = os.path.join(REPO_ROOT, "maps", "Reel_To_Reel_Test.json")
MODELS = tuple("assets/models/%s.glb" % name for name in (
    "reel_mainframe_tall", "reel_mainframe_wide",
    "tape_reel_blue", "tape_reel_cream", "tape_reel_orange"))

SUPPLY = ("Tall_Reel_Supply", "Wide_Reel_Supply")
TAKEUP = ("Tall_Reel_Takeup", "Wide_Reel_Takeup")
TALL_REELS = ("Tall_Reel_Supply", "Tall_Reel_Takeup")
WIDE_REELS = ("Wide_Reel_Supply", "Wide_Reel_Takeup")


@pytest.fixture
def state(qt_app):
    from editor.editor_state import EditorState
    with open(MAP, encoding="utf-8") as fh:
        level = json.load(fh)
    loaded = EditorState()
    loaded.load_from_data(level, save_undo=False)
    return loaded


def _named(state, name):
    found = state.find_entity_by_name(name)
    assert found is not None, "%s is not in the map" % name
    return found


# -- the pack ---------------------------------------------------------------

def test_the_pack_is_a_data_only_zip_of_models():
    import zipfile
    with zipfile.ZipFile(PACK) as pack:
        assert pack.testzip() is None
        assert sorted(pack.namelist()) == sorted(MODELS)


def test_the_pack_models_resolve_through_the_asset_reader():
    from engine import asset_archives
    asset_archives.prepare_asset_archives(REPO_ROOT)
    assert "bundled_reel_to_reel.fiopak" in asset_archives.user_packs()
    for model in MODELS:
        assert asset_archives.asset_exists(model), model
        assert asset_archives.asset_archive_name(model) == "bundled_reel_to_reel.fiopak"
        assert asset_archives.asset_bytes(model)[:4] == b"glTF"
    listed = {p.replace(os.sep, "/")              # a backslash on Windows
              for p in asset_archives.asset_files("assets/models", (".glb",))}
    assert set(MODELS) <= listed, "the Asset Browser would not list them"


def test_a_reel_spins_about_its_hub():
    """A reel is 40 x 40 x 9.35, its hub on local Z: Axis 'auto' is Z."""
    pytest.importorskip("OpenGL", reason="the GLB loader imports PyOpenGL")
    from engine import asset_archives
    from engine.glb_loader import GLBLoader
    from engine.model_rotate import axis_index
    import numpy as np

    asset_archives.prepare_asset_archives(REPO_ROOT)
    path = os.path.join(REPO_ROOT, "assets", "models", "tape_reel_blue.glb")
    loader = GLBLoader()
    assert loader.load(path)
    verts = np.asarray(loader.get_flattened_vertices(), dtype=float).reshape(-1, 3)
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    assert hi - lo == pytest.approx((40.0, 40.0, 9.35), abs=0.01)
    assert axis_index("auto", (lo, hi)) == 2


# -- the map ----------------------------------------------------------------

def test_the_map_has_a_room_a_start_a_light_and_both_mainframes(state):
    types = [t.properties["type"] for t in state.things]
    assert "playerstart" in types and "light" in types
    assert len(state.brushes) >= 6
    paths = {t.properties.get("model_path") for t in state.things}
    assert set(MODELS) <= paths


def test_every_reel_spins_and_one_machine_starts_running(state):
    for name in TALL_REELS + WIDE_REELS:
        props = _named(state, name).properties
        assert props["rotate_enabled"] is True, name
        assert props["rotate_axis"] == "auto", name
        assert props["rotate_autostart"] is (name in WIDE_REELS), name
    for supply, takeup in zip(SUPPLY, TAKEUP):
        a = _named(state, supply).properties["rotate_speed"]
        b = _named(state, takeup).properties["rotate_speed"]
        # The same way round, the take-up reel a little faster.
        assert a * b > 0 and abs(b) > abs(a)


def test_the_reels_sit_on_their_mainframes_hubs(state):
    """Hub centres read from the GLBs: tall (+-22, 151), wide (+-27, 142);
    the reel's back (local z -3.35) on the collar's front."""
    hubs = {"Mainframe_Tall": (TALL_REELS, 22.0, 151.0, 33.7),
            "Mainframe_Wide": (WIDE_REELS, 27.0, 142.0, 32.5)}
    for frame_name, (reels, dx, dy, collar) in hubs.items():
        frame = _named(state, frame_name)
        assert frame.properties["rotation"] == [0, 0, 0]
        for reel, side in zip(reels, (-1.0, 1.0)):
            thing = _named(state, reel)
            assert thing.properties["rotation"] == [0, 0, 0]
            assert thing.pos == pytest.approx(
                [frame.pos[0] + side * dx, frame.pos[1] + dy, frame.pos[2] + collar + 3.35])


def test_the_buttons_toggle_one_machine_and_reverse_the_other(state):
    from editor.io_system import get_connections
    wiring = {"Toggle_Tall_Reels": ("ToggleRotate", TALL_REELS),
              "Reverse_Wide_Reels": ("ReverseRotate", WIDE_REELS)}
    for button, (input_name, reels) in wiring.items():
        brush = next(b for b in state.brushes if b.get("name") == button)
        assert brush.get("is_trigger") and brush.get("trigger_activation") == "use"
        assert brush.get("trigger_type") == "Multiple"
        connections = get_connections(brush)
        assert sorted(c.target_name for c in connections) == sorted(reels)
        for conn in connections:
            assert (conn.output_name, conn.input_name) == ("OnTrigger", input_name)
            assert conn.target_id == _named(state, conn.target_name).properties["id"]


def test_the_maps_connections_all_validate(state):
    from editor.io_system import validate_all_scene_connections
    report = validate_all_scene_connections(state.brushes, state.things)
    assert report["problems"] == [], [m for *_, m in report["problems"]]


# -- in the editor and in Play ------------------------------------------------

def test_the_editor_opens_the_map(main_window):
    assert main_window.load_level_file(MAP)
    names = {t.properties.get("name") for t in main_window.state.things}
    assert set(TALL_REELS + WIDE_REELS) <= names


def test_in_play_one_machine_runs_and_the_buttons_drive_the_reels(fio_session):
    session = fio_session("maps/Reel_To_Reel_Test.json")
    state = session.state
    reels = {name: _named(state, name) for name in TALL_REELS + WIDE_REELS}
    session.start_play()
    logic = session.logic
    session.step(30)
    seconds = 30 * logic.TICK_DURATION

    for name, reel in reels.items():
        assert reel._rotate_axis == 2, name
    # Autostarted at full speed, clockwise (a negative angle, wrapped).
    for name in WIDE_REELS:
        speed = reels[name].properties["rotate_speed"]
        assert reels[name]._rotate_angle == pytest.approx((speed * seconds) % 360.0, abs=0.5)
    for name in TALL_REELS:
        assert reels[name]._rotate_angle == 0.0

    def press(button):
        brush = next(b for b in state.brushes if b.get("name") == button)
        logic.io_manager.fire_output(brush, "OnTrigger")

    press("Toggle_Tall_Reels")
    session.step(120)
    for name in TALL_REELS:
        assert logic.prop_runtime.rotate_velocity_of(reels[name]) == pytest.approx(
            reels[name].properties["rotate_speed"]), name

    press("Reverse_Wide_Reels")
    session.step(180)
    for name in WIDE_REELS:
        assert logic.prop_runtime.rotate_velocity_of(reels[name]) == pytest.approx(
            -reels[name].properties["rotate_speed"]), name

    press("Toggle_Tall_Reels")
    session.step(120)
    for name in TALL_REELS:
        assert logic.prop_runtime.rotate_velocity_of(reels[name]) == 0.0, name
    session.stop_play()
    assert all(reel._rotate_angle == 0.0 for reel in reels.values())
