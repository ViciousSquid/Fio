"""Release hardening for 3.5.1: hostile input is refused, faults stay contained.

Each section is one defect found by the 3.5.1 release audit:

* terrain_data from a map sized allocations and divided with no checks (a
  bounding box of a billion chunks, a NaN chunk size, a zip-bombed paint
  chunk, a list field holding a number);
* a GLB whose primitives all name one accessor, and an OBJ face of a million
  corners, decoded to gigabytes;
* an angled brush with a NaN plane normal, or a coordinate of 1e308, loaded
  and failed Play start half-way;
* package members and manifests of the wrong shape raised instead of being
  skipped; a decal image name could leave the decal folder;
* identical tracebacks flooded the crash log (and truncated away the first);
* a plugin whose register() raised left half its features live; a failing
  event handler logged a traceback on every emit for ever; wrap() could run
  the original twice;
* map logic could run ``condump`` and grow the console queue without bound.

All headless.
"""

import base64
import json
import struct
import sys
import zlib

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Terrain data from a map
# ---------------------------------------------------------------------------

def test_a_huge_terrain_bounding_box_falls_back_to_the_default_bounds():
    from engine.terrain import Terrain

    terrain = Terrain()
    terrain.from_dict({'min_chunk_x': -100000, 'max_chunk_x': 100000,
                       'min_chunk_z': -100000, 'max_chunk_z': 100000})
    span = ((terrain.max_chunk_x - terrain.min_chunk_x + 1)
            * (terrain.max_chunk_z - terrain.min_chunk_z + 1))
    assert span <= Terrain.MAX_BOUNDED_CHUNKS


def test_bad_terrain_numbers_fall_back_to_defaults():
    from engine.terrain import Terrain

    terrain = Terrain()
    terrain.from_dict({'chunk_size': float('nan'), 'offset_x': 'q',
                       'min_chunk_x': 'a', 'base_resolution': 0,
                       'sculpt_grid_resolution': 0, 'mesh_scale': float('inf')})
    assert terrain.chunk_size == Terrain.DEFAULT_CHUNK_SIZE
    assert terrain.offset_x == 0.0
    assert terrain.min_chunk_x == -2
    assert terrain.base_resolution >= 2
    assert terrain.sculpt_grid_resolution > 0
    assert np.isfinite(terrain.mesh_scale)


def test_authored_terrain_numbers_round_trip_unchanged():
    from engine.terrain import Terrain

    terrain = Terrain()
    terrain.from_dict({'chunk_size': 512.0, 'base_resolution': 20,
                       'min_chunk_x': -4, 'max_chunk_x': 4,
                       'min_chunk_z': -3, 'max_chunk_z': 5, 'offset_x': 12.5})
    data = terrain.to_dict()
    assert (data['chunk_size'], data['base_resolution']) == (512.0, 20)
    assert (data['min_chunk_x'], data['max_chunk_x'],
            data['min_chunk_z'], data['max_chunk_z']) == (-4, 4, -3, 5)
    assert data['offset_x'] == 12.5


def test_terrain_list_fields_of_the_wrong_type_are_ignored():
    from engine.terrain import Terrain

    terrain = Terrain()
    terrain.from_dict({'grass_color': 5, 'texture_stamps': 3,
                       'paint_stamps': {'a': 1}, 'sculpt_offsets': 7,
                       'csg_subtractions': 'x', 'custom_biome': 4,
                       'texture_paint': [{'coord': 5, 'data': 'x'}]})
    assert terrain.paint_stamps == [] and terrain.sculpt_offsets == {}


def test_a_zip_bombed_paint_chunk_is_not_inflated():
    from engine.terrain import Terrain

    terrain = Terrain()
    res = terrain.TERRAIN_PAINT_RESOLUTION
    # Ten times the chunk it claims to be; the decoder must stop at one.
    payload = base64.b64encode(zlib.compress(bytes(res * res * 4 * 10))).decode()
    assert terrain._decode_texture_paint([{'coord': [0, 0], 'data': payload}]) == {}

    exact = base64.b64encode(zlib.compress(bytes(res * res * 4))).decode()
    assert (0, 0) in terrain._decode_texture_paint([{'coord': [0, 0], 'data': exact}])


def test_ensure_bounds_refuses_a_grid_too_large_to_make_resident():
    from engine.terrain import Terrain

    terrain = Terrain()
    terrain.table.ensure_bounds((-1000, 1000, -1000, 1000), 256.0, 0.0, 0.0)
    assert terrain.table.count == 0
    terrain.table.ensure_bounds((-1, 1, -1, 1), 256.0, 0.0, 0.0)
    assert terrain.table.count == 9


def test_a_unc_stamp_texture_is_never_read():
    from engine.terrain import Terrain

    terrain = Terrain()
    reads = []
    terrain._load_texture_rgba = lambda path: reads.append(path)
    assert terrain._paint_image('//attacker/share/x.png') is None
    assert terrain._paint_image('\\\\attacker\\share\\x.png') is None
    assert reads == []


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _glb(primitives):
    """A GLB of one 3-vertex accessor that *primitives* primitives all use."""
    positions = np.zeros((3, 3), dtype='<f4').tobytes()
    doc = {
        'asset': {'version': '2.0'},
        'buffers': [{'byteLength': len(positions)}],
        'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': len(positions)}],
        'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3,
                       'type': 'VEC3'}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}] * primitives}],
    }
    text = json.dumps(doc).encode()
    text += b' ' * (-len(text) % 4)
    body = (struct.pack('<II', len(text), 0x4E4F534A) + text
            + struct.pack('<II', len(positions), 0x004E4942) + positions)
    return struct.pack('<III', 0x46546C67, 2, 12 + len(body)) + body


@pytest.mark.qt          # engine.glb_loader imports PyOpenGL: the Qt tier has it
def test_glb_primitives_sharing_an_accessor_are_charged_against_one_budget(monkeypatch):
    from engine import glb_loader

    loader = glb_loader.GLBLoader()
    assert loader.load_bytes(_glb(4))
    assert len(loader.meshes[0]['primitives']) == 4

    # 36 bytes decode per primitive; a budget of 100 allows two.
    monkeypatch.setattr(glb_loader, 'MAX_DECODED_BYTES', 100)
    assert not glb_loader.GLBLoader().load_bytes(_glb(4))


def test_an_obj_with_too_many_triangles_is_not_loaded(monkeypatch):
    from engine import obj_loader

    monkeypatch.setattr(obj_loader, 'MAX_OBJ_TRIANGLES', 1000)
    text = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf " + " ".join(["1"] * 2000) + "\n"
    loader = obj_loader.OBJLoader()
    assert loader.load_text(text, "bomb.obj") is False
    assert loader.faces == []

    ok = obj_loader.OBJLoader()
    assert ok.load_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "tri.obj")
    assert len(ok.faces) == 1


# ---------------------------------------------------------------------------
# Map validation
# ---------------------------------------------------------------------------

def _angled(planes):
    return {'brushes': [{'pos': [0, 0, 0], 'geometry': {'planes': planes}}]}


@pytest.mark.parametrize('plane', [
    {'n': [float('nan'), 0, 0], 'd': 0},
    {'n': [0, 0, 0], 'd': 0},
    {'n': 'abc', 'd': 0},
    {'n': [1, 0], 'd': 0},
    {'n': [1, 0, 0], 'd': 1e308},
    {'n': [1, 0, 0]},
    5,
])
def test_a_bad_angled_brush_plane_is_refused(plane):
    from engine.level_validation import validate_level

    with pytest.raises(ValueError):
        validate_level(_angled([plane]))


def test_good_angled_brush_planes_pass():
    from engine.level_validation import validate_level

    validate_level(_angled([{'n': [1.0, 0.0, 0.0], 'd': -200.0},
                            {'n': [-1, 0, 0], 'd': 600}]))


def test_a_coordinate_beyond_the_world_is_refused():
    from engine.level_validation import validate_level

    with pytest.raises(ValueError):
        validate_level({'brushes': [{'pos': [1e308, 0, 0], 'size': [1, 1, 1]}]})
    with pytest.raises(ValueError):
        validate_level({'things': [{'pos': [0, -1e9, 0]}]})


# ---------------------------------------------------------------------------
# Packages and decals
# ---------------------------------------------------------------------------

def test_a_manifest_plugins_field_of_the_wrong_type_lists_none():
    from engine.fiopak import FioPackage

    package = FioPackage.__new__(FioPackage)
    for value in (5, "tidy", {"a": 1}, None):
        package._manifest = {"plugins": value}
        assert package.required_plugins == []
    package._manifest = {"plugins": ["tidy", 3]}
    assert package.required_plugins == ["tidy"]


def test_a_decal_image_name_cannot_leave_the_decal_folder(monkeypatch):
    from engine import decal_textures

    monkeypatch.setitem(decal_textures._source, 'source', None)
    for name in ('../../../settings.ini', '/etc/hostname', '\\\\host\\share\\x.png'):
        assert decal_textures.read_image(name) is None


def test_a_paint_file_whose_stamps_are_not_a_list_is_ignored():
    from engine.terrain_stamp import PAINT_FORMAT, apply_stamp_delta

    level = {'terrain_data': {}}
    out = apply_stamp_delta(level, {'format': PAINT_FORMAT, 'stamps': 7})
    assert 'paint_stamps' not in out['terrain_data']


# ---------------------------------------------------------------------------
# Crash log
# ---------------------------------------------------------------------------

def test_the_crash_log_writes_a_repeated_exception_once(tmp_path, monkeypatch):
    from editor import crash_log

    monkeypatch.setattr(crash_log, "_path", str(tmp_path / "fio_crash.log"))
    monkeypatch.setattr(crash_log, "_last_text", None)
    monkeypatch.setattr(crash_log, "_repeats", 0)

    def fail():
        raise RuntimeError("every frame")

    for _ in range(50):
        try:
            fail()
        except RuntimeError:
            crash_log.record(*sys.exc_info())
    crash_log.record(ValueError, ValueError("different"), None)

    text = (tmp_path / "fio_crash.log").read_text(encoding="utf-8")
    assert text.count("RuntimeError: every frame") == 1
    assert "repeated 49 more time(s)" in text
    assert "ValueError: different" in text


def test_a_full_crash_log_is_rotated_not_truncated(tmp_path, monkeypatch):
    from editor import crash_log

    path = tmp_path / "fio_crash.log"
    path.write_text("FIRST ROOT CAUSE\n" + "x" * 2000, encoding="utf-8")
    monkeypatch.setattr(crash_log, "_path", str(path))
    monkeypatch.setattr(crash_log, "_last_text", None)
    monkeypatch.setattr(crash_log, "MAX_BYTES", 1000)
    crash_log.record(ValueError, ValueError("later"), None)

    assert "FIRST ROOT CAUSE" in (tmp_path / "fio_crash.log.1").read_text(encoding="utf-8")
    assert "ValueError: later" in path.read_text(encoding="utf-8")


def test_a_native_crash_leaves_its_stack_in_the_fault_log(tmp_path):
    """A segfault (a GL driver, Qt) ends the process before any Python hook
    runs: a windowed build used to leave no trace of it anywhere."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from editor import crash_log\n"
        "crash_log.install(%r)\n"
        "def crash_here():\n"
        "    import faulthandler; faulthandler._sigsegv()\n"
        "crash_here()\n" % (root, str(tmp_path)))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONFAULTHANDLER"}
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, timeout=60)
    assert result.returncode != 0
    text = (tmp_path / "fio_fault.log").read_text(encoding="utf-8", errors="replace")
    assert "crash_here" in text, text


def test_the_fault_log_leaves_an_enabled_faulthandler_alone(tmp_path, monkeypatch):
    """pytest and ``python -X faulthandler`` chose where crashes go."""
    import faulthandler
    from editor import crash_log
    monkeypatch.setattr(crash_log, "_fault_file", None)
    if not faulthandler.is_enabled():
        pytest.skip("faulthandler is not enabled in this run")
    crash_log._install_fault_log(str(tmp_path))
    assert crash_log._fault_file is None
    assert not (tmp_path / "fio_fault.log").exists()


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(monkeypatch):
    from plugins.manager import PluginManager

    value = PluginManager()
    value.messages = []
    monkeypatch.setattr(value, "_log", value.messages.append)
    return value


def test_a_plugin_whose_register_raises_leaves_nothing_live(manager, monkeypatch):
    import types
    from plugins.api import FioPlugin

    class Half(FioPlugin):
        name = "halfway"

        def register(self, api):
            api.register_console_command("halfcmd", lambda *a, **k: "ran")
            api.register_menu_action("Half thing", lambda: None)
            api.register_tools_action("Half tool", lambda: None)
            raise RuntimeError("register failed half-way")

    module = types.ModuleType("plugins._audit_half")
    module.PLUGIN = Half()
    monkeypatch.setitem(sys.modules, "plugins._audit_half", module)
    manager._load_one("_audit_half")

    assert module.PLUGIN not in manager.plugins
    assert module.PLUGIN.enabled is False
    assert manager.dispatch_console_command("halfcmd", "") == (False, None)
    assert not [a for a in manager.menu_actions() if a[0] is module.PLUGIN]
    assert not [a for a in manager.tools_actions() if a[0] is module.PLUGIN]


def _host(manager):
    from plugins.api import FioPlugin
    from plugins.host import PluginHost

    plugin = FioPlugin()
    plugin.name = "flaky"
    manager.plugins.append(plugin)
    manager._enabled_generation += 1

    class Logic:
        io_manager = None
        player = None
        game_state = None
        monster_ai = None
        session_runtime = type("S", (), {"play_mode": False})()
        editor_state = type("E", (), {"things": [], "brushes": []})()

    return plugin, PluginHost(manager, Logic(), plugin)


def test_an_event_handler_that_always_fails_switches_its_plugin_off(manager):
    plugin, host = _host(manager)
    calls = []

    def handler(event):
        calls.append(1)
        raise RuntimeError("bad overlay")

    host.on("render.overlay", handler)
    for _ in range(100):
        manager.emit("render.overlay")

    assert plugin.enabled is False
    assert len(calls) == manager.MAX_HOOK_FAILURES
    tracebacks = [m for m in manager.messages if "Traceback" in m]
    assert len(tracebacks) <= manager.HOOK_TRACEBACK_LIMIT


def test_wrap_never_runs_the_original_twice(manager):
    _plugin, host = _host(manager)
    calls = []

    class Target:
        def act(self):
            calls.append("original")
            return "stock"

    target = Target()

    def wrapper(original, *args):
        original(*args)
        raise RuntimeError("after calling through")

    assert host.wrap(target, "act", wrapper)
    target.act()
    assert calls == ["original"]

    target2 = Target()
    host.wrap(target2, "act", lambda original, *a: (_ for _ in ()).throw(RuntimeError()))
    calls.clear()
    assert target2.act() == "stock" and calls == ["original"]


# ---------------------------------------------------------------------------
# Map logic and the console
# ---------------------------------------------------------------------------

@pytest.mark.qt
def test_map_logic_cannot_run_condump():
    from editor.console_commands import ConsoleCommandHandler

    assert 'condump' in ConsoleCommandHandler.USER_ONLY_COMMANDS


def test_the_logic_command_queue_is_bounded():
    from engine.threaded_game_state import ThreadedGameState

    state = ThreadedGameState.__new__(ThreadedGameState)
    ThreadedGameState.__init__(state)
    for index in range(ThreadedGameState.CONSOLE_QUEUE_LIMIT * 4):
        state.queue_console_command(f"echo {index}")
    drained = state.consume_console_commands()
    assert len(drained) == ThreadedGameState.CONSOLE_QUEUE_LIMIT
    assert drained[-1] == f"echo {ThreadedGameState.CONSOLE_QUEUE_LIMIT * 4 - 1}"
