"""The player's decals: the images a world plays with, and what is drawn."""
import io

import numpy as np
from PIL import Image

from engine import decal_textures
from engine.decal_table import box_matrix
from player.render import glmath
from player.render.decals import has_decals, visible_decals
from player.runtime.fiopak import FioPackage
from player.tests.fixtures import build_package
from player.world import decal_source


def _png():
    out = io.BytesIO()
    Image.new('RGBA', (4, 4), (10, 20, 30, 255)).save(out, format='PNG')
    return out.getvalue()


def test_a_world_plays_with_the_bundled_blood_and_its_package_decals():
    raw = build_package({'map_path': 'maps/a.json'}, {'maps/a.json': {}}, {
        'assets/textures/decals/stain.png': _png(),
        'assets/textures/decals/notes.txt': b'not an image',
        'assets/textures/floor.png': _png()})
    with FioPackage.from_bytes(raw) as package:
        source = decal_source(package)
        names = source.names()
        assert 'stain.png' in names and 'floor.png' not in names and 'notes.txt' not in names
        assert any(n.startswith('blood_wall_') for n in names)
        assert any(n.startswith('blood_floor_') for n in names)
        assert source.read('stain.png') == _png()
        try:
            version, listed = decal_textures.use_source(source)
            assert listed == names
            # The logic side's layer for a placed decal is the renderer's.
            assert decal_textures.layer_of('stain.png') == names.index('stain.png')
            assert decal_textures.read_image('stain.png') == _png()
            first, walls, _floor, floors = decal_textures.blood_ranges(listed)
            assert walls > 0 and floors > 0
        finally:
            decal_textures.use_source(None)


def test_only_decals_in_view_and_big_enough_are_drawn():
    eye = np.array((0.0, 0.0, 0.0))
    view = glmath.look_at(eye, (0, 0, -1))
    projection = glmath.perspective(np.radians(70), 1.0, .5, 4000)
    clip = projection @ view
    ahead = box_matrix((0, 0, -300), (0, 0, -1), 100, 32)
    behind = box_matrix((0, 0, 300), (0, 0, 1), 100, 32)
    tiny_far = box_matrix((0, 0, -3500), (0, 0, -1), .5, .5)
    models = np.stack((ahead, behind, tiny_far)).astype(np.float32)
    assert list(visible_decals(models, clip, eye, 720, projection[1, 1])) == [0]
    assert not has_decals(None)
    assert not has_decals((np.zeros((0, 16)), np.zeros((0, 4))))
    assert has_decals((models, np.zeros((3, 4))))
