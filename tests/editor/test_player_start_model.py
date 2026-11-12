"""The private editor sign remains a spawn, with player-sized presentation."""

import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip('PyQt5')

from editor.things import PlayerStart, Prop, Thing
from engine.entity_models import editor_model_ray_distance, model_path
from engine.entity_table import EntityTable, classify_slots
from engine.obj_loader import OBJLoader

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('view_name', ['view_top', 'view_front', 'view_side'])
def test_right_click_places_a_playerstart_with_private_model(main_window, monkeypatch, view_name):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QContextMenuEvent
    from PyQt5.QtWidgets import QMenu

    def choose_spawn(menu, *args):
        return next(action for action in menu.actions() if action.text() == 'PlayerStart')

    monkeypatch.setattr(QMenu, 'exec_', choose_spawn)
    main_window.state.clear_scene()
    view = getattr(main_window, view_name)
    clicked = view.snap_to_grid(view.screen_to_world(QPoint(80, 80)))
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(80, 80)))
    start, = main_window.state.things
    assert type(start) is PlayerStart
    assert not isinstance(start, Prop)
    assert start.primary
    expected_feet_y = 0 if view_name == 'view_top' else clicked.y()
    assert start.pos[1] - 50.0 == expected_feet_y
    from engine.asset_archives import asset_exists
    assert asset_exists(model_path(start))      # shipped in editor.fiopak
    assert start.get_instance_pixmap() is None


def test_existing_spawn_records_acquire_the_model_without_model_properties():
    record = {'type': 'playerstart', 'pos': [20, 50, -40],
              'properties': {'name': 'Entrance', 'angle': 135.0, 'primary': True}}
    start = Thing.from_dict(record)
    saved = start.to_dict()
    restored = Thing.from_dict(saved)
    assert type(restored) is PlayerStart
    assert restored.pos == [20, 50, -40]
    assert restored.get_angle() == 135.0 and restored.primary
    assert restored.name == 'Entrance'
    assert model_path(restored) == model_path(start)
    assert saved['type'] == 'playerstart'
    assert not {'model_path', 'render_mode', 'rotation', 'scale'} & saved['properties'].keys()


@pytest.mark.parametrize('play,debug,expected', [
    (False, False, [0]), (False, True, [0]),
    (True, False, []), (True, True, [0]),
])
def test_sign_uses_model_pass_and_editor_visibility_policy(play, debug, expected):
    start = PlayerStart()
    table = EntityTable()
    hidden = table.begin_frame([start], epoch=1)
    models, sprites = classify_slots(table, np.array([0]), hidden, play, debug)
    assert models.tolist() == expected
    assert sprites.tolist() == []
    assert table.sprite_key_id[0] == -1
    recipe, = table.model_recipes()
    assert recipe == (model_path(start), None, None, None)
    from engine.renderer.core.visibility import VisibilityMixin
    assert not len(VisibilityMixin._dense_shadow_model_slots(table, hidden))
    start.properties['hidden'] = True
    table.begin_frame([start], epoch=2)
    models, sprites = classify_slots(table, np.array([0]), np.array([True]), False, True)
    assert not len(models) and not len(sprites)


@pytest.mark.parametrize('angle', [0, 45, 90, 180, 270])
def test_sign_faces_heading_and_matches_the_non_rotating_player_hull(angle):
    start = PlayerStart(properties={'angle': angle})
    table = EntityTable()
    table.begin_frame([start], epoch=1)
    transform = table.model_base_matrix[0].reshape(4, 4).T
    heading = transform @ np.array([0, 0, 1, 0])
    heading /= np.linalg.norm(heading)
    rad = math.radians(angle)
    np.testing.assert_allclose(heading[:3], [math.cos(rad), 0, -math.sin(rad)], atol=1e-6)
    model = OBJLoader()
    assert model.load(model_path(start))
    vertices = np.asarray(model.vertices) @ transform[:3, :3].T
    from engine.player import Player
    player = Player(0, 0, 0, physics_enabled=False)
    np.testing.assert_allclose(np.ptp(vertices, axis=0),
                               [player.width, player.height, player.depth], atol=1e-4)


def test_shipped_sign_is_100_units_high_and_has_clear_acrylic():
    model = OBJLoader()
    assert model.load(model_path(PlayerStart()))
    assert model.up_axis == 'y'
    vertices = np.asarray(model.vertices)
    np.testing.assert_allclose(np.ptp(vertices, axis=0), [50, 100, 50])
    np.testing.assert_allclose([vertices[:, 1].min(), vertices[:, 1].max()], [-50, 50])
    assert model.materials['ink']['alpha'] == 1.0
    assert 0 < model.materials['acrylic']['alpha'] < .2
    assert {face['material'] for face in model.faces} == {'ink', 'edge', 'acrylic'}


def test_private_sign_is_outside_every_asset_browser_root(qt_app):
    from editor.asset_browser import AssetBrowser

    private_model = Path(model_path(PlayerStart())).resolve()
    browser = AssetBrowser(str(private_model.parents[1]))
    try:
        for tab in (browser.tab_models, browser.tab_textures):
            assert all(Path(item.file_path).resolve() != private_model for item in tab.items)
        assert not private_model.is_relative_to(Path(browser.models_path).resolve())
        assert not private_model.is_relative_to(Path(browser.textures_path).resolve())
    finally:
        browser.close()
        browser.deleteLater()


def test_3d_pick_reaches_head_and_feet_but_misses_outside_sign():
    start = PlayerStart(pos=[10, 50, 20], properties={'angle': 90})
    for height in (1, 89, 99):
        distance = editor_model_ray_distance(start, (10, height, 200), (0, 0, -1))
        assert distance is not None and distance > 0
    assert editor_model_ray_distance(start, (60, 89, 200), (0, 0, -1)) is None


@pytest.mark.parametrize('floor_y', [0.0, 64.0])
def test_model_bottom_matches_actual_player_feet_on_entering_play(main_window, floor_y):
    from tests.helpers.worlds import box_brush

    main_window.state.clear_scene()
    main_window.state.brushes.append(box_brush(
        'floor', (0, floor_y - 10, 0), (300, 20, 300)))
    start = PlayerStart(pos=[0, floor_y + 50, 0])
    main_window.state.things.append(start)
    main_window.set_selected_objects([start])
    model = OBJLoader()
    assert model.load(model_path(start))
    model_bottom = start.pos[1] + min(v[1] for v in model.vertices)
    main_window.enter_play_mode()
    try:
        player = main_window.view_3d.player
        assert model_bottom == pytest.approx(float(player.pos.y) - player.height / 2)
        assert model_bottom == floor_y
    finally:
        main_window.enter_play_mode()
