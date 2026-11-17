"""A physics is anything that satisfies ``engine.physics.Physics``.

The architectural tests for the physics boundary, the counterpart of
``tests/renderer/test_renderer_contract.py``: a physics that shares nothing
with the built-in one registers by name and is created by the engine's own
path; the built-in one satisfies the same contract; and the engine reaches
neither except through it.
"""

import ast
import pathlib
import subprocess
import sys

import pytest

from engine.physics import (
    BODY_PROPERTIES, DEFAULT_PHYSICS, Body, Physics, available_physics,
    create_physics, physics_factory, register_physics, select_physics,
    selected_physics, world_gravity)
from engine.physics import registry
from engine.prop_entity import Prop
from engine.spatial_grid import SpatialGrid
from tests.helpers.physics import StubPhysics

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_registry():
    saved = dict(registry._factories), registry._selected
    yield
    registry._factories.clear()
    registry._factories.update(saved[0])
    registry._selected = saved[1]


def _grid():
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}])
    return grid


def _body_brush(prop):
    x, y, z = prop.pos
    return {'pos': [x, y + 20.0, z], 'size': [40.0, 40.0, 40.0],
            '_physics_body': True, '_physics_entity': prop}


def _prop(**props):
    properties = {'physics_enabled': True, 'no_collision': False}
    properties.update(props)
    return Prop(pos=[0.0, 0.0, 0.0], properties=properties)


def test_soma_is_the_built_in_default():
    assert DEFAULT_PHYSICS == "Soma"
    assert "Soma" in available_physics()
    assert selected_physics() == "Soma"


def test_a_physics_with_nothing_from_fio_plugs_in(clean_registry):
    assert register_physics("Stub", StubPhysics) is True
    assert "Stub" in available_physics()
    physics = create_physics(_grid(), "Stub")
    assert isinstance(physics, StubPhysics)
    assert isinstance(physics, Physics)

    prop = _prop()
    physics.rebuild([_body_brush(prop)])
    assert isinstance(physics.get_body(prop), Body)
    physics.wake(prop, [60.0, 0.0, 0.0])
    physics.step(1.0 / 60.0)
    assert prop.pos[0] == pytest.approx(1.0)


def test_the_selected_physics_is_the_one_a_session_creates(clean_registry):
    register_physics("Stub", StubPhysics)
    select_physics("Stub")
    assert selected_physics() == "Stub"
    assert isinstance(create_physics(_grid()), StubPhysics)
    with pytest.raises(KeyError):
        select_physics("NoSuchPhysics")
    assert selected_physics() == "Stub"


def test_an_object_missing_a_member_is_not_a_physics():
    partial = type("Partial", (), {k: v for k, v in vars(StubPhysics).items()
                                    if k != "sync_entity_position"})(None)
    assert not isinstance(partial, Physics)


def test_an_unknown_physics_name_is_an_error():
    assert physics_factory("NoSuchPhysics") is None
    with pytest.raises(KeyError):
        create_physics(_grid(), "NoSuchPhysics")


def test_soma_satisfies_the_contract():
    from engine.physics.soma import Soma

    physics = create_physics(_grid(), "Soma")
    assert isinstance(physics, Soma)
    assert isinstance(physics, Physics)

    prop = _prop()
    physics.rebuild([_body_brush(prop)])
    assert isinstance(physics.get_body(prop), Body)

    physics.gravity = -100.0
    physics.sleep_enabled = False
    physics.reset_controls()
    assert physics.gravity == world_gravity() == world_gravity(None)
    assert physics.sleep_enabled is True


def test_soma_reads_only_contract_properties():
    """Every authored property the built-in physics reads is in the contract,
    so another implementation knows the whole of what a body is."""
    source = (ROOT / 'engine' / 'physics' / 'soma.py').read_text(encoding='utf-8')
    read = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ('props', 'p')):
            read.add(node.args[0].value)
    assert read and read <= set(BODY_PROPERTIES), read - set(BODY_PROPERTIES)


def test_the_boundary_imports_without_numpy_or_glm():
    code = ("import sys, engine.physics as p; "
            "p.world_gravity(); p.available_physics(); "
            "print('numpy' in sys.modules or 'glm' in sys.modules)")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == 'False'


def _imports(path):
    """Every module *path* imports, as dotted names relative imports resolved."""
    package = '.'.join(path.relative_to(ROOT).with_suffix('').parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ()
        elif isinstance(node, ast.ImportFrom):
            base = package.split('.') if package else []
            if node.level:
                base = base[:len(base) - (node.level - 1)]
                module = '.'.join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ''
            yield module, tuple(alias.name for alias in node.names)


#: What the engine may not name: an implementation, or its classes.
_PRIVATE = {'Soma', 'PhysicsWorld', 'PhysicsBody'}


@pytest.mark.parametrize('folder', ['engine', 'editor'])
def test_the_engine_reaches_physics_only_through_the_boundary(folder):
    offenders = []
    for path in sorted((ROOT / folder).rglob('*.py')):
        if (ROOT / 'engine' / 'physics') in path.parents:
            continue
        for module, names in _imports(path):
            if module.startswith('engine.physics.') and module != 'engine.physics.api':
                offenders.append(f"{path.relative_to(ROOT)}: imports {module}")
            if module == 'engine.physics' and _PRIVATE & set(names):
                offenders.append(f"{path.relative_to(ROOT)}: imports "
                                 f"{sorted(_PRIVATE & set(names))} from engine.physics")
    assert not offenders, "\n".join(offenders)


def test_the_built_in_physics_keeps_no_copy_of_the_world():
    """The spatial grid is the single source of truth: Soma names no brush
    list, cell bucket or mover table of its own."""
    path = ROOT / 'engine' / 'physics' / 'soma.py'
    modules = {module for module, _names in _imports(path)}
    assert 'engine.mover_table' not in modules
    assert 'engine.constants' not in modules
    source = path.read_text(encoding='utf-8')
    assert '.cells' not in source
    assert 'world_bounds' not in source


@pytest.mark.qt
def test_a_play_session_runs_on_the_selected_physics(clean_registry):
    pytest.importorskip("PyQt5", reason="a play session needs the real LogicThread")
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    register_physics("Stub", StubPhysics)
    select_physics("Stub")
    state = EditorState()
    state.brushes = [{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}]
    state.things = []
    logic = LogicThread(ThreadedGameState(), state)
    try:
        logic.session_runtime.apply_play_mode(True)
        physics = logic.session_runtime.physics_world
        assert isinstance(physics, StubPhysics)
        assert physics.spatial_grid is logic.session_runtime.spatial_grid
    finally:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()
