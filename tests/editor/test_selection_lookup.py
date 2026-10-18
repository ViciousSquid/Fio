"""Linear selection indexing preserves list equality and identity semantics."""

from types import SimpleNamespace

import pytest

pytest.importorskip('PyQt5', reason='EditorState uses Qt-backed entities')

from editor.editor_state import EditorState
from editor.selection import SelectionLookup

pytestmark = pytest.mark.qt


def test_deduplication_uses_identity_and_preserves_order():
    first = {'id': 'same', 'pos': [1, 2, 3]}
    equal_copy = dict(first)
    thing = SimpleNamespace(properties={'id': 'thing'})
    state = EditorState()
    state.selected_objects = [first, thing, first, equal_copy, None, thing, None]
    got = state._selection_list()
    assert [id(obj) for obj in got] == [id(first), id(thing), id(equal_copy), id(None)]


def test_lookup_matches_list_membership_for_copied_and_legacy_brushes():
    brushes = [{'id': str(i), 'pos': [i, 0, 0]} for i in range(100)]
    brushes += [{'pos': [5, 6, 7]}, {'id': None, 'pos': [5, 6, 7]},
                {'id': ['legacy'], 'pos': [1, 2, 3]},
                {'id': 'duplicate', 'value': 1}, {'id': 'duplicate', 'value': 2}]
    selected = brushes[::2]
    lookup = SelectionLookup(selected)
    for brush in brushes + [dict(b) for b in brushes]:
        assert (brush in lookup) == (brush in selected)
    assert ({'id': 'missing'} in lookup) is False


def test_identity_only_entities_and_custom_equality_still_match():
    class Entity:
        pass
    class EqualEntity:
        def __eq__(self, other):
            return isinstance(other, EqualEntity)
    class BrushSubclass(dict):
        pass
    entity = Entity()
    selected = [entity, EqualEntity(), BrushSubclass(id='subclass')]
    lookup = SelectionLookup(selected)
    for query in (entity, Entity(), EqualEntity(), {'id': 'subclass'},
                  BrushSubclass(id='subclass'), None):
        assert (query in lookup) == (query in selected)


def test_index_is_rebuilt_for_each_draw_instead_of_caching_mutable_selection():
    selected = [{'id': 'old'}]
    assert {'id': 'old'} in SelectionLookup(selected)
    selected[:] = [{'id': 'new'}]
    assert {'id': 'old'} not in SelectionLookup(selected)
    assert {'id': 'new'} in SelectionLookup(selected)


def test_valid_distinct_brush_ids_do_not_compare_every_selected_dictionary():
    class CountingId(str):
        comparisons = 0
        def __eq__(self, other):
            type(self).comparisons += 1
            return super().__eq__(other)
        __hash__ = str.__hash__
    selected = [{'id': CountingId(str(i))} for i in range(2000)]
    lookup = SelectionLookup(selected)
    CountingId.comparisons = 0
    assert {'id': CountingId('absent')} not in lookup
    assert {'id': CountingId('1999')} in lookup
    assert CountingId.comparisons < 10


@pytest.mark.parametrize('view_name', ['view_top', 'view_front', 'view_side'])
def test_real_brush_drawing_matches_list_membership_pixels(main_window, monkeypatch, view_name):
    from PyQt5.QtCore import Qt, QPointF, QRectF
    from PyQt5.QtGui import QImage, QPainter
    from tests.helpers.worlds import box_brush
    view = getattr(main_window, view_name)
    view.resize(640, 480)
    brushes = [box_brush(str(i), (i * 30 - 120, 0, 0), (20, 20, 20))
               for i in range(8)]
    main_window.state.brushes = brushes
    def legacy_rect(pos, size, axis1, axis2, transform):
        minimum = QPointF(pos[axis1] - size[axis1]/2, pos[axis2] - size[axis2]/2)
        extent = QPointF(size[axis1], size[axis2])
        return QRectF(view.world_to_screen(minimum),
                      view.world_to_screen(minimum + extent)).normalized()
    def draw():
        image = QImage(640, 480, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            view.draw_brushes(painter, view.get_visible_world_bounds())
        finally:
            painter.end()
        return image
    for selected in ([], brushes[:4], [dict(brushes[0])], [brushes[0], brushes[0]]):
        main_window.state.selected_objects = selected
        actual = draw()
        with monkeypatch.context() as patch:
            patch.setattr('editor.view_2d.SelectionLookup', list)
            patch.setattr(view, '_brush_screen_rect', legacy_rect)
            expected = draw()
        assert actual == expected


@pytest.mark.parametrize('view_name', ['view_top', 'view_front', 'view_side'])
def test_cached_projection_matches_original_rectangle_coordinates(main_window, view_name):
    import numpy as np
    from PyQt5.QtCore import QPointF, QRectF
    view = getattr(main_window, view_name)
    view.resize(641, 479)
    axes = {'x': 0, 'y': 1, 'z': 2}
    a1, a2 = [axes[a] for a in view.get_axes()]
    rng = np.random.default_rng(381)
    for dtype in (np.float32, np.float64):
        for _ in range(200):
            pos = rng.uniform(-1e6, 1e6, 3).astype(dtype)
            size = rng.uniform(.001, 10000, 3).astype(dtype)
            view.pan_offset = QPointF(*rng.uniform(-10000, 10000, 2))
            view.zoom_factor = float(rng.choice([.05, .25, 1., 1.7, 20.]))
            transform = (view.width()/2, view.height()/2,
                         view.pan_offset.x(), view.pan_offset.y(), view.zoom_factor,
                         view.view_type in ('front', 'side'))
            minimum = QPointF(pos[a1] - size[a1]/2, pos[a2] - size[a2]/2)
            extent = QPointF(size[a1], size[a2])
            expected = QRectF(view.world_to_screen(minimum),
                              view.world_to_screen(minimum + extent)).normalized()
            actual = view._brush_screen_rect(pos, size, a1, a2, transform)
            assert actual.getRect() == expected.getRect()
