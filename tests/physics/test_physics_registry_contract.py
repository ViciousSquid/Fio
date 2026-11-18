"""The registry checks what a physics factory returns before the engine
drives it: every member of the contract, and a contract version it can run
on. The contract has a version of its own, apart from the plugin API's."""

import pytest

from engine import physics
from engine.physics import api, registry
from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid


class _Complete:
    """Every member of the contract, doing nothing: built from NoPhysics's
    names so it is complete whatever the contract holds."""

    def __init__(self, grid, **attrs):
        for name in api.missing_members(object()):
            value = getattr(api.NO_PHYSICS, name)
            setattr(self, name, value)
        for name, value in attrs.items():
            setattr(self, name, value)


@pytest.fixture
def registered():
    names = []

    def register(name, factory):
        registry.register_physics(name, factory)
        names.append(name)
        return name

    yield register
    for name in names:
        registry._factories.pop(name, None)


def test_the_contract_has_its_own_version():
    assert physics.PHYSICS_CONTRACT_VERSION == "0.9.0"
    assert physics.PHYSICS_CONTRACT_VERSION_INFO == (0, 9, 0)
    from plugins.api import API_VERSION
    assert API_VERSION == "2.0.0", "the plugin API's version is its own"
    assert Soma.contract_version == physics.PHYSICS_CONTRACT_VERSION


def test_soma_and_no_physics_satisfy_the_contract():
    assert physics.missing_members(Soma(SpatialGrid())) == []
    assert physics.missing_members(physics.NO_PHYSICS) == []
    assert physics.contract_compatible(Soma(SpatialGrid())) is None
    created = registry.create_physics(SpatialGrid(), "Soma")
    assert isinstance(created, Soma)


def test_a_factory_returning_an_incomplete_object_is_refused(registered):
    class Half(_Complete):
        pass

    def factory(grid):
        half = Half(grid)
        del half.step
        half.raycast = "not a method"
        return half

    name = registered("Half", factory)
    with pytest.raises(TypeError) as caught:
        registry.create_physics(SpatialGrid(), name)
    message = str(caught.value)
    assert "'Half'" in message and "raycast" in message and "step" in message
    assert physics.missing_members(object()) == sorted(physics.missing_members(object()))


def test_a_complete_object_is_created(registered):
    name = registered("Complete", _Complete)
    assert isinstance(registry.create_physics(SpatialGrid(), name), _Complete)


@pytest.mark.parametrize("declared, runs", [
    (None, True),                       # does not say: this contract
    ("0.9.0", True),
    ("0.9", True),
    ("0.9.5", False),                   # newer than this Fio's
    ("0.8.0", False),                   # 0.x: another minor may differ
    ("1.0.0", False),                   # another (and newer) major
])
def test_a_physics_written_for_another_contract_version(registered, declared, runs):
    attrs = {} if declared is None else {"contract_version": declared}
    name = registered("Versioned", lambda grid: _Complete(grid, **attrs))
    if runs:
        registry.create_physics(SpatialGrid(), name)
    else:
        with pytest.raises(TypeError, match="physics contract"):
            registry.create_physics(SpatialGrid(), name)


FALLBACK_SESSION = '''
from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.physics import register_physics, select_physics
from engine.threaded_game_state import ThreadedGameState

class Broken:
    """A physics that is not one: it has no step, among much else."""
    def __init__(self, grid):
        self.gravity = -800.0

register_physics("Broken", Broken)
select_physics("Broken")
state = EditorState()
state.brushes = [{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}]
logic = LogicThread(ThreadedGameState(), state)
session = logic.session_runtime
session.physics_enabled = True
session.apply_play_mode(True)
print("RESULT", type(session.physics_world).__name__)
session.apply_play_mode(False)
logic.stop()
'''


@pytest.mark.qt
def test_a_selected_physics_that_is_refused_leaves_play_on_soma():
    import os
    import pathlib
    import subprocess
    import sys
    pytest.importorskip("PyQt5", reason="a play session needs the real LogicThread")
    root = pathlib.Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, '-c', FALLBACK_SESSION], cwd=root,
                         capture_output=True, text=True,
                         env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
    assert out.returncode == 0, out.stderr[-2000:]
    assert "RESULT Soma" in out.stdout, out.stdout[-2000:]
    assert "[Physics] physics 'Broken' does not satisfy the physics contract" in out.stdout
