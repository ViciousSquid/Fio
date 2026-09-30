"""What a newly created terrain starts as, and what a saved map keeps.

New terrain is Rocky Mountains with textures and grass on. A map that saved its terrain
keeps what it saved: the defaults apply to terrain created fresh, not to maps
being loaded.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("glm")
pytest.importorskip("OpenGL")

import engine.terrain as terrain_module  # noqa: E402
from engine.terrain import BIOMES, DEFAULT_BIOME, Terrain  # noqa: E402


@pytest.fixture
def new_terrain(monkeypatch):
    monkeypatch.setattr(Terrain, "_init_shader", lambda self: None)
    return Terrain


def test_new_terrain_is_rocky_mountains_with_textures_and_grass(new_terrain):
    t = new_terrain()
    assert DEFAULT_BIOME == "rocky_mountains"
    assert t.biome is BIOMES["rocky_mountains"]
    assert t.use_textures is True
    assert terrain_module.DEFAULT_USE_TEXTURES is True
    assert t.grass_enabled is True
    assert terrain_module.DEFAULT_GRASS_ENABLED is True


def test_a_saved_map_keeps_its_own_choices(new_terrain):
    saved = new_terrain()
    saved.set_biome("desert")
    saved.use_textures = False
    saved.set_grass(False)
    data = saved.to_dict()

    loaded = new_terrain()
    loaded.from_dict(data)
    assert loaded.use_textures is False
    assert loaded.grass_enabled is False
    assert loaded.biome.name == BIOMES["desert"].name


def test_biome_names_carry_no_emoji_and_name_their_own_key():
    from engine.terrain import biome_key_for_name
    for key, biome in BIOMES.items():
        assert biome.name.isascii(), f"{key}: {biome.name!r}"
        # The terrain editor selects the dropdown entry by this mapping.
        assert biome_key_for_name(biome.name) == key


def test_a_map_saved_with_an_emoji_biome_name_loads_clean(new_terrain):
    from engine.terrain import biome_key_for_name
    assert biome_key_for_name("Grassy Hills \U0001F33F") == "grassy_hills"
    assert biome_key_for_name("Desert \U0001F3DC️") == "desert"
    t = new_terrain()
    data = t.to_dict()
    data["custom_biome"]["name"] = "Mountains ⛰️"
    t.from_dict(data)
    assert t.biome.name == "Mountains"


def test_a_map_saved_before_grass_existed_stays_grass_free(new_terrain):
    data = new_terrain().to_dict()
    data.pop("grass_enabled")
    loaded = new_terrain()
    loaded.from_dict(data)
    assert loaded.grass_enabled is False
