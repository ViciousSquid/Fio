"""Tests for procedural weapon, ammo, and texture generation."""

import random

import pytest

pytest.importorskip("PyQt5", reason="the generator is editor-tier")

from editor.procedural_generator import (  # noqa: E402
    RANDOM_FLOOR_TEXTURES,
    RANDOM_WALL_TEXTURES,
    create_map_data,
)


def _params(**overrides):
    params = {
        "world_width": 2048,
        "world_height": 2048,
        "min_room": 256,
        "max_room": 384,
        "room_count": 10,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "random_wall_texture": False,
        "random_floor_texture": False,
        "enable_floors": False,
        "floor_height": 128,
        "floor_room_count": 0,
        "spawn_monsters": False,
        "monster_count": 0,
        "spawn_health": False,
        "health_count": 0,
        "ammo_count": 4,
    }
    params.update(overrides)
    return params


def _props(data, collect_type):
    return [
        thing for thing in data["things"]
        if thing["type"] == "prop"
        and thing["properties"].get("collect_type") == collect_type
    ]


def _weapon_props(data):
    return [
        thing for thing in data["things"]
        if thing["type"] == "prop"
        and thing["properties"].get("collect_type") == "weapon"
    ]


def test_procedural_generator_places_weapon_on_playerstart_and_ammo_only_for_gun2():
    saw_gun1 = False
    saw_gun2 = False

    for seed in range(200):
        random.seed(seed)
        data = create_map_data(_params())

        player = next(t for t in data["things"] if t["type"] == "playerstart")
        weapons = _weapon_props(data)
        assert len(weapons) == 1
        weapon = weapons[0]

        assert weapon["pos"] == player["pos"]
        assert weapon["properties"]["collect_weapon"] in {"gun1", "gun2"}

        ammo = _props(data, "ammo")
        if weapon["properties"]["collect_weapon"] == "gun2":
            saw_gun2 = True
            assert len(ammo) == 4
        else:
            saw_gun1 = True
            assert ammo == []

    assert saw_gun1
    assert saw_gun2


def test_procedural_generator_random_weapon_is_approximately_60_40():
    gun1 = 0
    gun2 = 0
    for seed in range(1000):
        random.seed(seed)
        data = create_map_data(_params())
        weapon = _weapon_props(data)[0]["properties"]["collect_weapon"]
        if weapon == "gun1":
            gun1 += 1
        else:
            gun2 += 1

    # The generator uses an explicit 60/40 split. A broad statistical band
    # keeps this test about the machinery rather than a brittle exact count.
    assert 520 <= gun1 <= 680
    assert 320 <= gun2 <= 480


def test_random_texture_generation_uses_only_allowed_textures():
    random.seed(1234)
    data = create_map_data(
        _params(random_wall_texture=True, random_floor_texture=True)
    )

    wall_textures = {
        texture
        for brush in data["brushes"]
        for texture in brush["textures"].values()
        if texture != "nodraw.jpg"
        and brush["id"].startswith(("wall_", "corner_pillar_"))
    }
    floor_textures = {
        texture
        for brush in data["brushes"]
        for face, texture in brush["textures"].items()
        if texture != "nodraw.jpg"
        and face in {"top", "down"}
        and (
            brush["id"] == "ground_plane"
            or brush["id"] == "global_ceiling"
            or brush["id"].startswith(("room_ceil_", "mezz"))
        )
    }

    assert wall_textures <= set(RANDOM_WALL_TEXTURES)
    assert floor_textures <= set(RANDOM_FLOOR_TEXTURES)


def test_missing_selected_textures_fall_back_to_default():
    random.seed(1)
    data = create_map_data(
        _params(wall_tex="does-not-exist.png", floor_tex="also-missing.png")
    )

    wall_faces = [
        texture
        for brush in data["brushes"]
        if brush["id"].startswith("wall_")
        for texture in brush["textures"].values()
        if texture != "nodraw.jpg"
    ]
    ground_top = next(
        brush["textures"]["top"]
        for brush in data["brushes"]
        if brush["id"] == "ground_plane"
    )

    assert wall_faces
    assert set(wall_faces) == {"default.png"}
    assert ground_top == "default.png"


def test_procedural_generator_preserves_health_when_weapon_is_gun2():
    found = False
    for seed in range(200):
        random.seed(seed)
        data = create_map_data(_params(spawn_health=True, health_count=3))
        weapon = _weapon_props(data)[0]["properties"]["collect_weapon"]
        if weapon == "gun2":
            found = True
            assert len(_props(data, "health")) == 3
            assert len(_props(data, "ammo")) == 4
            break

    assert found
