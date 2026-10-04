"""Visual tier: the GPU heightfield terrain reproduces the old CPU mesh exactly.

The terrain used to be drawn from a CPU-built vertex buffer: 6 vertices per
quad, 14 floats each (position, flat normal, colour, UV, smooth normal). It is
now drawn from the chunk's height grid alone -- one texture layer per chunk,
no vertex buffer -- with every one of those attributes rebuilt in the vertex
shader. The triangle topology is unchanged (same vertex order, same quad
diagonal), so any difference is a reconstruction error, and that is what these
tests look for, at two levels:

* **Data.** Transform feedback captures exactly what the new vertex shader
  emits, vertex by vertex, and it is compared against the frozen CPU builder in
  ``tests/helpers/terrain_reference.py`` -- across biomes (including hard
  colour stops), every LOD resolution, flat mode, heightmap and mesh-scale
  terrains, far-from-origin chunks and degenerate gradients.
* **Pixels.** The same scene is drawn through the real renderer with the
  heightfield path, and again from the frozen CPU meshes with the old
  pass-through vertex shader and the *same* fragment shader and uniform state.

The preserved quirks (downward flat normal, per-chunk colour normalisation,
the iz-only variation, LOD-dependent colour) are part of what must match.
"""

import ctypes
import json
import os

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [
    pytest.mark.gl, pytest.mark.slow,
    # PyOpenGL's glGetActiveUniform uses a deprecated NumPy call internally.
    pytest.mark.filterwarnings("ignore:tostring:DeprecationWarning"),
]

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SIZE = 256

#: Worst difference the data comparison accepts, per attribute. The
#: reconstruction is float32 arithmetic in the old order; on Mesa it is
#: bit-exact except for the flat normal's sqrt/divide (1 ulp). Other drivers
#: may fuse multiply-adds, so allow a few ulps of the attribute's magnitude --
#: all orders of magnitude below one 8-bit colour step (0.0039).
ATOL = {"pos": 2e-3, "normal": 1e-5, "colour": 1e-5, "uv": 1e-5, "smooth": 1e-5}
FIELDS = {"pos": slice(0, 3), "normal": slice(3, 6), "colour": slice(6, 9),
          "uv": slice(9, 11), "smooth": slice(11, 14)}


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def map_terrain():
    from engine.terrain import Terrain
    t = Terrain()
    with open(os.path.join(ROOT, "maps", "BigWorld_streaming_test.json")) as f:
        t.from_dict(json.load(f)["terrain_data"])
    return t


def build(t, cx, cz, res):
    slot = t.table.ensure(cx, cz, t.chunk_size, t.offset_x, t.offset_z)
    heights = t._chunk_heights(slot, res)
    t.table.store(slot, res, t.LOD_RESOLUTIONS.index(res), heights)
    t._upload_heightfield(slot)
    return slot


def reference_mesh(t, slot, seamless=True):
    """The frozen CPU mesh for a slot.

    *seamless* swaps in the one deliberate change -- smooth normals by central
    differences across chunk edges (see terrain_reference). Only the textured
    mode reads the smooth normal, so the untextured pixel tests compare
    against the fully frozen mesh.
    """
    from tests.helpers.terrain_reference import (
        reference_chunk_mesh, reference_chunk_mesh_seamless)
    table = t.table
    build = reference_chunk_mesh_seamless if seamless else reference_chunk_mesh
    mesh, _, _ = build(
        t, float(table.world[slot, 0]), float(table.world[slot, 1]),
        float(table.size[slot]), int(table.grid_res[slot]))
    return mesh


# ---------------------------------------------------------------------------
# Data parity: transform feedback
# ---------------------------------------------------------------------------

class Capture:
    """The heightfield vertex shader, linked for transform feedback only."""

    VARYINGS = (b"FragPos", b"Normal", b"VertexColor", b"TexCoords", b"SmoothNormal")

    def __init__(self):
        import OpenGL.GL as gl
        from OpenGL.GL.shaders import compileShader
        from engine import shaders
        from engine.terrain import Terrain
        vs = compileShader(shaders.DEFAULT_SHADERS['terrain.vert'],
                           gl.GL_VERTEX_SHADER)
        self.program = gl.glCreateProgram()
        gl.glAttachShader(self.program, vs)
        names = (ctypes.c_char_p * len(self.VARYINGS))(*self.VARYINGS)
        gl.glTransformFeedbackVaryings(
            self.program, len(self.VARYINGS),
            ctypes.cast(names, ctypes.POINTER(ctypes.POINTER(ctypes.c_char))),
            gl.GL_INTERLEAVED_ATTRIBS)
        gl.glLinkProgram(self.program)
        assert gl.glGetProgramiv(self.program, gl.GL_LINK_STATUS), \
            gl.glGetProgramInfoLog(self.program)
        self.u = {n: gl.glGetUniformLocation(self.program, n)
                  for n in Terrain._UNIFORM_NAMES}
        self.vao = gl.glGenVertexArrays(1)
        self.buffer = gl.glGenBuffers(1)

    def run(self, t, slot):
        """Everything the shader emits for one chunk: ``(6*res^2, 14)``."""
        import glm
        import OpenGL.GL as gl
        res = int(t.table.grid_res[slot])
        n = 6 * res * res
        gl.glUseProgram(self.program)
        identity = glm.mat4(1.0)
        gl.glUniformMatrix4fv(self.u['projection'], 1, gl.GL_FALSE, glm.value_ptr(identity))
        gl.glUniformMatrix4fv(self.u['view'], 1, gl.GL_FALSE, glm.value_ptr(identity))
        unit = 8
        t.set_heightfield_frame_uniforms(self.u, unit)
        (res_u, layer), cx, cy = t.chunk_uniforms(slot)
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY,
                         t._height_pages[slot // t._page_layers])
        gl.glUniform2i(self.u['uChunkI'], res_u, layer)
        gl.glUniform3f(self.u['uChunkX'], *cx)
        gl.glUniform2f(self.u['uChunkY'], *cy)
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_TRANSFORM_FEEDBACK_BUFFER, self.buffer)
        gl.glBufferData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, n * 14 * 4, None, gl.GL_STATIC_READ)
        gl.glBindBufferBase(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, self.buffer)
        gl.glEnable(gl.GL_RASTERIZER_DISCARD)
        gl.glBeginTransformFeedback(gl.GL_TRIANGLES)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, n)
        gl.glEndTransformFeedback()
        gl.glDisable(gl.GL_RASTERIZER_DISCARD)
        data = gl.glGetBufferSubData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, n * 14 * 4)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return np.frombuffer(data, dtype=np.float32).reshape(n, 14).copy()


def assert_matches_reference(got, ref, label):
    for name, cols in FIELDS.items():
        diff = float(np.abs(got[:, cols] - ref[:, cols]).max())
        assert diff <= ATOL[name], f"{label}: {name} differs by {diff}"
    # Flat semantics: normal and colour are one value per triangle, exactly.
    tri = got.reshape(-1, 3, 14)
    assert np.array_equal(tri[:, :, 3:9], np.repeat(tri[:, :1, 3:9], 3, axis=1)), \
        f"{label}: a triangle's three vertices disagree about its normal/colour"


def _set_gradient(t, stops):
    from engine.terrain import BiomeConfig
    t.biome = BiomeConfig(name="test", color_gradient=stops)


CASES = {
    # (cx, cz, res, setup)
    "sculpted chunk, LOD 0": (0, -2, 48, None),
    "LOD 32": (5, 7, 32, None),
    "LOD 16, flat mode": (-3, 1, 16, lambda t: setattr(t, "flat_mode", True)),
    "LOD 8": (20, 20, 8, None),
    "hard colour stops (desert canyon)": (3, 3, 48, lambda t: t.set_biome("desert_canyon")),
    "last stop below 1 (dark cliffs)": (2, -6, 48, lambda t: t.set_biome("dark_cliffs")),
    "mountains + valleys (jagged peaks)": (-7, 4, 48, lambda t: t.set_biome("jagged_peaks")),
    "far from origin": (30000, -30000, 48, None),
    "empty gradient -> 0.5 grey": (1, 1, 32, lambda t: _set_gradient(t, [])),
    "single stop": (1, 2, 32, lambda t: _set_gradient(t, [(0.5, (0.9, 0.1, 0.2))])),
    "first stop above 0, repeated stop": (1, 3, 32, lambda t: _set_gradient(
        t, [(0.3, (0.2, 0.4, 0.6)), (0.3, (0.9, 0.9, 0.1)), (0.8, (0.1, 0.2, 0.3))])),
    # All heights equal: the range falls back to 1, every triangle normalises
    # to exactly 0.0 and lands on the first stop's boundary -- where an
    # off-by-one in the stop test would show (the repeated stop at 0 decides
    # which colour wins).
    "flat chunk, range 0 -> 1, repeated first stop": (40, 40, 48, lambda t: (
        _set_gradient(t, [(0.0, (0.8, 0.2, 0.2)), (0.0, (0.2, 0.8, 0.2)),
                          (1.0, (0.2, 0.2, 0.8))]),
        setattr(t.biome, "height_scale", 0.0),
        setattr(t, "sculpt_offsets", {}), t._touch_sculpt())),
    "mesh scale 1.5, offsets": (2, 2, 48, lambda t: (
        t.set_mesh_scale(1.5), setattr(t, "offset_x", 37.5),
        setattr(t, "offset_z", -91.25), setattr(t, "offset_y", 12.0))),
    "heightmap overlay": (0, 0, 48, lambda t: setattr(
        t, "heightmap_data",
        np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64))),
}


@pytest.mark.parametrize("label", list(CASES))
def test_the_vertex_shader_emits_the_old_vertices(context, label):
    cx, cz, res, setup = CASES[label]
    t = map_terrain()
    if setup is not None:
        setup(t)
    capture = Capture()
    slot = build(t, cx, cz, res)
    got = capture.run(t, slot)
    assert_matches_reference(got, reference_mesh(t, slot), label)
    # The smooth-normal change is confined to the chunk's edge vertices:
    # inside, they are still the frozen np.gradient normals.
    frozen = reference_mesh(t, slot, seamless=False)
    step = float(t.table.size[slot]) / res
    lx = np.rint((got[:, 0] - np.float32(t.table.world[slot, 0])) / step)
    lz = np.rint((got[:, 2] - np.float32(t.table.world[slot, 1])) / step)
    inside = (lx > 0) & (lx < res) & (lz > 0) & (lz < res)
    assert np.abs(got[inside, 11:14] - frozen[inside, 11:14]).max() <= ATOL["smooth"], \
        f"{label}: an interior smooth normal changed"


# ---------------------------------------------------------------------------
# Pixel parity: the real renderer against the frozen CPU meshes
# ---------------------------------------------------------------------------

def checker_texture(c0, c1):
    import OpenGL.GL as gl
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:16, 0:16]
    mask = ((xx // 4 + yy // 4) % 2).astype(bool)
    img[~mask] = (*c0, 255)
    img[mask] = (*c1, 255)
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 16, 16, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    return tex


class ReferenceDraw:
    """The old draw: CPU meshes through the old pass-through vertex shader."""

    def __init__(self):
        import OpenGL.GL as gl
        from OpenGL.GL.shaders import compileProgram, compileShader
        from engine import shaders
        from tests.helpers.terrain_reference import OLD_TERRAIN_VERT
        vs = compileShader(OLD_TERRAIN_VERT, gl.GL_VERTEX_SHADER)
        fs = compileShader(shaders.light_ubo_source(shaders.DEFAULT_SHADERS['terrain.frag']),
                           gl.GL_FRAGMENT_SHADER)
        self.program = compileProgram(vs, fs, validate=False)
        block = gl.glGetUniformBlockIndex(self.program, 'FioLightBlock')
        if block != getattr(gl, 'GL_INVALID_INDEX', 0xFFFFFFFF):
            gl.glUniformBlockBinding(self.program, block, shaders.LIGHT_UBO_BINDING)
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)

    def copy_uniforms_from(self, source):
        """Give this program every uniform value *source* currently holds."""
        import OpenGL.GL as gl
        floats = {gl.GL_FLOAT: 1, gl.GL_FLOAT_VEC2: 2, gl.GL_FLOAT_VEC3: 3,
                  gl.GL_FLOAT_VEC4: 4, gl.GL_FLOAT_MAT4: 16}
        setters_f = {1: gl.glUniform1fv, 2: gl.glUniform2fv, 3: gl.glUniform3fv,
                     4: gl.glUniform4fv}
        gl.glUseProgram(self.program)
        count = gl.glGetProgramiv(self.program, gl.GL_ACTIVE_UNIFORMS)
        for i in range(count):
            name, size, utype = gl.glGetActiveUniform(self.program, i)
            name = name.decode() if isinstance(name, bytes) else name
            base = name[:-3] if name.endswith("[0]") else name
            for e in range(size):
                el = f"{base}[{e}]" if size > 1 else name
                dst = gl.glGetUniformLocation(self.program, el)
                src = gl.glGetUniformLocation(source, el)
                if dst < 0 or src < 0:
                    continue
                if utype in floats:
                    n = floats[utype]
                    buf = np.zeros(n, dtype=np.float32)
                    gl.glGetUniformfv(source, src, buf)
                    if n == 16:
                        gl.glUniformMatrix4fv(dst, 1, gl.GL_FALSE, buf)
                    else:
                        setters_f[n](dst, 1, buf)
                else:                      # ints, bools and samplers
                    buf = np.zeros(4, dtype=np.int32)
                    gl.glGetUniformiv(source, src, buf)
                    gl.glUniform1i(dst, int(buf[0]))

    def draw(self, meshes):
        import OpenGL.GL as gl
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        stride = 14 * 4
        for loc, size, offset in ((0, 3, 0), (1, 3, 12), (2, 3, 24), (3, 2, 36), (4, 3, 44)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
        for mesh in meshes:
            data = np.ascontiguousarray(mesh, dtype=np.float32)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STREAM_DRAW)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, len(mesh))
        gl.glBindVertexArray(0)


def render_pair(context, t, eye, target, far, lights=True, seamless=False):
    """``(new image, reference image, chunks drawn, LOD resolutions drawn)``."""
    import glm
    import OpenGL.GL as gl
    from editor.things import Light
    from engine.view_distance import ViewDistance
    from tests.helpers.worlds import make_thing

    renderer = glh.make_renderer()
    renderer.view_distance = ViewDistance(far)
    renderer.setup_terrain_shader(t)
    things = []
    if lights:
        things.append(make_thing(
            Light, "sun", (eye[0] + 200.0, eye[1] + 400.0, eye[2] - 300.0),
            color=[255, 230, 200], intensity=1.5, radius=4000.0, state="on",
            casts_shadows=False))
    eye_v = glm.vec3(*eye)
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0, far)
    view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
    config = glh.render_config(all_brushes=[], all_things=things, terrain=t,
                               shadows_enabled=False)
    t.MAX_UPDATES_PER_FRAME = 100000              # build everything at once
    t.UPDATE_BUDGET_MS = 1e9

    def frame():
        context.bind()
        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
        renderer.render_scene(projection, view, eye_v, None, config,
                              brush_slots=config["all_brush_slots"])
        gl.glFinish()
        return context.read_pixels()

    frame()                                       # stream + build
    new = frame()                                 # draw built chunks
    drawn = list(t.drawn_slots)
    assert drawn, "the scene drew no terrain"
    resolutions = sorted({int(t.table.grid_res[s]) for s in drawn})

    # Reference: identical uniform state, the frozen meshes, the old shader.
    ref = ReferenceDraw()
    ref.copy_uniforms_from(t.shader_program)
    context.bind()
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    gl.glUseProgram(ref.program)
    ref.draw([reference_mesh(t, s, seamless=seamless) for s in drawn])
    gl.glFinish()
    old = context.read_pixels()
    try:
        renderer.cleanup()
    except Exception:
        pass
    return new, old, len(drawn), resolutions


def assert_images_match(new, old, label):
    diff = np.abs(new.astype(int) - old.astype(int)).max(axis=-1)
    changed = np.count_nonzero(diff > 1) / diff.size
    assert not glh.is_blank(new), f"{label}: nothing was rendered"
    # Same triangles, same fragment shader, same varyings to within an ulp:
    # the images agree to rounding. Allow a vanishing fraction of pixels a
    # step apart for a driver that rasterises an ulp-shifted edge differently.
    assert changed <= 0.001, f"{label}: {changed:.4%} of pixels differ by >1/255"
    assert diff.max() <= 3 or changed <= 0.0002, \
        f"{label}: worst pixel differs by {diff.max()}/255"


def sculpted_view(t):
    # Looking across the sculpted chunks south of the origin.
    return (60.0, 420.0, 300.0), (0.0, 120.0, -600.0)


def test_the_sculpted_map_renders_identically(context):
    t = map_terrain()
    eye, target = sculpted_view(t)
    new, old, n, _ = render_pair(context, t, eye, target, far=3000.0)
    assert n >= 4
    assert_images_match(new, old, "sculpted map")


def test_textured_terrain_renders_identically(context):
    """Splat weights read the smooth normal, UVs and height: all rebuilt."""
    t = map_terrain()
    t.use_textures = True
    t.grass_tex = checker_texture((60, 170, 60), (30, 120, 40))
    t.rock_tex = checker_texture((140, 140, 150), (90, 90, 100))
    t.sand_tex = checker_texture((210, 190, 130), (180, 150, 100))
    t.snow_tex = checker_texture((250, 250, 255), (220, 220, 235))
    eye, target = sculpted_view(t)
    new, old, _, _ = render_pair(context, t, eye, target, far=3000.0, seamless=True)
    assert_images_match(new, old, "textured terrain")


def test_flat_mode_renders_identically(context):
    t = map_terrain()
    t.flat_mode = True
    eye, target = sculpted_view(t)
    new, old, _, _ = render_pair(context, t, eye, target, far=3000.0)
    assert_images_match(new, old, "flat mode")


def test_several_lods_in_one_frame_render_identically(context):
    """A long view over wide bounds: LOD 48, 32, 16 and 8 side by side."""
    t = map_terrain()
    t.set_bounds(-4, 60, -3, 3, prune=False)
    new, old, _, resolutions = render_pair(
        context, t, eye=(0.0, 900.0, 0.0), target=(6000.0, 0.0, 0.0),
        far=16000.0, lights=False)
    assert len(resolutions) >= 3, f"expected mixed LODs, drew {resolutions}"
    assert_images_match(new, old, "mixed LODs")
