> Applies to Fio 3.1.0.0611 (CANARY).

## Requirements

- **CPython 3.14 or newer, with the GIL.** `python_runtime.require_supported_python()` stops start-up on anything else, and refuses free-threaded builds (3.14t) explicitly.
- **OpenGL 3.3 core** (the surface format `main.py` requests), 24-bit depth, 8-bit stencil.
- The pinned dependencies in `requirements.txt`:

```
PyQt5==5.15.11            (PyQt5-WinARM64==5.15.11.post1 on Windows ARM64)
numpy==2.5.3
Pillow==11.3.0
PyOpenGL==3.1.10
pygame-ce==2.5.8
PyGLM==2.8.3
```

```bash
python -m pip install -r requirements.txt
```

```bash
python main.py
```

On Linux the Qt/XCB and Mesa runtime libraries are needed; the list CI installs is in `.github/workflows/tests.yml`.

## What `main.py` sets up

- Clears `QT_PLUGIN_PATH` and `QT_QPA_PLATFORM_PLUGIN_PATH`.
- Sets `PYOPENGL_ERROR_CHECKING=0` unless `FIO_GL_DEBUG=1` (PyOpenGL otherwise calls `glGetError` after every GL call).
- Sets the thread switch interval to 1 ms: the UI/renderer, the logic thread and the monster AI are Python threads.
- Turns on background terrain builds (`Terrain.BACKGROUND_BUILDS`).
- Changes to its own directory: `assets/`, `maps/`, `cutscenes/` and `settings.ini` are resolved against it.
- Installs an exception hook so an error in a Qt callback is reported in the Debug Console instead of ending the process.
- With `ANDROID_ARGUMENT` or `FIO_PLAYER` set it starts the standalone player instead of the editor; that needs the staged Android runtime ([Fio Player (Android)](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android))), not a plain checkout.

## `settings.ini`

Read once at start-up from the install folder and written atomically. When the install folder is read-only (a translocated macOS app, a system location) the per-user copy is used instead: `~/Library/Application Support/Fio`, `%APPDATA%/Fio`, or `$XDG_DATA_HOME/Fio` (`editor/user_paths.py`).

| Section | Keys (shipped value) |
| --- | --- |
| `[Display]` | `show_caulk` (True), `sync_selection` (True), `click_select_3d` (True), `selection_transparency` (50), `show_connections` (True), `locked_not_selectable_2d` (True), `glow_arrow_scale` (100), `show_fps` (True), `always_show_sysmon` (True), `always_show_io_debug` (True), `disable_toasts` (False), `font_size` (11), `vsync` (True), `high_dpi_scaling` (True), `big_toolbar_buttons` (False), `animate_connections` (False), `show_hud` (True), `hudopacity` (100), `hudfade` (True), `place_camera_at_player_start` (True), `show_glasses` (False), `damage_indicators` (True), `show_whole_world` |
| `[Settings]` | `physics` (True), `save_mode` (`full`; `delta`, `both`), `restore_world_on_stop` (True) |
| `[Controls]` | `invert_mouse` (False), `middle_click_drag` (True), `p2_turn_sensitivity` (30) |
| `[Shortcuts]` | `key_show_connections` (F1), `key_toggle_wireframe` (F2), `key_sysmon` (F3), `key_play_mode` (F5) |
| `[Renderer]` | `shadows_enabled` (True), `shadow_map_size` (384), `occlusion_culling` (True), `lowpower_mode` |
| `[Kiosk]` | `window_mode` (Fullscreen), `res_width` (1280), `res_height` (720), `launch_in_editor` (False) |
| `[Editor]` | `property_editor_tooltips` (True), `toolbar_tooltips` (True), `autosave_enabled`, `autosave_interval` |
| `[KeyBindings]` | Console commands bound to keys with `bind`; key names are percent-encoded and `%` in a command is doubled. |
| `[Tooltips]` | `camera_movement_learned` |

The old `[Renderer] arm_mode` key is not read; use `lowpower_mode`.

## Repository layout

| Path | Contents |
| --- | --- |
| `main.py`, `python_runtime.py` | Entry point; interpreter check. |
| `editor/` | The PyQt5 editor: main window, views, property editor, console, I/O system, packaging. |
| `engine/` | The runtime: logic thread and its runtimes, physics, tables, terrain, and `engine/renderer/`. |
| `plugins/` | The plugin API and the shipped plugins (`deathmatch`, `bigworld`, `tidy`). |
| `player/` | The standalone player (Android). |
| `assets/`, `maps/`, `cutscenes/` | Stock content. |
| `tests/` | The test suite ([Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI)). |
| `tools/` | Debug Tables, benchmarks, oracles, `pack_assets.py`, `package_e2e/`. |
| `docs/` | Design records and audits. |
| `wiki/` | Replacement pages for this wiki. |

## Docker

`Dockerfile` builds an Ubuntu image with Python, Mesa, SDL2 and the XCB libraries; `docker-compose.yml` runs it against the host X server with `/dev/dri`. The compose file says so itself: it uses the host network and the host X11 socket and is **not** an isolation boundary.

## Release builds

`.github/workflows/build.yml` builds Windows, Linux and macOS with **Nuitka 4.2.2** in `--standalone` mode (`--output-filename=Fio`), including the `engine`, `editor`, `plugins` and `PIL` packages and the `assets`, `maps`, `cutscenes` and `tools` folders, `settings.ini` and `editor/version.txt`. The standalone player is deliberately left out of desktop builds (`--nofollow-import-to=player`); the package reader and export asset collector the editor needs are in the engine (`engine/fiopak.py`, `engine/package_assets.py`). `build-windows-arm64.yml` builds Windows ARM64. `macos_app.py` and `.github/macos-dmg/` make the macOS bundle and disk image. The Android APK is built by `android-build.yml` with Buildozer.

`clean.py` removes Python caches.
