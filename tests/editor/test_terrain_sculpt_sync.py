"""A sculpt stroke re-encodes the terrain once, at its end.

Every dab used to call ``terrain.to_dict()`` to refresh the editor's
``terrain_data``. The dab now leaves a deferred sync on the editor state; the
stroke's end runs it, and so does anything that keeps the scene first (a
save, an autosave, an undo checkpoint), so none of them sees a stale terrain.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="the 3D view is editor-tier")

from tests.editor.test_terrain_stamp_editing import _mouse  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def sculpt(main_window, monkeypatch):
    from engine.terrain import Terrain
    terrain = Terrain(seed=1)
    main_window.terrain = terrain
    main_window.state.terrain_data = terrain.to_dict()
    view = main_window.view_3d
    view.terrain_sculpt_mode = 'raise'
    view.terrain_sculpt_strength = 5.0
    view.terrain_sculpt_radius = 40.0
    view.terrain_edit.set_terrain_sculpt_active(True)
    monkeypatch.setattr(view.terrain_edit, 'raycast_terrain',
                        lambda mx, my: (10.0, 0.0, 10.0))
    encodes = []
    real = terrain.to_dict
    monkeypatch.setattr(terrain, 'to_dict', lambda: encodes.append(True) or real())
    yield main_window, view, terrain, encodes, real
    view.terrain_edit.set_terrain_sculpt_active(False)
    main_window.terrain = None
    main_window.state.terrain_data = None


def _stroke_dabs(view, dabs):
    _mouse(view, 'press')
    view.terrain_sculpt_painting = True         # dragging: a dab per move
    for _ in range(dabs - 1):
        _mouse(view, 'move')


def test_a_stroke_encodes_the_terrain_once_at_its_end(sculpt):
    window, view, terrain, encodes, real = sculpt
    _stroke_dabs(view, 12)
    assert encodes == [], "a dab re-encoded the whole terrain"
    view.terrain_sculpt_painting = False
    _mouse(view, 'release')
    assert len(encodes) == 1
    assert json.dumps(window.state.terrain_data, sort_keys=True) == \
        json.dumps(real(), sort_keys=True)


def test_a_save_or_checkpoint_mid_stroke_sees_the_sculpted_terrain(sculpt):
    window, view, terrain, encodes, real = sculpt
    before = json.dumps(window.state.terrain_data, sort_keys=True)
    _stroke_dabs(view, 3)
    level = window.state.get_level_data()           # what a save writes
    assert json.dumps(level['terrain_data'], sort_keys=True) != before
    assert json.dumps(level['terrain_data'], sort_keys=True) == \
        json.dumps(real(), sort_keys=True)
    _stroke_dabs(view, 2)
    window.save_state()                             # an undo checkpoint
    assert json.dumps(window.state.terrain_data, sort_keys=True) == \
        json.dumps(real(), sort_keys=True)
    view.terrain_sculpt_painting = False
    _mouse(view, 'release')
