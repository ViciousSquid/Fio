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
