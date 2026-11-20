"""A portal draws its virtual scene only when some of its aperture is in view.

A portal's view is a whole second scene -- cull, uploads, every pass -- shown
through a stencil of its aperture. The aperture's scissor rect gave up when
any corner was behind the camera, and "no rect" meant "no scissor", so a
portal behind the camera, or beside it, rendered that whole scene for a
stencil that masked every fragment. Now an aperture outside the frustum is
skipped, a partly visible one is clipped to the near plane for its rect, and
a camera straddling an aperture still draws through it.

The headless tests drive the real ``draw_portals`` with OpenGL replaced by a
recorder (which views are drawn is decided before any GL call matters); the
``gl`` ones render through a real context.
"""

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the portals are editor Things")
pytest.importorskip("OpenGL")

from editor.things import Portal                              # noqa: E402
from engine.entity_table import EntityTable                   # noqa: E402
from engine.renderer.api import RenderStats                   # noqa: E402
from engine.renderer.core.visibility import VisibilityMixin   # noqa: E402
from engine.renderer.forward import portals                   # noqa: E402
from tests.helpers.worlds import make_thing                   # noqa: E402

pytestmark = pytest.mark.qt

_REAL_GL = portals.gl
WIDTH, HEIGHT = 640, 480
PROJECTION = glm.perspective(glm.radians(70.0), WIDTH / HEIGHT, 1.0, 5000.0)


class _GLRecorder:
    """Records every GL call and makes none; enums are OpenGL's own."""

    def __init__(self):
        self.calls = []

    def glGetIntegerv(self, enum):
        self.calls.append('glGetIntegerv')
        return [0, 0, WIDTH, HEIGHT] if enum == _REAL_GL.GL_VIEWPORT else 0

    def __getattr__(self, name):
        if name.startswith('GL_'):
            return getattr(_REAL_GL, name)
        return lambda *args, **kwargs: self.calls.append(name)


class _PortalPass(portals.PortalsMixin, VisibilityMixin):
    """The portal pass's GL handles, as _init_portal_gl leaves them."""

    def __init__(self):
        self.render_stats = RenderStats()
        self._portal_gl_ready = True
        self._portal_quad_vao = self._portal_quad_vbo = 1
        self._portal_mask_shader = self._portal_rim_shader = 1
        self._portal_fade_shader = 0
        self._portal_mask_proj_loc = self._portal_mask_view_loc = 0
        self._portal_rim_proj_loc = self._portal_rim_view_loc = 0
        self._portal_rim_color_loc = 0
        self._portal_behind_texture = 0
        self._portal_behind_size = (0, 0)
        self._proj_ptr = self._view_ptr = None
        self._current_shader = None
        self._portal_scene_pass = False
        self._instance_view = None
        self._portal_proj_inv_sig = self._portal_proj_inv = None
        self._identity_mat4 = glm.mat4(1.0)


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.setattr(portals, 'gl', _GLRecorder())
    return _PortalPass()


def _pair(name_a, name_b, pos_a, pos_b, **props):
    a = make_thing(Portal, name_a, pos_a, id="portal-" + name_a, portal_target=name_b,
                   width=400.0, height=400.0, show_rim=False, **props)
    b = make_thing(Portal, name_b, pos_b, id="portal-" + name_b, portal_target=name_a,
                   width=400.0, height=400.0, show_rim=False, **props)
    return [a, b]


def _table(things):
    table = EntityTable()
    table.begin_frame(things, epoch=1)
    return table


def _frame(renderer, table, eye, target):
    """The apertures whose virtual scene one frame draws."""
    drawn = []
    view = glm.lookAt(eye, target, glm.vec3(0, 1, 0))
    renderer.draw_portals(table, table.portal_slots, PROJECTION, view, eye,
                          {"play_mode": True, "entity_table": table},
                          lambda view_state, cfg: drawn.append(view_state.aperture_slot))
    return drawn


def _basis(table, slot=0):
    normal = glm.vec3(*(float(v) for v in table.portal_basis[slot, 2]))
    pos = glm.vec3(*(float(v) for v in table.pos[slot]))
    return pos, normal


@pytest.fixture
def pair():
    # B is far behind A's back, out of every view below.
    return _table(_pair("A", "B", (0.0, 0.0, 0.0), (2000.0, 0.0, -3000.0)))


def test_a_portal_in_view_draws_its_scene(renderer, pair):
    pos, normal = _basis(pair)
    assert _frame(renderer, pair, pos + normal * 300.0, pos) == [0]


def test_a_portal_in_view_draws_its_scene_once(renderer):
    # Both in reach, B behind the camera: each names A's view as well.
    table = _table(_pair("A", "B", (0.0, 0.0, 0.0), (0.0, 0.0, 1500.0)))
    pos, normal = _basis(table)
    assert _frame(renderer, table, pos + normal * 300.0, pos) == [0], (
        "a pair linked both ways named A's view twice; the second draw finds "
        "its mask flattened and renders a scene for no fragment")


def test_a_portal_behind_the_camera_draws_no_scene(renderer, pair):
    pos, normal = _basis(pair)
    eye = pos + normal * 300.0
    assert _frame(renderer, pair, eye, pos + normal * 1000.0) == []


def test_a_portal_off_to_the_side_draws_no_scene(renderer, pair):
    pos, normal = _basis(pair)
    eye = pos + normal * 300.0
    side = glm.cross(normal, glm.vec3(0, 1, 0))
    assert _frame(renderer, pair, eye, eye + side * 1000.0) == []


@pytest.mark.parametrize("look", ["at", "away", "along"])
def test_a_camera_straddling_the_aperture_still_draws_through_it(renderer, pair, look):
    """Within PORTAL_NEAR_STRADDLE of the plane and inside the aperture the
    mask covers the screen, wherever the aperture's corners project to."""
    pos, normal = _basis(pair)
    for offset in (8.0, -8.0):                 # either side, before transit
        eye = pos + normal * offset + glm.vec3(0.0, 40.0, 0.0)
        target = {"at": pos,
                  "away": eye + normal * 500.0,
                  "along": eye + glm.cross(normal, glm.vec3(0, 1, 0)) * 500.0}[look]
        assert 0 in _frame(renderer, pair, eye, target), (look, offset)


def test_portals_behind_the_camera_do_not_use_up_the_budget(renderer):
    """MAX_PORTALS counts the views drawn. Pairs behind the camera listed
    first used to spend it all, leaving the one in front undrawn."""
    things = []
    for i in range(3):
        things += _pair("H%d" % i, "K%d" % i, (i * 500.0, 0.0, 2000.0),
                        (i * 500.0, 0.0, 3000.0), fade_distance=99999.0)
    things += _pair("V", "W", (0.0, 0.0, -300.0), (5000.0, 0.0, 5000.0),
                    fade_distance=99999.0)
    table = _table(things)
    visible = len(things) - 2
    eye = glm.vec3(0.0, 0.0, 300.0)
    target = glm.vec3(0.0, 0.0, -300.0)
    pos, normal = _basis(table, visible)
    assert glm.dot(normal, eye - pos) > 0, "V faces the camera"
    assert visible in _frame(renderer, table, eye, target)


def _visible_points(corners, pv, samples=41):
    """Window coordinates of points across the aperture inside the frustum."""
    a, b, _c, d = (np.asarray(c, dtype=np.float64) for c in corners)
    u, v = np.meshgrid(np.linspace(0, 1, samples), np.linspace(0, 1, samples))
    points = a + u[..., None] * (b - a) + v[..., None] * (d - a)
    m = np.array(pv.to_list(), dtype=np.float64)          # m[col][row]
    hom = np.concatenate((points.reshape(-1, 3), np.ones((points.size // 3, 1))), axis=1)
    clip = hom @ m
    w = clip[:, 3]
    inside = np.all(np.abs(clip[:, :3]) <= w[:, None], axis=1) & (w > 0)
    clip = clip[inside]
    sx = (clip[:, 0] / clip[:, 3] * 0.5 + 0.5) * WIDTH
    sy = (clip[:, 1] / clip[:, 3] * 0.5 + 0.5) * HEIGHT
    return sx, sy


@pytest.mark.parametrize("yaw", [60.0, 75.0, 90.0, 110.0, 120.0])
def test_a_partly_visible_aperture_gets_a_rect_holding_all_of_it(renderer, pair, yaw):
    """Two corners behind the camera no longer give up on the rect: the
    aperture is clipped to the near plane and the rect still bounds every
    pixel of it that can be seen."""
    pos, normal = _basis(pair)
    side = glm.cross(normal, glm.vec3(0, 1, 0))
    eye = pos + normal * 60.0 + side * 150.0
    turn = glm.radians(yaw)
    target = eye + (-normal * float(np.cos(turn)) - side * float(np.sin(turn))) * 100.0
    pv = PROJECTION * glm.lookAt(eye, target, glm.vec3(0, 1, 0))
    corners = renderer._portal_slot_corners(pair, 0, renderer.PORTAL_APERTURE_INSET)
    behind = [(pv * glm.vec4(*(float(v) for v in c), 1.0)).w <= 0.0 for c in corners]
    assert sum(behind) == 2
    sx, sy = _visible_points(corners, pv)
    assert len(sx), "the view chosen should see some of the aperture"
    x, y, w, h = renderer._portal_screen_rect(corners, pv)
    assert w > 0 and h > 0, "a visible aperture was skipped"
    assert sx.min() >= x - 1 and sx.max() <= x + w + 1
    assert sy.min() >= y - 1 and sy.max() <= y + h + 1
    # Clipped, not the whole screen: the aperture is on one side of it.
    assert w * h < WIDTH * HEIGHT


@pytest.mark.parametrize("yaw", [135.0, 160.0])
def test_an_aperture_beside_the_camera_with_corners_behind_has_an_empty_rect(
        renderer, pair, yaw):
    """What used to fall back to no scissor at all: two corners behind the
    camera, and the two in front off the edge of the screen."""
    pos, normal = _basis(pair)
    side = glm.cross(normal, glm.vec3(0, 1, 0))
    eye = pos + normal * 60.0 + side * 150.0
    turn = glm.radians(yaw)
    target = eye + (-normal * float(np.cos(turn)) - side * float(np.sin(turn))) * 100.0
    pv = PROJECTION * glm.lookAt(eye, target, glm.vec3(0, 1, 0))
    corners = renderer._portal_slot_corners(pair, 0, renderer.PORTAL_APERTURE_INSET)
    assert not len(_visible_points(corners, pv)[0])
    _x, _y, w, h = renderer._portal_screen_rect(corners, pv)
    assert w == 0 and h == 0


def test_an_aperture_wholly_behind_the_near_plane_has_an_empty_rect(renderer, pair):
    pos, normal = _basis(pair)
    eye = pos + normal * 300.0
    pv = PROJECTION * glm.lookAt(eye, eye + normal, glm.vec3(0, 1, 0))
    corners = renderer._portal_slot_corners(pair, 0, renderer.PORTAL_APERTURE_INSET)
    _x, _y, w, h = renderer._portal_screen_rect(corners, pv)
    assert w == 0 and h == 0


# ---------------------------------------------------------------------------
# Real OpenGL
# ---------------------------------------------------------------------------

SIZE = 64


@pytest.fixture
def ctx():
    from tests.helpers import gl as glh
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        real = glh.make_renderer()
        try:
            yield context, real
        finally:
            real.cleanup()
            glh.reset_texture_cache()


def _render_real(ctx, eye, target):
    """Draw the pair's portals over a red frame, the view through any of
    them blue; returns the apertures drawn and the centre pixel."""
    import OpenGL.GL as gl
    context, real = ctx
    table = _table(_pair("A", "B", (0.0, 0.0, 0.0), (2000.0, 0.0, -3000.0)))
    projection = glm.perspective(glm.radians(90.0), 1.0, 1.0, 5000.0)
    view = glm.lookAt(eye, target, glm.vec3(0, 1, 0))
    context.bind()
    gl.glClearColor(1.0, 0.0, 0.0, 1.0)
    gl.glClearStencil(0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    drawn = []
    identity = glm.mat4(1.0)

    def portal_view(view_state, cfg):
        drawn.append(view_state.aperture_slot)
        real._portal_upload_quad([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)])
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(real._portal_rim_shader)
        ident = glm.value_ptr(identity)
        gl.glUniformMatrix4fv(real._portal_rim_proj_loc, 1, gl.GL_FALSE, ident)
        gl.glUniformMatrix4fv(real._portal_rim_view_loc, 1, gl.GL_FALSE, ident)
        gl.glUniform4f(real._portal_rim_color_loc, 0.0, 0.0, 1.0, 1.0)
        gl.glBindVertexArray(real._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glEnable(gl.GL_DEPTH_TEST)

    real.draw_portals(table, table.portal_slots, projection, view, eye,
                      {"play_mode": True, "entity_table": table}, portal_view)
    return drawn, context.read_pixels()[SIZE // 2, SIZE // 2].astype(int)


@pytest.mark.gl
def test_real_gl_a_portal_in_view_shows_its_scene(ctx):
    drawn, pixel = _render_real(ctx, glm.vec3(0, 0, 150), glm.vec3(0, 0, 0))
    assert drawn == [0]
    assert pixel[2] > 200 and pixel[0] < 50, pixel


@pytest.mark.gl
def test_real_gl_a_portal_behind_the_camera_renders_nothing(ctx):
    drawn, pixel = _render_real(ctx, glm.vec3(0, 0, 150), glm.vec3(0, 0, 1000))
    assert drawn == []
    assert pixel[0] > 200 and pixel[2] < 50, pixel


@pytest.mark.gl
def test_real_gl_a_straddling_camera_sees_through_the_aperture(ctx):
    drawn, pixel = _render_real(ctx, glm.vec3(0, 40, 6), glm.vec3(0, 40, -500))
    assert drawn == [0]
    assert pixel[2] > 200 and pixel[0] < 50, pixel
