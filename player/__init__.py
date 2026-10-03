"""Fio Player — standalone Android runtime for .fiopak game packages.

This package is a slim, player-only front end for the Fio engine. It replaces
the PyQt5 desktop windowing layer (``editor/`` + ``engine/qt_game_view.py``)
with a lightweight SDL2/OpenGL-ES host that runs on Android (via
python-for-android / buildozer) and on the desktop as a development harness.

The existing runtime systems in ``engine/`` (map loading, entity system,
physics, AI, audio, gameplay logic) are reused largely unchanged; only the
windowing, input and graphics-API layers are player-specific.

Public surface
--------------
- :mod:`player.fiopak`        streaming ``.fiopak`` package reader
- :mod:`player.gles_shaders`  desktop GLSL 3.30 -> OpenGL ES 3.x translation
- :mod:`player.input`         touch + gamepad -> normalized input state
- :mod:`player.platform`      host abstraction, lifecycle, adaptive resolution
- :mod:`player.app`           the composed player application
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
