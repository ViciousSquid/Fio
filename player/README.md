# Fio Android Player

The SDL2/OpenGL ES 3 player runs Fio worlds exported as `.fiopak` ZIP archives.
Android builds use CPython 3.14, target API 36 and require Android 7.0+ with
OpenGL ES 3.0. The phone APK targets ARM64; CI also builds x86_64 for testing.

Installable ARM64 debug APK: [`fio-player-arm64-debug.apk`](fio-player-arm64-debug.apk).
This is a test build; the Pixel 8a/Android 17 still needs device validation.

The repository includes `player/_SHOWCASE.fiopak`, the actual desktop export
used for Android acceptance. Transfer that file to the phone and import it;
the example is excluded from the APK payload.

## Desktop → Android

1. Save the world in desktop Fio and choose **Export Game Package...**. Resolve
   missing-asset warnings before transferring the `.fiopak`.
2. Install the debug APK, launch Fio Player and select the package using
   Android's system document picker. Broad storage permission is unnecessary.
3. Walk with the left stick, look with the right side, and use the labeled
   action buttons. Android Back or the pause button opens Resume/Open world/Exit.

Imports are copied to private storage (1 GiB limit). Assets then stream from
ZIP entries rather than being extracted as loose files. A bundled
`player/game.fiopak` or development `FIO_PACKAGE` path is also supported.

## Desktop source remains unchanged

The Android build reads the existing CANARY engine, entities, I/O and plugins.
`android_hooks.py` adapts only Buildozer's private application copy using
`overlays/android-runtime.patch`. Runtime entity modules are copied from the
editor into that private engine, with lightweight compatibility imports and
no Qt dependency. `runtime-build.json` records the original source hashes.
The build fails if the overlay no longer matches the checked-out engine.
The repository's engine, desktop entry point, plugins and desktop CI remain
unchanged. The only editor change is `editor/package_exporter.py`, which now
collects a complete portable world archive; its package tests are updated too.

The legacy `player/fiopak.py`, which the desktop editor imports, stays unchanged.
The Android reader is separate in `player/runtime/fiopak.py`. Existing desktop
OpenGL 3.3 Core rendering, package extraction and fullscreen kiosk/settings
behavior therefore remain with the existing desktop implementation. Only the
Android app uses the new player renderer in production.

## Compatibility and limits

Android uses the existing engine's movement/collision, combat, AI, entity I/O,
moving brushes, cutscenes and linked-level simulation. Fixed ticks and AI run
on the app thread and pause in the background. The renderer supports textured
box/convex brushes with authored UV mapping, animated sprites/effects, OBJ/GLB
models, point lights and terrain. Audio, held-weapon/muzzle-flash and health/ammo
HUD, touch controls, pause menus and context recreation are included.
Portal virtual views and advanced desktop water/glass rendering
remain absent; Android does not yet have full desktop visual parity.

Brush back faces are culled; models and transparent billboards stay double-sided
as in the desktop renderer. Camera distance/frustum culling and radial fog use
the existing engine's `ViewDistance`, frustum helpers and fog shader block.
The default distance is 4096 units; fog starts at 60% and is opaque at 92%.
Terrain receives the same environment values. HUD and touch controls stay clear.
Lights beyond 4096 units from the player are excluded before selecting up to
32 lights and four point-light shadow maps. During cutscenes the cutoff follows
the camera; a light stays while its radius can still illuminate visible geometry
in front of opaque fog, avoiding a visible switch-off at the cutoff.

The desktop exporter preserves linked-map paths, reads loose files and category
archives (loose files override archive entries), scans current asset properties,
and follows OBJ/MTL and glTF external buffers/textures. Stock gameplay sprites,
effects and audio required implicitly by the engine are carried in the world
archive too. Android streams those bytes directly from the same `.fiopak`;
there is no installed-stock-asset fallback hiding missing package content.
The format remains the ZIP/metadata format accepted by existing desktop Fio.

Packages cannot supply executable Python plugins. Required plugins must already
be installed with the APK. Python errors can be exported through Android's
native document picker; crashes before Python starts require logcat.

## Development checks

Use CPython 3.14 with the normal GIL build and install `requirements.txt` plus
pytest. From the repository root, export fixtures using the exporter,
then test the same private runtime used by the APK:

```sh
python -m player.tools.contract_fixture /tmp/universal.fiopak
python -m player.tools.export_playback_fixture /tmp/_ANDROID_PLAYBACK.fiopak
QT_QPA_PLATFORM=offscreen python -m player.tools.export_showcase /tmp/_SHOWCASE.fiopak
python -m player.tools.stage_runtime /tmp/fio-android-runtime
cd /tmp/fio-android-runtime
python -m pytest player/tests -q
python -m player.tools.verify_package /tmp/universal.fiopak
python -m player.tools.verify_package /tmp/_SHOWCASE.fiopak
PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless python -m player.tools.smoke_gles --fixture /tmp/universal.fiopak
python -m player.main /tmp/_SHOWCASE.fiopak
```

Choose a fresh staging directory for each source revision. Verification fails
when referenced assets or model companions are absent from the world archive.
The desktop-export automation uses the real editor, loader and export dialog;
only the destination file chooser is automated. Mesa EGL/GLES is required for
the graphics check. `--desktop` tests the development harness with OpenGL Core;
it does not replace desktop Fio.

The GPU smoke check asserts rendered pixels for back-face/distance culling,
linear/density fog, real point-light shadow occlusion and animated effects.
Pass `--evidence /tmp/fio-render-images` to save those images. The additional
native gameplay check uses a desktop-exported diagnostic world with an immediate
gun pickup, point-blank monster, more than eight lights and animated effects:

```sh
python -m player.tools.test_playback player/fio-player-arm64-debug.apk /tmp/_ANDROID_PLAYBACK.fiopak --output fio-playback-results
```

This supplements the real `_SHOWCASE` import/movement check. Inspect the saved
HUD/effect and firing screenshots as well as the reported health/death result.

## APK builds and signing

The **Android Player Build** GitHub workflow exports worlds, checks the isolated
runtime, builds debug and release APKs, checks native 16 KiB compatibility and
runs real showcase touch acceptance on an emulator. Download the debug APK for
testing. Release signing is optional in CI; an unsigned release APK cannot be
installed. See [SIGNING.md](SIGNING.md) for the four repository secrets,
permanent keystore creation/backup and local signing instructions. APK is the
appropriate format for direct downloads; Google Play normally requires AAB.

On Linux install JDK 17+, build-essential, autoconf/automake/libtool, pkg-config,
cmake, ninja, ccache, gettext/autopoint, patch, zip/unzip and development headers
for zlib, libffi and OpenSSL. In a host Python 3.12 virtual environment:

```sh
python -m pip install buildozer==1.6.0 legacy-cgi setuptools 'cython==0.29.37'
cd player
APP_ANDROID_ACCEPT_SDK_LICENSE=True buildozer -v android debug
APP_ANDROID_ACCEPT_SDK_LICENSE=True buildozer -v android release
```

Pinned python-for-android supplies Android CPython 3.14. Local recipes package
pygame-ce 2.5.8, PyGLM 2.8.3 and the required C++ runtime. The initial native
build downloads several GB; incremental packaging reuses native libraries.

## Phone acceptance

Connecting USB to your computer does not expose the phone to this cloud machine.
Install Android Platform Tools locally, enable USB debugging, unlock the phone
and accept its debugging key. With the supplied debug APK and desktop export:

```sh
adb devices
python -m player.tools.test_device fio-player-arm64-debug.apk _SHOWCASE.fiopak --output pixel-validation
```

Use `--serial SERIAL` for multiple devices and `--adb PATH` if necessary. The
script installs without clearing data, opens the picker, waits for a stable
spawn after the cutscene and sends actual forward/reverse touch gestures. It
saves screenshots, Fio logcat, package hash and a JSON result. It never uninstalls
the app to work around signing conflicts. Pose logging requires its explicit
validation intent and a debuggable APK; release builds ignore that flag.
See [VALIDATION.md](VALIDATION.md) for results and remaining device checks.
