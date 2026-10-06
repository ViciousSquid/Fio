"""Visual tier: the real renderer, on a real OpenGL context.

Everything here is marked ``gl`` and skips with a readable reason where no
context can be created, so a machine with no GPU still runs the rest of the
suite.  Under CI they run against Mesa's software rasteriser via ``xvfb-run``
(see ``tests/README.md``).

These are integration smoke tests, not graphics conformance tests.  What they
assert is that the pipeline *runs*: shaders compile, geometry is submitted,
shadow maps are generated, the frame comes back non-blank and changes when the
scene changes, no GL errors are raised, and the renderer's GL objects are
released on cleanup.  Nothing depends on a specific GPU's pixel values — the
image checks compare regions of one render against regions of another.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192


@pytest.fixture
def context():
    """A current core-profile context with an offscreen colour+depth target."""
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    """A real ``Renderer_F`` on that context, cleaned up afterwards."""
    made = glh.make_renderer()
    yield made
    try:
        made.cleanup()
    except Exception:
        pass


def _render(renderer, context, brushes, things, **config_overrides):
    """Draw one frame of a scene and return the image."""
    import OpenGL.GL as gl

    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               **config_overrides)
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=config["all_brush_slots"])
    gl.glFinish()
    return context.read_pixels()


# ---------------------------------------------------------------------------
# Visual test 1 — a dynamic light and its shadow
# ---------------------------------------------------------------------------

def test_the_context_reports_a_usable_gl_version(context):
    info = context.info()
    assert info["version"] != "?", "the context reported no GL version"
    # Recorded in the failure message of everything below it.
    assert context.width == SIZE and context.height == SIZE


def test_the_renderer_initialises_its_shaders(renderer):
    assert renderer.shaders, "no shaders were compiled"
    for required in ("lit", "textured"):
        assert any(required in name for name in renderer.shaders), (
            "no %r shader among %s" % (required, sorted(renderer.shaders)))


def test_shadow_resources_are_allocated_when_shadows_are_enabled(renderer):
    if "depth_cube" not in renderer.shaders:
        pytest.skip("this build has no depth_cube shader, so no shadow pass")
    assert renderer._shadow_fbo is not None, (
        "the shadow framebuffer was not created; shadow rendering is off")
    assert len(renderer._shadow_cubemaps) == renderer.MAX_SHADOW_LIGHTS, (
        "expected %d shadow cube-maps, got %d"
        % (renderer.MAX_SHADOW_LIGHTS, len(renderer._shadow_cubemaps)))


def test_a_lit_cube_scene_renders_without_gl_errors(renderer, context):
    brushes, things = glh.lit_cube_scene()
    with glh.no_gl_errors("rendering the lit cube scene"):
        _render(renderer, context, brushes, things)


def test_the_rendered_frame_is_not_blank(renderer, context):
    brushes, things = glh.lit_cube_scene()
    image = _render(renderer, context, brushes, things)
    assert not glh.is_blank(image), (
        "every pixel of the %dx%d frame is the same colour (%s); nothing was "
        "drawn" % (SIZE, SIZE, image[0, 0].tolist()))


def test_the_geometry_occupies_the_middle_of_the_image(renderer, context):
    """The camera is aimed at the cube; the centre must not be background."""
    brushes, things = glh.lit_cube_scene()
    image = _render(renderer, context, brushes, things)

    centre = glh.region(image, 0.35, 0.35, 0.65, 0.65)
    corner = glh.region(image, 0.0, 0.0, 0.12, 0.12)     # sky above the floor
    assert glh.luminance(centre).mean() > glh.luminance(corner).mean(), (
        "the centre of the frame (mean luminance %.1f) is no brighter than the "
        "empty top-left corner (%.1f); the cube and floor did not draw"
        % (glh.luminance(centre).mean(), glh.luminance(corner).mean()))


def test_the_render_stats_report_the_submitted_geometry(renderer, context):
    brushes, things = glh.lit_cube_scene()
    _render(renderer, context, brushes, things)
    assert renderer.render_stats.total_brushes == len(brushes), (
        "the renderer counted %d brushes, %d were submitted"
        % (renderer.render_stats.total_brushes, len(brushes)))


def test_a_lit_scene_differs_from_an_unlit_one(renderer, context):
    """Dynamic lighting is actually being applied, not just geometry drawn."""
    brushes, things = glh.lit_cube_scene()
    lit = _render(renderer, context, brushes, things)
    unlit = _render(renderer, context, brushes, [])       # same scene, no light

    difference = np.abs(glh.luminance(lit) - glh.luminance(unlit))
    assert difference.mean() > 1.0, (
        "removing the only light changed the mean luminance by %.3f; the "
        "lighting path is not affecting the image" % difference.mean())


def _shadow_brush_projection(brushes):
    from engine.render_table import RenderTable
    table = RenderTable()
    table.sync(brushes, 1)
    slots = np.arange(table.count, dtype=np.int32)
    return table, slots


def test_the_shadow_pass_collects_the_cube_and_the_floor(renderer):
    """Shadow-caster collection operates on dense RenderTable slots."""
    brushes, things = glh.lit_cube_scene()
    table, slots = _shadow_brush_projection(brushes)
    light = things[0]
    caster_slots, signature = renderer._casters_in_reach(
        table, renderer._shadow_caster_slots(table, slots),
        light.pos[0], light.pos[1], light.pos[2], light.get_radius())

    names = {brushes[int(slot)]["name"] for slot in caster_slots}
    assert names == {"floor", "cube"}, (
        "the shadow pass collected %s; both the floor and the cube are within "
        "the light's %.0f-unit reach" % (sorted(names), light.get_radius()))
    assert isinstance(signature, tuple) and signature[0], (
        "the caster signature is empty, so the cube-map cache could never "
        "detect a change")


def test_a_caster_outside_the_lights_reach_is_not_collected(renderer):
    from tests.helpers.worlds import box_brush
    brushes, things = glh.lit_cube_scene()
    brushes.append(box_brush("distant", (50000, 0, 0), (64, 64, 64)))
    table, slots = _shadow_brush_projection(brushes)
    light = things[0]

    caster_slots, _sig = renderer._casters_in_reach(
        table, renderer._shadow_caster_slots(table, slots),
        light.pos[0], light.pos[1], light.pos[2], light.get_radius())

    assert "distant" not in {brushes[int(slot)]["name"] for slot in caster_slots}, (
        "a brush 50000 units away was collected for a light with a %.0f-unit "
        "reach" % light.get_radius())


def test_the_caster_signature_changes_when_a_caster_moves(renderer):
    """What stops a static scene re-rendering its cube-maps every frame."""
    brushes, things = glh.lit_cube_scene()
    light = things[0]
    table, slots = _shadow_brush_projection(brushes)
    active = renderer._shadow_caster_slots(table, slots)
    first_slots, first = renderer._casters_in_reach(
        table, active, light.pos[0], light.pos[1], light.pos[2],
        light.get_radius())
    again_slots, again = renderer._casters_in_reach(
        table, active, light.pos[0], light.pos[1], light.pos[2],
        light.get_radius())
    assert np.array_equal(again_slots, first_slots)
    assert again == first, (
        "the caster signature changed with nothing moving; every frame would "
        "re-render every shadow cube-map")

    brushes[1]["pos"] = [64.0, 64.0, 0.0]
    table.sync(brushes, 2)
    active = renderer._shadow_caster_slots(
        table, np.arange(table.count, dtype=np.int32))
    _moved_slots, moved = renderer._casters_in_reach(
        table, active, light.pos[0], light.pos[1], light.pos[2],
        light.get_radius())
    assert moved != first, (
        "moving the cube did not change the caster signature; its shadow would "
        "stay where the cube used to be")


def test_a_scene_with_shadows_renders_without_gl_errors(renderer, context):
    brushes, things = glh.lit_cube_scene(shadows=True)
    with glh.no_gl_errors("rendering with shadows enabled"):
        _render(renderer, context, brushes, things)
    assert renderer._light_shadow_index or not renderer._shadow_cubemaps, (
        "shadows are enabled and a shadow-casting light is in the scene, but "
        "no light was assigned a cube-map slot")


def test_a_shadowed_region_differs_from_a_lit_one(renderer, context):
    """The cube must darken the floor somewhere behind it.

    Compared against the same scene with the cube removed rather than against
    an absolute colour, so the check does not depend on the GPU's shading.
    """
    brushes, things = glh.lit_cube_scene(shadows=True)
    with_cube = _render(renderer, context, brushes, things)
    floor_only = _render(renderer, context, [brushes[0]], things)

    difference = np.abs(glh.luminance(with_cube) - glh.luminance(floor_only))
    assert difference.max() > 8.0, (
        "removing the cube changed the image by at most %.1f luminance; "
        "neither the cube nor its shadow is visible" % difference.max())


def test_shadow_samplers_are_assigned_once_and_keep_their_units(
        renderer, context, monkeypatch):
    """A program's shadowMaps[i] samplers read units base+i on every frame.

    Sampler units are program state, so they are assigned on a program's
    first lit pass only; later passes bind the cube-maps and nothing else.
    Checked against GL's own record of each program's uniforms, and against
    the image, which must not change once the assignment stops being re-sent.
    """
    import ctypes
    import OpenGL.GL as gl

    brushes, things = glh.lit_cube_scene(shadows=True)
    first = _render(renderer, context, brushes, things)
    if not renderer._shadow_cubemaps:
        pytest.skip("shadows are unavailable on this driver")

    base = renderer.SHADOW_TEXTURE_UNIT_BASE
    count = renderer.MAX_SHADOW_LIGHTS

    def shadow_locations(program):
        locations = {}
        for i in range(count):
            location = gl.glGetUniformLocation(program, "shadowMaps[%d]" % i)
            if location != -1:
                locations[location] = i
        return locations

    resent = []
    real_uniform1i = gl.glUniform1i

    def recording_uniform1i(location, value):
        program = int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        if program and location in shadow_locations(program):
            resent.append((program, location, value))
        return real_uniform1i(location, value)

    monkeypatch.setattr(gl, "glUniform1i", recording_uniform1i)
    again = _render(renderer, context, brushes, things)
    monkeypatch.undo()

    assert not resent, (
        "shadow sampler units were re-sent on a later frame: %s" % resent)
    checked = 0
    for name, uniforms in renderer.uniforms.items():
        if uniforms.shadow_slots is None:
            continue
        locations = shadow_locations(uniforms.program)
        assert sorted(locations.values()) == list(uniforms.shadow_slots), name
        for location, index in locations.items():
            unit = (ctypes.c_int * 1)()
            gl.glGetUniformiv(uniforms.program, location, unit)
            assert unit[0] == base + index, (
                "%s: shadowMaps[%d] reads unit %d, not %d"
                % (name, index, unit[0], base + index))
            checked += 1
    assert checked, "no lit pass assigned any shadow sampler"
    assert np.array_equal(first, again), (
        "the second frame of an unchanged scene differs from the first")


# ---------------------------------------------------------------------------
# Visual test 2 — a moving dynamic light
# ---------------------------------------------------------------------------

def test_moving_the_light_changes_the_rendered_frame(renderer, context):
    """The regression this guards: a dynamic light behaving as a static one.

    If the light's position never reached the renderer, or the shadow cube-map
    cache never invalidated, every frame would come out identical however far
    the light moved.
    """
    brushes, things = glh.lit_cube_scene(shadows=True)
    light = things[0]

    frames = []
    for position in glh.LIGHT_POSITIONS:
        light.pos = list(position)
        frames.append(_render(renderer, context, brushes, things))

    for index in range(1, len(frames)):
        difference = np.abs(glh.luminance(frames[index])
                            - glh.luminance(frames[0]))
        assert difference.mean() > 0.5, (
            "moving the light from %s to %s changed the mean luminance by only "
            "%.4f; the light is behaving as a static one"
            % (glh.LIGHT_POSITIONS[0], glh.LIGHT_POSITIONS[index],
               difference.mean()))


def test_the_shadow_cache_notices_the_light_moving(renderer, context):
    """A cube-map keyed only on its casters would never refresh for a mover."""
    brushes, things = glh.lit_cube_scene(shadows=True)
    light = things[0]

    _render(renderer, context, brushes, things)
    if not renderer._shadow_cubemaps:
        pytest.skip("shadows are unavailable on this driver")
    first_signature = list(renderer._shadow_slot_sig)

    light.pos = list(glh.LIGHT_POSITIONS[1])
    _render(renderer, context, brushes, things)

    assert list(renderer._shadow_slot_sig) != first_signature, (
        "the shadow slot signature is unchanged after the light moved, so the "
        "stale cube-map would be reused: %s" % (first_signature,))


def test_returning_the_light_reproduces_the_earlier_frame(renderer, context):
    """Determinism: the same scene renders the same way twice."""
    brushes, things = glh.lit_cube_scene(shadows=True)
    light = things[0]

    light.pos = list(glh.LIGHT_POSITIONS[0])
    first = _render(renderer, context, brushes, things)
    light.pos = list(glh.LIGHT_POSITIONS[1])
    _render(renderer, context, brushes, things)
    light.pos = list(glh.LIGHT_POSITIONS[0])
    again = _render(renderer, context, brushes, things)

    difference = np.abs(glh.luminance(first) - glh.luminance(again))
    assert difference.mean() < 2.0, (
        "returning the light to its first position produced a frame differing "
        "by %.3f mean luminance; something is accumulating between frames"
        % difference.mean())


# ---------------------------------------------------------------------------
# Resource lifetime
# ---------------------------------------------------------------------------

def test_cleanup_releases_the_renderers_gl_objects(context):
    import OpenGL.GL as gl

    made = glh.make_renderer()
    brushes, things = glh.lit_cube_scene()
    _render(made, context, brushes, things)

    shadow_maps = list(made._shadow_cubemaps)
    with glh.no_gl_errors("renderer cleanup"):
        made.cleanup()

    for texture in shadow_maps:
        assert gl.glIsTexture(texture) == gl.GL_FALSE, (
            "shadow cube-map %d is still a live GL texture after cleanup()"
            % texture)


def test_a_geometry_mesh_is_released_when_its_brush_stops_being_drawn(
        renderer, context):
    """The renderer caches a VBO per angled brush; it must not leak them."""
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush

    brushes, things = glh.lit_cube_scene()
    ramp = box_brush("ramp", (200, 32, 0), (128, 64, 128))
    bg.clip_brush(ramp, (0.0, 1.0, 1.0), 20.0)
    brushes.append(ramp)

    _render(renderer, context, brushes, things)
    cached = dict(renderer._geo_mesh_cache)
    assert len(cached) >= 1, (
        "no GPU mesh was built for the angled brush; the test is not "
        "exercising the cache it is about")
    mesh = next(iter(cached.values()))

    # The eviction sweep runs on the renderer's own frame clock (every 240
    # frames, for meshes untouched for 240).  Advancing that clock directly is
    # the same code path the per-frame call takes, without 500 real renders.
    brushes.remove(ramp)
    for _ in range(600):
        renderer._begin_geo_frame()

    assert renderer._geo_mesh_cache == {}, (
        "the angled brush has not been drawn for 600 frames but the renderer "
        "still holds %d cached meshes; GPU memory would grow with every brush "
        "the camera ever saw" % len(renderer._geo_mesh_cache))
    import OpenGL.GL as gl
    assert gl.glIsBuffer(mesh.vbo) == gl.GL_FALSE, (
        "the evicted mesh's vertex buffer %d is still a live GL object"
        % mesh.vbo)


def test_rendering_an_empty_world_is_harmless(renderer, context):
    with glh.no_gl_errors("rendering an empty world"):
        _render(renderer, context, [], [])


def test_resizing_the_target_between_frames_is_harmless(renderer, context):
    import OpenGL.GL as gl

    brushes, things = glh.lit_cube_scene()
    _render(renderer, context, brushes, things)
    gl.glViewport(0, 0, SIZE // 2, SIZE // 2)
    with glh.no_gl_errors("rendering after a viewport change"):
        config = glh.render_config(all_brushes=brushes, all_things=things)
        renderer.render_scene(*glh.camera_matrices(aspect=1.0), None, config,
                              brush_slots=config["all_brush_slots"])
    gl.glViewport(0, 0, SIZE, SIZE)


def test_a_structural_edit_does_not_rebuild_unchanged_geometry_meshes(
        renderer, context):
    """Both render buffers' tables, across a reconcile, share one mesh per
    unchanged angled brush. The cache was keyed by (table generation,
    geometry id), and every reconcile moves the generation: adding one box
    rebuilt every convex mesh on the map, once per buffer."""
    import numpy as np
    from engine import brush_geometry as bg
    from engine.render_table import RenderTable
    from tests.helpers.worlds import box_brush

    ramps = []
    for i in range(6):
        ramp = box_brush("ramp%d" % i, (200 * i, 32, 0), (128, 64, 128))
        bg.clip_brush(ramp, (0.0, 1.0, 1.0), 20.0)
        ramps.append(ramp)

    built = []
    original = renderer._build_geo_mesh

    def counting(record, convex, key):
        built.append(key)
        return original(record, convex, key)

    renderer._build_geo_mesh = counting
    context.bind()
    front, back = RenderTable(), RenderTable()
    front.sync(ramps, 1)
    back.sync(ramps, 1)
    for table in (front, back):
        renderer._prepare_geo_meshes(table, np.arange(table.count, dtype=np.int32))
    assert len(built) == len(ramps), (
        "the two buffers' tables built %d meshes for %d identical angled "
        "brushes; each brush's mesh must be built once" % (len(built), len(ramps)))

    # A structural edit: one new box. Both tables reconcile.
    scene = ramps + [box_brush("new", (0, 500, 0))]
    for table in (front, back):
        generation = table.generation
        table.sync(scene, 2, dirty_objects={id(scene[-1])})
        assert table.generation != generation
        renderer._prepare_geo_meshes(table, np.arange(table.count, dtype=np.int32))
    assert len(built) == len(ramps), (
        "adding one box rebuilt %d unchanged convex meshes"
        % (len(built) - len(ramps)))

    # A real shape change still builds a new mesh for that brush alone.
    bg.clip_brush(ramps[0], (1.0, 0.0, 0.0), 10.0)
    front.sync(scene, 3, dirty_objects={id(ramps[0])})
    renderer._prepare_geo_meshes(front, np.arange(front.count, dtype=np.int32))
    assert len(built) == len(ramps) + 1



@pytest.mark.integration
def test_editor_state_publication_is_submitted_to_the_real_renderer(context, renderer):
    """EditorState -> LogicRender -> published tables -> real OpenGL renderer.

    The world is authored through the real EditorState and projected by the
    real LogicThread.  The renderer receives the resulting published tables
    and slot arrays, not a freshly rebuilt test projection.
    """
    import OpenGL.GL as gl

    from editor.editor_state import EditorState
    from editor.things import Light
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState
    from tests.helpers.worlds import box_brush, make_thing

    state = EditorState()
    state.brushes = [
        box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
        box_brush("cube", (0, 64, 0), (128, 128, 128)),
    ]
    state.things = [
        make_thing(Light, "publication-light", (0, 260, 160),
                   intensity=2.0, radius=1400.0, state="on",
                   casts_shadows=True)
    ]

    logic = LogicThread(ThreadedGameState(), state)
    try:
        logic.render_runtime.culling_enabled = False
        logic.render_runtime.prepare_render_state()
        assert logic._publish_frame() is True

        published = logic.game_state.get_render_state()
        try:
            assert published.render_table.count == 2
            assert published.entity_table.count == 1

            config = glh.render_config(
                render_table=published.render_table,
                render_refs=published.render_refs,
                all_brush_slots=published.all_brush_slots,
                visible_brush_slots=published.visible_brush_slots,
                entity_table=published.entity_table,
                entity_refs=published.entity_refs,
                visible_thing_slots=published.visible_thing_slots,
                thing_hidden=published.thing_hidden,
            )

            projection, view, eye = glh.camera_matrices(aspect=1.0)
            context.bind()
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            with glh.no_gl_errors("published EditorState render"):
                renderer.render_scene(
                    projection, view, eye, None, config,
                    brush_slots=published.all_brush_slots)
                gl.glFinish()

            image = context.read_pixels()
            assert not glh.is_blank(image), (
                "the real renderer received the published tables but submitted "
                "no visible geometry")
            assert renderer.render_stats.total_brushes == 2
        finally:
            logic.game_state.release_render_state(published)
    finally:
        logic.stop()
