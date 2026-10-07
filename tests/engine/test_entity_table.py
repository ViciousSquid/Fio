"""Focused regression tests for EntityTable asset-path resolution.

These tests exercise the exact helper and call sites that previously shipped with
an undefined _split_asset_path global. They intentionally avoid Qt and GL:
the failure occurs in the pure data projection before rendering.
"""

import numpy as np
import pytest

from engine import entity_table
from engine.change_journal import MOVED, touch
from engine.effect_entity import Effect
from engine.effect_table import EffectStore


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


class Barrel:
    """A Qt-free stand-in for an entity with a ``properties`` dict."""

    def __init__(self, **properties):
        self.properties = properties


def test_sprite_candidates_resolves_prop_sprite_path_without_name_error():
    thing = Barrel(sprite_path="assets/sprites/props/barrel.png")

    candidates = entity_table.sprite_candidates(thing)

    assert candidates[-1] == ("Barrel", "barrel.png", "sprites/props", True)


def test_sprite_candidates_resolves_nested_sprite_path_without_name_error():
    thing = Barrel(sprite_path="assets/sprites/animated/door/frame_01.png")

    candidates = entity_table.sprite_candidates(thing)

    assert candidates[-1] == ("Barrel", "frame_01.png", "sprites/animated/door", True)


@pytest.mark.qt
def test_a_dead_monster_row_interns_a_distinct_dead_sprite_recipe():
    pytest.importorskip("PyQt5", reason="Monster lives in editor.things")
    from editor.things import Monster
    from tests.helpers.worlds import make_thing

    table = entity_table.EntityTable()
    grunt = make_thing(Monster, "grunt", monster_type="human")
    # A journalled state change re-resolves the already-projected row.
    table.begin_frame([grunt], epoch=1)
    idle_id = int(table.sprite_key_id[0])
    grunt.properties["dead"] = True
    touch(grunt)
    table.begin_frame([grunt], epoch=1)
    dead_id = int(table.sprite_key_id[0])

    assert dead_id != idle_id
    assert any(candidate[1] == "dead.png" for candidate in table.sprite_recipes()[dead_id])



@pytest.fixture
def renderer():
    pytest.importorskip("PyQt5", reason="renderer construction requires Qt")
    pytest.importorskip("OpenGL", reason="sprite cache test requires OpenGL")
    from tests.helpers.gl import GLTestContext, make_renderer, reset_texture_cache

    reset_texture_cache()
    with GLTestContext(64, 64):
        value = make_renderer()
        try:
            yield value
        finally:
            value.cleanup()
            reset_texture_cache()

@pytest.mark.gl
def test_sprite_gl_cache_resolves_recipes_added_after_capacity_growth(renderer):
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


@pytest.mark.gl
def test_sprite_gl_cache_survives_the_render_buffers_alternating(renderer):
    """Each render buffer's EntityTable interns its own recipe list and the
    renderer sees them alternately; resolving must happen once per list, not
    once per frame."""
    renderer._sprite_recipes_seen = None
    renderer._sprite_gl_by_id = np.zeros(0, dtype=np.int32)
    renderer._sprite_gl_resolved = 0
    resolved = []
    renderer._resolve_sprite_recipe = lambda recipe: resolved.append(recipe) or 7

    def table_of(recipes):
        return type("Table", (), {"sprite_recipes": lambda self: recipes})()

    front = table_of([(("idle", "idle.png", "sprites", True),)])
    back = table_of([(("idle", "idle.png", "sprites", True),)])
    for _ in range(10):
        assert renderer._sprite_gl_ids(front)[0] == 7
        assert renderer._sprite_gl_ids(back)[0] == 7
    assert len(resolved) == 2, (
        "%d resolutions for two recipe lists over ten frames" % len(resolved))


def test_moved_effect_rows_sync_to_effect_store_with_one_masked_write(monkeypatch):
    store = EffectStore()
    table = entity_table.EntityTable()
    first = object()
    second = object()
    things = [first, second]

    # Configure the dense metadata that _apply_changes normally receives from
    # the cold entity resolve. This keeps the test focused on the MOVED path.
    table._slot_of_obj = {id(first): 0, id(second): 1}
    table.class_bits = np.asarray(
        [entity_table.ENT_EFFECT, entity_table.ENT_EFFECT],
        dtype=table.class_bits.dtype,
    )
    table.effect_store_index = np.asarray([0, 1], dtype=np.int32)
    table.pos = np.asarray(
        [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
        dtype=np.float64,
    )
    store.pos[:2] = table.pos

    new_positions = {
        id(first): (100.0, 1.0, 2.0),
        id(second): (200.0, 3.0, 4.0),
    }

    def fail_set_position(*args, **kwargs):
        raise AssertionError("per-effect set_position loop was used")

    monkeypatch.setattr(EffectStore, "set_position", fail_set_position)

    table._apply_changes(
        things,
        {id(first): MOVED, id(second): MOVED},
        new_positions,
        effect_store=store,
    )

    np.testing.assert_array_equal(
        store.pos[:2],
        np.asarray(
            [new_positions[id(first)], new_positions[id(second)]],
            dtype=np.float32,
        ),
    )
