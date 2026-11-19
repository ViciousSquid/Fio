"""Native kiosk presentation: the same renderer, on a window of its own.

With ``[Kiosk] native_surface`` kiosk mode presents on a native window (a
QWindow) instead of the 3D view's QOpenGLWidget: the view's one GL context is
made current on the window and the view's own paintGL draws the published
frame there. These hold it to:

* the same picture as the widget draws, for the same frame;
* the context handed back to the widget -- its framebuffer -- when kiosk mode
  ends, the editor window back and drawing as before;
* a failing makeCurrent, at entry or mid-game, ending in kiosk mode on the 3D
  view rather than a broken window;
* the view's geometry, input and mouse capture following the native window.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt                             # noqa: E402
from PyQt5.QtGui import QKeyEvent, QOpenGLContext               # noqa: E402

from editor.things import PlayerStart                           # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room   # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.qt]

SIZE = (320, 180)


def _pump(app, until, seconds=5.0):
    """Run the event loop until *until()* holds, for up to *seconds* of wall
    clock. Time, not a count of turns: a Wayland compositor exposes a new
    window only after a round trip, and spinning processEvents can finish
    before that answer arrives."""
    import time
    from PyQt5.QtCore import QEventLoop
    deadline = time.monotonic() + seconds
    while True:
        app.processEvents()
        app.processEvents(QEventLoop.AllEvents, 20)
        if until():
            return True
        if time.monotonic() >= deadline:
            return until()
        time.sleep(0.002)


@pytest.fixture
def session(fio_session):
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]), size=SIZE)
    view, window = session.view, session.window
    if view.renderer is None:
        pytest.skip("no OpenGL context")
    config = window.config
    if not config.has_section('Kiosk'):
        config.add_section('Kiosk')
    config.set('Kiosk', 'window_mode', 'Windowed')
    config.set('Kiosk', 'res_width', str(SIZE[0]))
    config.set('Kiosk', 'res_height', str(SIZE[1]))
    config.set('Display', 'show_fps', 'False')      # a counter differs frame to frame
    # The HUD's fade runs on wall clock: off, so a frame's translucency (the
    # health count's opacity) does not depend on how long the test took.
    view.hud.set_hud_fade_enabled(False)
    session.start_play()
    session.step(20)
    # The "ESC to Pause" hint lasts three seconds of wall clock from Play's
    # start: whether a frame shows it depends on timing, not presentation.
    view._clear_play_mode_hint()
    yield session
    if window.is_kiosk_mode:
        window.exit_kiosk_mode(keep_play_mode=True, confirm=False)


def _report(message):
    """A line CI greps for (play-click-native.yml): what this platform did."""
    print("[native-kiosk] " + message, flush=True)


def _enter_native(session):
    """Kiosk mode presented natively -- asserted, never assumed: a fallback
    to the widget fails the test rather than turning it into a widget test."""
    window, view = session.window, session.view
    window.config.set('Kiosk', 'native_surface', 'True')
    window.enter_kiosk_mode()
    surface = view._native_surface
    assert surface is not None, "native presentation did not start (fell back to the widget)"
    assert _pump(session.app, surface.isExposed), "the native window was never exposed"
    assert not window.isVisible(), "the editor window is still the one shown"
    assert surface.context is view.context()
    from PyQt5.QtGui import QSurface
    assert surface.surfaceType() == QSurface.OpenGLSurface
    assert int(surface.winId()) != 0, "no native window behind the surface"
    return surface


def _native_frame(session, surface):
    """Present the newest frame natively; the pixels drawn, before the swap."""
    from OpenGL import GL
    captured = {}

    def grab(width, height):
        # Drawn into the window's own default framebuffer -- never the
        # widget's framebuffer object, which a fallback would have used.
        bound = int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))
        assert bound == surface.context.defaultFramebufferObject(), bound
        assert bound != _widget_fbo(session.view), "drawn into the widget's framebuffer"
        captured['framebuffer'] = bound
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        data = GL.glReadPixels(0, 0, width, height, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        captured['image'] = np.frombuffer(data, np.uint8).reshape(height, width, 3)[::-1].copy()
    surface.before_swap = grab
    session.game_state.try_swap()
    assert view_is_native(session, surface), "presentation fell back before the frame"
    assert surface.present(), "the native window drew no frame"
    surface.before_swap = None
    assert view_is_native(session, surface), "presentation fell back during the frame"
    image = captured['image']
    assert image.std() > 1.0, "the native framebuffer holds no picture"
    _native_frame.last_framebuffer = captured['framebuffer']
    return image


def _widget_fbo(view):
    """The widget's own framebuffer object (QOpenGLWidget's, not the
    presentation target's)."""
    from PyQt5.QtWidgets import QOpenGLWidget
    return int(QOpenGLWidget.defaultFramebufferObject(view))


def view_is_native(session, surface):
    """The view presents on *surface* (without touching its paint device:
    that is made only while the surface's context is current, in a frame)."""
    return session.view._native_surface is surface


def test_the_native_window_shows_what_the_widget_shows(session):
    """The same frame, presented natively, is the same picture: the scene
    and the HUD (QPainter on the native window's paint device) identical to
    the widget's, within 1 level.

    The HUD is checked to have drawn at all, on both: a paint device made
    while no context was current once drew no HUD on the native window, and
    a comparison of frames could not tell a missing faint HUD from a small
    blending difference."""
    view = session.view
    view._hud_runtime_visible = False
    widget_scene = session.paint()
    view._hud_runtime_visible = None             # the HUD as configured
    widget_hud = session.paint()
    surface = _enter_native(session)
    assert (surface.width(), surface.height()) == SIZE
    view._hud_runtime_visible = False
    native_scene = _native_frame(session, surface)
    view._hud_runtime_visible = None
    native_hud = _native_frame(session, surface)

    def differs(a, b):
        return np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 1
    assert native_hud.shape == widget_hud.shape
    assert not differs(native_scene, widget_scene).any(), "the scene differs"
    assert differs(widget_hud, widget_scene).sum() > 50, "the HUD drew nothing on the widget"
    assert differs(native_hud, native_scene).sum() > 50, "the HUD drew nothing on the native window"
    mismatch = differs(native_hud, widget_hud)
    assert not mismatch.any(), "the frame with its HUD differs in %d pixels" % int(mismatch.sum())

    from OpenGL import GL
    surface.make_current()
    renderer = GL.glGetString(GL.GL_RENDERER).decode(errors='replace')
    session.view._restore_widget_framebuffer()
    _report("created and presented on %s (%s), %dx%d, native window 0x%x, drawn into "
            "framebuffer %d (the widget's is %d): the same picture as the widget, "
            "scene and HUD"
            % (session.app.platformName(), renderer, surface.width(), surface.height(),
               int(surface.winId()), _native_frame.last_framebuffer, _widget_fbo(view)))


def test_leaving_kiosk_mode_hands_the_context_back_to_the_widget(session):
    from PyQt5.QtWidgets import QOpenGLWidget
    window, view = session.window, session.view
    surface = _enter_native(session)
    _native_frame(session, surface)
    assert not window.isVisible(), "the editor window stays out of sight"
    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert view._native_surface is None
    assert window.isVisible()
    # The view is the widget again: its own size (the editor layout comes
    # back on leaving kiosk mode, as it always has), drawn into its own
    # framebuffer, the same picture twice.
    real = (QOpenGLWidget.width(view), QOpenGLWidget.height(view))
    assert (view.width(), view.height()) == real
    first, second = session.paint(), session.paint()
    assert first.shape[:2] == (real[1] * int(view.devicePixelRatioF()),
                               real[0] * int(view.devicePixelRatioF()))
    assert first.std() > 1.0 and np.array_equal(first, second)


def test_the_view_follows_the_native_window(session):
    window, view = session.window, session.view
    surface = _enter_native(session)
    assert (view.width(), view.height()) == (surface.width(), surface.height())
    assert view.mapToGlobal(view.rect().center()) == surface.mapToGlobal(view.rect().center())
    assert surface.make_current()
    try:
        assert view._paint_device() is surface.paint_device()
    finally:
        view._restore_widget_framebuffer()
    surface.resize(400, 200)
    # The window's resize event (not resize() itself) is what the view follows.
    _pump(session.app, lambda: view._cached_aspect_ratio == pytest.approx(400 / 200))
    assert view.width() == 400
    assert view._cached_aspect_ratio == pytest.approx(400 / 200)
    resized = _native_frame(session, surface)          # drawn at the new size
    ratio = surface.devicePixelRatio()
    assert resized.shape[:2] == (int(200 * ratio), int(400 * ratio))


def test_keys_on_the_native_window_reach_the_game(session):
    window, view = session.window, session.view
    surface = _enter_native(session)
    window.keys_pressed.clear()
    from PyQt5.QtWidgets import QApplication
    QApplication.sendEvent(surface, QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier, "w"))
    assert Qt.Key_W in window.keys_pressed, "the key never reached the editor's key handling"
    QApplication.sendEvent(surface, QKeyEvent(QEvent.KeyRelease, Qt.Key_W, Qt.NoModifier, "w"))
    assert Qt.Key_W not in window.keys_pressed


def test_a_failing_makecurrent_at_entry_keeps_kiosk_mode_on_the_3d_view(session, monkeypatch):
    window, view = session.window, session.view
    original = QOpenGLContext.makeCurrent

    def refuse(context, surface):
        from engine.kiosk_surface import NativeKioskSurface
        if isinstance(surface, NativeKioskSurface):
            return False
        return original(context, surface)
    monkeypatch.setattr(QOpenGLContext, 'makeCurrent', refuse)
    window.config.set('Kiosk', 'native_surface', 'True')
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode
    assert view._native_surface is None, "a window that cannot be drawn to was kept"
    assert window.isVisible()
    monkeypatch.setattr(QOpenGLContext, 'makeCurrent', original)
    assert session.paint().std() > 1.0, "the 3D view no longer draws"


def test_a_failing_present_mid_game_falls_back_to_the_3d_view(session, monkeypatch):
    window, view = session.window, session.view
    surface = _enter_native(session)
    monkeypatch.setattr(surface.context, 'makeCurrent', lambda s: False)
    view._present_native_frame()
    _pump(session.app, lambda: view._native_surface is None)
    monkeypatch.undo()
    assert view._native_surface is None
    assert window.is_kiosk_mode and window.isVisible()
    assert session.paint().std() > 1.0


def test_off_by_default(session):
    """The widget presents kiosk mode unless native presentation is asked
    for: the shipped settings.ini says off, and so does a settings.ini that
    does not mention it."""
    import configparser
    import os
    from tests.helpers.paths import REPO_ROOT
    shipped = configparser.ConfigParser()
    shipped.read(os.path.join(REPO_ROOT, 'settings.ini'), encoding='utf-8')
    assert shipped.getboolean('Kiosk', 'native_surface') is False
    window, view = session.window, session.view
    window.config.remove_option('Kiosk', 'native_surface')
    assert window.kiosk_native_surface_enabled() is False
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode and view._native_surface is None


_STATE = None


def _gl_state():
    from OpenGL import GL
    get = GL.glGetIntegerv
    return {
        'framebuffer': int(get(GL.GL_FRAMEBUFFER_BINDING)),
        'viewport': tuple(int(v) for v in get(GL.GL_VIEWPORT)),
        'scissor test': bool(GL.glIsEnabled(GL.GL_SCISSOR_TEST)),
        'depth test': bool(GL.glIsEnabled(GL.GL_DEPTH_TEST)),
        'blend': bool(GL.glIsEnabled(GL.GL_BLEND)),
        'cull face': bool(GL.glIsEnabled(GL.GL_CULL_FACE)),
        'stencil test': bool(GL.glIsEnabled(GL.GL_STENCIL_TEST)),
        'depth mask': bool(GL.glGetBooleanv(GL.GL_DEPTH_WRITEMASK)),
        'colour mask': tuple(bool(v) for v in GL.glGetBooleanv(GL.GL_COLOR_WRITEMASK)),
        'program': int(get(GL.GL_CURRENT_PROGRAM)),
        'active texture': int(get(GL.GL_ACTIVE_TEXTURE)),
        'vertex array': int(get(GL.GL_VERTEX_ARRAY_BINDING)),
    }


def test_leaving_native_kiosk_restores_the_widgets_gl_state(session, monkeypatch):
    """The state Qt hands the widget's paintGL -- its framebuffer, the
    viewport, scissor and the rest -- is what it was before native kiosk
    mode borrowed the context for another surface."""
    from PyQt5.QtWidgets import QOpenGLWidget
    window, view = session.window, session.view
    seen = []
    original = type(view).paintGL

    def snapshot(self):
        if self._native_surface is None and self._qt_paint_event_active:
            state = _gl_state()
            # The widget's size as this paint sees it: a window manager may
            # resize the editor window between two checks.
            ratio = self.devicePixelRatioF()
            state['size'] = (int(QOpenGLWidget.width(self) * ratio),
                             int(QOpenGLWidget.height(self) * ratio))
            seen.append(state)
        return original(self)
    monkeypatch.setattr(type(view), 'paintGL', snapshot)

    def widget_frame():
        seen.clear()
        session.game_state.try_swap()
        view.repaint()                       # a real paint event: Qt sets up the widget
        session.app.processEvents()
        assert seen, "the widget did not paint"
        return seen[0]

    before = widget_frame()
    surface = _enter_native(session)
    for _ in range(3):
        _native_frame(session, surface)
    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    ratio = view.devicePixelRatioF()
    size = (int(QOpenGLWidget.width(view) * ratio), int(QOpenGLWidget.height(view) * ratio))

    # GL work outside a paint (the decal tool, a renderer switch, uploads)
    # makes the context current on the widget itself; Qt then binds the
    # widget's framebuffer but sets no viewport. It must not inherit the
    # native window's.
    view.makeCurrent()
    try:
        direct = _gl_state()
    finally:
        view.doneCurrent()
    assert direct['framebuffer'] == view.defaultFramebufferObject()
    assert direct['viewport'] == (0, 0) + size
    assert not direct['scissor test']

    after = widget_frame()
    assert after['framebuffer'] == view.defaultFramebufferObject() != 0
    assert after['viewport'] == (0, 0) + after['size']
    assert not after['scissor test']
    unchanged = {k: v for k, v in before.items() if k not in ('framebuffer', 'viewport', 'size')}
    assert {k: after[k] for k in unchanged} == unchanged
    _report("restored: the widget's framebuffer, viewport and GL state after leaving")


def test_the_handover_itself_leaves_the_widgets_framebuffer_and_viewport(session):
    """Straight after native presentation ends -- before Qt paints the widget
    again -- GL work made current on the widget (makeCurrent binds its
    framebuffer, sets no viewport) finds the widget's viewport, no scissor."""
    from PyQt5.QtWidgets import QOpenGLWidget
    window, view = session.window, session.view
    surface = _enter_native(session)
    surface.resize(400, 220)                   # not the widget's size
    _pump(session.app, lambda: surface.width() == 400)
    for _ in range(2):
        _native_frame(session, surface)
    ratio = view.devicePixelRatioF()
    size = (int(QOpenGLWidget.width(view) * ratio), int(QOpenGLWidget.height(view) * ratio))
    assert size != (400, 220)
    view.end_native_presentation()             # no event processing after this
    view.makeCurrent()
    try:
        state = _gl_state()
    finally:
        view.doneCurrent()
    assert state['framebuffer'] == view.defaultFramebufferObject()
    assert state['viewport'] == (0, 0) + size
    assert not state['scissor test']
    window.show()


def test_native_kiosk_is_only_a_presentation_surface(fio_session):
    """Fio's editor is the runtime: native kiosk presentation changes where
    frames appear and nothing else. The same renderer, GL context, logic
    thread, publication and live world (EditorState, its brushes and things)
    run the game; no second one of any exists; and what the game does to the
    world is there in the editor afterwards."""
    import gc
    from engine.logic_thread import LogicThread
    from engine.renderer.forward.renderer import ForwardRenderer
    from engine.threaded_game_state import ThreadedGameState
    level = {"version": 5,
             "brushes": [{"id": "floor", "name": "floor", "pos": [0, -8, 0],
                          "size": [2000, 16, 2000]}],
             "things": [{"type": "playerstart", "pos": [0, 40, 600],
                         "properties": {"type": "player_start", "name": "s",
                                        "id": "ps", "primary": True}},
                        {"type": "prop", "pos": [0, 300, 0],
                         "properties": {"type": "prop", "id": "drum", "name": "drum",
                                        "model_path": "assets/models/Oil_Drum.obj",
                                        "render_mode": "model", "physics_enabled": True,
                                        "no_collision": False, "scale": 1.0}}]}
    session = fio_session(level, size=SIZE)
    window, view = session.window, session.view
    if view.renderer is None:
        pytest.skip("no OpenGL context")
    config = window.config
    if not config.has_section('Kiosk'):
        config.add_section('Kiosk')
    config.set('Kiosk', 'window_mode', 'Windowed')
    config.set('Kiosk', 'res_width', str(SIZE[0]))
    config.set('Kiosk', 'res_height', str(SIZE[1]))

    def counts():
        gc.collect()
        objects = gc.get_objects()
        return tuple(sum(1 for o in objects if isinstance(o, kind))
                     for kind in (LogicThread, ForwardRenderer, ThreadedGameState))

    world = window.state
    brushes, things = world.brushes, world.things
    drum = next(t for t in things if t.properties.get("id") == "drum")
    editor_side = (view.renderer, view.context(), view.logic_thread, view.game_state)
    session.start_play()
    before = counts()

    surface = _enter_native(session)
    assert (view.renderer, view.context(), view.logic_thread, view.game_state) == editor_side
    assert surface.context is view.context(), "the window presents with the view's context"
    assert view.logic_thread.editor_state is world is window.state
    assert world.brushes is brushes and world.things is things
    start_height = drum.pos[1]
    session.step(240)                          # the game runs: the drum falls
    _native_frame(session, surface)
    assert counts() == before, "native kiosk made a second runtime object"
    fallen = drum.pos[1]
    assert fallen < start_height - 50.0, "the game did not run on the live world"

    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    # Back in the editor (Play still on): the same world, as the game left it.
    assert window.state is world and next(
        t for t in window.state.things if t.properties.get("id") == "drum") is drum
    assert drum.pos[1] == fallen
    assert (view.renderer, view.context(), view.logic_thread, view.game_state) == editor_side
    session.stop_play()


def _editor_pick(session):
    """Look straight down at the floor from 200 above with the editor camera,
    draw the editor frame in the widget, and pick the surface under the
    centre as the decal tool does: from the widget's own depth buffer, with
    GL made current on the widget outside a paint."""
    import glm
    view = session.view
    editor_camera = view.logic_thread.camera.get_editor_camera()
    editor_camera.pos = glm.vec3(0.0, 200.0, 0.0)
    editor_camera.yaw = 0.0
    editor_camera.pitch = -89.0
    session.publish()
    frame = session.paint()
    assert frame.std() > 1.0
    assert tuple(round(v, 3) for v in view.camera.pos) == (0.0, 200.0, 0.0), \
        "the editor frame was not drawn from the camera moved to"
    mx, my = view.width() // 2, view.height() // 2
    hit = view.decal_tool.surface_at(mx, my)
    # Where that pixel's ray meets the floor's top (y = 0), from the frame's
    # own matrices: the depth the widget wrote must put the pick there.
    scale = view.devicePixelRatioF()
    width, height = int(view.width() * scale), int(view.height() * scale)
    px, py = int(mx * scale), int(height - 1 - my * scale)
    inverse = glm.inverse(view.projection_matrix * view.view_matrix)

    def unproject(z):
        p = inverse * glm.vec4((px + 0.5) / width * 2.0 - 1.0,
                               (py + 0.5) / height * 2.0 - 1.0, z, 1.0)
        return np.array((p.x / p.w, p.y / p.w, p.z / p.w))
    near, far = unproject(-1.0), unproject(1.0)
    expected = near + (far - near) * (near[1] / (near[1] - far[1]))
    # How far apart along the ray two neighbouring depth values are at the
    # floor, from the widget framebuffer's own depth bits: a pick is exact
    # to the depth buffer's resolution at best.
    from OpenGL import GL
    view.makeCurrent()
    try:
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, view.defaultFramebufferObject())
        depth_bits = int(GL.glGetFramebufferAttachmentParameteriv(
            GL.GL_READ_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT,
            GL.GL_FRAMEBUFFER_ATTACHMENT_DEPTH_SIZE)) or 24
    except Exception:
        depth_bits = 16                      # the coarsest a widget is given
    finally:
        view.doneCurrent()
    clip = view.projection_matrix * view.view_matrix * glm.vec4(*expected, 1.0)
    z = clip.z / clip.w
    step = 2.0 / (2 ** depth_bits - 1)       # one depth value, in NDC
    quantum = float(np.linalg.norm(unproject(min(1.0, z + step)) - unproject(z)))
    # Rounding to the nearest depth value is off by half a step at most.
    _editor_pick.tolerance = max(0.5, quantum)
    _editor_pick.depth_bits = depth_bits
    if hit is None:
        # What the pick saw, for a platform where it found nothing.
        from OpenGL import GL
        view.makeCurrent()
        try:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, view.defaultFramebufferObject())
            depth = GL.glReadPixels(px, py, 1, 1, GL.GL_DEPTH_COMPONENT, GL.GL_FLOAT)
            depth_bits = GL.glGetFramebufferAttachmentParameteriv(
                GL.GL_READ_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT,
                GL.GL_FRAMEBUFFER_ATTACHMENT_DEPTH_SIZE)
            error = GL.glGetError()
        except Exception as exc:
            depth, depth_bits, error = None, None, repr(exc)
        finally:
            view.doneCurrent()
        _editor_pick.seen = dict(
            pixel=(px, py), size=(width, height), logical=(view.width(), view.height()),
            ratio=scale, depth=np.asarray(depth).ravel().tolist() if depth is not None else None,
            depth_bits=depth_bits, gl_error=error, fbo=view.defaultFramebufferObject(),
            samples=view.format().samples(), frame_mean=float(frame.mean()))
    return hit, expected


def test_the_editor_carries_on_after_native_kiosk(session):
    """Leaving native kiosk mode, the editor resumes at once -- the same
    renderer and the same world, nothing recreated or reloaded: selection,
    viewport navigation, a decal pick that reads the widget's depth buffer
    (finding the same floor point and normal as before kiosk mode), and
    Play / kiosk transitions as usual."""
    window, view = session.window, session.view
    renderer, context, world = view.renderer, view.context(), window.state
    session.stop_play()
    before, expected = _editor_pick(session)
    assert before is not None, \
        "no surface under the editor camera before kiosk mode: %r" % getattr(_editor_pick, 'seen', None)
    # The pick before kiosk mode is the platform's own accuracy. It must hit
    # the floor along the centre pixel's ray -- within 1% of the distance
    # from the eye -- and the pick after kiosk mode must be as good. (Mesa
    # puts it within 0.02; macOS's software renderer, with a 24-bit depth
    # buffer, 0.82 too deep at 200 away, the same every run.)
    eye = np.asarray(tuple(view.camera.pos), dtype=float)
    error_before = float(np.linalg.norm(np.asarray(before[0]) - expected))
    distance = float(np.linalg.norm(expected - eye))
    assert error_before <= 0.01 * distance, \
        (before[0], expected, distance, _editor_pick.depth_bits)
    tolerance = max(_editor_pick.tolerance, 2.0 * error_before + 0.1)

    session.start_play()
    session.step(5)
    surface = _enter_native(session)
    _native_frame(session, surface)
    window.exit_kiosk_mode(keep_play_mode=False, confirm=False)
    assert not view.play_mode and not window.is_kiosk_mode
    assert view.renderer is renderer and view.context() is context
    assert window.state is world

    floor = world.brushes[0]
    window.set_selected_objects([floor])
    assert window.primary_selection() is floor

    # The view is the editor's widget again (the editor layout comes back on
    # leaving kiosk mode, so its size, and the centre pixel's ray, change).
    after, expected = _editor_pick(session)
    assert after is not None, \
        "the decal pick found no surface in the widget's depth: %r" % getattr(_editor_pick, 'seen', None)
    point, normal = np.asarray(after[0]), np.asarray(after[1])
    # The floor's top is y = 0 (tests.helpers.worlds.room), facing up: the
    # depth read back from the widget puts the pick where the ray meets it.
    error_after = float(np.linalg.norm(point - expected))
    assert error_after <= tolerance, (point, expected, error_before, tolerance)
    assert abs(point[1]) < tolerance and normal[1] > 0.99, (point, normal)

    # Play again, on the widget, then native kiosk again, then back.
    session.start_play()
    session.step(5)
    assert session.paint().std() > 1.0
    surface = _enter_native(session)
    assert view.renderer is renderer
    _native_frame(session, surface)
    window.exit_kiosk_mode(keep_play_mode=False, confirm=False)
    assert not view.play_mode and view._native_surface is None
    assert view.renderer is renderer and window.state is world
    session.publish()
    assert session.paint().std() > 1.0
    _report("editor resumed: selection, navigation, decal depth pick (%d-bit depth; off by "
            "%.3f before kiosk mode, %.3f after), Play and kiosk again"
            % (_editor_pick.depth_bits, error_before, error_after))


def test_the_console_switches_native_presentation_live(session):
    """``r_nativekiosk on|off`` -- the [Kiosk] native_surface value, saved to
    settings.ini -- switches a running kiosk mode between the 3D view and the
    native window, by the same entry and exit paths."""
    window, view = session.window, session.view
    handler = window.console_handler
    window.config.set('Kiosk', 'native_surface', 'False')
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode and view._native_surface is None

    handler.handle_command('r_nativekiosk on')
    surface = view._native_surface
    assert surface is not None, "the console did not switch to the native window"
    assert window.config.getboolean('Kiosk', 'native_surface')
    with open(window.config_path, encoding='utf-8') as handle:
        assert 'native_surface = True' in handle.read(), "not saved to settings.ini"
    assert _pump(session.app, surface.isExposed)
    assert not window.isVisible()
    _native_frame(session, surface)

    handler.handle_command('r_nativekiosk off')
    assert view._native_surface is None and window.isVisible() and window.is_kiosk_mode
    assert not window.config.getboolean('Kiosk', 'native_surface')
    assert session.paint().std() > 1.0, "the 3D view does not draw again"

    handler.handle_command('r_nativekiosk toggle')
    assert view._native_surface is not None
    _report("console: r_nativekiosk switched a running kiosk mode both ways")


def test_events_while_the_window_is_created_do_not_touch_the_view(session, monkeypatch):
    """Windows delivers expose and resize while the native window is being
    created, before the constructor returns and before the view presents
    there (CI run #48: every native test logged an AttributeError from
    that, and the traceback kept the window alive). Such events, and any
    after presentation ended, leave the view alone."""
    import sys
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent, QWindow
    from PyQt5.QtWidgets import QApplication
    from engine.kiosk_surface import NativeKioskSurface
    view = session.view
    errors = []
    monkeypatch.setattr(sys, 'excepthook', lambda *info: errors.append(info))
    original_create = QWindow.create

    def create_with_early_events(window):
        original_create(window)
        QApplication.sendEvent(window, QResizeEvent(QSize(640, 100), QSize(1, 1)))
        QApplication.sendEvent(window, QEvent(QEvent.UpdateRequest))
        window.schedule_update()
    monkeypatch.setattr(QWindow, 'create', create_with_early_events)
    aspect = view._cached_aspect_ratio
    presented = []
    monkeypatch.setattr(type(view), '_present_native_frame', lambda self: presented.append(1))
    surface = NativeKioskSurface(view)
    session.app.processEvents()
    assert not errors, errors
    assert view._native_surface is None and not presented
    # The view keeps the widget's own aspect (a window manager may have
    # resized the editor window meanwhile), never the creating window's.
    from PyQt5.QtWidgets import QOpenGLWidget
    own = QOpenGLWidget.width(view) / max(1, QOpenGLWidget.height(view))
    assert view._cached_aspect_ratio != pytest.approx(640 / 100), \
        "a window not yet presenting resized the view"
    assert view._cached_aspect_ratio == pytest.approx(own, rel=0.02) or \
        view._cached_aspect_ratio == pytest.approx(aspect)
    surface.destroy()
    surface.deleteLater()


def test_a_context_qt_would_rebuild_on_show_keeps_kiosk_mode_on_the_3d_view(session, monkeypatch):
    """Where the view's context could not share with its window's (macOS:
    a core-profile view in a window of legacy format), Qt rebuilds the
    context -- and the renderer -- when the editor window is shown again.
    Native kiosk mode hides it and shows it on leaving, so it is refused
    there: kiosk mode stays on the 3D view, the same renderer throughout."""
    window, view = session.window, session.view
    assert view.context().shareContext() is not None, \
        "the test harness should run Fio's GL format as the default (engine/gl_format.py)"
    renderer, context = view.renderer, view.context()
    monkeypatch.setattr(type(view), '_context_survives_window_show', lambda self: False)
    window.config.set('Kiosk', 'native_surface', 'True')
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode and window.isVisible()
    assert view._native_surface is None, "native presentation started on a context Qt would rebuild"
    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert view.renderer is renderer and view.context() is context


def test_map_logic_cannot_switch_native_presentation(session):
    """A stored preference: map logic (logic_command) may not change it."""
    window = session.window
    assert 'r_nativekiosk' in window.console_handler.USER_ONLY_COMMANDS


def test_repeated_transitions_between_the_editor_and_both_kiosk_presentations(session):
    """Editor -> widget kiosk -> native kiosk -> widget kiosk -> native kiosk
    -> editor, twice over. At every step: the right target presents a real
    frame, the same renderer, context, logic thread and world run it, the
    widget's framebuffer and viewport are back whenever it presents again,
    and no native window outlives its turn."""
    import gc
    from PyQt5.QtWidgets import QOpenGLWidget
    from engine.kiosk_surface import NativeKioskSurface
    window, view = session.window, session.view
    same = (view.renderer, view.context(), view.logic_thread, view.game_state, window.state)

    def unchanged():
        assert (view.renderer, view.context(), view.logic_thread, view.game_state,
                window.state) == same

    def widget_presents(in_kiosk):
        assert view._native_surface is None and window.isVisible()
        assert window.is_kiosk_mode is in_kiosk
        ratio = view.devicePixelRatioF()
        size = (int(QOpenGLWidget.width(view) * ratio), int(QOpenGLWidget.height(view) * ratio))
        view.makeCurrent()
        try:
            state = _gl_state()
        finally:
            view.doneCurrent()
        assert state['framebuffer'] == _widget_fbo(view)
        assert state['viewport'] == (0, 0) + size, (state['viewport'], size)
        session.game_state.try_swap()
        assert session.paint().std() > 1.0
        unchanged()

    def native_presents():
        import weakref
        surface = view._native_surface
        assert surface is not None, "native presentation did not start"
        assert _pump(session.app, surface.isExposed)
        assert window.is_kiosk_mode and not window.isVisible()
        _native_frame(session, surface)
        unchanged()
        ids.append(id(surface))
        return weakref.ref(surface)

    surfaces, ids = [], []
    for cycle in range(2):
        session.step(3)
        window.config.set('Kiosk', 'native_surface', 'False')
        widget_presents(in_kiosk=False)                       # editor
        window.enter_kiosk_mode()
        widget_presents(in_kiosk=True)                        # widget kiosk
        assert window.set_kiosk_native_surface(True)
        surfaces.append(native_presents())                    # native kiosk
        assert not window.set_kiosk_native_surface(False)
        widget_presents(in_kiosk=True)                        # widget kiosk
        assert window.set_kiosk_native_surface(True)
        surfaces.append(native_presents())                    # native kiosk
        window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
        widget_presents(in_kiosk=False)                       # editor
        assert view.play_mode, "Play went on throughout"

    session.app.processEvents()
    from PyQt5.QtCore import QEvent
    session.app.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    live = [o for o in gc.get_objects() if isinstance(o, NativeKioskSurface)]
    assert not live and not any(ref() is not None for ref in surfaces), \
        "%d native windows outlived their turn" % len(live)
    assert len(ids) == 4
    _report("transitions: editor > widget kiosk > native > widget > native > editor, twice")
