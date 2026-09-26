"""Focused regression tests for EntityTable asset-path resolution.

These tests exercise the exact helper and call sites that previously shipped with
an undefined _split_asset_path global. They intentionally avoid Qt and GL:
the failure occurs in the pure data projection before rendering.
"""

import pytest

from engine import entity_table


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sprites/foo.png", ("foo.png", "sprites")),
        ("assets/sprites/monsters/orc/dead.png", ("dead.png", "sprites/monsters/orc")),
        ("assets/foo.png", ("foo.png", "")),
        ("foo.png", ("foo.png", "")),
    ],
)
def test_split_asset_path_returns_filename_and_asset_relative_folder(path, expected):
    assert entity_table._split_asset_path(path) == expected


def test_sprite_candidates_resolves_prop_sprite_path_without_name_error():
    thing = {"sprite_path": "assets/sprites/props/barrel.png"}

    candidates = entity_table.sprite_candidates(thing)

    assert candidates[-1] == ("dict", "barrel.png", "sprites/props", True)


def test_sprite_candidates_resolves_nested_sprite_path_without_name_error():
    thing = {"sprite_path": "assets/sprites/animated/door/frame_01.png"}

    candidates = entity_table.sprite_candidates(thing)

    assert candidates[-1] == ("dict", "frame_01.png", "sprites/animated/door", True)


def test_monster_dead_snapshot_interns_distinct_dead_sprite_recipe():
    table = entity_table.EntityTable()
    idle = {
        "pos": [0.0, 0.0, 0.0],
        "dead": False,
        "is_shooting": False,
        "monster_type": "human",
        "variant": "<None>",
        "sprite_width": 128,
        "sprite_height": 128,
        "custom_idle": "",
        "custom_shoot": "",
        "custom_dead": "",
    }
    dead = dict(idle, dead=True)

    # The snapshot path updates an already-projected monster row.
    table.begin_frame([idle], epoch=1)
    table.update_monster_snapshot(0, idle)
    idle_id = int(table.sprite_key_id[0])
    table.update_monster_snapshot(0, dead)
    dead_id = int(table.sprite_key_id[0])

    assert dead_id != idle_id
    assert any(candidate[1] == "dead.png" for candidate in table.sprite_recipes()[dead_id])


def test_sprite_gl_cache_resolves_recipes_added_after_capacity_growth():
    from engine.renderer_core import BaseRenderer
    import numpy as np

    renderer = BaseRenderer.__new__(BaseRenderer)
    renderer._sprite_recipes_seen = None
    renderer._sprite_gl_by_id = np.zeros(0, dtype=np.int32)
    renderer._sprite_gl_resolved = 0
    resolved = []

    renderer._resolve_sprite_recipe = lambda recipe: resolved.append(recipe) or (len(resolved) + 100)

    recipes = [(("idle", "idle.png", "sprites/monsters/human", True),)]
    table = type("Table", (), {"sprite_recipes": lambda self: recipes})()
    first = renderer._sprite_gl_ids(table)
    assert first[0] == 101
    assert len(resolved) == 1

    recipes.append((("dead", "dead.png", "sprites/monsters/human", True),))
    second = renderer._sprite_gl_ids(table)
    assert second[1] == 102
    assert len(resolved) == 2
