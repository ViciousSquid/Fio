"""Colour and alpha for the lit pass's instance payload.

The per-brush path chose these with an if/elif chain; the instanced path
writes masks over one array, so the write order is what encodes the priority.
Getting that order wrong is invisible in most scenes -- a selected trigger is
an unusual thing to have on screen -- so it is pinned here rather than left to
the pixel tests.

Alpha in particular has no other coverage: the transparent pass enables
blending without ever setting a blend function, so a trigger's 0.3 does not
currently reach the image at all (GL's default GL_ONE/GL_ZERO ignores it).
That is pre-existing behaviour, and not something to change here -- but the
value still has to match what the per-brush path set, or fixing the blend
func later would silently change what triggers look like.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="renderer tests import editor Things")

from engine import render_table as rt
from engine.render_table import RenderTable
pytestmark = pytest.mark.qt

from engine.renderer_F import (Renderer_F, _TRIGGER_COLOR, _SELECTED_COLOR,
                               _SUBTRACT_COLOR)


def _brush(bid, **kw):
    b = {'id': bid, 'pos': [0.0, 0.0, 0.0], 'size': [64.0, 64.0, 64.0],
         'textures': {}}
    b.update(kw)
    return b


def _payload(brushes, selected=None):
    table = RenderTable()
    table.sync(brushes, 1)
    slots = np.arange(table.count, dtype=np.int32)
    chosen = -1 if selected is None else table.slot_of_id[selected]
    return Renderer_F.lit_instance_payload(table, slots, chosen)


def test_a_plain_brush_uses_its_own_colour_at_full_alpha():
    payload = _payload([_brush('a', tint=[255, 0, 0])])
    np.testing.assert_allclose(payload[0, 0:3], [1.0, 0.0, 0.0], atol=1e-6)
    assert payload[0, 3] == pytest.approx(1.0)


def test_a_trigger_is_cyan_and_partly_transparent():
    payload = _payload([_brush('t', is_trigger=True, tint=[255, 0, 0])])
    np.testing.assert_allclose(payload[0, 0:3], _TRIGGER_COLOR, atol=1e-6)
    assert payload[0, 3] == pytest.approx(0.3), (
        "a trigger's alpha is what the per-brush path uploaded; it must survive "
        "the move into instance data even while the pass's blend func does not "
        "yet let it show")


def test_a_subtract_brush_is_red():
    payload = _payload([_brush('s', operation='subtract', tint=[0, 255, 0])])
    np.testing.assert_allclose(payload[0, 0:3], _SUBTRACT_COLOR, atol=1e-6)


def test_the_selected_brush_is_yellow():
    brushes = [_brush('a'), _brush('b')]
    payload = _payload(brushes, selected='b')
    np.testing.assert_allclose(payload[1, 0:3], _SELECTED_COLOR, atol=1e-6)
    assert not np.allclose(payload[0, 0:3], _SELECTED_COLOR)


def test_a_selected_trigger_still_reads_as_a_trigger():
    """The if/elif chain tested is_trigger first, so it outranks selection."""
    brushes = [_brush('t', is_trigger=True)]
    payload = _payload(brushes, selected='t')
    np.testing.assert_allclose(payload[0, 0:3], _TRIGGER_COLOR, atol=1e-6)
    assert payload[0, 3] == pytest.approx(0.3)


def test_a_selected_subtract_brush_reads_as_selected():
    """Selection outranks subtract, which the chain tested last."""
    brushes = [_brush('s', operation='subtract')]
    payload = _payload(brushes, selected='s')
    np.testing.assert_allclose(payload[0, 0:3], _SELECTED_COLOR, atol=1e-6)


def test_an_unselected_scene_touches_no_colours_it_should_not():
    brushes = [_brush('a', tint=[10, 20, 30]), _brush('b', tint=[40, 50, 60])]
    payload = _payload(brushes)
    table = RenderTable()
    table.sync(brushes, 1)
    np.testing.assert_allclose(payload[:, 0:3], table.colour[:2], atol=1e-6)
    np.testing.assert_allclose(payload[:, 3], 1.0)


def test_an_empty_slot_array_yields_an_empty_payload():
    table = RenderTable()
    table.sync([_brush('a')], 1)
    payload = Renderer_F.lit_instance_payload(
        table, np.empty(0, dtype=np.int32), -1)
    assert payload.shape == (0, 4)
