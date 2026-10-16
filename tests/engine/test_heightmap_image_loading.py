"""Image import preserves rows, normalization, and existing data on failure."""

import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from engine.terrain import Terrain

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('width', [1, 2, 3, 4, 5, 7, 9, 16])
@pytest.mark.parametrize('use_qt', [False, True], ids=['pillow', 'qt-fallback'])
def test_heightmap_preserves_every_pixel_and_owns_storage(tmp_path, monkeypatch, width, use_qt):
    path = tmp_path / 'height.png'
    pixels = np.random.default_rng(width).integers(0, 256, (5, width), dtype=np.uint8)
    pixels[0, 0], pixels[-1, -1] = 0, 255
    Image.fromarray(pixels).save(path)
    if use_qt:
        monkeypatch.setitem(sys.modules, 'PIL', None)
    dirty = []
    terrain = SimpleNamespace(heightmap_data=None, mark_all_dirty=lambda: dirty.append(True))
    Terrain.load_heightmap(terrain, str(path))
    expected = pixels.astype(np.float32) / 255.0
    np.testing.assert_array_equal(terrain.heightmap_data.view(np.uint32), expected.view(np.uint32))
    assert terrain.heightmap_data.flags.owndata
    assert dirty == [True]
    # No borrowed QImage/Pillow storage is needed after the loader returns.
    path.unlink()
    np.testing.assert_array_equal(terrain.heightmap_data, expected)


@pytest.mark.parametrize('use_qt', [False, True], ids=['pillow', 'qt-fallback'])
def test_bad_image_leaves_previous_heightmap_and_dirty_state_intact(tmp_path, monkeypatch, use_qt):
    path = tmp_path / 'bad.png'
    path.write_bytes(b'not an image')
    if use_qt:
        monkeypatch.setitem(sys.modules, 'PIL', None)
    dirty = []
    previous = np.ones((2, 3), dtype=np.float32)
    terrain = SimpleNamespace(heightmap_data=previous, mark_all_dirty=lambda: dirty.append(True))
    with pytest.raises((OSError, ValueError)):
        Terrain.load_heightmap(terrain, str(path))
    assert terrain.heightmap_data is previous
    assert dirty == []
