"""Directional selection must be relative to each camera, never global state."""

from types import SimpleNamespace

import numpy as np
import pytest

from engine.entity_table import _monster_sprite_candidates
from engine.monster_facing import face_direction, sprite_keys_for_view, view_rotations


def test_eight_views_follow_the_reference_order():
    positions = np.zeros((8, 3))
    bearings = np.radians(np.arange(8) * 45)
    # Place each monster opposite its desired camera bearing.
    positions[:, 0] = -np.sin(bearings) * 100
    positions[:, 2] = -np.cos(bearings) * 100
    np.testing.assert_array_equal(view_rotations(positions, np.zeros(8), (0, 0, 0)),
                                  np.arange(8))


@pytest.mark.parametrize('heading', [0, 45, 90, 180, 315, -45, 720])
def test_view_rotates_with_heading(heading):
    angle = np.radians(heading)
    eye = (100*np.sin(angle), 50, 100*np.cos(angle))
    assert view_rotations(np.zeros((1, 3)), np.array([heading]), eye)[0] == 0


def test_sector_boundaries_and_wrap():
    bearings = np.radians([22.49, 22.51, 337.49, 337.51])
    positions = np.column_stack((-100*np.sin(bearings), np.zeros(4), -100*np.cos(bearings)))
    np.testing.assert_array_equal(view_rotations(positions, np.zeros(4), (0, 0, 0)),
                                  [0, 1, 7, 0])


def test_separate_cameras_choose_different_frames_without_changing_table():
    table = SimpleNamespace(sprite_key_id=np.array([99, 100]),
                            sprite_direction_ids=np.array([np.arange(8), [-1]*8]),
                            monster_angle=np.zeros(2), pos=np.zeros((2, 3)))
    slots = np.array([0, 1])
    np.testing.assert_array_equal(sprite_keys_for_view(table, slots, (0, 0, 100)), [0, 100])
    np.testing.assert_array_equal(sprite_keys_for_view(table, slots, (0, 0, -100)), [4, 100])
    np.testing.assert_array_equal(table.sprite_key_id, [99, 100])
    np.testing.assert_array_equal(sprite_keys_for_view(table, slots, (0, 100, 0)), [0, 100])


def test_heading_survives_zero_length_and_vertical_movement():
    monster = SimpleNamespace(properties={'angle': 90.0})
    face_direction(monster, (0, 10, 0))
    assert monster.properties['angle'] == 90
    face_direction(monster, (-10, 0, 0))
    assert monster.properties['angle'] == 270
    face_direction(monster, (float('nan'), 0, 0))
    assert monster.properties['angle'] == 270


def test_directional_recipe_preserves_custom_and_variant_fallbacks():
    props = dict(monster_type='human', variant='variant1', is_shooting=True,
                 custom_shoot='assets/sprites/custom.png')
    recipe = _monster_sprite_candidates(props, 5)
    assert [(r[1], r[2]) for r in recipe[:4]] == [
        ('custom_5.png', 'sprites'),
        ('shoot_5.png', 'sprites/monsters/human/variant1'),
        ('shoot_5.png', 'sprites/monsters/human'),
        ('idle_5.png', 'sprites/monsters/human/variant1')]
    assert any(r[1] == 'idle_5.png' for r in recipe)
    assert recipe[0][0] != _monster_sprite_candidates(props, 1)[0][0]


@pytest.mark.parametrize('selected', range(1, 9))
def test_selecting_any_custom_view_resolves_the_whole_rotation_set(selected):
    props = dict(monster_type='human', custom_idle=f'assets/sprites/monsters/ranger_{selected}.png')
    assert [_monster_sprite_candidates(props, view)[0][1] for view in range(1, 9)] == [
        f'ranger_{view}.png' for view in range(1, 9)]


@pytest.mark.qt
def test_missing_custom_view_uses_matching_builtin_direction():
    from editor.things import Monster
    from engine.entity_table import EntityTable
    from player.render.renderer import GLESRenderer

    class Package:
        def has_asset(self, name):
            return name in {'assets/sprites/monsters/custom_1.png',
                            'assets/sprites/monsters/human/idle_5.png'}
    table = EntityTable()
    table.begin_frame([Monster([0, 96, 0], {
        'custom_idle': 'assets/sprites/monsters/custom_1.png'})], epoch=1)
    renderer = GLESRenderer()
    renderer._scene = {'package': Package()}
    assert renderer._sprite_image(table, 0, (0, 96, -100)).endswith('human/idle_5.png')


def test_corpse_retains_the_single_rotation_zero_frame():
    props = dict(monster_type='flying', dead=True)
    assert _monster_sprite_candidates(props, 5) == _monster_sprite_candidates(props)


@pytest.mark.qt
def test_heading_and_state_refresh_both_render_buffers():
    from editor.things import Monster
    from engine.entity_table import EntityTable
    from engine.change_journal import touch

    monster = Monster([0, 96, 0], {'angle': 90})
    tables = [EntityTable(), EntityTable()]
    for table in tables:
        table.begin_frame([monster], epoch=1)
    face_direction(monster, (0, 0, -1))
    monster.properties['is_shooting'] = True
    touch(monster)
    for table in tables:
        table.begin_frame([monster], epoch=1)
        assert table.monster_angle[0] == 180
        key = sprite_keys_for_view(table, np.array([0]), (0, 0, -100))[0]
        assert table.sprite_recipes()[key][0][1] == 'shoot_1.png'
    monster.properties['dead'] = True
    touch(monster)
    tables[0].begin_frame([monster], epoch=1)
    assert np.all(tables[0].sprite_direction_ids[0] == -1)


@pytest.mark.parametrize('appearance', ['human', 'human/variant1', 'flying', 'flying/variant1'])
def test_every_builtin_has_complete_packaged_rotation_images(appearance):
    import zipfile
    from pathlib import Path
    from PIL import Image

    root = Path(__file__).resolve().parents[2]
    expected_size = (256, 384) if appearance.startswith('human') else (256, 256)
    # The loose files are the source of truth; a shipped sprites.fiopak is
    # optional, but when one is present it must carry the same bytes.
    pak = root / 'assets/sprites.fiopak'
    archive = zipfile.ZipFile(pak) if pak.is_file() else None
    try:
        for state in ('idle', 'shoot'):
            images = []
            for rotation in range(1, 9):
                name = f'assets/sprites/monsters/{appearance}/{state}_{rotation}.png'
                data = (root / name).read_bytes()
                if archive is not None:
                    assert archive.read(name) == data
                with Image.open(root / name) as image:
                    assert image.mode == 'RGBA' and image.size == expected_size
                    low, high = image.getchannel('A').getextrema()
                    assert low == 0 and high >= 250
                images.append(data)
            assert len(set(images)) == 8
    finally:
        if archive is not None:
            archive.close()


@pytest.mark.qt
@pytest.mark.parametrize('camera,expected', [((0, 0, 100), 'idle_1.png'),
                                             ((0, 0, -100), 'idle_5.png')])
def test_standalone_player_uses_camera_relative_recipe(camera, expected):
    from editor.things import Monster
    from engine.entity_table import EntityTable
    from player.render.renderer import GLESRenderer

    table = EntityTable()
    table.begin_frame([Monster([0, 96, 0])], epoch=1)
    class Package:
        def has_asset(self, name):
            return True
    renderer = GLESRenderer()
    renderer._scene = {'package': Package()}
    assert renderer._sprite_image(table, 0, camera).endswith(expected)
