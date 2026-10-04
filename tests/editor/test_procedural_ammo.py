"""Tests for ammo placement in the procedural map generator."""

import random

import pytest

pytest.importorskip("PyQt5", reason="the generator is editor-tier")

from editor.procedural_generator import create_map_data  # noqa: E402


def _params(**overrides):
    params = {
        "world_width": 2048,
        "world_height": 2048,
        "min_room": 256,
        "max_room": 384,
        "room_count": 10,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "enable_floors": False,
        "floor_height": 128,
        "floor_room_count": 0,
        "spawn_monsters": False,
        "monster_count": 0,
        "spawn_health": False,
        "health_count": 0,
        "spawn_ammo": True,
        "ammo_count": 6,
    }
    params.update(overrides)
    return params


def _ammo_props(map_data):
    return [
        thing for thing in map_data["things"]
        if thing["type"] == "prop"
        and thing["properties"].get("collect_type") == "ammo"
    ]


def test_procedural_generator_spawns_requested_ammo():
    random.seed(1234)
    data = create_map_data(_params())

    ammo = _ammo_props(data)

    assert len(ammo) == 6
    assert [prop["properties"]["id"] for prop in ammo] == [
        f"ammo_prop_{i}" for i in range(6)
    ]
    assert all(
        prop["properties"]["collect_value"] == 8
        and prop["properties"]["sprite_path"] == "assets/sprites/ammo.png"
        and prop["properties"]["collect_enabled"] is True
        for prop in ammo
    )


def test_procedural_generator_can_disable_ammo():
    random.seed(1234)
    data = create_map_data(_params(spawn_ammo=False))

    assert _ammo_props(data) == []


def test_procedural_generator_preserves_existing_health_and_adds_ammo():
    random.seed(5678)
    data = create_map_data(
        _params(spawn_health=True, health_count=3, spawn_ammo=True, ammo_count=4)
    )

    health = [
        thing for thing in data["things"]
        if thing["type"] == "prop"
        and thing["properties"].get("collect_type") == "health"
    ]
    ammo = _ammo_props(data)

    assert len(health) == 3
    assert len(ammo) == 4
