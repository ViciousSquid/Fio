# Buildozer configuration for the Fio Player (python-for-android).
#
# Build a debug APK from the repository root:
#     cd player && buildozer -v android debug
# The resulting APK lands in player/bin/.
#
# For a Google Play release build (signed AAB), set the (android.release)
# options below and run:  buildozer -v android release
#
# NOTE ON NATIVE DEPS: numpy and pyopengl have maintained p4a recipes. PyGLM is
# C++ and has no upstream recipe yet — see player/README.md ("Native dependency
# strategy") for the two supported paths (a local PyGLM recipe, or the pure-glm
# math shim). Keep `requirements` in sync with whichever path you choose.

[app]
title = Fio Player
package.name = fioplayer
package.domain = org.vicioussquid.fio

# Ship the player package and the engine runtime it reuses, plus a bundled
# game. `source.dir` points at the repo root so both `player/` and `engine/`
# are packaged.
source.dir = ..
source.include_exts = py,png,jpg,jpeg,tga,bmp,ogg,wav,json,fiopak,glsl,vert,frag,obj,glb,mtl
source.include_patterns = player/*,engine/*,assets/*,game.fiopak
# Exclude the desktop-only editor and its heavy PyQt5 dependency. The repo-root
# main.py IS the entry point (it detects Android and launches the player), so it
# is intentionally NOT excluded.
source.exclude_dirs = editor, tools, maps, .git, player/tests, player/bin
source.exclude_patterns = editor/*, settings.ini, Dockerfile, docker-compose.yml

version = 0.1.0

# Runtime dependencies. hostpython3/python3 come from p4a.
#   pygame  -> SDL2 window, GLES surface, touch (FINGER*), controllers, audio
#   numpy   -> vector/matrix math and geometry batching (has a p4a recipe)
#   pyopengl-> GL ES entry points
#   pillow  -> texture decoding from streamed asset bytes
requirements = python3,pygame,numpy,pyopengl,pillow

orientation = landscape
fullscreen = 1

# Entry point: python-for-android runs `main.py` at source.dir (the repo root).
# That file detects ANDROID_ARGUMENT and calls player.main.main(), so the player
# launches on device while the same file still opens the editor on desktop.
android.entrypoint = org.kivy.android.PythonActivity

# --- Android platform ---
android.api = 34
android.minapi = 24               # Android 7.0 — GLES 3.x is universal by here
android.ndk_api = 24
# arm64-v8a covers essentially all modern phones and keeps CI fast. Add
# armeabi-v7a for older-device reach (roughly doubles build time).
android.archs = arm64-v8a
android.allow_backup = 1

# Controllers + immersive full-screen.
android.permissions =
android.features = android.hardware.gamepad

# GLES 3.x requirement advertised to the Play Store so incompatible devices are
# filtered out.
android.manifest_placeholders = glEsVersion=0x00030000

# --- Presentation ---
presplash.filename = %(source.dir)s/assets/splash.png
icon.filename = %(source.dir)s/assets/logo.png

[buildozer]
log_level = 2
warn_on_root = 1
