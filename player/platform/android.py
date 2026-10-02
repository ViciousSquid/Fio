"""Android host: pygame/SDL2 with Android lifecycle + native surface.

python-for-android packages CPython + pygame (SDL2) into an APK/AAB. The SDL2
backend already delivers touch as ``FINGER*`` events and controllers through the
joystick API, so :class:`~player.platform.desktop.DesktopHost` does most of the
work. This subclass adds what is Android-specific:

* **Lifecycle events** — SDL raises ``APP_WILLENTERBACKGROUND`` /
  ``APP_DIDENTERBACKGROUND`` / ``APP_WILLENTERFOREGROUND`` /
  ``APP_DIDENTERFOREGROUND``. On background we halt sim + audio; the EGL surface
  is often destroyed, so on foreground we may need to rebuild GL resources.
* **Native full-screen surface** — the window is created at the panel's own
  resolution (SDL reports it as the display mode); adaptive resolution then
  renders the scene into a smaller FBO.
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

    def run(self) -> None:  # pragma: no cover - requires device
        import pygame

        self._pg = pygame
        pygame.init()
        try:
            pygame.joystick.init()
        except Exception:
            pass

        self._configure_gl_attributes()
        # (0, 0) asks SDL for the native display resolution on Android.
        flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN
        self._surface = pygame.display.set_mode((0, 0), flags)
        self.width, self.height = self._surface.get_size()

        self._apply_android_window_flags()

        self.lifecycle.on_surface_created()
        self.callbacks.on_gl_ready(self)
        self.callbacks.on_resize(self.width, self.height)

        self._open_gamepads()
        self._running = True
        self._loop()

        self.callbacks.on_shutdown()
        pygame.quit()

    def _pump_events(self) -> None:  # pragma: no cover - requires device
        pg = self._pg
        for event in pg.event.get():
            et = event.type
            if et == pg.QUIT:
                self.request_quit()
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

    def _read_keyboard_mouse(self) -> None:  # pragma: no cover
        # No hardware keyboard/mouse on a phone; touch + gamepad only.
        return

    def _apply_android_window_flags(self) -> None:  # pragma: no cover - device
        """Immersive full-screen + keep-screen-on via pyjnius, if present."""
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            View = autoclass("android.view.View")
            Params = autoclass("android.view.WindowManager$LayoutParams")

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

            activity.runOnUiThread(_ui)
        except Exception as exc:  # pyjnius missing or API change — non-fatal
            print(f"[AndroidHost] window flag setup skipped: {exc}")
