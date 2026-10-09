"""LowPoly_Tree_v1.obj is Y-up, like Fio and every other bundled model.

It was exported Z-up (its trunk ran along +Z), so inserted it lay on its side,
and maps stood it up with a 270 degree rotation. The file now holds the tree
upright; the maps' trees dropped that rotation (their world shape is the same).
"""

import json
import os

from tests.helpers.paths import REPO_ROOT

TREE = os.path.join(REPO_ROOT, "assets", "models", "LowPoly_Tree_v1.obj")


def _vertices():
    with open(TREE, encoding="utf-8") as handle:
        return [tuple(float(v) for v in line.split()[1:4])
                for line in handle if line.startswith("v ")]


def test_the_trunk_rises_along_y_from_the_origin():
    verts = _vertices()
    foot, ring = verts[:5], verts[5:10]           # the trunk's first two rings
    assert all(abs(y) < 1e-6 for _x, y, _z in foot)
    assert all(abs(y - 53.9898) < 1e-3 for _x, y, _z in ring)
    for (fx, _fy, fz), (rx, _ry, rz) in zip(foot, ring):
        assert (fx, fz) == (rx, rz)               # straight up: same x and z
    assert min(y for _x, y, _z in verts) == 0.0


def test_maps_place_the_tree_without_the_old_270_compensation():
    for name in ("_SHOWCASE.json", "DevTest.json"):
        with open(os.path.join(REPO_ROOT, "maps", name), encoding="utf-8") as handle:
            things = json.load(handle)["things"]
        trees = [t["properties"] for t in things
                 if "LowPoly_Tree_v1" in str(t["properties"].get("model_path"))]
        assert trees, name
        assert all(float(p["rotation"][0]) == 0.0 for p in trees), name
