"""OBJ models authored Z-up are turned to Fio's Y-up when they load.

An OBJ does not say which way is up. A model standing on z = 0, deep along Z
and hanging well below y = 0 is Z-up (LowPoly_Tree_v1.obj was, and lay on
its side); it is turned -90 degrees about X. Y-up models standing on y = 0,
models centred on both axes and thin wall-mounted ones are left alone, and a
``# fio:up=y`` / ``# fio:up=z`` comment settles any file.
"""

import pytest

pytest.importorskip("OpenGL", reason="engine.obj_loader imports PyOpenGL")

from engine.obj_loader import (                     # noqa: E402
    OBJLoader, detect_up_axis, up_axis_tag, z_up_to_y_up,
)
from tests.helpers.paths import REPO_ROOT           # noqa: E402

pytestmark = pytest.mark.qt

# A Z-up "tree": a trunk from z=0 to z=100, a canopy either side of y=0.
Z_UP = [(0, 0, 0), (5, 0, 0), (0, 5, 0), (0, 0, 100),
        (-40, -40, 60), (40, 40, 100), (0, -40, 80)]


def _write(tmp_path, name, vertices, extra=""):
    path = tmp_path / name
    lines = [extra] if extra else []
    lines += ["v %g %g %g" % v for v in vertices]
    lines += ["vn 0 0 1", "f 1//1 2//1 3//1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _load(path):
    loader = OBJLoader()
    assert loader.load(path)
    return loader


def test_a_z_up_model_is_turned_y_up(tmp_path):
    loader = _load(_write(tmp_path, "tree.obj", Z_UP))
    assert loader.up_axis == "z"
    assert loader.vertices[3] == (0, 100, 0)               # the trunk's top is up
    assert min(v[1] for v in loader.vertices) == 0          # standing on y = 0
    assert loader.normals == [(0, 1, 0)]                    # +Z normal now points up


@pytest.mark.parametrize("vertices", [
    [(0, 0, 0), (10, 65, 10), (-10, 30, -10)],             # Y-up, on the ground (a drum)
    [(-10, -10, -2.5), (10, 10, 2.5)],                     # centred both ways (a book)
    [(-20, -15, 0), (20, 15, 2)],                          # thin, mounted on a wall
    [(-5, -5, 0), (5, 5, 0)],                              # flat in Z
])
def test_other_models_are_left_as_they_are(tmp_path, vertices):
    loader = _load(_write(tmp_path, "m.obj", vertices))
    assert loader.up_axis == "y" and loader.vertices == [tuple(v) for v in vertices]


def test_a_comment_settles_the_up_axis(tmp_path):
    centred = [(-10, -10, -10), (10, 10, 10)]
    assert _load(_write(tmp_path, "a.obj", centred, "# fio:up=z")).up_axis == "z"
    assert _load(_write(tmp_path, "b.obj", Z_UP, "#  Fio:Up = Y")).up_axis == "y"
    assert up_axis_tag("# fio:up=sideways") is None
    assert up_axis_tag("# an ordinary comment") is None


def test_the_conversion_is_a_rotation():
    assert z_up_to_y_up((1, 2, 3)) == (1, 3, -2)
    assert detect_up_axis([]) == "y"


def test_every_bundled_model_loads_y_up():
    import glob
    import os
    for path in glob.glob(os.path.join(REPO_ROOT, "assets", "models", "*.obj")):
        loader = _load(path)
        assert loader.up_axis == "y", os.path.basename(path)


def test_the_thumbnail_shows_the_model_as_the_editor_places_it(qt_app, tmp_path):
    from editor.asset_browser import render_obj_thumbnail
    z_up = render_obj_thumbnail(_write(tmp_path, "z.obj", Z_UP), 64, 64).toImage()
    y_up = render_obj_thumbnail(
        _write(tmp_path, "y.obj", [z_up_to_y_up(v) for v in Z_UP]), 64, 64).toImage()
    assert z_up == y_up
