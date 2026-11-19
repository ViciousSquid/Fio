# Native kiosk presentation

`[Kiosk] native_surface = True` makes kiosk mode present frames on a native window instead of through the editor's 3D view. It is off by default, and the shipped `settings.ini` says so.

There are three ways to set it:

* Settings > Play Modes, "Present the game on its own window".
* The console: `r_nativekiosk on|off|toggle`. This saves the value, and switches a running kiosk mode at once. It is user-only: map logic cannot change it.
* `settings.ini` directly.

The widget path stays the default until measurements on real hardware (the Snapdragon target) justify a change. Even then it stays a choice per machine: on a platform where native presentation is no faster, the widget is simply the better presentation there.

## What it is, and what it is not

Fio's editor is the runtime. The world being played is the world being edited: one renderer, one GL context, one `LogicThread`, one `EditorState`, with the editor's own entities and resources. Native presentation keeps all of that. **It changes only the surface frames are presented on.**

* There is no second renderer, GL context, runtime, game state or copy of the world.
* `RenderState` publication, paint-on-publish and the fixed 60 Hz simulation are unchanged.
* What the game does to the world is there in the editor when kiosk mode ends: the same objects.

`tests/visual/test_native_kiosk_presentation.py::test_native_kiosk_is_only_a_presentation_surface` holds this invariant. It checks that:

* the renderer, context, logic thread, publication and world objects are identical during native kiosk mode;
* no second instance of any of them exists;
* a physics prop the game drops is where the game left it, back in the editor.

**Two arrangements, not a framework.** Fio presents in exactly two ways: the editor's widget, and in kiosk mode optionally one native window. Both are owned by the existing runtime (`QtGameView`, `MainWindow`). There is no general multi-window presentation layer, and none is intended.

**Repeated transitions.** Editor → widget kiosk → native kiosk → widget kiosk → native kiosk → editor, twice over, is a test of its own (`test_repeated_transitions_between_the_editor_and_both_kiosk_presentations`). At every step it checks four things:

* the right target presents a real frame;
* the same runtime objects run it;
* the widget's framebuffer and viewport are back whenever it presents again;
* no native window outlives its turn.

**The editor carries on.** When kiosk mode ends, the editor resumes at once, with no renderer recreated and no world reloaded. Selection, viewport navigation, decal operations (GL work on the widget outside a paint) and Play / kiosk transitions all continue. `test_the_editor_carries_on_after_native_kiosk` checks this.

## Why

In the editor (and in kiosk mode by default) the 3D view is a `QOpenGLWidget`. Fio renders into the widget's framebuffer object, and Qt then composites that image into the window on every frame. That is an extra full-screen pass, a copy, and a frame of latency. A native window removes the composition: Fio renders straight into the window's back buffer and swaps.

## How

| Piece | Where |
|---|---|
| The native window: a `QWindow` with no GL objects of its own; compatibility check; `present()`; input forwarding; window modes | `engine/kiosk_surface.py` (`NativeKioskSurface`) |
| Presentation target | `engine/qt_game_view.py`, "PRESENTATION TARGET" |
| Kiosk entry/exit, fallback, window modes | `editor/main_window.py` (`_begin_native_kiosk`, `exit_kiosk_mode`, `kiosk_native_presentation_failed`) |
| Instrumentation | `PresentationStats` (`engine/kiosk_surface.py`), `r_presentation`, `tools/bench_kiosk_presentation.py` |

**Presentation target.** While the native window presents, the view's geometry (`width`, `height`, `rect`, `devicePixelRatioF`, `mapToGlobal`), focus, mouse capture and repaint requests (`update`) answer for the window. The paint path, HUD, picking and mouse look read those from the view, so they follow the target unchanged. Qt's own C++ never calls these Python overrides, so the editor's widget path is untouched. With no native window, every override is the widget's own.

**A frame.** The frame is drawn as follows:

1. The context is made current on the window.
2. Framebuffer 0 is bound, and the viewport is set to the window's pixel size.
3. The view's own `paintGL` draws the newest published frame. The HUD's QPainter draws on a `QOpenGLPaintDevice`.
4. The buffers are swapped.

**GL container objects.** VAOs and FBOs are per context, which is why a second, shared context was rejected.

**Scheduling.** A frame is presented on the event loop's next turn after it is published, through one coalesced callback. `QWindow.requestUpdate()` is not used: Qt delivers it on a refresh-rate timer, which reintroduced doubled frames.

**Input.** The window delivers key, mouse and wheel events to the view with `QApplication.sendEvent`. They therefore propagate to the editor window's key handling and pass the same event filters. The window answers macOS's input-method query itself, so a held key cannot open the accent picker. The play console's line is shown as a frameless window over the game window.

**Entry and exit.** Entry runs in this order:

1. The 3D view's context must share with its editor window's. A `QOpenGLWidget` shares its context with one the top-level window makes from the window's format, which is the application default. Where the platform refuses (macOS will not share a core-profile context with a legacy one: "falling back to unshared context"), Qt rebuilds the widget's context, and Fio's renderer with it, the next time the widget is shown. Native kiosk mode hides the editor window and shows it again on leaving, so it is refused in that case. `main.py` makes Fio's format (`engine/gl_format.py`) the default, so in Fio the two share. The test harness and the benchmark tool set the same default; the first macOS CI run without it rebuilt the renderer on every exit from native kiosk mode.
2. The window is created.
3. The context's and the window's pixel formats are compared (renderable type, colour, depth, stencil, samples). Version and profile belong to the context, not the surface.
4. The window is shown in the kiosk window mode.
5. `makeCurrent` is tried once.
6. The context is handed back to the widget.

Any failure leaves kiosk mode on the 3D view as before, and the reason goes to the Debug Console. A present that fails mid-game does the same.

The window acts on its own events (expose, resize, frame requests, input) only while the view presents on it. Windows delivers an expose and a resize while the native window is still being created, before the view has switched to it. A window that has ended and is waiting to be deleted gets events too. Neither may touch the view. Frame requests go through a timer the window owns, so no queued request outlives it.

On exit, the context is made current on the widget before the window is destroyed. The widget's framebuffer, viewport and scissor are put back explicitly. Qt's own paint setup restores the framebuffer and viewport, but GL work done outside a paint (the decal tool, a renderer switch, uploads) does not get that, and must not inherit the native window's viewport.

## Measuring it

```bash
python tools/bench_kiosk_presentation.py --map maps/_SHOWCASE.json --seconds 30 --repeats 3 --mode Fullscreen --vsync on --json result.json
```

Each run is a fresh Fio process. Every run uses the same map, window mode and size, the same rendering settings, and the same scripted walking route. Runs alternate widget, native, widget, and so on.

Per presented frame, both paths record the same things:

* the frame interval;
* render time (`paintGL` CPU);
* presentation time: for the widget, Qt's composition and swap; for native, `swapBuffers`;
* duplicate frames (the same publication shown twice);
* publications never shown;
* prepare-to-presented latency.

The summary gives each metric's median over the runs and its range.

A short swap does not mean the frame reached the display sooner: a driver may queue it and block later. Judge by the intervals, duplicates, skipped publications and latency, which include that wait.

**The decisive comparison is on the Snapdragon ARM target.** Run it there, fullscreen, vsync on, nothing else running.

### Results so far (not the target)

These were measured on x86, Mesa llvmpipe (software GL) under Xvfb, `Office_Corridor` at 960×540, vsync off, 3 runs per path. Values are the median over runs, with the range in brackets:

| | widget | native |
|---|---|---|
| render (paintGL CPU) | 1.83 [1.83–1.97] ms | 1.90 [1.87–1.99] ms |
| present | 9.74 [9.59–10.35] ms | 3.14 [3.10–3.19] ms |
| prepare-to-presented latency | 12.0 [11.8–12.8] ms | 5.4 [5.4–5.6] ms |
| frame interval p99 | 21.9 [20.8–22.4] ms | 19.9 [19.6–20.2] ms |
| publications never shown | 2 [1–4] | 0 |

Software GL presents through the X server in software, so these numbers say little about a real GPU's composition cost.

## The picture

The native window shows the same picture as the widget, scene and HUD alike, to within 1 level (`test_the_native_window_shows_what_the_widget_shows`). The test also checks that the HUD drew at all on each target.

A `QOpenGLPaintDevice` belongs to the GL context current when it is created. One created with none current paints nothing, silently. The native window therefore makes its paint device only while its own context is current, inside a frame, and refuses otherwise. An earlier version of the test asked for the device outside a frame, and the faint HUD vanished from native frames. That was first mistaken for a blending difference between the two targets; there is none.

## Platforms

| Platform | Status |
|---|---|
| Linux X11, GLX (Mesa) | All 16 tests pass, including the same-picture test. |
| Linux X11, EGL (Mesa) | All 16 tests pass, including the same-picture test. |
| Windows (Mesa llvmpipe) | All 16 pass in CI (run #53), on the runner's real window system. CI runs them on every push touching these files (`.github/workflows/play-click-native.yml`) and requires all 16 to pass, and each step to be reported: created and presenting (with the native window handle and the framebuffer drawn into, never the widget's), restored, editor resumed, console switch, repeated transitions. |
| macOS (Cocoa OpenGL, Apple Software Renderer) | All 16 pass in CI (run #53), every step reported. The first run there (Apple Software Renderer) found the renderer rebuilt on leaving native kiosk mode: the test harness had not set Fio's GL format as the default (see Entry and exit). With that fixed, one difference remained, and it is not native presentation's: the decal pick there lands 0.82 too deep on a floor 200 away, along the right ray, before kiosk mode as after it and the same every run, with a 24-bit depth buffer (Mesa: within 0.02). The test takes the pick before kiosk mode as the platform's accuracy (it must be within 1% of the distance) and requires the pick after kiosk mode to be as good (run #53: 0.816 before, 0.662 after). |
| Linux Wayland (sway) | All 16 pass in CI (run #53). Sway runs with floating windows there, as openbox on X11: tiling overrides the size a window asks for. |
| Linux X11 (openbox, CI) | All 16 pass in CI (run #53). |
| Snapdragon ARM (Windows on ARM) | Not yet tested. Run the tool above. |

Where `makeCurrent` on a second window is not supported, kiosk mode falls back to the widget.
