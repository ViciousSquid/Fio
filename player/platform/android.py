"""Android host: pygame/SDL2 with Android lifecycle + native surface.

python-for-android packages CPython + pygame (SDL2) into an APK/AAB. The SDL2
backend already delivers touch as ``FINGER*`` events and controllers through the
joystick API, so :class:`~player.platform.desktop.DesktopHost` does most of the
work. This subclass adds what is Android-specific:

* **Lifecycle events** — SDL raises ``APP_WILLENTERBACKGROUND`` /
  ``APP_DIDENTERBACKGROUND`` / ``APP_WILLENTERFOREGROUND`` /
  ``APP_DIDENTERFOREGROUND``. On background we halt sim + audio; the EGL surface
  is often destroyed, so on foreground we may need to rebuild GL resources.
* **Native full-screen surface** — the window uses the panel's resolution.
* **Immersive / wake-lock hints** — done via pyjnius when available.
"""

from __future__ import annotations

from .base import HostConfig, FrameCallbacks
from .desktop import DesktopHost


class AndroidHost(DesktopHost):
    """Desktop pygame loop + Android lifecycle wiring."""

    def __init__(self, config: HostConfig, callbacks: FrameCallbacks):
        # Force native full-screen; the panel size is discovered at surface
        # creation from the SDL display mode.
        config.fullscreen = True
        super().__init__(config, callbacks)

    def _configure_gl_attributes(self):
        pg = self._pg
        pg.display.gl_set_attribute(pg.GL_CONTEXT_MAJOR_VERSION, 3)
        pg.display.gl_set_attribute(pg.GL_CONTEXT_MINOR_VERSION, 0)
        pg.display.gl_set_attribute(pg.GL_CONTEXT_PROFILE_MASK, pg.GL_CONTEXT_PROFILE_ES)
        pg.display.gl_set_attribute(pg.GL_DEPTH_SIZE, self.config.depth_bits)
        pg.display.gl_set_attribute(pg.GL_STENCIL_SIZE, self.config.stencil_bits)
        pg.display.gl_set_attribute(pg.GL_DOUBLEBUFFER, 1)

    def run(self) -> None:  # pragma: no cover - requires device
        import pygame

        self._pg = pygame
        print("[AndroidHost] initializing SDL", flush=True)
        pygame.init()
        try:
            pygame.joystick.init()
        except Exception:
            pass

        self._configure_gl_attributes()
        # (0, 0) asks SDL for the native display resolution on Android.
        flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN
        print("[AndroidHost] creating OpenGL ES 3 surface", flush=True)
        self._surface = pygame.display.set_mode((0, 0), flags, vsync=int(self.config.vsync))
        self.width, self.height = self._surface.get_size()
        print(f"[AndroidHost] surface ready: {self.width}x{self.height}", flush=True)

        self._apply_android_window_flags()

        self.lifecycle.on_surface_created()
        self.callbacks.on_gl_ready(self)
        self.lifecycle.consume_gl_reload()
        self.callbacks.on_resize(self.width, self.height)
        print("[AndroidHost] renderer ready", flush=True)

        self._open_gamepads()
        self._running = True
        self._loop()

        self.callbacks.on_shutdown()
        pygame.quit()

    def _pump_events(self) -> None:  # pragma: no cover - requires device
        pg = self._pg
        for event in pg.event.get():
            et = event.type
            if et == getattr(pg, "FINGERDOWN", -1):
                self.ui_events.append((event.x * self.width, event.y * self.height))
            if et == pg.QUIT:
                self.request_quit()
            elif et == getattr(pg, "KEYDOWN", -1) and event.key == getattr(pg, "K_AC_BACK", -2):
                from ..input.state import ACTION_PAUSE
                self.input_state.set_button(ACTION_PAUSE, True)
            elif et == getattr(pg, "APP_WILLENTERBACKGROUND", -1):
                self._pause()
            elif et == getattr(pg, "APP_DIDENTERBACKGROUND", -1):
                # The GL surface is typically invalid past this point.
                self.lifecycle.on_surface_destroyed()
            elif et == getattr(pg, "APP_WILLENTERFOREGROUND", -1):
                pass
            elif et == getattr(pg, "APP_DIDENTERFOREGROUND", -1):
                self.lifecycle.on_surface_created()
                self._resume()
            elif et in (getattr(pg, "FINGERDOWN", -1), getattr(pg, "FINGERMOTION", -1)):
                self._pointers[event.finger_id] = (
                    event.x * self.width,
                    event.y * self.height,
                )
            elif et == getattr(pg, "FINGERUP", -1):
                self._pointers.pop(event.finger_id, None)
            elif et == getattr(pg, "VIDEORESIZE", -1):
                self._on_resize(event.w, event.h)
            elif et in (getattr(pg, 'JOYDEVICEADDED', -1), getattr(pg, 'JOYDEVICEREMOVED', -1)):
                self._open_gamepads()

    def _before_frame(self):  # pragma: no cover - requires device
        # The picker backgrounds SDL. Apply its result only after foreground
        # events and any context recreation, while GL uploads are safe. This
        # also prevents on_resume from immediately pausing a newly opened world.
        from ..diagnostics import show_error
        documents = getattr(self, 'documents', None)
        if documents is not None:
            documents.drain(self.on_open_package,
                            lambda exc: show_error(exc, documents))

    def set_mouse_captured(self, captured):
        pass

    def _read_keyboard_mouse(self) -> None:  # pragma: no cover
        # No hardware keyboard/mouse on a phone; touch + gamepad only.
        return

    def _apply_android_window_flags(self) -> None:  # pragma: no cover - device
        """Immersive full-screen + keep-screen-on via pyjnius, if present."""
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            View = autoclass("android.view.View")
            Params = autoclass("android.view.WindowManager$LayoutParams")

            @run_on_ui_thread
            def _ui():
                window = activity.getWindow()
                window.addFlags(Params.FLAG_KEEP_SCREEN_ON)
                decor = window.getDecorView()
                flags = (
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                )
                decor.setSystemUiVisibility(flags)

            _ui()
        except Exception as exc:  # pyjnius missing or API change — non-fatal
            print(f"[AndroidHost] window flag setup skipped: {exc}")
