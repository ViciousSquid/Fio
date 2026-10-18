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
17 input hashes (including the shared fog noise resource) and staging verifies
the originals remain unchanged.
These adapters are never applied to the desktop checkout.

The exporter now preserves map hierarchy, scans current asset references,
reads loose/category-archive assets and follows OBJ/MTL/glTF companions.
Implicit stock sprites, effects and audio are included in the exported world.
The Android APK does not install a stock-asset fallback. Strict verification
therefore detects missing world files instead of resolving them from desktop
resources. One regression test deletes its project before reading every
exported model/material/texture/sprite back from the world archive.

## Original movement/lifecycle acceptance

| Check | Result |
| --- | --- |
| Actual desktop editor + export dialog | PASS; no export warnings |
| `_SHOWCASE.fiopak` | 24,095,077 bytes; SHA-256 `28cf3f6ccc452f6122222bf892a42fd9461e5e29763e71e639cece0719efa80f` |
| Strict world asset verification and engine tick | PASS; 1 map, 31 referenced assets |
| Isolated Android runtime tests | 127 passed |
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

## Rendering and combat follow-up

The first native gate demonstrated loading, movement and resume. It did not
establish complete lighting, effects or weapon HUD correctness. The owner's
subsequent reports exposed omissions in the mobile renderer:

- Brush UVs previously forced one repeat per 128 world units and ignored authored
  Fit/Natural, scale, rotation and shift. Box bases and convex plane metadata now
  follow the desktop Surface Inspector mapping. Caulk/nodraw faces are omitted.
- Lighting previously kept only the first eight lights, omitted Effect lights,
  used a different ambient term and rendered no shadows. The player now consumes
  engine EntityTable projections, up to 32 contributing lights, engine ambient,
  and four point-light shadow maps in a GLES depth atlas.
- GIFs previously stayed on their first frame, and Effect entities had no draw
  batch. FIRE, ORB, CUSTOM and explosion sprites now consume engine effect state.
  GIFs preserve full resolution, frame durations, composition and transparency,
  retaining at most four decoded frames per animation rather than all frames.
- Weapon HUD and muzzle flash were never painted. The held item now comes from
  the authoritative combat state after real pickups, using packaged HUD assets.
- A ray starting inside a monster's hit sphere could reject its negative entry
  root. The APK-private combat adapter treats that as an immediate hit while
  retaining wall occlusion, weapon damage, ammunition and cooldown rules.
- Brush back faces, transformed AABB frustum tests, the XZ centre-distance cull,
  projection far plane and radial fog now follow existing engine rules. The
  literal engine fog shader block is reused, including density/color/ambient;
  terrain receives the same values. UI and alpha geometry keep their proper
  separate draw state.
- Lights farther than 4096 units from the player are removed before light/shadow
  selection. In cutscenes the cutoff follows the camera and retains lights whose
  influence can still reach geometry visible through fog.
- Buildozer's case-sensitive nested filename handling could include the committed
  uppercase `_SHOWCASE.fiopak` despite its exclusion pattern. Staging explicitly
  removes that fixture; APK inspection now rejects a bundled showcase/fallback.

Current host results: **127 player tests passed**. Real Mesa GLES and OpenGL Core
pixel checks passed for brush back faces, distance culling, radial/density fog,
active point-light shadow occlusion and animated Effect geometry. Existing
texture/model, terrain, level-transition, HUD/menu and context-loss checks passed.
These are host GPU checks. The committed `_SHOWCASE` also loaded in the new
host GLES pass with no missing images (19 lights, one shadow light, five effect
batches). The 98-frame 384x512 fire GIF matched sampled engine-decoded frames
exactly; its retained RGBA cache was 3 MiB versus 73.5 MiB for every frame.

The rebuilt APK passed real native diagnostic-world import, immediate weapon
pickup/HUD, 17-light/two-effect projection and point-blank firing: four 25-damage
pistol hits took the monster from 100 health to zero/dead. Captures show the
pistol, fire/orb and authored texture grid. Native movement retry passed in the same process/world: **193.57 horizontal
units forward and 91.27 reverse progress**, with neutral input after each gesture.
The original short-gesture attempt failed its movement threshold; that failure
is retained in native-v3. The host tool now scales gestures fully and waits for
a new, post-gesture neutral report, retaining the same 16/8-unit thresholds.
The Pixel has still not been ADB tested.

New APKs and hashes are in `/workspace/artifacts/android-render-fixes`, and
combat evidence is in `android-render-fixes-native-v3`, and the passing movement
retry is in `android-render-fixes-movement-v4`. The first retry was
blocked by an Android external-storage-provider background ANR; restarting the
picker/provider and disabling the disposable emulator's cached-app freezer
allowed normal automated filename selection. No phone settings were changed.
The installed APK hash and private runtime receipt match the rebuilt APK.

No `engine/**` or `editor/**` file changed in this follow-up. The private build
adapter includes the combat correction; desktop behavior remains unchanged.

## Nodraw surface follow-up

Android omits faces whose texture basename is `nodraw` or `caulk`, with or
without an image extension, regardless of case or slash direction. This applies
to box faces and authored convex planes. Only the marked faces are omitted;
the brush's collision bounds and materials remain intact. Omitted geometry
also stays out of the player's shadow batches. Similar names such as
`nodraw_bricks.jpg` continue to render.

The isolated player suite passes **141 tests**. Mesa GLES pixel checks verify
that bare and path-qualified nodraw surfaces reveal the background for both
box and convex geometry; existing rendering smoke checks also pass. ARM64 debug
and unsigned release APKs were rebuilt and checked for 16 KiB native-library
alignment. This follow-up has not been tested on the Pixel. No engine or editor
source changed.

## Lighting parity follow-up

The player now derives its world fragment shader from the canonical desktop
`textured.frag`, adapting the interface and shadow lookup for GLES. Ambient,
diffuse falloff, radial shadow depth, slope bias and 20-sample PCF use desktop
formulas. A std140 UBO supports the same 64-light budget without exceeding the
minimum GLES fragment-uniform capacity. The atlas supports eight shadow lights
at 384 px per face, reducing resolution on GPUs limited to 2048 px textures.
The 4096-unit light cutoff and cutscene camera exception remain intact.

The previous player reversed north/south box materials and omitted local fog
volumes. Both are corrected. Fog reuses the desktop shader and the same noise
bytes, copied into the private APK tree from `assets/noise_3d.bin`; world image,
model and audio assets still stream from the package. Desktop files are unchanged.

Real host GLES pixel checks cover the 64th light, authored ambient colour,
grazing-angle self-shadow rejection, actual occlusion, local fog viewed inside
and outside, and an ORB lighting nearby geometry while its sprite stays away
from the sampled pixel. The OpenGL Core development harness runs the same checks.

At the same exported `_SHOWCASE` spawn-room camera and a fixed effect flicker
phase, the unchanged desktop ForwardRenderer and GLES player both produce
**RGB (33, 50, 71)** at the sampled floor point. Full-scene and static-light
diagnostic images are labelled separately; independently timed animated captures
cannot establish pixel-exact parity. These are host GPU comparisons, not new
Pixel captures. Complete visual parity, phone driver behaviour and performance
remain unverified; the feature limits below still apply.

The first native APK of this follow-up (`9a30f562`) failed on the emulator:
SwiftShader counted the four-vec4-per-light UBO against a 256-vector fragment
uniform limit (261 active vectors). Mesa had accepted it. The replacement packs
radius into position.w and intensity into colour.w, retaining all 64 lights in
three vec4 slots each. This stays below the GLES minimum of 224 vectors while
preserving lighting values. Native error reporting also exposed a Pyjnius
CharSequence overload failure; dialog strings now use explicit Java String
objects. The failed native run is retained separately from the retry.

The compact-buffer replacement passed native Android 16/API 36 x86_64 startup
and real document-picker import. The installed APK SHA-256 matches the rebuilt
emulator APK (`22559cb6…`), and the imported diagnostic world hash matches the
desktop export. Its actual frame/report has **17 lights, one shadow light,
two effects, gun1 weapon/HUD and no missing images**. Quickstep and System UI
ANR dialogs on the software emulator blocked automatic picker inspection;
the assistant dismissed those system dialogs and tapped the visible exact
filename. Assertions and screenshot capture then passed in the same process.
This retry covers rendering/startup/import, not a new movement or full lifecycle
run. Evidence: `android-nodraw/native-v3/result.json` and `native-rendering.png`.

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

Android portal virtual views and advanced desktop water/glass passes
are not implemented. Shared gameplay does not imply complete visual parity.
Plugins must be installed with the APK; worlds cannot carry executable Python
plugins. Earlier native PyOpenGL pixel-store probes logged nonfatal invalid-enum
warnings; image uploads now bypass those desktop-only probes with raw GL calls. Pixel acceptance, sustained
lifecycle/performance testing and all native desktop release builds remain
unverified. No claim of complete cross-device certification is made.
