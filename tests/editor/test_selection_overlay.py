"""The editor's selection reaches the renderer only as a SelectionOverlay.

``editor.selection_overlay.describe_selection`` is the one place that knows
brush dicts, ``Thing`` and ``Effect``; the renderer draws from the descriptor
it returns. These pin that data path without a GL context: what each kind of
selection becomes, and that the renderer's lit-pass selection tint reads the
descriptor rather than an editor object.
"""

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from engine.constants import brush_aabb_bounds  # noqa: E402
from engine.effect_entity import Effect  # noqa: E402
from engine.render_table import RenderTable  # noqa: E402
from engine.renderer import EffectBillboard, SelectionOverlay  # noqa: E402
from editor.selection_overlay import describe_selection  # noqa: E402
from editor.things import Light  # noqa: E402
from tests.helpers.worlds import box_brush  # noqa: E402

pytestmark = pytest.mark.qt


def _brush(**props):
    return box_brush("selected", pos=(10, 20, 30), size=(64, 32, 16), **props)


def test_nothing_selected_draws_nothing():
    assert describe_selection(None) is None


def test_a_brush_gets_an_outline_and_a_gizmo():
    brush = _brush()
    assert describe_selection(brush) == SelectionOverlay(
        brush_id=brush['id'],
        outline=((10.0, 20.0, 30.0), (64.0, 32.0, 16.0)),
        gizmo_pos=(10.0, 20.0, 30.0),
    )


def test_a_locked_brush_keeps_its_outline_but_has_no_gizmo():
    brush = _brush(lock=True)
    overlay = describe_selection(brush)
    assert overlay.brush_id == brush['id']
    assert overlay.outline == ((10.0, 20.0, 30.0), (64.0, 32.0, 16.0))
    assert overlay.gizmo_pos is None


def test_a_trigger_with_bounds_shown_gets_its_dashed_aabb():
    brush = _brush(is_trigger=True, show_aabb_bounds=True)
    overlay = describe_selection(brush)
    assert overlay.dashed_bounds == tuple(brush_aabb_bounds(brush))
    assert overlay.dashed_bounds == (-22.0, 4.0, 22.0, 42.0, 36.0, 38.0)
    assert overlay.outline is not None and overlay.gizmo_pos is not None


def test_a_trigger_without_bounds_shown_has_no_dashed_aabb():
    assert describe_selection(_brush(is_trigger=True)).dashed_bounds is None


def test_an_entity_gets_only_a_gizmo():
    overlay = describe_selection(Light(pos=[1.0, 2.0, 3.0]))
    assert overlay == SelectionOverlay(gizmo_pos=(1.0, 2.0, 3.0))


def test_an_effect_with_preview_enabled_gets_its_billboard_box():
    effect = Effect(pos=[4.0, 5.0, 6.0],
                    properties={'preview': True, 'width': 40.0, 'height': 50.0})
    overlay = describe_selection(effect)
    assert overlay.effect_billboard == EffectBillboard(
        pos=(4.0, 5.0, 6.0), width=40.0, height=50.0, explosion=False)
    assert overlay.gizmo_pos == (4.0, 5.0, 6.0)
    assert overlay.brush_id is None and overlay.outline is None


def test_an_effect_with_preview_disabled_gets_only_a_gizmo():
    effect = Effect(pos=[4.0, 5.0, 6.0], properties={'preview': False})
    assert describe_selection(effect) == SelectionOverlay(gizmo_pos=(4.0, 5.0, 6.0))


def test_an_explosion_preview_is_marked_as_one():
    effect = Effect(pos=[0.0, 0.0, 0.0],
                    properties={'preview': True, 'effect_type': 'explosion',
                                'width': 64.0, 'height': 64.0})
    billboard = describe_selection(effect).effect_billboard
    assert billboard.explosion is True
    assert (billboard.width, billboard.height) == (64.0, 64.0)


def test_the_lit_pass_selection_tint_reads_the_descriptor():
    from engine.renderer.core import RendererCore

    brushes = [box_brush("a"), box_brush("b", pos=(100, 0, 0))]
    table = RenderTable()
    table.sync(brushes, 1)
    selected = describe_selection(brushes[1])

    def slot(selection):
        return RendererCore._selected_slot(table, {'primary_selection': selection})

    assert slot(selected) == int(table.slot_of_id[brushes[1]['id']])
    assert slot(describe_selection(Light(pos=[0.0, 0.0, 0.0]))) == -1
    assert slot(None) == -1
