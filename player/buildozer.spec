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

# Ship the player package and the engine runtime it reuses, the plugin system,
# plus a bundled game. `source.dir` points at the repo root so `player/`,
# `engine/` and `plugins/` are all packaged. Including `plugins/*` bakes the
# plugin code + assets (e.g. the tidy object model) into the APK, so plugin
# games run on-device; a .fiopak that carries its own plugins still works too.
source.dir = ..
source.include_exts = py,png,jpg,jpeg,tga,bmp,ogg,wav,json,fiopak,glsl,vert,frag,obj,glb,mtl
source.include_patterns = player/*,engine/*,plugins/*,assets/*,game.fiopak
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

# --- Toolchain pin (IMPORTANT) ---
# python-for-android master builds CPython 3.14, but its bundled pygame recipe
# is still pygame 2.1.0, which cannot compile on Python >= 3.12 (fatal error:
# 'longintrepr.h' file not found). Pin p4a to the last release that builds
# CPython 3.10, where pygame 2.1.0 compiles cleanly, and pin the matching NDK.
p4a.branch = v2023.09.16
android.ndk = 25b

# --- Android platform ---
# minapi 24 = Android 7.0; GLES 3.x is universal by then. NOTE: buildozer does
# NOT strip inline comments from value lines, so keep comments on their own line.
android.api = 34
android.minapi = 24
android.ndk_api = 24
# arm64-v8a covers essentially all modern phones and keeps CI fast. Add
# armeabi-v7a for older-device reach (roughly doubles build time).
android.archs = arm64-v8a
android.allow_backup = 1

# NOTE: android.features (a manifest <uses-feature> hint) is intentionally not
# set — the pinned p4a v2023.09.16 does not accept p4a's --feature flag. Game
# controllers still work without advertising the feature; add it back only if
# you later move to a newer p4a that supports --feature.

# NOTE: android.manifest_placeholders is intentionally not set. p4a injects its
# value straight into a Groovy map in build.gradle, and the pinned p4a expects
# `key:value` (colon), not `key=value` — a stray entry there fails gradle
# evaluation ("Could not set unknown property"). The GLES 3.x <uses-feature>
# Play-Store hint isn't referenced by the manifest anyway; add it later via a
# proper manifest entry if you need Play filtering.

# --- Presentation ---
presplash.filename = %(source.dir)s/assets/splash.png
icon.filename = %(source.dir)s/assets/logo.png

[buildozer]
log_level = 2
warn_on_root = 1
