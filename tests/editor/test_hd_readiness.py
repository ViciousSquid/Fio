"""Tools > HD Readiness Report: where a map's textures get their HD maps."""

import os

from editor import hd_readiness
from tests.helpers.paths import repo_path


class _Thing:
    def __init__(self, **properties):
        self.properties = properties


class Decal(_Thing):
    pass


class _State:
    def __init__(self, brushes=(), things=(), terrain=None):
        self.brushes, self.things, self.terrain = list(brushes), list(things), terrain


def _brush(**faces):
    return {"textures": faces}


def test_the_report_lists_what_the_map_uses_and_how_each_gets_its_maps(tmp_path):
    state = _State(
        brushes=[_brush(top="cobblestone.jpg", north="Dev/512.jpg", down="nodraw.jpg"),
                 _brush(top="cobblestone.jpg", east="no_such_texture.png")],
        things=[Decal(texture="Signage.png"), _Thing(texture="ignored.png")])
    rows = hd_readiness.texture_report(state, repo_path("assets", "textures"))
    found = {name: (status, uses) for name, status, uses in rows}
    assert found["cobblestone.jpg"] == ("generated", ["brush"])
    assert found["Dev/512.jpg"] == ("tool", ["brush"])
    assert found["nodraw.jpg"] == ("tool", ["brush"])
    assert found["decals/Signage.png"] == ("generated", ["decal"])
    assert found["no_such_texture.png"] == ("missing", ["brush"])
    assert "ignored.png" not in found and len(rows) == 5
    # What wants attention comes first.
    assert rows[0][1] == "missing" and rows[-1][1] == "tool"
    assert hd_readiness.summary(rows) == {"missing": 1, "generated": 2, "tool": 2}


def test_a_texture_with_no_maps_beside_it_is_generated_at_load(tmp_path):
    from PIL import Image
    Image.new("RGB", (8, 8), (120, 90, 60)).save(tmp_path / "bare.png")
    Image.new("RGB", (8, 8), (120, 90, 60)).save(tmp_path / "painted.png")
    Image.new("RGBA", (8, 8), (128, 128, 255, 255)).save(tmp_path / "painted_n.png")
    Image.new("RGBA", (8, 8), (255, 200, 0, 255)).save(tmp_path / "painted_m.png")
    state = _State(brushes=[_brush(top="bare.png", down="painted.png")])
    found = {name: status for name, status, _uses
             in hd_readiness.texture_report(state, str(tmp_path))}
    assert found == {"bare.png": "runtime", "painted.png": "hand-made"}


def test_terrain_brings_its_layers():
    class Terrain:
        enabled = True
    rows = hd_readiness.texture_report(_State(terrain=Terrain()),
                                       repo_path("assets", "textures"))
    assert [name for name, _s, _u in rows] == sorted(hd_readiness.TERRAIN_LAYERS)
    assert {status for _n, status, _u in rows} == {"generated"}
    assert all(uses == ["terrain"] for _n, _s, uses in rows)
    assert os.path.isfile(repo_path("assets", "textures", "terrain", "rock_n.webp"))
