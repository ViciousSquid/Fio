"""Grass controls persist, preserve legacy defaults and affect the actual runtime."""

import numpy as np
import pytest

pytestmark = pytest.mark.qt


def test_grass_controls_update_the_terrain_and_survive_save_load(qt_app):
    from editor.terrain_editor import TerrainEditorPanel
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    terrain.set_grass(True, density=0.023)
    panel = TerrainEditorPanel(terrain)
    try:
        # Showing rounded density must not change the authored value.
        assert terrain.grass_density == 0.023
        changed = []
        panel.terrain_changed.connect(lambda: changed.append(True))
        panel.grass_blade_height_spin.setValue(12.5)
        panel.grass_density_slider.setValue(1200)
        assert terrain.GRASS_BLADE_HEIGHT == 12.5
        assert terrain.grass_density == 1200 * 0.0001
        assert len(changed) == 2
        loaded = Terrain(seed=1)
        loaded.from_dict(terrain.to_dict())
        assert loaded.GRASS_BLADE_HEIGHT == 12.5
        assert loaded.grass_density == terrain.grass_density
    finally:
        panel.close()
        panel.deleteLater()


def test_density_slider_keeps_fine_steps_across_the_full_range(qt_app):
    from editor.terrain_editor import TerrainEditorPanel
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    terrain.set_grass(True, density=0.023)
    panel = TerrainEditorPanel(terrain)
    try:
        assert panel.grass_density_slider.value() == 230
        assert terrain.grass_density == 0.023
        assert panel.grass_density_value.text() == '115%'
        panel.grass_density_slider.setValue(231)
        assert terrain.grass_density == pytest.approx(0.0231)
        panel.grass_density_slider.setValue(panel.grass_density_slider.maximum())
        assert terrain.grass_density == pytest.approx(0.24)
        assert panel.grass_density_value.text() == '1200%'
    finally:
        panel.close()
        panel.deleteLater()


def test_authored_height_and_existing_shader_height_share_one_value():
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    terrain.grass_height = 12.5
    assert terrain.GRASS_BLADE_HEIGHT == 12.5
    terrain.GRASS_BLADE_HEIGHT = 17.5
    assert terrain.grass_height == 17.5
    loaded = Terrain(seed=11)
    loaded.from_dict(terrain.to_dict())
    assert loaded.grass_height == loaded.GRASS_BLADE_HEIGHT == 17.5


def test_height_needs_no_instance_rebuild_and_density_invalidates_instances():
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    terrain.table.ensure_bounds(terrain._chunk_bounds(), terrain.chunk_size, 0, 0)
    slots = terrain.table.live_slots()
    terrain.table.grass_dirty[slots] = False
    terrain.set_grass(terrain.grass_enabled, height=20)
    assert not terrain.table.grass_dirty[slots].any()
    terrain.set_grass(terrain.grass_enabled, density=0.04)
    assert terrain.table.grass_dirty[slots].all()


def test_legacy_grass_height_stays_at_five_units():
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    data = terrain.to_dict()
    data.pop('grass_height')
    terrain.set_grass(terrain.grass_enabled, height=20)
    terrain.from_dict(data)
    assert terrain.GRASS_BLADE_HEIGHT == 5.0


def test_increased_density_actually_adds_blades_beyond_the_old_chunk_cap():
    from engine.terrain import Terrain, MAX_GRASS_DENSITY
    from tests.engine.test_terrain_appearance import _build, _slot
    terrain = Terrain(seed=11)
    terrain.set_biome('grassy_hills')
    terrain.set_bounds(-1, 0, -1, 0)
    _build(terrain)
    slot = _slot(terrain)
    terrain.set_grass(True, density=0.06, height_range=(0, 1))
    legacy = terrain._generate_grass_blades(slot)
    terrain.set_grass(True, density=MAX_GRASS_DENSITY)
    dense = terrain._generate_grass_blades(slot)
    assert len(dense) > len(legacy) * 2
    assert len(dense) <= terrain.GRASS_MAX_PER_CHUNK * 4 * terrain.GRASS_BLADES_PER_TUFT
    loaded = Terrain()
    loaded.from_dict(terrain.to_dict())
    assert loaded.grass_density == MAX_GRASS_DENSITY


@pytest.mark.parametrize('value, expected', [(-1, 0.1), (1000, 256), (np.nan, 5)])
def test_height_has_finite_supported_bounds(value, expected):
    from engine.terrain import Terrain
    terrain = Terrain(seed=11)
    terrain.set_grass(terrain.grass_enabled, height=value)
    assert terrain.GRASS_BLADE_HEIGHT == expected
