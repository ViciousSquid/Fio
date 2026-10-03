"""Procedural generation must actually yield when asked to.

``generate_brushes_from_grid`` and ``create_map_data`` accept a ``yield_hook``
so a caller driving them from a UI thread can keep its event loop alive during
the two O(w*h) grid walks. The parameter was added with a syntax error, the
file was then truncated and restored, and what came out the far side accepted
the hook and threaded it through without ever calling it -- so the benchmark
plugin, which passes one, froze the editor for the whole of geometry
generation believing it had asked not to be.
"""
import copy
import random

import pytest

pytest.importorskip("PyQt5", reason="the generator is editor-tier")

from editor.procedural_generator import (  # noqa: E402
    YIELD_EVERY_COLUMNS, create_map_data, generate_brushes_from_grid)


def _params(**overrides):
    params = {
        "world_width": 2048, "world_height": 2048,
        "min_room": 256, "max_room": 512, "room_count": 6,
        "wall_tex": "default.png", "floor_tex": "default.png",
        "enable_floors": False, "floor_height": 128, "floor_room_count": 0,
        "spawn_monsters": False, "monster_count": 0, "spawn_health": False,
    }
    params.update(overrides)
    return params


def test_create_map_data_calls_the_yield_hook():
    calls = []
    random.seed(11)
    create_map_data(_params(), yield_hook=lambda: calls.append(1))

    assert calls, (
        "yield_hook was accepted and never called -- a caller asking to stay "
        "responsive is frozen for the whole of geometry generation")


def test_the_yield_hook_does_not_change_the_geometry():
    """Responsiveness must not cost determinism."""
    random.seed(11)
    with_hook = create_map_data(_params(), yield_hook=lambda: None)
    random.seed(11)
    without_hook = create_map_data(_params(), yield_hook=None)

    assert with_hook["brushes"] == without_hook["brushes"]


def test_the_hook_fires_across_the_grid_not_just_once():
    """A single call at the start would satisfy a naive check and help nobody."""
    calls = []
    random.seed(11)
    create_map_data(_params(world_width=4096, world_height=4096),
                    yield_hook=lambda: calls.append(1))

    # Two grid walks, each yielding every YIELD_EVERY_COLUMNS columns.
    assert len(calls) > 2, (
        "yield_hook fired %d time(s); the event loop needs servicing "
        "throughout the walk, not once" % len(calls))


def test_generation_still_works_without_a_hook():
    random.seed(11)
    data = create_map_data(_params(), yield_hook=None)
    assert data["brushes"], "generation produced no geometry"


def test_yield_cadence_is_declared():
    assert isinstance(YIELD_EVERY_COLUMNS, int) and YIELD_EVERY_COLUMNS >= 1
