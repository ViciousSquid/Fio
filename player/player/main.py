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

from .fiopak import FioPackage, PackageError
from .platform.base import HostConfig
from .app import FioPlayerApp, is_android


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
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--no-vsync", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    package_path = args.package or _default_package_path()
    package = None
    if package_path:
        try:
            package = FioPackage.open(package_path)
        except PackageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
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
        fullscreen=args.fullscreen or is_android(),
        vsync=not args.no_vsync,
    )

    app = FioPlayerApp(package, config)
    try:
        app.run()
    finally:
        if package is not None:
            package.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
