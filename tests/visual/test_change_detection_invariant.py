"""M4's invariant: recognising a still frame costs no look at a row.

Change detection reports the rows each site examines
(``engine.change_detection.DETECT``). A still frame -- the logic thread's
publication (the table brought up to date from a RowList, the kept cull)
and the renderer's frame (kept matrices, kept pass arrays, instance streams)
-- must examine none, on a world and on one four times its size, with the
two double-buffered tables taking turns as the game draws them; a frame
with k rows moved examines no more than those.
"""

import math

import glm
import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

SIZE = 96


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def _world(n):
    from engine.row_list import RowList
    from tests.helpers.worlds import box_brush
    side = int(math.ceil(math.sqrt(n)))
    return RowList(box_brush("b%d" % i, ((i % side) * 60.0, 20.0, (i // side) * 60.0),
                             (40.0, 40.0, 40.0)) for i in range(n))


def _frames(context, n, frames=4):
    """Rows examined by change detection on each of *frames* still frames,
    after two to settle, and the last frame's picture."""
    from engine import change_detection
    from engine.logic_render import LogicRender
    from engine.render_table import RenderTable
    brushes = _world(n)
    tables = [RenderTable(), RenderTable()]
    culler = LogicRender.__new__(LogicRender)
    renderer = glh.make_renderer()
    renderer.shadows_enabled = False
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0, 20000.0)
    view = glm.lookAt(glm.vec3(-200.0, 300.0, -200.0), glm.vec3(300.0, 0.0, 300.0),
                      glm.vec3(0.0, 1.0, 0.0))
    planes = culler.extract_frustum_planes(projection * view)
    examined = []
    try:
        for frame in range(frames + 2):
            table = tables[frame % 2]
            change_detection.DETECT.reset()
            table.begin_frame(brushes, 1)
            keep, shown = table.shown()
            visible = culler.cull_brushes(table, planes, keep)
            config = glh.render_config(all_brushes=list(brushes), render_table=table,
                                       all_brush_slots=shown, shadows_enabled=False,
                                       brush_display_mode="Solid")
            context.bind()
            renderer.render_scene(projection, view, glm.vec3(-200.0, 300.0, -200.0),
                                  None, config, brush_slots=visible)
            if frame >= 2:
                examined.append(change_detection.DETECT.rows)
        return examined, context.read_pixels(), renderer
    finally:
        renderer.cleanup()


def test_a_still_frame_examines_no_rows_whatever_the_world_size(context):
    small, _, _ = _frames(context, 400)
    large, _, _ = _frames(context, 1600)
    assert small == [0, 0, 0, 0]
    assert large == [0, 0, 0, 0]
