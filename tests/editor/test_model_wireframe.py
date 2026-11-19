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


# ---------------------------------------------------------------------------
# Release hardening: bounded caches, and values that cannot be drawn
# ---------------------------------------------------------------------------

class _Model:
    """A loaded model as model_edges reads it."""
    cpu_triangles = None

    def __init__(self, vertices):
        self.cpu_vertices = vertices


def test_the_edge_cache_drops_the_least_recently_used_model_not_all(monkeypatch):
    monkeypatch.setattr(mw, '_EDGE_CACHE', {})
    monkeypatch.setattr(mw, '_EDGE_CACHE_LIMIT', 4)
    models = [_Model(_cube()) for _ in range(5)]
    first = [mw.model_edges(m) for m in models[:4]]
    mw.model_edges(models[0])                 # used again: now the newest
    mw.model_edges(models[4])                 # evicts models[1] only
    assert len(mw._EDGE_CACHE) == 4
    assert mw.model_edges(models[0])[0] is first[0][0], "rebuilt a cached model"
    assert id(models[1]) not in mw._EDGE_CACHE
    assert mw.model_edges(models[2])[0] is first[2][0]


def test_a_model_with_a_non_finite_corner_has_no_wireframe(monkeypatch):
    monkeypatch.setattr(mw, '_EDGE_CACHE', {})
    verts = _cube()
    verts[5, 1] = np.nan
    model = _Model(verts)
    assert mw.model_edges(model) is None
    assert mw.model_edges(model) is None      # remembered, not an error


@pytest.mark.parametrize('scale, rotation', [
    (float('inf'), (0, 0, 0)), (1.0, (0, float('nan'), 0)), ('2', (0, 0, 0)),
    ([1.0, 2.0], (0, 0, 0)), (1.0, (0, 0)), (None, (0, 0, 0)), (1.0, 'abc'),
    (['a', 1, 1], (0, 0, 0)),
])
def test_a_placement_that_cannot_be_drawn_is_refused(scale, rotation):
    assert mw.placement(scale, rotation) is None


def test_a_placement_is_three_floats_each():
    assert mw.placement(2, [0, 90, 0]) == ((2.0, 2.0, 2.0), (0.0, 90.0, 0.0))
    assert mw.placement([1, 2, 3, 4], (1, 2, 3)) == ((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))


@pytest.mark.qt
def test_the_image_cache_holds_a_bounded_number_of_entries(monkeypatch):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    mw.clear_image_cache()
    monkeypatch.setattr(mw, 'IMAGE_CACHE_ENTRIES', 16)
    positions, edges = mw.build_edges(_cube(), Flat())
    model = object()
    # Tiny images, each a different yaw: far inside the pixel budget.
    for yaw in range(100):
        mw.wireframe_image(model, positions, edges, 1.0, (0, yaw, 0), (0, 2),
                           1.0, False, (0, 255, 255, 100))
    assert len(mw._IMAGE_CACHE) == 16
    assert mw._image_cache_pixels == sum(
        e[1].width() * e[1].height() + mw.IMAGE_ENTRY_COST
        for e in mw._IMAGE_CACHE.values())
    mw.clear_image_cache()


@pytest.mark.qt
@pytest.mark.parametrize('prop, value', [
    ('scale', float('inf')), ('scale', [1.0, 'x', 1.0]), ('rotation', [0, float('nan'), 0]),
    ('rotation', 'sideways'),
])
def test_a_model_that_cannot_be_placed_is_drawn_as_a_box(main_window, monkeypatch,
                                                         prop, value):
    """A typed 'inf' or a malformed map value used to raise in paintEvent on
    every repaint of every 2D view."""
    from PyQt5.QtGui import QImage, QPainter
    from editor.things import Prop
    positions, edges = mw.build_edges(_cube() * 32.0, Flat())
    view = main_window.view_top
    monkeypatch.setattr(view, '_model_edges_for',
                        lambda thing: (object(), positions, edges))
    thing = Prop.for_model('assets/models/chair.obj', pos=[0, 0, 0])
    thing.properties[prop] = value
    image = QImage(200, 200, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        rect = view._draw_model_wireframe(painter, thing, ax_map, 'x', 'z')
    finally:
        painter.end()
    assert rect.width() == 20 and rect.height() == 20
    assert view._compute_model_screen_coords(thing, ax_map, 'x', 'z') is None
