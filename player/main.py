"""Entry point for the Fio Player.

Desktop (development harness)::

    python -m player.main path/to/game.fiopak
    python -m player.main --width 1600 --height 900 game.fiopak

Android:
    python-for-android runs this module as the app's entry point. When no
    package path is given, the player looks for a bundled ``game.fiopak`` next
    to the app (the recommended single-game shipping layout for Google Play), or
    for one passed in via the launch intent / ``FIO_PACKAGE`` env var.
"""

from __future__ import annotations

import argparse
import os
import sys

from .runtime.fiopak import FioPackage
from .platform.base import HostConfig
from python_runtime import require_supported_python


def _default_package_path() -> str | None:
    # 1. Explicit env override (set from an Android intent handler).
    env = os.environ.get("FIO_PACKAGE")
    if env and os.path.exists(env):
        return env
    # 2. A package bundled alongside the app (single-game Play Store build).
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(here), os.getcwd()):
        candidate = os.path.join(base, "game.fiopak")
        if os.path.exists(candidate):
            return candidate
    return None


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fio Player — play .fiopak packages")
    parser.add_argument("package", nargs="?", help="path to a .fiopak file")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=60)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--fullscreen", action="store_true")
    window.add_argument("--windowed", action="store_true", help="override fullscreen kiosk play")
    window.add_argument("--borderless", action="store_true", help="desktop-sized borderless window")
    parser.add_argument("--no-vsync", action="store_true")
    parser.add_argument("--touch-controls", action="store_true")
    return parser.parse_args(argv)


def is_android():
    return bool(os.environ.get("ANDROID_ARGUMENT")) or "android" in sys.platform


def _main(argv=None) -> int:
    require_supported_python()
    from .app import FioPlayerApp
    args = parse_args(argv)

    package_path = args.package or _default_package_path()
    package = None
    if package_path:
        # The outer handler offers a native report for startup package errors.
        package = FioPackage.open(package_path)
        print(f"[Fio Player] loaded '{package.title}' from {package_path}")
        print(f"[Fio Player] maps: {package.list_maps()}")
    else:
        # No game bundled/given: run an empty session so the app still starts
        # and shows the on-screen controls (useful for a bring-up test APK).
        print("[Fio Player] no .fiopak found — starting empty (controls + test view)")

    title = f"Fio Player — {package.title}" if package is not None else "Fio Player"
    config = HostConfig(
        title=title,
        width=args.width,
        height=args.height,
        target_fps=args.fps,
        fullscreen=is_android() or args.fullscreen or not (args.windowed or args.borderless),
        borderless=args.borderless and not is_android(),
        vsync=not args.no_vsync,
        show_touch_controls=is_android() or args.touch_controls,
    )

    from .diagnostics import device_validation_requested
    app = FioPlayerApp(package, config, validation=device_validation_requested())
    try:
        app.run()
    finally:
        if package is not None:
            package.close()
    return 0


def main(argv=None) -> int:
    # Select EGL before the first PyOpenGL import on Android.
    if is_android():
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        # SDL 2 otherwise prefers OpenSL ES, whose legacy buffer queue can
        # stall on current Android audio services. AAudio is available from
        # API 26; AudioTrack remains the fallback for our API 24 minimum.
        os.environ.setdefault("SDL_AUDIODRIVER", "aaudio,android")
        # pygame.event.get() pumps SDL while holding Python's GIL. SDL's
        # default blocking Android pause can then prevent Java activity-result
        # callbacks from acquiring that GIL, so the document picker never
        # returns. Our lifecycle loop already yields while backgrounded.
        os.environ["SDL_ANDROID_BLOCK_ON_PAUSE"] = "0"
        # SDL exposes the gravity sensor as the first joystick by default.
        # Reading it as a gamepad produces constant movement and can cancel
        # touch input. Tilt control is not part of the player's input scheme.
        os.environ["SDL_ACCELEROMETER_AS_JOYSTICK"] = "0"
    try:
        return _main(argv)
    except Exception as exc:
        from .diagnostics import show_error
        if not is_android():
            show_error(exc)
            return 1
        from .platform.documents import AndroidDocuments
        import time
        documents = AndroidDocuments()
        documents.fatal = True
        show_error(exc, documents)
        # Keep the native report-export activity alive even if SDL/GL failed.
        try:
            while not documents.finished:
                documents.drain(lambda path: None, lambda error: print(error))
                time.sleep(0.1)
        finally:
            documents.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
