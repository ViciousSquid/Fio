"""The global physics switch (Settings > Play Modes > Enable physics).

Off, a play session has no physics engine at all: none is created, Soma is
not loaded -- and is unloaded if an earlier session had it -- and a physics
Prop is the scenery it otherwise is. The engine runs without one.
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

SESSION = '''
import sys
from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.prop_entity import Prop
from engine.threaded_game_state import ThreadedGameState

state = EditorState()
state.brushes = [{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}]
state.things = [Prop(pos=[0.0, 200.0, 0.0], properties={
    'physics_enabled': True, 'no_collision': False,
    'model_path': 'assets/models/Oil_Drum.obj', 'collision_shape': 'aabb'})]
logic = LogicThread(ThreadedGameState(), state)
session = logic.session_runtime
out = []
for enabled in %(sequence)r:
    session.physics_enabled = enabled
    session.apply_play_mode(True)
    for _ in range(5):
        logic._step_frame(logic.TICK_DURATION) if hasattr(logic, '_step_frame') else None
    out.append((session.physics_world is not None,
                'engine.physics.soma' in sys.modules,
                len(logic.collision_runtime._physics_body_brushes)))
    session.apply_play_mode(False)
logic.stop()
print(out)
'''


def _sessions(*sequence):
    pytest.importorskip("PyQt5", reason="a play session needs the real LogicThread")
    out = subprocess.run([sys.executable, '-c', SESSION % {'sequence': sequence}],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**__import__('os').environ, 'QT_QPA_PLATFORM': 'offscreen'})
    assert out.returncode == 0, out.stderr[-2000:]
    return eval(out.stdout.strip().splitlines()[-1])


@pytest.mark.qt
def test_with_physics_off_soma_is_never_loaded():
    (has_engine, loaded, bodies), = _sessions(False)
    assert has_engine is False
    assert loaded is False                      # not even imported
    assert bodies == 0                          # a physics Prop is scenery


@pytest.mark.qt
def test_with_physics_on_a_session_runs_soma():
    (has_engine, loaded, bodies), = _sessions(True)
    assert has_engine is True and loaded is True
    assert bodies == 1


@pytest.mark.qt
def test_switching_physics_off_unloads_soma_and_on_loads_it_again():
    on, off, on_again = _sessions(True, False, True)
    assert on == (True, True, 1)
    assert off == (False, False, 0)             # unloaded, not merely unused
    assert on_again == (True, True, 1)


def test_unloading_leaves_the_boundary_and_reloads_on_demand():
    # In a process of its own: unloading swaps the class other tests hold.
    code = (
        "import sys\n"
        "from engine.physics import create_physics, unload_physics\n"
        "from engine.spatial_grid import SpatialGrid\n"
        "first = create_physics(SpatialGrid())\n"
        "assert 'engine.physics.soma' in sys.modules\n"
        "assert unload_physics() == ['engine.physics.soma']\n"
        "assert 'engine.physics.soma' not in sys.modules\n"
        "assert 'engine.physics.api' in sys.modules\n"
        "again = create_physics(SpatialGrid())\n"
        "assert type(again).__name__ == 'Soma' and type(again) is not type(first)\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr[-2000:]


def test_unloading_leaves_no_soma_name_on_the_physics_package():
    """After unload_physics nothing on engine.physics still points at Soma
    (not the submodule, not a class), so no stale name pins it while another
    physics is selected. In a process of its own, as above."""
    code = (
        "import sys\n"
        "import engine.physics as boundary\n"
        "from engine.physics import create_physics, unload_physics\n"
        "from engine.spatial_grid import SpatialGrid\n"
        "create_physics(SpatialGrid())\n"
        "assert 'soma' in vars(boundary)\n"
        "unload_physics()\n"
        "left = [n for n in ('soma', 'Soma', 'PhysicsWorld', 'PhysicsBody')\n"
        "        if n in vars(boundary)]\n"
        "assert not left, left\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr[-2000:]
