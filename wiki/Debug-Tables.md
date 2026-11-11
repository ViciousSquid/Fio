> Applies to Fio 3.1.0.0611 (CANARY).

<img src="https://github.com/user-attachments/assets/23a671e6-624c-4900-95ae-b5cc8426c701" width="700">

# Debug Tables

Debug Tables is a read-only instrument window (`tools/debug_tables.py: DebugTablesWindow`) that shows the dense numeric state Fio renders and simulates from: the published [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable) and [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable), the monster AI's `MonsterTable`, the terrain's `TerrainTable`, the render keys and runs derived from them, the change records, per-pass timings and the active renderer's own counters. It reads only the `Renderer` contract (`render_stats`) and published data, so it works unchanged with any renderer. It is also the reporter used by the oracle capture/diff tools described at the end of this page.

Open it with **Debug → Debug Tables**. The module is imported only when the action is first triggered; one window exists per main window (`show_debug_tables` raises the existing one).

## How it samples

- A 250 ms `QTimer` calls `refresh()`; **Refresh** calls it immediately; a selection change (`MainWindow.selection_changed`) updates the follow views at once from the tables already copied.
- `refresh()` leases the published frame (`ThreadedGameState.get_render_state()`), copies every non-object NumPy column of both tables into `FrozenTable` objects together with `visible_brush_slots`, `is_play_mode`, `timestamp` and `prepare_ms`, and releases the lease before doing anything else. It never keeps the frame, because a held lease stops the logic thread publishing (`tests/tools/test_debug_tables.py::test_sampling_does_not_hold_the_published_frame`).
- `MonsterTable` is copied under the monster lock **only if the lock is free** (`acquire(blocking=False)`); when the AI holds it the previous copy is kept and its age is shown.
- `TerrainTable` is copied from `main_window.terrain.table` on the GUI thread (which is where the terrain is advanced), with no lock. A map without an enabled terrain has no terrain table: the tab is empty and the dashboard says so.
- Object columns (`refs`) are not copied.
- Opening the window turns on change-detection timing (`engine.change_detection.timing(True)`), so the per-site microsecond figures are measured from then on.

Nothing is written to the world, the tables or the renderer.

## Window controls

| Control | Default | Effect |
|---|---|---|
| renderer label | — | registry name of the active renderer in upper case (`QtGameView.renderer_mode`; change it with `r_renderer`) |
| **FOLLOW SELECTION** | on | trace the editor's primary selection through the tables (below) |
| **ALWAYS ON TOP** | on | toggles `Qt.WindowStaysOnTopHint` (the window is a `Qt.Tool` window) |
| **Refresh** | — | sample now |
| **Export** | — | write the snapshot archive (below) |

The status line reads `DETACHED — ...` when there is no logic thread, `ATTACHED — dense tables not published yet` before the first frame, and otherwise `ATTACHED | frame age N ms | inspector sample N ms`, followed by `| FOLLOW ...` and, after an export, `| EXPORT <path>`.

## Tabs

### PIPELINE

A bar chart (logic `prepare`, GUI `paint`, and the eight most expensive passes, in microseconds) above a text dashboard:

| Section | Lines | Source |
|---|---|---|
| `PIPELINE / PUBLISHED STATE` | per table: rows, capacity, dense bytes, **rows read last frame**; total ndarray storage | `count`, column lengths, `nbytes`, `rows_read` |
| `PUBLICATION (double-buffered)` | frames published per second; swaps declined per second | `ThreadedGameState.published_frames` / `declined_swaps`, as rates over at least 0.5 s |
| `TIMINGS (measured, CPU)` | renderer name; simulation tick; monster AI update; prepare (logic thread); paint (GUI thread) | `LogicThread.tick_ms`, the AI thread's `update_ms`, `RenderState.prepare_ms`, `QtGameView.paint_ms` |
| | draw calls, shadow-map draws, batched draws, visible triangles | `RenderStats.draw_calls`, `shadow_draw_calls`, `batched_draws`, `visible_tris` |
| | entity rows offered, frustum-culled, drawn | `RenderStats.entity_candidates`, `culled_entities` |
| | one line per entry of `RenderStats.details` | renderer-published text or callables (the forward renderer publishes `sprite texture array` and `occlusion`) |
| `CHANGE RECORDS (M4)` | RenderTable `generation` and its `transform`, `material`, `extent`, `shown` versions; per change-detection site, rows examined and microseconds per published frame since the last refresh; `oracle ON` when `FIO_RENDER_ORACLE` is set | `engine.change_detection.DETECT.sites` |
| `MONSTER AI (MonsterTable)` | pass (`dense` or the reason for the per-monster path); rows, capacity, bytes, rows read last tick, copy age; AI update time and how much of it waited for the monster lock; row count per mode; line-of-sight rays, shots fired, rows through Python, teams; dense phase times | `MonsterTable` and the AI thread |
| `TERRAIN (TerrainTable)` | residency (streaming radius or bounded), resident / built / dirty / allocated / capacity; drawn and frustum-culled chunks, triangles drawn of built, terrain pass ms; built chunks per LOD grid; build budget and textures/grass flags; CPU table bytes and GPU heightfield bytes | `TerrainTable`, the terrain's counters copied back by the host, `Terrain.UPDATE_BUDGET_MS`, `MAX_UPDATES_PER_FRAME` |
| `PASSES (inclusive)` | every `timed_pass` name and its CPU ms, most expensive first | `RenderStats.pass_ms` |

On a still frame every change-detection site should read 0 rows whatever the size of the world, and "rows read last frame" should be 0 for both tables. Pass times are inclusive: `portals (incl. their views)` and `shadow maps` include the passes they run. The `GPU heightfield` figure is computed from `_height_pages` / `_page_layers` read off the `Terrain` object; those attributes belong to the renderer's `TerrainGL`, so the line reports `0 page(s) x 0 layers = 0 bytes`.

Monster modes are `skipped`, `dead`, `asleep`, `chase`, `search`, `scalar`, `waiting` (`engine.monster_table.MODE_NAMES`).

### RENDERTABLE, ENTITYTABLE, MONSTERTABLE, TERRAINTABLE

Each is a raw array browser (`RawTable`): an **ARRAY** drop-down listing every non-empty ndarray field of the copied table, a header line `shape=(...) dtype=... bytes=...`, and a grid with one row per slot. Multi-dimensional columns are flattened to one grid column per element, named `name[i]`. Only live rows are listed (`[:count]`; for `TerrainTable`, whose freed slots are reused, up to `row_extent`); `bytes` is the whole allocation. Integers print as integers, floats to five significant digits, booleans as `1`/`0`. The scroll position is kept across refreshes, and the grid is only reset when the selected array changed.

`RenderTable`'s `center` and `half` are views of `bounds` and are browsed through `bounds`. `MonsterTable` exists only after a dense AI tick in Play.

### KEY MICROSCOPE

Rebuilds the textured box pass's render keys from the copied tables: the visible slots without `CLASS_HAS_GEOMETRY`, their drawable faces (`tex_name_id >= 0`, not `TEX_ID_SKIP`, and not `TEX_ID_NODRAW` in Play), packed with `KeyLayout([('texture', 32), ('face', 3)])` and sorted with `sort_into_runs`. It lists the number of visible cube rows, drawable faces and contiguous runs, the first 80 runs (`rows a-b  n=  key=0x...  tex=  face=`) and a histogram of the first 32 distinct keys.

The key shown is the **logical** key: its texture field is the table's texture-name id. The forward renderer's `BRUSH_RUN_KEY` has the same layout with the resolved GL texture name in that field, so run boundaries correspond but key values differ. Convex brushes are not part of this view.

### MEMORY

For every copied table, each ndarray field with its live shape, dtype and allocated bytes.

### SELECTION

With **FOLLOW SELECTION** on, the primary selection's id (`brush['id']` or `properties['id']`) is looked up in each copied table's `slot_of_id`. The tab prints every column value of every row found, per table. The same rows are highlighted (orange) and scrolled into view in the raw tabs, and the status line shows the chain:

```text
FOLLOW id=<uuid> -> render-row=<slot> -> key=0x... -> run=<n>
FOLLOW id=<uuid> -> entity-row=<slot> -> sprite-key=<id> -> monster-row=<row> mode=<name> -> target=player
```

`key` and `run` appear only when the brush's row is in `visible_brush_slots` and has a drawable face (the first drawable face is used). `sprite-key` appears when `sprite_key_id >= 0`. Other outcomes: `NO SELECTION`, `SELECTION HAS NO ID`, `ID NOT PRESENT IN RENDERTABLE, ENTITYTABLE OR MONSTERTABLE`.

## Export

**Export** writes a ZIP (`fio_debug_tables_YYYYMMDD_HHMMSS.zip` by default):

| Member | Content |
|---|---|
| `manifest.json` | format `fio-debug-tables-v1`, the member list, and a note that arrays are exported at full capacity |
| `pipeline.json` | export and snapshot times, frame age, the timings dictionary, play mode, renderer name, rows/capacity/bytes of each table, the draw counters, and the monster and terrain summaries (or `"terrain_table": null`) |
| `pipeline.txt` | the PIPELINE dashboard text |
| `RenderTable/*.npy`, `EntityTable/*.npy` | every copied ndarray field, full allocated capacity |
| `MonsterTable/*.npy` | in Play, after a dense tick |
| `TerrainTable/*.npy` | maps with an enabled terrain |
| `visible_brush_slots.npy` | the frame's visible slots |
| `KeyMicroscope/*.npy`, `key_microscope.json`, `key_microscope.txt` | `visible_cube_slots`, `row`, `face`, `texture_name_id`, `logical_keys`, `sort_order`, `run_starts`, `sorted_keys`, `unique_keys`, `key_counts`; the layout; the tab text |
| `memory.json`, `memory.txt` | per field: `shape`, `live_shape`, `dtype`, `bytes`; the tab text |
| `follow_selection.txt` | the follow chain |

Arrays are saved with `allow_pickle=False`. Use `count` / `live_shape` from `memory.json` to find the populated prefix.

## What to use it for

| Question | Where to look |
|---|---|
| Did an edit or I/O input reach the renderer's data? | SELECTION / the raw tab for the row; "rows read last frame" should rise by the rows touched |
| Is something re-reading the world every frame? | "rows read last frame" and the change-detection sites on a still frame |
| Why is a brush in the wrong pass? | `class_bits` of its row |
| Why are there so many draws? | KEY MICROSCOPE run count; `draw calls` / `batched draws` |
| Is the renderer starving publication? | `swaps declined /s` against `frames published /s` |
| Which pass costs CPU? | PASSES |
| Is a new renderer filling the contract's counters? | TIMINGS and the `details` lines with that renderer active |
| Did a new column resolve correctly? | add it to the table's `_COLUMNS`; it appears in the raw tab, MEMORY and the export with no change to this tool |

Debug Tables reports what the tables hold and what the renderer says it did; it measures neither. The oracle workflow below pairs it with an independent measurement for that reason.

## Oracle capture and diff

`tools/oracle_capture.py` and `tools/oracle_diff.py` compare two revisions of the engine by what they render and what Debug Tables reports.

### Capture

```text
xvfb-run -a python tools/oracle_capture.py --out before.npz \
    --unbudgeted-terrain maps/_SHOWCASE.json maps/BigWorld_streaming_test.json
```

| Argument | Default | Meaning |
|---|---|---|
| `maps...` | required | levels to capture |
| `--out` | required | the `.npz` capture file |
| `--tree` | this checkout | another checkout to capture (for example a `git worktree` at another commit); its `settings.ini` is restored afterwards |
| `--ticks` | 120 | ticks to play |
| `--every` | 20 | record every N ticks (tick 0 included) |
| `--unbudgeted-terrain` | off | lift the terrain's wall-clock build budget (`Terrain.UPDATE_BUDGET_MS`, `MAX_UPDATES_PER_FRAME`) so streaming does not depend on timing |

It needs a display with OpenGL. Each level runs in a `tests.helpers.session.FioTestSession` (240×136) on a **stepped clock** (`time.perf_counter` replaced by the effect clock origin plus whole 60 Hz ticks), with `random` and NumPy seeded to 1234 and scripted input (walk forward, alternate strafing every 30 ticks, turn ±5 every 25, fire every 15). A level with no Player Start is captured in the editor view. SysMon and the FPS counter are switched off, and `effect_spawn_time` is stored relative to the clock origin (zero stays zero).

At every checkpoint it records, under keys `<source>/<level>/<tick>/...`:

| Source | Records |
|---|---|
| `indep/` | read without Debug Tables: every column of the published `render_table` and `entity_table`, `visible`, the frame's `pixels`, and `gl_draws` — the number of `glDrawArrays`, `glDrawElements`, `glDrawArraysInstanced` and `glDrawElementsInstanced` calls issued inside `render_scene`, excluding `draw_grid` and `_draw_selection_overlays` |
| `oracle/` | read from a `DebugTablesWindow` (timer stopped, `refresh()` called): every frozen column, `rows_read` per table, `visible`, and `draws` — the four counters parsed from the dashboard line `draw calls ... shadow-map draws ... batched draws ... visible triangles ...` |

### Diff

```text
python tools/oracle_diff.py before.npz after.npz [--noise again.npz]
```

Compares the two captures record by record, bit for bit (shape, dtype and bytes). It prints the number of identical records, differing records and records present in only one capture, then one line per differing field with the number of checkpoints and an example (for `pixels`: how many pixels differ and their bounding box). With `--noise` — a second capture of the *after* revision — an element that differs before/after is reported only if it is stable between the two after-runs, which removes run-to-run noise. Exit status is 1 if anything remains, else 0.

A change passes when both the `oracle/` and the `indep/` records agree with the other revision.

### In the test suite

| Test | What it holds |
|---|---|
| `tests/e2e/test_debug_tables_oracle.py` | Debug Tables' counters against GL draws counted by wrapping `OpenGL.GL`, on the same frames, with Forward, then an unrelated renderer, then Forward again |
| `tests/e2e/test_change_records.py` | Debug Tables' copy of the published RenderTable equals a table rebuilt from scratch from the same world, after each kind of write |
| `tests/visual/test_oracle_crosscheck.py` | the table oracle (published RenderTable equals a rebuild) and the draw oracle (what reached the GPU, per triangle) checked against each other, with injected faults |
| `tests/tools/test_debug_tables.py` | sampling does not hold the frame; the copy is a copy; every tab, control, follow chain and export member described on this page (the `test_wiki_*` cases) |

`FIO_RENDER_ORACLE=1` is a different mechanism with a related purpose: it makes every site that trusts a change record redo the comparison the record replaced and raise `engine.change_detection.MissedChange`; `FIO_RENDER_ORACLE=record` appends the miss to `change_detection.MISSES` instead, and the suite's `conftest.py` fails any test that leaves one. Debug Tables shows `oracle ON` when it is active.

## Related tools

| Tool | Purpose |
|---|---|
| `tools/bench_compare.py` | scripted end-to-end frame benchmark through the real host (`--occlusion settings\|off\|on`, `--frames`, `--json`) |
| `tools/occlusion_compare.py off.json on.json` | A/B report of two `bench_compare.py` runs; see [Cell Occlusion Culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling) |
| `tools/bench_instance_uploads.py` | change-detection rows and instance bytes uploaded per frame (`--no-raster`, `--logic`, `--scale N`) |
| `tools/bench_terrain.py` | terrain draw cost |
| `tools/perf_gate.py` | the production-path performance gate against a base revision |
