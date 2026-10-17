# Android rebuild validation

Acceptance means: export `_SHOWCASE` using desktop Fio, load that exact
self-contained `.fiopak` through the real Android APK's document picker and
move forward and backward using real Android touch input. The Pixel 8a with
Android 17 has not been tested; its original startup crash is not diagnosed.

## Source scope

Relative to CANARY `5cd6175e2f5c1fd45726cf69aa46a1cd02a73858`, the rebuild
changes `player/**`, the new Android workflow, `editor/package_exporter.py`
and its package regression tests. **No engine source or other editor source
changes.** Desktop entry points, rendering, package playback, kiosk/settings
behavior, plugins and desktop workflows remain unchanged.

Android adapts a private build copy using `player/overlays/android-runtime.patch`.
The hook copies existing runtime definitions from the editor, removes Qt startup
dependencies in that copy and supplies compatibility aliases. A receipt records
15 input source hashes and staging verifies the originals remain unchanged.
These adapters are never applied to the desktop checkout.

The exporter now preserves map hierarchy, scans current asset references,
reads loose/category-archive assets and follows OBJ/MTL/glTF companions.
Implicit stock sprites, effects and audio are included in the exported world.
The Android APK does not install a stock-asset fallback. Strict verification
therefore detects missing world files instead of resolving them from desktop
resources. One regression test deletes its project before reading every
exported model/material/texture/sprite back from the world archive.

## Current checks

| Check | Result |
| --- | --- |
| Actual desktop editor + export dialog | PASS; no export warnings |
| `_SHOWCASE.fiopak` | 24,095,077 bytes; SHA-256 `28cf3f6ccc452f6122222bf892a42fd9461e5e29763e71e639cece0719efa80f` |
| Strict world asset verification and engine tick | PASS; 1 map, 31 referenced assets |
| Isolated Android runtime tests | 113 passed |
| Desktop exporter/package/player-start regression tests | 37 passed |
| Mesa GLES 3 | PASS: pixels, texture/model loading, terrain, level transitions, HUD/menu and context recreation |
| Development harness OpenGL 3.3 Core | Same checks passed; existing desktop renderer unchanged |
| ARM64 debug APK | Built; signature verifies; 144 native libraries pass 16 KiB alignment checks |
| ARM64 release APK | Built unsigned; 144 native libraries pass 16 KiB checks |
| x86_64 emulator debug APK | Built; 144 native libraries pass 16 KiB checks |
| Android workflow syntax | actionlint passed; GitHub execution not observed |
| Corrected-scope native acceptance | PASS on Android 16/API 36 x86_64: automatic picker import, exact hashes, stable spawn and forward/reverse touch movement |
| Corrected-scope lifecycle | PASS: background → native activity stop → foreground pause menu → Resume; same process/world/position, zero drift |

The actual APK rendered 85 brushes and 46 entities from the committed world.
The picker selection was fully automated. A stable idle spawn with no controller
input preceded real touch gestures: **314.05 horizontal units forward** and
**191.71 units of reverse progress**. Vertical falling is excluded from the
movement gate. Input logs show one touch with positive/negative axes, then zero
input after release. All before/forward/reverse screenshots were inspected.

| Pose | Position (x, y, z) |
| --- | --- |
| Stable spawn | -160, 146, 352 |
| After forward touch | -160, 2, 37.949 |
| After reverse touch | -160, 2, 229.661 |

World SHA-256 matches the archive imported into Android. The installed APK
SHA-256 matches the delivered emulator APK, and the installed runtime receipt
matches the build receipt. Re-exporting with the final desktop exporter also
produced the same 122 entry names and identical entry payloads (ZIP timestamps
may differ).

Current APK/world artifacts are under `/workspace/artifacts/android-only`.
Current native evidence is `/workspace/artifacts/android-scoped-emulator-v5`,
including `result.json`, `installed-apk-proof.json`, Fio-only logcat and PNGs.
The committed `player/_SHOWCASE.fiopak` is that exact tested archive; it is
excluded from the APK payload so testing exercises normal document import.
The local phone kit is `fio-pixel-test-kit-v5.zip`. Older root APKs, v4 kit and
broad shared-source report are superseded.

## Native debug findings

Reviewed defects included desktop-only Qt startup imports, omitted PyGLM/C++
runtime dependencies and GLES matrix transpose usage. The APK-only entrypoint,
recipes and renderer correct these. Native toolchain build issues included a
NumPy Git-tag mismatch and current pygame/SDL image recipe requirements.

Emulator debugging also found SDL OpenSL buffer-queue timeouts, a possible
GIL/activity-result deadlock when returning from the picker, and accelerometer
input exposed as an unintended joystick. Android now prefers AAudio with
AudioTrack fallback, forces `SDL_ANDROID_BLOCK_ON_PAUSE=0`, and disables
`SDL_ACCELEROMETER_AS_JOYSTICK` before SDL initializes. Logs distinguish input,
SDL/surface creation, renderer readiness and world loading.

These are demonstrated defects and fixes, **not a confirmed explanation of the
owner's Pixel crash**. The original failing APK and Pixel crash log have not
been examined. Cloud ADB connects only to the emulator, not USB devices on the
owner's computer.

The disposable emulator uses Android 16/API 36, x86_64 TCG and SwiftShader;
KVM is unavailable. CPU emulation makes boot and cutscenes slow. Android itself
has shown framework/System UI/provider ANRs. A prior Fio process also had a
focus-event ANR, so not every failure should be attributed to Android services.
Screenshots and final result JSON must be inspected before declaring success.

## Phone test and signing

Run `player/tools/test_device.py` locally with the supplied ARM64 debug APK and
world, after enabling USB debugging and accepting the computer's ADB key.
The script never uninstalls Fio or clears private data. It installs, opens the
picker, waits for stable idle input, sends real forward/reverse touch gestures,
checks imported world hashes and saves screenshots/logs/JSON. Release builds
ignore the explicit validation intent. See README for the command.

APK remains correct for direct distribution; Google Play generally requires
AAB. Production signing secret presence is unverified: metadata access returned
HTTP 403. No production key was created and no secret values were read.
`SIGNING.md` explains the four secrets, permanent keystore backup and signing.
The unsigned release APK cannot be installed until signed by its owner.

## Remaining limits

Android portal virtual views and advanced desktop water/glass/fog/effect passes
are not implemented. Shared gameplay does not imply complete visual parity.
Plugins must be installed with the APK; worlds cannot carry executable Python
plugins. Native PyOpenGL pixel-store probes have logged nonfatal invalid-enum
warnings despite passing strict host GLES checks. Pixel acceptance, sustained
lifecycle/performance testing and all native desktop release builds remain
unverified. No claim of complete cross-device certification is made.
