"""Terrain defaults and persistence through a real GL Terrain instance."""

import pytest

pytest.importorskip("PyQt5", reason="Terrain shader setup uses the Qt/OpenGL test context")
pytest.importorskip("OpenGL")

import engine.terrain as terrain_module
from engine.terrain import BIOMES, DEFAULT_BIOME, Terrain
from tests.helpers.gl import GLTestContext

pytestmark = pytest.mark.gl


@pytest.fixture
def new_terrain():
    with GLTestContext(64, 64):
        terrain = Terrain()
        try:
            yield terrain
        finally:
            terrain.cleanup()


def test_new_terrain_is_rocky_mountains_with_textures_and_grass(new_terrain):
    assert DEFAULT_BIOME == "rocky_mountains"
    assert new_terrain.biome is BIOMES["rocky_mountains"]
    assert new_terrain.use_textures is True
    assert terrain_module.DEFAULT_USE_TEXTURES is True
    assert new_terrain.grass_enabled is True
    assert terrain_module.DEFAULT_GRASS_ENABLED is True


def test_a_saved_map_keeps_its_own_choices(new_terrain):
    new_terrain.set_biome("desert")
    new_terrain.use_textures = False
    new_terrain.set_grass(False)
    data = new_terrain.to_dict()

    new_terrain.from_dict(data)

    assert new_terrain.use_textures is False
    assert new_terrain.grass_enabled is False
    assert new_terrain.biome.name == BIOMES["desert"].name


def test_biome_names_carry_no_emoji_and_name_their_own_key():
    from engine.terrain import biome_key_for_name

    for key, biome in BIOMES.items():
        assert biome.name.isascii(), f"{key}: {biome.name!r}"
        assert biome_key_for_name(biome.name) == key


def test_a_map_saved_with_an_emoji_biome_name_loads_clean(new_terrain):
    from engine.terrain import biome_key_for_name

    assert biome_key_for_name("Grassy Hills \U0001F33F") == "grassy_hills"
    assert biome_key_for_name("Desert \U0001F3DC️") == "desert"

    data = new_terrain.to_dict()
    data["custom_biome"]["name"] = "Mountains ⛰️"
    new_terrain.from_dict(data)

    assert new_terrain.biome.name == "Mountains"


def test_a_map_saved_before_grass_existed_stays_grass_free(new_terrain):
    data = new_terrain.to_dict()
    data.pop("grass_enabled")

    new_terrain.from_dict(data)

    assert new_terrain.grass_enabled is False
