"""A GLB thumbnail draws a bounded number of lines, and small models exactly
as before.

The Asset Browser draws a model's wireframe thumbnail on the UI thread. It
projected each vertex in a Python loop and drew every triangle edge with its
own ``drawLine`` call: a 7 MB GLB (400 000 triangles) froze the editor for
3.1 s. The projection is now NumPy, and past MAX_THUMBNAIL_TRIANGLES an
evenly spaced sample of the triangles is drawn. Counts work, not time.
"""

import json
import math
import struct

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.glb_loader imports PyOpenGL")

from engine import glb_loader                      # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.perf]


def _glb_file(path, vertices, triangles):
    pos = np.asarray(vertices, "<f4")
    idx = np.asarray(triangles, "<u4").reshape(-1)
    blob = pos.tobytes() + idx.tobytes()
    gltf = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(blob)}],
            "bufferViews": [{"buffer": 0, "byteLength": pos.nbytes},
                            {"buffer": 0, "byteOffset": pos.nbytes, "byteLength": idx.nbytes}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos),
                           "type": "VEC3"},
                          {"bufferView": 1, "componentType": 5125, "count": len(idx),
                           "type": "SCALAR"}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    chunks = (struct.pack("<II", len(js), 0x4E4F534A) + js
              + struct.pack("<II", len(blob), 0x004E4942) + blob)
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks)
    return str(path)


def _random_model(rng, vertices, triangles):
    return (rng.random((vertices, 3)) * [3.0, 1.0, 2.0] - 0.5,
            rng.integers(0, vertices, size=(triangles, 3)))


def _reference_thumbnail(filepath, width, height):
    """The per-vertex, per-edge thumbnail the asset browser used to draw."""
    from PyQt5.QtCore import QPointF
    from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
    loader = glb_loader.GLBLoader()
    assert loader.load(filepath)
    vertices = loader.get_flattened_vertices()
    min_v, max_v = [float("inf")] * 3, [float("-inf")] * 3
    for v in vertices:
        for i in range(3):
            if v[i] < min_v[i]:
                min_v[i] = v[i]
            if v[i] > max_v[i]:
                max_v[i] = v[i]
    center = [(min_v[i] + max_v[i]) / 2 for i in range(3)]
    scale = 0
    for i in range(3):
        scale = max(scale, (max_v[i] - min_v[i]) / 2)
    cos_y, sin_y = math.cos(math.radians(45)), math.sin(math.radians(45))
    cos_x, sin_x = math.cos(math.radians(30)), math.sin(math.radians(30))
    points = []
    for v in vertices:
        x, y, z = ((v[i] - center[i]) / scale for i in range(3))
        rx = x * cos_y - z * sin_y
        rz = x * sin_y + z * cos_y
        ry = y * cos_x - rz * sin_x
        points.append(QPointF(width / 2 + rx * (width * 0.4),
                              height / 2 - ry * (height * 0.4)))
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(50, 50, 60))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(0, 200, 255))
    pen.setWidthF(1.0)
    painter.setPen(pen)
    for tri in loader.get_flattened_triangles():
        for j in range(3):
            painter.drawLine(points[tri[j]], points[tri[(j + 1) % 3]])
    painter.end()
    return pixmap


@pytest.mark.parametrize("triangles", [1, 40, 2000])
@pytest.mark.parametrize("edge", [58, 90])
def test_a_small_model_draws_pixel_for_pixel_as_before(qt_app, tmp_path, triangles, edge):
    rng = np.random.default_rng(triangles)
    path = _glb_file(tmp_path / "m.glb", *_random_model(rng, triangles // 2 + 3, triangles))
    made = glb_loader._render_glb_thumbnail_impl(path, edge, edge)
    assert made.toImage() == _reference_thumbnail(path, edge, edge).toImage()


def test_a_large_model_draws_a_bounded_sample(qt_app, tmp_path, monkeypatch):
    from PyQt5.QtGui import QPainter
    drawn = []
    real_lines = QPainter.drawLines

    class Counting(QPainter):
        def drawLine(self, *args):                  # the old per-edge path
            drawn.append(1)
            return super().drawLine(*args)

        def drawLines(self, lines):
            drawn.append(len(lines))
            return real_lines(self, lines)

    monkeypatch.setattr("PyQt5.QtGui.QPainter", Counting)
    rng = np.random.default_rng(1)
    count = 5 * glb_loader.MAX_THUMBNAIL_TRIANGLES
    path = _glb_file(tmp_path / "big.glb", *_random_model(rng, count // 2, count))

    made = glb_loader._render_glb_thumbnail_impl(path, 90, 90)

    assert made is not None and not made.isNull()
    assert len(drawn) == 1                          # one call, not one a line
    assert drawn[0] <= 3 * glb_loader.MAX_THUMBNAIL_TRIANGLES
    assert drawn[0] >= 3 * glb_loader.MAX_THUMBNAIL_TRIANGLES * 0.9


def test_out_of_range_and_non_finite_vertices_are_skipped():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [np.nan, 0, 0]], float)
    triangles = np.array([[0, 1, 2], [0, 1, 3], [0, 1, 99]])
    lines = glb_loader._thumbnail_lines(vertices, triangles, 90, 90)
    # Triangle 0's three edges, and the one edge each of triangles 1 and 2
    # that touches neither the NaN vertex nor the missing vertex 99.
    assert lines.shape == (5, 4)
    assert np.isfinite(lines).all()
