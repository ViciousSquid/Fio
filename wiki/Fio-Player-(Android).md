> Applies to Fio 3.5.0.1111.

The **Fio Player** (`player/`) plays exported [`.fiopak`](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive) worlds on Android. Authoring and export happen in desktop Fio; the player only plays. App version: **3.5.0** (`player/buildozer.spec`, `player.__version__`).

## Requirements (from `player/README.md`)

| | |
| --- | --- |
| OS | Android 7.0 / API 24 or newer (the configured minimum); targets API 36 |
| CPU | 64-bit ARM, `arm64-v8a`. A separate x86_64 debug APK exists for the emulator. |
| Graphics | OpenGL ES 3.0 or newer |
| Display | Landscape, multitouch, fullscreen |
| Permissions | `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, `CHANGE_WIFI_MULTICAST_STATE` (LAN play); worlds are opened through the system document picker |

Python and its dependencies ship inside the APK.

## Shared engine, private adapters

The player runs Fio's own simulation: `player/world.py: PlayerWorld` hosts a `LogicThread` without Qt and steps it at 60 Hz, with the monster AI stepped by the same owner so nothing ticks a paused app. The APK is built from a **staged copy** of the engine (`player/tools/stage_runtime.py`): the checked-out engine is copied, the modules in `RUNTIME_MODULES` (`editor/things.py`, `io_system.py`, `io_handlers.py`, `state_values.py`, `portal_links.py`) are staged as `engine.*` with thin `editor.*` aliases, and `player/overlays/android-runtime.patch` is applied to `ENGINE_ADAPTERS` (`logic_combat`, `logic_thread`, `logic_session`, `logic_player`, `threaded_game_state`, `glb_loader`, `obj_loader`, `logic_collision`). The patch must apply with no fuzz, so an engine change that moves one of those hunks fails the build until the adapter follows. Desktop sources are never modified. The player therefore cannot be run from a plain checkout.

## Loading a world

- `engine/fiopak.py: FioPackage` reads the archive in place (the reader is shared with the desktop editor; `player.runtime.fiopak` is an alias of it). Limits: 128 MiB an asset entry, 512 MiB of cached assets, 64 MiB a map or JSON document; an import is copied to private storage with a 1 GiB limit. A package that contains plugin code is refused: plugins must be installed with the APK.
- Assets resolve exactly, with `assets/` added or removed, in the conventional sub-folders, then by basename. `member()` and `load_json()` answer the engine's own package calls (packaged cutscenes).
- Each brush record is brought to the engine's in-memory layout on load, as the desktop does: play state dropped, an angled face's material on its plane, and planes moved into the brush's own frame (packages carry map version 4, with world-space planes).
- Level changes resolve inside the package (`resolve_map`), carrying the loadout.

## Renderer

`player/render/renderer.py: GLESRenderer` is the player's own renderer. Desktop GLSL 3.30 sources are translated to GLSL ES 3.00 (`player/gles_shaders.py`). It reads the engine's `EntityTable` for entities and lights (`player/render/projection.py`), builds brush batches with the engine's geometry functions (`player/render/scene.py`), and draws terrain through the engine's `TerrainGL` with its programs compiled for the host (`player/render/terrain_gl.py`). Quality levels: `mobile` and `reference`, with adaptive resolution. A lost GL context drops GPU state and rebuilds it from retained data.

Not implemented (`player/VALIDATION.md`): portal virtual views and the advanced desktop water and glass passes. Shared gameplay does not imply complete visual parity.

## Input, menus, plugins

Touch controls (`player/input/touch.py`) and gamepads (`player/input/gamepad.py`) feed `InputState`; Android Back opens the pause menu or steps out of a plugin page. The APK ships the same plugins as desktop -- [Deathmatch](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin) (LAN included), [BigWorld](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin) and [Tidy](https://github.com/ViciousSquid/Fio/wiki/Tidy-Plugin) -- which run unchanged on the shared engine through `player/plugin_host.py`; their screens are drawn by `player/plugin_ui/` (lobby, arena, scoreboard, menus, BigWorld panel).

## Building and testing

`.github/workflows/android-build.yml`:

| Job | Proves |
| --- | --- |
| `validate` | Desktop export of a contract world and the showcase; staging; `player/tests`; `verify_package` (reader, every referenced asset, a simulation step); `smoke_gles` in desktop-GL, GLES and mobile-quality modes under Mesa. |
| `apk` | Debug and release APKs with Buildozer (`player/buildozer.spec`, pinned recipes in `player/p4a-recipes/`), native libraries checked. |
| emulator | The x86_64 APK installed on an Android emulator; the showcase opened and moved through with touch input (`player/tools/test_device.py`). |

On a development host:

```bash
python -m player.tools.stage_runtime /tmp/fio-android
```

```bash
cd /tmp/fio-android && PYOPENGL_PLATFORM=egl SDL_AUDIODRIVER=dummy python -m pytest player/tests -q
```

`tools/package_e2e/` runs a world built in the editor through a pristine desktop install and this staged runtime and compares the engine's dense tables ([Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI)).
