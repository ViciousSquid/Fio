<img src="https://github.com/user-attachments/assets/23a671e6-624c-4900-95ae-b5cc8426c701" width="700">

**Debug Tables** (**Dense Numerical Instrument**) is real-time developer tool for inspecting the dense data used by the [renderer](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview).

> Intended to make the [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable)/[EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) rendering pipeline visible rather than treating it as a black box.

### More: [What Debug Tables is for](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables#what-debug-tables-is-for)<br><br>

Open it from:

**Debug → Debug Tables**

The **Debug** menu also contains **Project Overview…** and **Validate All Connections…**, providing map-level, I/O-level and dense-rendering diagnostics in one place.

## Purpose

Fio's renderer does not normally need to reconstruct authored objects in order to render the world. The runtime publishes dense numerical state through:

* `RenderTable` — dense brush/render-projection state
* `EntityTable` — dense entity state
* visible integer slot arrays — the rows selected for the current view

Debug Tables exposes those structures directly.

The window is **read-only**. It does not create a second world representation and does not modify the rendering pipeline.

The tool reads the published render-state snapshot used by the live editor/runtime.

## Renderer label

The top bar shows the **active renderer's registry name**, upper-cased, in Fio orange (`#f08000`): `FORWARD`, `DEFERRED`, … Its tooltip points at `r_renderer`, the console command that switches renderer live.

Debug Tables works **unchanged with any renderer** — the built-in `Forward`, a test renderer, a plugin renderer such as the Deferred example. It reads only the published dense tables and the renderer contract (`render_stats` and the registry name); it knows no renderer's internals. Switching renderer while the window is open simply updates the label and the renderer's figures on the next refresh.

**ALWAYS ON TOP** (on by default) keeps the window above the editor; untick it to let the editor cover it.

## Pipeline

The Pipeline tab shows the current dense execution path:

```text
World / LogicThread
        ↓
RenderTable + EntityTable
        ↓
visible integer slots
        ↓
renderer key generation / sorting
        ↓
contiguous runs
        ↓
instanced GL submission
```

It also reports:

* live RenderTable row count
* RenderTable allocated capacity
* live EntityTable row count
* EntityTable allocated capacity
* NumPy memory used by each table
* total dense numerical storage
* published render-state frame age
* rows read last frame for each table
* frames published and swaps declined per second
* measured CPU timings: simulation tick, monster AI update, prepare (logic thread), paint (UI thread)
* the active renderer (`renderer <name>`)
* renderer draw-call count
* **shadow-map draws**
* batched draw count
* visible triangle count
* entity rows offered to the entity passes, frustum-culled and drawn
* **renderer detail lines** — whatever diagnostics the active renderer publishes in `RenderStats.details` (for example `ForwardRenderer`'s `sprite texture array`, or the Deferred example's `G-buffer`)
* a **MONSTER AI** section (MonsterTable rows, modes, line-of-sight rays, dense phase timings) in Play
* a **TERRAIN** section (TerrainTable residency, built/dirty chunks, drawn/culled chunks, triangles, LOD grids, memory) when the map has terrain
* per-pass CPU timings (inclusive)

**Draw calls** count scene draws only. **Shadow-map draws** are counted separately (`RenderStats.shadow_draw_calls`, counted at every shadow draw site), and editor overlays such as the grid, selection outline and gizmo are in neither. For the built-in renderer, draw calls + shadow-map draws equals the GL draws issued inside `render_scene` minus those overlays.

The tool deliberately does **not** invent CPU stage timings. It reports only measurements that are actually available from the published state and renderer statistics.

## RenderTable

The **RENDERTABLE** tab provides direct read-only access to the NumPy arrays held by the live `RenderTable`.

Select any available array from the `ARRAY` selector to inspect its rows and values.

Each array view reports its:

* shape
* dtype
* allocated byte size
* live row values

The table contains dense render information such as transforms, classification flags, texture identifiers, UV state, colours, geometry identifiers and special-material state.

This is the actual numerical representation consumed by the renderer; the Debug Tables window does not translate it back into Brush objects for display.

## EntityTable

The **ENTITYTABLE** tab performs the same inspection for the live `EntityTable`.

This exposes the dense numerical representation of renderable entities, including fields used to classify and submit models, sprites and Effects.

This makes it possible to inspect the data that reaches the renderer without relying on the original authored `Thing` representation.

## MonsterTable

The **MONSTERTABLE** tab shows the monster AI's dense `MonsterTable` in the same way. It exists only in Play mode.

The table is copied only when the monster lock is free, so the instrument never makes the AI wait; when the lock is busy the previous copy is kept, and its age is shown on the dashboard.

## TerrainTable

The **TERRAINTABLE** tab shows the terrain's dense `TerrainTable`. A map without terrain, or with terrain switched off, has no table, and the dashboard says so explicitly.

## Key Microscope

The **KEY MICROSCOPE** shows the logical render-key stream generated from the visible RenderTable rows.

For the cube-brush path, the logical key layout is:

```text
[ texture-name-id: 32 bits | cube-face: 3 bits ]
```

The tool:

1. Starts from the current visible RenderTable slots.
2. Filters out non-cube geometry.
3. Examines the six texture slots for each visible cube brush.
4. Removes faces marked as non-drawable.
5. Packs each drawable face into an integer render key.
6. Sorts those keys into contiguous runs.
7. Reports the resulting runs and key distribution.

The display includes:

* visible cube rows
* drawable faces
* number of contiguous runs
* run start/end positions
* run lengths
* packed hexadecimal keys
* texture-name IDs
* cube-face IDs
* key frequency distribution

For example:

```text
RENDER-KEY MICROSCOPE

Logical key layout: [ texture-name-id:32 | cube-face:3 ]

visible cube rows   1,024
drawable faces      5,781
contiguous runs        47

RUNS
  000  rows     0-  143  n=144  key=0x0000000123  tex=  36 face=3
  001  rows   144-  287  n=144  key=0x0000000243  tex=  72 face=3
  ...
```

This makes batching behaviour directly inspectable instead of relying on a single draw-call count.

### Logical key vs final renderer key

The microscope displays the **logical brush key**.

The logical key uses `texture-name-id` so the numerical projection remains independent of OpenGL resource handles.

At the renderer boundary, the texture-name ID is resolved to the corresponding **GL texture ID**, which is used by the renderer's final brush key.

Therefore the microscope intentionally shows the key one step before the GL-resource resolution boundary.

## Follow Selection

Enable **FOLLOW SELECTION** to trace the currently selected object through the dense representation.

When the selected object has an ID, Debug Tables resolves that ID against the live tables and can show:

```text
authored ID
    ↓
RenderTable row
    ↓
logical render key
    ↓
render run
```

For entity rows it can also show:

```text
authored ID
    ↓
EntityTable row
    ↓
sprite-key
```

This allows a specific object selected in the editor to be followed into the numerical rendering pipeline.

In Play, a monster also shows its MonsterTable row:

```text
authored ID
    ↓
MonsterTable row (mode)
    ↓
target
```

The chain is shown on the status line and is re-applied on every refresh, so it stays visible. (Previously it was only re-added when it changed, so it vanished on the next 250 ms refresh.)

If an ID cannot be found in any table, the tool reports, live:

```text
FOLLOW id=<id> -> ID NOT PRESENT IN RENDERTABLE, ENTITYTABLE OR MONSTERTABLE
```

(Previously this message appeared only in the export.)

This is useful for diagnosing stale projections, missing rows, selection mismatches and unexpected classification.

### Selection highlighting

With **FOLLOW SELECTION** on, selecting an object anywhere — in the viewport or in the [Scene Hierarchy](https://github.com/ViciousSquid/Fio/wiki/Scene-Hierarchy) — highlights its row in Fio orange in whichever of the **RENDERTABLE**, **ENTITYTABLE**, **MONSTERTABLE** and **TERRAINTABLE** views contain it. The row is selected and scrolled into view immediately, without waiting for the next refresh.

## Selection

The **SELECTION** tab shows the followed object's dense representation: every column of every row its ID has, table by table.

```text
SELECTION  id=<id>

RenderTable  row 42
  bounds                       ...
  class_bits                   ...
  ...
```

## Memory

The **MEMORY** tab lists every NumPy array in every copied table (RenderTable and EntityTable, plus MonsterTable in Play and TerrainTable when the map has terrain).

For each field it reports:

```text
TABLE / FIELD
SHAPE
DTYPE
BYTES
```

Both live row count and allocated capacity matter here.

Fio's dense tables retain allocated storage beyond the current live row count, so the displayed byte count represents the actual NumPy allocation rather than an estimate based only on populated rows.

## Live Updates

Debug Tables refreshes automatically every 250 ms while open.

The raw array views expose the currently published tables. The window therefore follows the runtime's published render-state boundary rather than independently rebuilding scene state.

The window can also be manually refreshed with **Refresh**.

## Export

Use **Export** to save a complete numerical debug snapshot as a ZIP archive.

The exported format is:

```text
fio-debug-tables-v1
```

The archive can contain:

```text
manifest.json
pipeline.json
pipeline.txt

RenderTable/
EntityTable/
MonsterTable/      (Play mode)
TerrainTable/      (maps with terrain)

visible_brush_slots.npy

KeyMicroscope/
key_microscope.json
key_microscope.txt

memory.json
memory.txt
follow_selection.txt
```

The RenderTable, EntityTable, MonsterTable and TerrainTable arrays are exported as NumPy `.npy` files.

`pipeline.json` records the active `renderer` and `shadow_draw_calls` alongside the draw-call, batched-draw and triangle counts, plus monster and terrain figures when those tables exist.

The arrays are exported at their **full allocated capacity** rather than being truncated. The live row count and live shape are recorded separately in the memory metadata.

This makes the export useful for offline analysis as well as immediate debugging.

## What Debug Tables is for

Debug Tables is particularly useful when investigating:

* unexpected RenderTable contents
* missing or duplicated dense rows
* entity classification problems
* sprite/model/Effect projection errors
* incorrect render keys
* excessive render runs
* poor batching
* stale selection mappings
* live entity or brush insertion
* dense-table capacity growth
* render-state publication problems
* discrepancies between dense state and visible rendering

It is also useful when developing the renderer itself: the numerical data can be inspected before changing the rendering code, making it possible to determine whether a problem originates in projection, classification, key generation, batching or GL submission.

## Using Debug Tables when writing a renderer

Because it reads only the dense tables and the renderer contract, Debug Tables is the natural instrument for a new renderer: switch to it with `r_renderer`, and the label, the table rows, the visible slots and your renderer's own counters appear with no changes to the tool. Publish your renderer's own diagnostics through `RenderStats.details` and they appear as lines in the PIPELINE tab.

Debug Tables reports what a renderer was given and what it *says* it did. Treat it as an **oracle to check against an independent measurement**, not as proof on its own: `tests/e2e/test_debug_tables_oracle.py` compares its readings with a measurement that never opens it — the published frame read directly and the GL draws counted by wrapping `OpenGL.GL` — across renderer swaps.

See the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide).

## Design principle

Debug Tables is deliberately built around the same dense representation used by the renderer.

It does not maintain a parallel debug scene, duplicate the world model or convert the tables back into collections of authored objects.

The purpose is simple:

> **What the renderer sees should be inspectable.**

That makes the dense rendering pipeline observable all the way from an authored object's identity through table rows and packed keys to the renderer's submission statistics.
