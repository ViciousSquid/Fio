"""2D-view model wireframes, drawn in bulk (editor/model_wireframe.py).

A drag of twelve 6,000-triangle chairs repainted each 2D view in ~265 ms:
every triangle was three Python drawLine calls. The wireframe is now the
model's distinct edges, projected and culled in NumPy, rasterised once per
(model, rotation, scale, zoom, view) and drawn at each instance's position.
These tests hold that to what the per-triangle loop drew.
"""

import numpy as np
import pytest

from editor import model_wireframe as mw


def _cube():
    """A unit cube as flat triangles (36 corners, as a flat OBJ lays out)."""
    c = [(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    verts = []
    for a, b, cc, d in quads:
        verts += [c[a], c[b], c[cc], c[a], c[cc], c[d]]
    return np.array(verts, dtype=float)


class Flat:
    cpu_triangles = None


def test_shared_corners_are_welded_and_shared_edges_drawn_once():
    positions, edges = mw.build_edges(_cube(), Flat())
    assert len(positions) == 8
    # 12 cube edges + one diagonal per face.
    assert len(edges) == 18
    assert len({tuple(e) for e in edges.tolist()}) == len(edges)


def test_indexed_triangles_are_used_and_bad_indices_skipped():
    class Indexed:
        cpu_triangles = [(0, 1, 2), (0, 2, 99)]          # second is malformed
    verts = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0)], dtype=float)
    positions, edges = mw.build_edges(verts, Indexed())
    assert len(positions) == 3 and len(edges) == 3


def test_projection_matches_the_matrix_the_views_always_used():
    rng = np.random.default_rng(3)
    pts = rng.normal(size=(50, 3))
    rot, scale, pos = (10.0, 35.0, -20.0), (1.5, 0.5, 2.0), (100.0, -40.0, 7.0)
    r = mw.rotation_matrix(rot)
    world = (pts * np.array(scale)) @ r.T + np.array(pos)
    xs, ys = mw.project(pts, pos, scale, rot, (0, 2), (400, 300), (10, 20), 2.0, False)
    assert np.allclose(xs, 400 + (world[:, 0] - 10) * 2.0)
    assert np.allclose(ys, 300 + (world[:, 2] - 20) * 2.0)
    xs, ys = mw.project(pts, pos, scale, rot, (0, 1), (400, 300), (10, 20), 2.0, True)
    assert np.allclose(ys, 300 - (world[:, 1] - 20) * 2.0)


def test_off_screen_edges_are_culled_and_pixel_duplicates_dropped():
    xs = np.array([10.0, 10.2, 10.4, -50.0, -60.0])
    ys = np.array([10.0, 10.1, 10.3, 5.0, 6.0])
    edges = np.array([(0, 1), (1, 2), (0, 2), (3, 4)], dtype=np.int32)
    seg = mw.visible_segments(xs, ys, edges, 100, 100)
    # The three short edges land on the same pixel; the off-screen one goes.
    assert len(seg) == 1


def test_a_long_edge_reaching_far_off_screen_is_kept():
    xs = np.array([50.0, 1e6])
    ys = np.array([50.0, 50.0])
    seg = mw.visible_segments(xs, ys, np.array([(0, 1)], dtype=np.int32), 100, 100)
    assert len(seg) == 1


def _pixels(image):
    ptr = image.constBits()
    ptr.setsize(image.byteCount())
    return np.frombuffer(ptr, dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()].copy()


def _pixels_bg(pixels):
    return pixels[0, 0]


@pytest.mark.qt
def test_the_cached_image_draws_what_the_lines_draw():
    from PyQt5.QtGui import QColor, QImage, QPainter, QPen
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841

    mw.clear_image_cache()
    positions, edges = mw.build_edges(_cube() * 40.0, Flat())
    model = object()
    colour = (0, 255, 255, 100)
    rot, scale, axes, zoom = (0.0, 30.0, 0.0), 1.0, (0, 2), 1.5
    origin = (120, 90)

    direct = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
    direct.fill(QColor(20, 20, 20))
    xs, ys = mw.project(positions, (0, 0, 0), scale, rot, axes, origin, (0, 0), zoom, False)
    painter = QPainter(direct)
    painter.setPen(QPen(QColor(*colour), 1))
    painter.drawLines(mw.segments_polygon(mw.visible_segments(xs, ys, edges, 240, 180)))
    painter.end()

    image, left, top = mw.wireframe_image(model, positions, edges, scale, rot, axes,
                                          zoom, False, colour)
    cached = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
    cached.fill(QColor(20, 20, 20))
    painter = QPainter(cached)
    painter.drawImage(origin[0] + left, origin[1] + top, image)
    painter.end()

    a, b = _pixels(direct), _pixels(cached)
    drawn = (a != _pixels_bg(a)).any(axis=2)
    assert drawn.sum() > 100, "nothing was drawn"
    # The same pixels lit, to within one level of 8-bit blending rounding
    # (one blend onto the image, then the image onto the view).
    assert np.array_equal(drawn, (b != _pixels_bg(b)).any(axis=2))
    assert np.abs(a.astype(int) - b.astype(int)).max() <= 2

    # The same picture is handed back for a second instance: no re-raster.
    assert mw.wireframe_image(model, positions, edges, scale, rot, axes,
                              zoom, False, colour)[0] is image
    mw.clear_image_cache()


@pytest.mark.qt
def test_a_model_too_big_on_screen_is_not_cached():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    positions, edges = mw.build_edges(_cube() * 10000.0, Flat())
    assert mw.wireframe_image(object(), positions, edges, 1.0, (0, 0, 0), (0, 2),
                              1.0, False, (0, 255, 255, 100)) is None
