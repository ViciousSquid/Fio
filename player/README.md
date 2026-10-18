# Fio Android Player

Create a world in desktop Fio, export a self-contained `.fiopak`, and open it in
the Android player. A world can contain one level or linked levels. The archive
format is shared with the existing Windows x64/ARM64, macOS and Linux desktop
releases; Android reads its assets directly from the ZIP archive.

- [Installable ARM64 debug APK](fio-player-arm64-debug.apk)
- [Desktop-exported `_SHOWCASE.fiopak`](./_SHOWCASE.fiopak)
- [Release signing instructions](SIGNING.md)
- [Validation results and outstanding checks](VALIDATION.md)

The committed APK is a signed **debug/test build**. `_SHOWCASE.fiopak` is a
separate download and is excluded from the APK. The current renderer has host
GLES coverage, but the Pixel 8a running Android 17 has not been device-tested.
Complete desktop visual parity is still a work in progress.

## Android system requirements

| Component | Requirement |
| --- | --- |
| Operating system | Android 7.0 / API 24 or newer: the configured minimum, not a tested certification for every Android version |
| Processor | 64-bit ARM with the `arm64-v8a` ABI for the phone APK; 32-bit ARM is not included |
| Graphics | OpenGL ES 3.0 or newer and a working device GLES driver |
| Display and input | Landscape display with multitouch; the app runs fullscreen |
| Storage | Space for the installed app, original archives and up to two private copies of a world during replacement/import |
| Memory | Depends on world geometry, decoded textures, models and effects; no fixed RAM minimum has been benchmarked |
| Package access | Android's system document picker must be available; broad storage permission is unnecessary |

A recent ARM64 device with **4 GB or more RAM and at least 1 GB free storage**
is a practical starting recommendation, not a measured minimum or a performance
guarantee. Large worlds require more. The current phone APK is about 33 MiB
before Android installs and unpacks its runtime.

The separate **x86_64 debug APK** is for Android emulator testing. The phone APK
does not run on an x86_64 emulator. All packaged native libraries are checked
for 16 KiB page compatibility. That packaging check does not certify a phone's
graphics driver or performance.

Android 16 / API 36 has been exercised on the cloud's x86_64 software emulator.
Android 7.0 is the build minimum; the owner's Pixel 8a / Android 17 remains an
outstanding acceptance target. The app targets API 36. Python and its runtime
dependencies ship inside the APK; users do not install Python, Qt or ADB to play.

## Install and open a world

1. Download the ARM64 debug APK to the phone and open it. If Android asks,
   allow the browser or Files app to install from that source.
2. Copy `_SHOWCASE.fiopak`, or your own exported world, to the phone or another
   location accessible through Android's document picker.
3. Launch Fio Player, choose **Open world**, and select the `.fiopak`.
4. Wait for import and loading to finish. `_SHOWCASE` begins with a cutscene;
   movement becomes available when gameplay resumes.

Imports are copied to the app's private storage, with a **1 GiB package-size
limit**. Required assets are then read from ZIP entries; they are not unpacked
as a loose asset tree. The imported copy remains available to the running app
after the picker closes. Uninstalling the app removes its private files, so
keep the original world archive somewhere outside the app.

The Android player provides playback; authoring and package export take place
in desktop Fio. Playing a self-contained, transferred world does not require
an internet connection or USB debugging.

### Controls

| Control | Action |
| --- | --- |
| Drag on the left side | Move using the floating movement stick |
| Drag on the right side, away from buttons | Look around |
| FIRE | Fire the held weapon |
| JUMP / CROUCH / SPRINT | Use the corresponding movement action |
| USE | Interact |
| Pause button | Open the pause menu |
| Resume / Open world / Exit | Continue, choose another archive, or leave the player |

Simulation pauses while the app is backgrounded. After returning, use **Resume**
if the pause menu is displayed. Fullscreen presentation is the Android default;
the existing desktop renderer and its kiosk/window settings remain unchanged.

## Export portable worlds from desktop Fio

1. Save the starting level and any linked levels in the project.
2. Choose **Export Game Package...** in desktop Fio.
3. Resolve missing-map, cutscene or asset warnings before distributing the
   resulting `.fiopak`.
4. Transfer the same archive to the Android player or an existing desktop Fio
   release. No Android-specific texture conversion is required.

The exporter preserves linked-map paths, reads loose files and category archives
(loose files take precedence), follows OBJ/MTL and glTF companions, and includes
implicit stock gameplay sprites, effects and audio. Android has no installed
stock-world-asset fallback to conceal missing package content.

Required Python plugins must already be installed with the APK. Worlds cannot
carry executable Python plugins. The Android reader currently limits a single
asset entry to **128 MiB**, cumulative cached asset bytes to **512 MiB**, and a
map/JSON document to **64 MiB**. These are reader limits, not RAM budgets:
decoded images, meshes, simulation state and GPU allocations consume additional
memory.

## Current playback and rendering

The player uses the existing engine's movement/collision, combat, AI, entity I/O,
moving brushes, cutscenes and linked-level simulation. Fixed simulation ticks and
AI run on the app thread. Playback includes audio, weapon and muzzle-flash HUD,
health/ammo display, touch controls, pause menus and GL context recreation.

Android renders through **OpenGL ES 3**. Existing desktop production rendering
continues through **OpenGL 3.3 Core Profile**. The player supports textured box
and convex brushes with authored UV transforms, animated sprites and effects,
OBJ/GLB models, point lights and terrain.

- `nodraw` and `caulk` faces emit no visible geometry. Bare names, image
  extensions, packaged paths and case variants are recognized. Other faces
  and the brush's collision bounds remain intact.
- North/south materials follow the desktop face convention. Brush back faces
  are culled; models and transparent billboards remain double-sided.
- Camera distance/frustum culling and radial fog use the engine's existing
  `ViewDistance` and frustum rules. The default distance is 4096 units; fog
  begins at 60% and becomes opaque at 92%. Terrain receives the same environment
  values; HUD and touch controls remain clear.
- World lighting reuses desktop ambient, diffuse falloff, radial shadow depth,
  slope bias and 20-sample shadow filtering. A uniform buffer supports the
  desktop budget of **64 lights**. Up to **eight shadow lights** use 384-pixel
  cube faces in an atlas, with reduced resolution on GPUs limited to smaller
  textures. Local fog uses the desktop shader and shared noise resource.
- Effect lights illuminate nearby geometry as well as drawing the effect's
  animated sprite. The authoritative engine projection supplies their colour,
  radius, enabled state and flicker.
- Lights beyond 4096 units from the player are excluded before selection.
  During cutscenes the cutoff follows the camera and retains lights whose
  influence still reaches visible geometry in front of opaque fog.

Portal virtual views and advanced desktop water/glass passes remain absent.
Shared simulation and matching lighting formulas do not establish complete
visual parity or equivalent performance on every device. See
[VALIDATION.md](VALIDATION.md) for the scope of the evidence.

## Build APKs with GitHub Actions

The [Android Player Build workflow](../.github/workflows/android-build.yml) is
configured to validate desktop package compatibility, export worlds, test the
private Android runtime, run GLES/Core pixel checks, build ARM64 debug/release
APKs, and build/test an x86_64 APK with real `_SHOWCASE` picker and touch input.
Cloud-local checks and configured CI jobs are distinct; a workflow definition
does not establish that a GitHub run succeeded.

Matching pushes to `CANARY` trigger the workflow automatically. Open **Actions →
Android Player Build**, select the latest `CANARY` run and inspect its jobs. After
adding signing secrets, **Re-run all jobs** on that run builds with the current
secret values.

The current default branch, `3.0.0.1010`, does not contain this workflow. GitHub
requires a workflow on the default branch for manual `workflow_dispatch`.
Once it is available there, **Run workflow → CANARY**, or this CLI command,
can start a new manual run:

```sh
gh workflow run android-build.yml --repo ViciousSquid/Fio --ref CANARY
```

Relevant artifacts are `fio-player-debug-apk`,
`fio-player-release-signed-apk` or `fio-player-release-unsigned-apk`,
`fio-player-emulator-debug-apk`, `desktop-showcase-world`, and
`android-showcase-device-evidence`.

Debug APKs are signed automatically and installable without release secrets.
An **unsigned release APK must be signed before installation**. Production
signing needs `FIO_ANDROID_KEYSTORE_BASE64`, `FIO_ANDROID_KEY_ALIAS`,
`FIO_ANDROID_KEYSTORE_PASSWORD` and `FIO_ANDROID_KEY_PASSWORD`.
Their presence and validity have not been verified from this cloud environment.
Follow [SIGNING.md](SIGNING.md) to create or reuse a permanent key, configure
secrets, sign locally, and verify the result. Keep the same signing identity for
future updates. A release signature cannot replace an installed debug build
signed with a different key.

**APK is the direct-download distribution format.** Google Play normally
requires an AAB; the current workflow produces APKs.

### Build locally on Linux

Install JDK 17 or newer, build-essential, autoconf, automake, libtool, libltdl-dev, pkg-config,
cmake, ninja, ccache, gettext/autopoint, patch, zip/unzip, and development headers
for zlib, libffi and OpenSSL. Use a host Python 3.12 virtual environment for the
CI-equivalent Buildozer toolchain.

On Ubuntu, `libltdl-dev` supplies the `LT_SYS_SYMBOL_USCORE` Autotools macro
required when python-for-android builds libffi. `libffi-dev` alone does not
supply it.

Install the Python build tools and build from `player/`:

```sh
python -m pip install buildozer==1.6.0 legacy-cgi setuptools 'cython==0.29.37'
cd player
APP_ANDROID_ACCEPT_SDK_LICENSE=True buildozer -v android debug
APP_ANDROID_ACCEPT_SDK_LICENSE=True buildozer -v android release
```

Build an emulator APK separately with
`APP_ANDROID_ARCHS=x86_64 APP_ANDROID_ACCEPT_SDK_LICENSE=True buildozer -v android debug`.
Do not run builds sharing the same private staging tree concurrently.

Outputs appear in `player/bin/`. The spec pins python-for-android, Android API
36 and NDK 29. It supplies Android CPython 3.14, pygame-ce 2.5.8, PyGLM 2.8.3
and the C++ runtime through the pinned/local recipes. First builds download
several GB; subsequent builds reuse native dependencies.

## Development and verification

Use a normal-GIL CPython 3.14 environment with `requirements.txt` and pytest for
host runtime checks. From the repository root:

```sh
python -m player.tools.contract_fixture /tmp/universal.fiopak
python -m player.tools.export_playback_fixture /tmp/_ANDROID_PLAYBACK.fiopak
QT_QPA_PLATFORM=offscreen python -m player.tools.export_showcase /tmp/_SHOWCASE.fiopak
python -m player.tools.stage_runtime /tmp/fio-android-runtime
```

Choose a fresh staging directory for each revision. Run tests in that private
tree so Android's adapters are exercised without changing desktop sources:

```sh
cd /tmp/fio-android-runtime
python -m pytest player/tests -q
python -m player.tools.verify_package /tmp/universal.fiopak
python -m player.tools.verify_package /tmp/_SHOWCASE.fiopak
PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless python -m player.tools.smoke_gles --fixture /tmp/universal.fiopak --evidence /tmp/fio-gles-images
PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless python -m player.tools.smoke_gles --desktop --fixture /tmp/universal.fiopak
python -m player.main /tmp/_SHOWCASE.fiopak
```

Mesa EGL/GLES is required for the graphics checks. The Core harness is a
development check and does not replace desktop Fio. Pixel checks cover nodraw,
back-face/distance culling, ambient colour, the 64th light, shadow bias and
occlusion, local/radial/density fog, effect illumination, animation and context
recreation. The showcase exporter automation uses the real desktop editor,
loader and export dialog; it automates the destination chooser.

Android imports the current engine, entities, I/O and plugins into Buildozer's
private tree. `android_hooks.py` applies `overlays/android-runtime.patch` there
and copies runtime entity modules without Qt startup dependencies.
`runtime-build.json` records 17 unchanged source/resource input hashes; staging
fails if the patch no longer matches. The repository's engine, desktop entry
point, plugins and desktop CI remain unchanged. The authorized editor change
is `editor/package_exporter.py` and its package regression coverage. The legacy
desktop reader `player/fiopak.py` is unchanged; Android uses
`player/runtime/fiopak.py`.

For a deliberate single-world developer build, `player/game.fiopak` or a
development `FIO_PACKAGE` path can supply a starting package. The committed
universal player instead uses the system picker.

## Phone testing and troubleshooting

Connecting USB to your computer does not expose the phone to this cloud
workspace. Install Android Platform Tools locally, enable USB debugging,
unlock the phone and accept the computer's debugging key. From the repository
root, with the APK and desktop export available:

```sh
adb devices
python -m player.tools.test_device player/fio-player-arm64-debug.apk player/_SHOWCASE.fiopak --output pixel-validation
```

Use `--serial SERIAL` for multiple devices or `--adb PATH` for a specific ADB
executable. The script installs without clearing data, opens the real picker,
waits for a stable spawn after the cutscene, sends forward/reverse touch
gestures, and saves screenshots, Fio logcat, the imported-world hash and JSON
results. It does not uninstall the app to bypass a signing-key conflict.
Validation logging requires an explicit test intent and a debuggable APK;
release builds ignore that flag.

For weapon pickup/HUD, effect and point-blank combat checks, run this separate
desktop-exported diagnostic world from the repository root:

```sh
python -m player.tools.test_playback player/fio-player-arm64-debug.apk /tmp/_ANDROID_PLAYBACK.fiopak --output fio-playback-results
```

Inspect screenshots alongside the JSON results. Python failures can offer a
native report-export dialog. For launch failures before Python starts, capture
`adb logcat -b crash -d` and a full `adb logcat -d` immediately after reproducing
the failure. Keep the APK/version, Android version and device model with the
report. Missing assets should be resolved in the desktop export, and signature
conflicts require the correct original signing key. See
[VALIDATION.md](VALIDATION.md) for known defects, completed checks and remaining
Pixel acceptance work.
