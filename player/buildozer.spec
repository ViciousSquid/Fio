# Buildozer configuration for the Fio Player (python-for-android).
#
# Build a debug APK from the repository root:
#     cd player && buildozer -v android debug
# The resulting APK lands in player/bin/.
#
# Build an unsigned release APK with: buildozer -v android release
# See player/SIGNING.md to configure CI signing or sign it locally.
#
# Local recipes pin pygame-ce, build PyGLM, and fetch checksum-verified upstream
# FreeType/dlg and OpenSSL sources through GitHub for restricted cloud builds.

[app]
title = Fio Player
package.name = fioplayer
package.domain = org.vicioussquid.fio

# Ship the player package and the engine runtime it reuses, the plugin system,
# plus a bundled game. `source.dir` points at the repo root so `player/`,
# `engine/` and `plugins/` are all packaged. Including `plugins/*` bakes the
# plugin code + assets (e.g. the tidy object model) into the APK, so plugin
# games run on-device. A .fiopak never carries plugin code; one that tries is
# refused by player/fiopak.py.
source.dir = ..
source.include_exts = py,png,jpg,jpeg,tga,bmp,ogg,wav,json,fiopak,glsl,vert,frag,obj,glb,gltf,bin,mtl,ttf,otf,gif,mp3,flac
# Exclude the desktop-only editor and its heavy PyQt5 dependency. The repo-root
# main.py in the PRIVATE BUILD COPY is replaced by android_entrypoint.py.
# The repository's desktop main.py is left unchanged.
# World assets stream from .fiopak. Shipping the entire desktop assets tree
# makes p4a extract editor resources and duplicate model/audio packs at launch.
# The icon/presplash below are copied separately as Android build resources.
source.exclude_dirs = assets, packages, editor, tools, maps, tests, audit, docs, wiki, .git, .github, player/tests, player/bin, player/.buildozer, player/p4a-recipes
source.exclude_patterns = editor/*, settings.ini, Dockerfile, docker-compose.yml, player/_SHOWCASE.fiopak

version = 3.0.0

# Runtime dependencies. hostpython3/python3 come from p4a.
#   pygame  -> SDL2 window, GLES surface, touch (FINGER*), controllers, audio
#   numpy   -> p4a fetches Git tags, including the leading v
#   pyopengl-> GL ES entry points
#   pillow  -> texture decoding from streamed asset bytes
requirements = python3,numpy==v2.5.3,pygame-ce==2.5.8,pyopengl==3.1.10,pillow==11.3.0,pyglm==2.8.3,pyjnius,android

orientation = landscape
fullscreen = 1

# The hook installs the APK-only entry point and engine compatibility adapters.
android.entrypoint = org.kivy.android.PythonActivity
p4a.hook = ./android_hooks.py

# --- CPython 3.14 Android toolchain ---
# Pin the stable p4a release used for the Fio Android baseline. The pygame-ce
# recipe is supplied locally because the upstream recipe is not pinned to the
# exact desktop baseline version.
p4a.branch = master
p4a.commit = 58d21141f17c889bf8585f5665921d72028f8831
p4a.local_recipes = ./p4a-recipes
android.ndk = 29

# --- Android platform ---
# Android 7.0+, with a GPU/driver supporting OpenGL ES 3.0. Buildozer does
# NOT strip inline comments from value lines, so keep comments on their own line.
android.api = 36
android.release_artifact = apk
android.accept_sdk_license = False
android.minapi = 24
android.ndk_api = 24
# arm64-v8a covers essentially all modern phones and keeps CI fast. Add
# armeabi-v7a for older-device reach (roughly doubles build time).
android.archs = arm64-v8a
android.allow_backup = 0
p4a.bootstrap = sdl2

# --- Presentation ---
presplash.filename = %(source.dir)s/assets/splash.png
icon.filename = %(source.dir)s/assets/logo.png

[buildozer]
log_level = 2
warn_on_root = 1
