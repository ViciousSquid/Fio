"""Real shadowing tests: authored geometry -> dense tables -> GL -> screen readback."""

import random

import numpy as np
import pytest

from engine.constants import RENDER_MODE_LIT
from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing

pytestmark = [pytest.mark.gl, pytest.mark.slow]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(192, 192) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    made = glh.make_renderer()
    try:
        yield made
    finally:
        made.cleanup()


def _project(brushes, things, epoch, shadows=True):
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable

    table = RenderTable()
    table.begin_frame(brushes, epoch=epoch)
    render_refs = np.empty(len(brushes), dtype=object)
    if brushes:
        render_refs[:] = brushes

    etable = EntityTable()
    hidden = etable.begin_frame(things, epoch=epoch)
    entity_refs = np.empty(len(things), dtype=object)
    if things:
        entity_refs[:] = things

    return {
        "render_table": table,
        "render_refs": render_refs,
        "all_brush_slots": np.arange(table.count, dtype=np.int32),
        "entity_table": etable,
        "entity_refs": entity_refs,
        "visible_thing_slots": np.arange(etable.count, dtype=np.int32),
        "thing_hidden": hidden,
        "render_mode": RENDER_MODE_LIT,
        "brush_display_mode": "Textured",
        "play_mode": True,
        "camera_distance_cull": False,
        "shadows_enabled": bool(shadows),
        "grid_visible": False,
    }


def _draw(renderer, context, brushes, things, epoch, eye, target, shadows=True):
    import OpenGL.GL as gl

    projection, view, eye_vec = glh.camera_matrices(
        aspect=1.0, eye=eye, target=target
    )
    config = _project(brushes, things, epoch, shadows=shadows)

    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(
        gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT
    )

    renderer.render_scene(
        projection,
        view,
        eye_vec,
        None,
        config,
        brush_slots=config["all_brush_slots"],
    )
    gl.glFinish()

    errors = glh.drain_gl_errors()
    assert not errors, "real shadow render raised GL errors: %s" % errors

    # Read the actual framebuffer. Internal render counters cannot satisfy this:
    # the authored objects must visibly reach the screen.
    image = context.read_pixels()
    assert not glh.is_blank(image), "the real geometry did not appear on screen"
    return image


def _moving_light_scene():
    from editor.things import Light

    brushes = [
        box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
        box_brush("pillar_a", (-180, 80, 0), (96, 192, 96)),
        box_brush("pillar_b", (0, 128, 80), (128, 288, 128)),
        box_brush("pillar_c", (180, 64, -40), (112, 160, 112)),
    ]
    light = make_thing(
        Light,
        "moving_shadow_light",
        (-300, 360, 220),
        color=[255, 255, 255],
        intensity=2.5,
        radius=1400.0,
        state="on",
        casts_shadows=True,
    )
    return brushes, light


def test_real_light_moves_brush_shadows_on_the_screen(renderer, context):
    """A real shadow-casting light moves real brush shadows on the framebuffer."""
    brushes, light = _moving_light_scene()
    eye = (0.0, 330.0, 620.0)
    target = (0.0, 80.0, 0.0)

    shadow_on = _draw(renderer, context, brushes, [light], 1, eye, target)
    shadow_off = _draw(
        renderer, context, brushes, [light], 2, eye, target, shadows=False
    )

    shadow_diff = np.abs(shadow_on.astype(np.int16) - shadow_off.astype(np.int16))
    assert float(shadow_diff.mean()) > 0.5, (
        "enabling the real shadow pass did not change the screen image"
    )

    # Move the same live Light through its tracked production position property.
    light.pos = (300.0, 360.0, 220.0)
    moved = _draw(renderer, context, brushes, [light], 3, eye, target)

    diff = np.abs(shadow_on.astype(np.int16) - moved.astype(np.int16))
    assert float(diff.mean()) > 1.0, (
        "moving the real shadow light did not change the screen image"
    )
    assert float((diff > 8).mean()) > 0.01, (
        "moving the real shadow light produced no substantial shadow/lighting movement"
    )


def test_generated_brush_field_casts_a_moved_light_shadow(renderer, context):
    """Benchmark-style generated geometry visibly reacts to a moved shadow light."""
    from editor.editor_state import EditorState
    from editor.procedural_generator import create_map_data
    from editor.things import Light

    # Match the benchmark's deterministic generator workload shape.
    params = {
        "world_width": 2048,
        "world_height": 2048,
        "min_room": 256,
        "max_room": 384,
        "room_count": 8,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "enable_floors": False,
        "floor_height": 128,
        "floor_room_count": 0,
        "spawn_monsters": False,
        "monster_count": 0,
        "spawn_health": False,
    }
    random.seed(0xF10)
    data = create_map_data(params)

    # Materialise through the real editor loader, exactly as the benchmark does.
    state = EditorState()
    state.load_from_data(data)

    # Add deterministic foreground occluders so the generated scene contains
    # an unambiguous set of brush casters in the camera's view.
    state.brushes.extend([
        box_brush("shadow_block_a", (-160, 96, 0), (96, 192, 96)),
        box_brush("shadow_block_b", (0, 144, 80), (128, 288, 128)),
        box_brush("shadow_block_c", (160, 80, -40), (112, 160, 112)),
    ])
    light = make_thing(
        Light,
        "generated_shadow_light",
        (-320, 420, 260),
        color=[255, 255, 255],
        intensity=2.0,
        radius=1800.0,
        state="on",
        casts_shadows=True,
    )
    state.things.append(light)

    eye = (0.0, 360.0, 700.0)
    target = (0.0, 80.0, 0.0)

    first = _draw(renderer, context, state.brushes, state.things, 1, eye, target)
    baseline = _draw(
        renderer, context, state.brushes, state.things, 2, eye, target,
        shadows=False,
    )
    baseline_diff = np.abs(first.astype(np.int16) - baseline.astype(np.int16))
    assert float(baseline_diff.mean()) > 0.25, (
        "generated brush field shows no visible effect from the real shadow pass"
    )

    light.pos = (320.0, 420.0, 260.0)
    second = _draw(renderer, context, state.brushes, state.things, 3, eye, target)

    diff = np.abs(first.astype(np.int16) - second.astype(np.int16))
    assert float(diff.mean()) > 0.5, (
        "generated brush field did not change after moving the real shadow light"
    )
    assert float((diff > 8).mean()) > 0.005, (
        "generated brush field shows no substantial shadow/lighting movement"
    )
