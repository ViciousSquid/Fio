"""The player checks a packaged map before it builds a world from it.

A ``.fiopak`` is the player's only input. The editor validates a map when it
opens one (``engine.level_validation``); the player built its world straight
from the package, so a brush sized 1e308 ran the spatial grid out of memory
and a ``pos`` that was a string raised half-way into construction.
"""

import pytest

from player.runtime.fiopak import PackageError
from player.world import PlayerWorld

TEXTURES = {face: "default.png" for face in ("north", "south", "east", "west", "top", "down")}
FLOOR = {"pos": [0, -16, 0], "size": [1024, 32, 1024], "textures": TEXTURES}
START = {"type": "PlayerStart", "pos": [0, 128, 0]}


def _level(brushes=(), things=()):
    return {"brushes": [FLOOR, *brushes], "things": [START, *things]}


@pytest.mark.parametrize("level, names", [
    (_level(brushes=[{"pos": [0, 0, 0], "size": [1e308, 64, 64], "textures": TEXTURES}]),
     "brushes[1]['size']"),
    (_level(brushes=[{"pos": "abc", "size": [64, 64, 64], "textures": TEXTURES}]),
     "brushes[1]['pos']"),
    (_level(things=[5]), "things[1]"),
    (_level(things=[{"type": "Light", "pos": [0, 64, 0],
                     "properties": {"radius": float("inf")}}]), "radius"),
    (_level(things=[{"type": "prop", "pos": [0, 64, 0],
                     "properties": {"collision_size": [1e308, 1, 1]}}]), "collision_size"),
])
def test_a_map_the_editor_would_refuse_is_refused(level, names):
    with pytest.raises(PackageError, match="cannot be loaded") as caught:
        PlayerWorld(level)
    assert names in str(caught.value)


def test_a_sound_map_still_loads():
    world = PlayerWorld(_level())
    world.close()
