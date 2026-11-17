"""Soma's tiers: ``full`` and ``crude``.

One physics, two amounts of it. ``full`` is everything Soma does. ``crude``
is Fio's physics exactly as it was before Soma -- the original class, pinned
by the tests it shipped with (``test_crude_*.py``). The tier is chosen for a
play session and reached through the same registry entry, ``"Soma"``.
"""
import configparser
import pathlib
import subprocess
import sys

import pytest

from engine.physics import Physics, create_physics
from engine.physics import soma
from engine.prop_entity import Prop
from engine.spatial_grid import SpatialGrid

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_tier():
    before = soma.selected_tier()
    yield
    soma.select_tier(before)


def _grid():
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([{'pos': [0.0, -50.0, 0.0], 'size': [4000.0, 100.0, 4000.0]}])
    return grid


def _body(x, **props):
    properties = {'physics_enabled': True, 'no_collision': False,
                  'friction': 0.0, 'linear_damping': 0.0}
    properties.update(props)
    prop = Prop(pos=[x, 0.0, 0.0], properties=properties)
    return prop, {'pos': [x, 20.0, 0.0], 'size': [40.0, 40.0, 40.0],
                  '_physics_body': True, '_physics_entity': prop}


def test_full_is_the_default_tier():
    assert soma.DEFAULT_TIER == 'full'
    assert soma.available_tiers() == ['full', 'crude']
    physics = create_physics(_grid(), "Soma")
    assert type(physics) is soma.Soma and physics.tier == 'full'


@pytest.mark.parametrize('tier', ['full', 'crude'])
def test_every_tier_is_soma_and_satisfies_the_contract(tier):
    soma.select_tier(tier)
    assert soma.selected_tier() == tier
    physics = create_physics(_grid(), "Soma")
    assert physics.tier == tier
    assert isinstance(physics, Physics)
    assert type(physics) is soma.tier_class(tier)

    prop, brush = _body(0.0)
    physics.rebuild([brush])
    physics.wake(prop, [0.0, 0.0, 60.0])
    physics.step(1.0 / 60.0)
    assert prop.pos[2] == pytest.approx(1.0)
    physics.gravity = -1.0
    physics.reset_controls()
    assert physics.gravity == -800.0


def test_the_crude_tier_is_the_original_class_not_the_full_one_with_less():
    from engine.physics.soma.crude import SomaCrude
    from engine.physics.soma.full import Soma

    assert soma.tier_class('crude') is SomaCrude is soma.SomaCrude
    assert soma.tier_class('full') is Soma is soma.Soma
    assert not issubclass(SomaCrude, Soma) and not issubclass(Soma, SomaCrude)


def test_the_tiers_differ_where_soma_added_something():
    """In ``crude`` a body slides through another, as it always did; in
    ``full`` it meets it."""
    ends = {}
    for tier in ('crude', 'full'):
        physics = soma.create(_grid(), tier)
        mover, mover_brush = _body(0.0)
        other, other_brush = _body(200.0)
        physics.rebuild([mover_brush, other_brush])
        physics.step(1.0 / 60.0)
        physics.wake(mover, [300.0, 0.0, 0.0])
        for _ in range(60):
            physics.step(1.0 / 60.0)
        ends[tier] = (mover.pos[0], other.pos[0])

    assert ends['crude'][0] == pytest.approx(300.0, abs=0.5)   # straight through
    assert ends['crude'][1] == 200.0
    assert ends['full'][1] > 200.0                             # knocked on
    assert ends['full'][0] < ends['full'][1]


def test_an_unknown_tier_is_an_error_and_names_match_in_any_case():
    with pytest.raises(KeyError):
        soma.select_tier('medium')
    assert soma.selected_tier() == 'full'
    soma.select_tier('  CRUDE ')
    assert soma.selected_tier() == 'crude'


def test_a_host_configures_the_tier_from_settings_and_the_environment():
    config = configparser.ConfigParser()
    config.read_string("[Physics]\ntier = crude\n")
    assert soma.configure_tier(config, environ={}) == 'crude'
    # The environment wins over the settings file.
    assert soma.configure_tier(config, environ={'FIO_PHYSICS_TIER': 'full'}) == 'full'
    # A value that names no tier is ignored: the selection stays.
    config.read_string("[Physics]\ntier = turbo\n")
    assert soma.configure_tier(config, environ={}) == 'full'
    assert soma.configure_tier(None, environ={}) == 'full'


def test_a_tier_that_is_not_selected_is_never_imported():
    """What a low-power host does not run, it does not load."""
    code = ("import sys\n"
            "from engine.physics import create_physics, soma\n"
            "from engine.spatial_grid import SpatialGrid\n"
            "soma.select_tier('crude')\n"
            "create_physics(SpatialGrid(), 'Soma')\n"
            "print('engine.physics.soma.crude' in sys.modules, "
            "'engine.physics.soma.full' in sys.modules)")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.split() == ['True', 'False']


@pytest.mark.qt
@pytest.mark.parametrize('tier', ['full', 'crude'])
def test_a_play_session_runs_soma_at_the_selected_tier(tier):
    pytest.importorskip("PyQt5", reason="a play session needs the real LogicThread")
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    soma.select_tier(tier)
    state = EditorState()
    state.brushes = [{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}]
    state.things = []
    logic = LogicThread(ThreadedGameState(), state)
    try:
        logic.session_runtime.apply_play_mode(True)
        physics = logic.session_runtime.physics_world
        assert physics.tier == tier
    finally:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()
