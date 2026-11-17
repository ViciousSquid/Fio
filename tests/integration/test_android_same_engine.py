"""The Android app runs the same Fio engine the desktop does.

Staging for Android used to patch a private copy of twelve engine and entity
modules. Everything that patch added is now the engine's own, so the staged
engine is the checkout's, byte for byte -- and these tests are what keep it
that way: an adaptation Android needs goes into the engine, for every host,
not into a copy of it.
"""
import hashlib
import json
import pathlib

import pytest

from player.tools import stage_runtime

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope='module')
def staged(tmp_path_factory):
    destination = tmp_path_factory.mktemp('android') / 'runtime'
    receipt = stage_runtime.stage_tree(destination)
    return destination, receipt


def test_nothing_patches_the_engine_for_android():
    assert not list((ROOT / 'player').rglob('*.patch'))
    source = (ROOT / 'player' / 'tools' / 'stage_runtime.py').read_text(encoding='utf-8')
    assert 'subprocess' not in source and "'patch'" not in source


def test_every_staged_engine_module_is_the_checkouts(staged):
    destination, receipt = staged
    sources = stage_runtime.engine_sources(ROOT)
    assert len(sources) > 100
    for source in sources:
        assert _digest(destination / source) == _digest(ROOT / source), source
        assert receipt['source_sha256'][source] == _digest(ROOT / source)
    assert receipt['same_engine'] is True
    assert json.loads((destination / 'player/runtime-build.json').read_text(encoding='utf-8')) == receipt


def test_the_only_modules_staging_adds_are_the_desktops_own_relocated(staged):
    destination, _receipt = staged
    checkout = set(stage_runtime.engine_sources(ROOT))
    extra = {path.relative_to(destination).as_posix()
             for path in (destination / 'engine').rglob('*.py')} - checkout
    assert extra == {f'engine/{new}.py' for new in stage_runtime.RUNTIME_MODULES.values()}
    for old, new in stage_runtime.RUNTIME_MODULES.items():
        assert (_digest(destination / f'engine/{new}.py')
                == _digest(ROOT / f'editor/{old}.py')), old


def test_android_runs_soma_through_the_same_registry(staged):
    destination, _receipt = staged
    for name in ('__init__.py', 'api.py', 'registry.py', 'soma.py'):
        assert (destination / 'engine' / 'physics' / name).is_file()
    assert (destination / 'engine' / 'spatial_grid.py').is_file()


def test_what_android_needs_of_the_engine_is_the_engines_own():
    """The host-facing hooks the player sets (player/world.py) exist on the
    desktop's engine, unused there and harmless."""
    from engine.threaded_game_state import ThreadedGameState

    state = ThreadedGameState()
    assert state.get_movement_axes() is None            # a keyboard host
    state.set_movement_axes(2.0, -0.25)
    assert state.get_movement_axes() == (1.0, -0.25)    # clamped
    # Read, not imported: the model loaders need a GL binding to import.
    engine = ROOT / 'engine'
    assert 'def load_bytes(' in (engine / 'glb_loader.py').read_text(encoding='utf-8')
    assert 'def load_text(' in (engine / 'obj_loader.py').read_text(encoding='utf-8')
    assert 'def load_model(' in (engine / 'model_source.py').read_text(encoding='utf-8')
    assert 'threaded_ai' in (engine / 'logic_thread.py').read_text(encoding='utf-8')
    assert 'request_level_change' in (ROOT / 'editor' / 'io_handlers.py').read_text(encoding='utf-8')
