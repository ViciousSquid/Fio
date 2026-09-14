"""Real-OpenGL scaffolding for the visual tier.

Everything here needs a live GL context, so nothing in it is imported by the
headless suite — the visual tests import it, and they are marked ``gl`` so they
skip with a readable reason when no context can be created.

What it provides is the minimum a renderer smoke test needs and no more: a
core-profile context on an offscreen surface, a colour+depth+stencil
framebuffer to render into, pixel readback, a GL error check that names the
call site, and a deterministic cube/floor/light/camera scene built from the
same brush dicts and ``Thing`` entities the editor uses.
"""

import contextlib

import numpy as np

_FALLBACK_TEXTURE = 0


# ---------------------------------------------------------------------------
# Context + framebuffer
# ---------------------------------------------------------------------------

class GLTestContext:
    """A current core-profile GL context rendering into an offscreen FBO.

    Used as a context manager.  On exit every GL object it created is deleted
    and the context is released, so a test that leaks renderer resources is
    visible as a leak in the *renderer*, not noise from the harness.
    """

    def __init__(self, width=256, height=256, gl_major=3, gl_minor=3):
        self.width = int(width)
        self.height = int(height)
        self.gl_major = gl_major
        self.gl_minor = gl_minor
        self.context = None
        self.surface = None
        self.fbo = None
        self.color_tex = None
        self.depth_rb = None

    # -- lifecycle --------------------------------------------------------
    def __enter__(self):
        from PyQt5.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        from PyQt5.QtWidgets import QApplication

        self._app = QApplication.instance() or QApplication([])
        fmt = QSurfaceFormat()
        fmt.setVersion(self.gl_major, self.gl_minor)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)

        self.surface = QOffscreenSurface()
        self.surface.setFormat(fmt)
        self.surface.create()
        if not self.surface.isValid():
            raise RuntimeError("could not create an offscreen surface")

        self.context = QOpenGLContext()
        self.context.setFormat(fmt)
        if not self.context.create():
            raise RuntimeError("QOpenGLContext.create() failed")
        if not self.context.makeCurrent(self.surface):
            raise RuntimeError("could not make the GL context current")

        self._make_framebuffer()
        return self

    def __exit__(self, exc_type, exc, tb):
        import OpenGL.GL as gl
        try:
            if self.fbo is not None:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
                gl.glDeleteFramebuffers(1, [self.fbo])
            if self.color_tex is not None:
                gl.glDeleteTextures([self.color_tex])
            if self.depth_rb is not None:
                gl.glDeleteRenderbuffers(1, [self.depth_rb])
        except Exception:
            pass  # teardown must not mask the test's own failure
        finally:
            self.fbo = self.color_tex = self.depth_rb = None
            if self.context is not None:
                self.context.doneCurrent()
        return False

    def _make_framebuffer(self):
        import OpenGL.GL as gl
        self.color_tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.color_tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, self.width, self.height,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)

        self.depth_rb = gl.glGenRenderbuffers(1)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.depth_rb)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8,
                                 self.width, self.height)

        self.fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, self.color_tex, 0)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                     gl.GL_RENDERBUFFER, self.depth_rb)
        status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
        if status != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("offscreen framebuffer incomplete: 0x%x" % status)
        gl.glViewport(0, 0, self.width, self.height)

    # -- use --------------------------------------------------------------
    def bind(self):
        import OpenGL.GL as gl
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, self.width, self.height)

    def read_pixels(self):
        """The colour attachment as an ``(h, w, 3)`` uint8 array, top row first."""
        import OpenGL.GL as gl
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
        raw = gl.glReadPixels(0, 0, self.width, self.height, gl.GL_RGB,
                              gl.GL_UNSIGNED_BYTE)
        image = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.width, 3)
        return np.flipud(image).copy()

    def info(self):
        import OpenGL.GL as gl
        def _s(enum):
            value = gl.glGetString(enum)
            return value.decode("utf-8", "replace") if value else "?"
        return {"version": _s(gl.GL_VERSION),
                "renderer": _s(gl.GL_RENDERER),
                "vendor": _s(gl.GL_VENDOR)}


def drain_gl_errors():
    """Every pending GL error, as readable names. Also clears the error queue."""
    import OpenGL.GL as gl
    names = {
        gl.GL_INVALID_ENUM: "GL_INVALID_ENUM",
        gl.GL_INVALID_VALUE: "GL_INVALID_VALUE",
        gl.GL_INVALID_OPERATION: "GL_INVALID_OPERATION",
        gl.GL_INVALID_FRAMEBUFFER_OPERATION: "GL_INVALID_FRAMEBUFFER_OPERATION",
        gl.GL_OUT_OF_MEMORY: "GL_OUT_OF_MEMORY",
    }
    out = []
    for _ in range(16):  # bounded: a driver in a bad state can error forever
        err = gl.glGetError()
        if err == gl.GL_NO_ERROR:
            break
        out.append(names.get(err, "0x%x" % err))
    return out


@contextlib.contextmanager
def no_gl_errors(what):
    """Assert that the wrapped GL work leaves no error behind."""
    drain_gl_errors()          # start from a clean slate
    yield
    errors = drain_gl_errors()
    assert not errors, "%s raised GL errors: %s" % (what, ", ".join(errors))


# ---------------------------------------------------------------------------
# The deterministic visual scene
# ---------------------------------------------------------------------------

#: Where the camera sits and what it looks at, for every visual test.  Fixed so
#: two renders of the same scene are comparable pixel-region for pixel-region.
CAMERA_EYE = (0.0, 180.0, 420.0)
CAMERA_TARGET = (0.0, 60.0, 0.0)

#: The light's positions for the movement test, in order.  Chosen so the cube's
#: shadow falls on visibly different parts of the floor.
LIGHT_POSITIONS = [
    (-220.0, 260.0, 160.0),
    (220.0, 260.0, 160.0),
    (0.0, 320.0, -220.0),
]


def lit_cube_scene(light_pos=LIGHT_POSITIONS[0], shadows=True):
    """A floor, one cube and one shadow-casting point light.

    Returns ``(brushes, things)`` in Fio's own world representation, so the
    renderer is handed exactly what the editor would hand it.
    """
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [
        box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
        box_brush("cube", (0, 64, 0), (128, 128, 128)),
    ]
    light = make_thing(
        Light, "test_light", light_pos,
        color=[255, 255, 255], intensity=2.0, radius=1400.0, state="on",
        casts_shadows=bool(shadows),
    )
    return brushes, [light]


def camera_matrices(aspect=1.0, fov_deg=70.0, eye=CAMERA_EYE, target=CAMERA_TARGET):
    """``(projection, view, eye)`` as PyGLM matrices, matching the engine's."""
    import glm
    eye_v = glm.vec3(*eye)
    projection = glm.perspective(glm.radians(fov_deg), aspect, 1.0, 10000.0)
    view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
    return projection, view, eye_v


def render_config(**overrides):
    """The ``config`` dict :meth:`Renderer_F.render_scene` reads.

    Only the keys the renderer actually looks up, with values that keep the
    visual tests deterministic: no camera-distance cull (so geometry cannot
    vanish because a machine picked a different default), no play mode, grid
    off.
    """
    from engine.constants import RENDER_MODE_LIT

    config = {
        "render_mode": RENDER_MODE_LIT,
        "brush_display_mode": "Textured",
        "play_mode": False,
        "grid_visible": False,
        "camera_distance_cull": False,
        "shadows_enabled": True,
    }
    config.update(overrides)
    return config


def make_renderer(config=None):
    """A :class:`engine.renderer_F.Renderer_F` on the current GL context.

    The texture loader returns a 1x1 white texture for everything, so the tests
    do not depend on which files happen to be in ``assets/``; lighting and
    shadowing are what is under test, not texture content.
    """
    from engine.renderer_F import Renderer_F

    white = _white_texture()

    def _loader(texture_name, subfolder):
        return white

    renderer = Renderer_F(_loader, 64, 4096, config)
    renderer.update_grid_buffers(4096, 64)
    renderer.set_sprite_textures({})
    return renderer


def _white_texture():
    """A cached 1x1 opaque white GL texture."""
    global _FALLBACK_TEXTURE
    import OpenGL.GL as gl
    if _FALLBACK_TEXTURE:
        return _FALLBACK_TEXTURE
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0, gl.GL_RGBA,
                    gl.GL_UNSIGNED_BYTE, bytes([255, 255, 255, 255]))
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    _FALLBACK_TEXTURE = tex
    return tex


def reset_texture_cache():
    """Forget the cached white texture (its GL name dies with the context)."""
    global _FALLBACK_TEXTURE
    _FALLBACK_TEXTURE = 0


# ---------------------------------------------------------------------------
# Image sanity checks
# ---------------------------------------------------------------------------

def luminance(image):
    """Perceptual luminance of an ``(h, w, 3)`` uint8 image, as float64."""
    return (image[..., 0] * 0.299 + image[..., 1] * 0.587 + image[..., 2] * 0.114)


def region(image, x0_frac, y0_frac, x1_frac, y1_frac):
    """A rectangular sub-image addressed in fractions of the image size.

    Fractions rather than pixels so a test reads the same region whatever
    resolution the context was created at.
    """
    h, w = image.shape[:2]
    x0, x1 = int(w * x0_frac), int(w * x1_frac)
    y0, y1 = int(h * y0_frac), int(h * y1_frac)
    return image[y0:y1, x0:x1]


def is_blank(image, tolerance=2):
    """True when every pixel is (near enough) the same colour."""
    flat = image.reshape(-1, image.shape[-1]).astype(np.int16)
    return bool(np.all(flat.max(axis=0) - flat.min(axis=0) <= tolerance))
