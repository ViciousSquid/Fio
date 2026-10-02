# Tidy plugin

Build **"put everything away"** games in Fio: books back on the shelf, tidy up
the museum, clear the warehouse floor. The player walks up to objects, picks
them up one at a time, and stows them in the right place until a goal is met —
scaling to **thousands** of objects.

Try it: open **`maps/Tidy_Test.json`** and hit Play. Look at a book, press
**E** to pick it up, face the shelf, press **E** to put it away. The HUD shows
`Tidied: N / 42`.

This plugin is **disabled by default** — it only matters for maps built around
its entities, so it stays inert until you load a level (like `Tidy_Test.json`)
that references its data, at which point the editor and player enable it
automatically. Starting a fresh map with **File ▸ New** (or loading a map that
doesn't use it) switches it back off. To place its entities in a new map, tick
**Enabled** under **Plugins ▸ tidy** in the menu bar first — a manual enable
sticks and isn't reverted underneath you.

---

## Entities

Place these from the 2D view's right-click menu under **Plugins ▸ tidy**.

### Tidy Object
A single carryable prop. Renders in play mode as a **book with a random cover** —
each object picks one of 12 bundled covers at creation, so a pile or shelf shows
varied books, not identical boxes. Swap the model/cover in the Properties panel
if you want something else (any `.obj`/`.glb` works).

| Property | Meaning |
|----------|---------|
| `category` | Logical group (`book`, `cup`, `bone`…). A receptacle only takes objects whose category it accepts. |
| `model_path` | 3D model to render (defaults to the UV-mapped `book.obj`). |
| `texture` | Per-instance cover image (defaults to a random `covers/cover_NN.png`). |
| `scale`, `rotation` | Standard model transform. |
| `no_collision` | `True` by default so thousands of props stay cheap and walk-through. |

Outputs: `OnPickedUp`, `OnDropped`, `OnTidied`.
Inputs: `Reset` (send home), `Enable`, `Disable`.

### Tidy Receptacle (shelf / bin)
A drop-zone. When the player places an object here it snaps into the next free
slot, arranged in a neat grid.

| Property | Meaning |
|----------|---------|
| `accepts` | Category it takes, or `any`. |
| `capacity` | Max objects it holds. |
| `slot_cols` | Objects per row before stacking upward. |
| `slot_spacing` | `[x, y, z]` spacing between slots (X across a row, Y per shelf). |
| `slot_offset` | `[x, y, z]` offset of the first slot from the receptacle origin. |
| `reach` | How close/aligned the player must be to place into it. |

Outputs: `OnObjectPlaced` (parameter = new count), `OnFull`.
Inputs: `Reset` (empty it), `Enable`, `Disable`.

The receptacle itself has no geometry — put it just above a shelf brush (or a
bin model) so placed objects visually land on the surface. Tune `slot_offset`
and `slot_spacing` to match your shelf.

### Tidy Goal
Invisible logic entity that tracks progress and ends the round.

| Property | Meaning |
|----------|---------|
| `target` | `all` (every object) or an integer count. |
| `category` | Restrict the goal to one category, or `any`. |
| `show_hud` | Show the live `Tidied: N / M` counter. |

Outputs: `OnProgress` (parameter = `done/need`, fired on every stow),
`OnComplete` (fired once when the target is reached).
Inputs: `Enable`, `Disable`.

Wire `OnComplete` to a `LevelChanger`, a `Speaker`, a door, a light — whatever
should happen when the room is tidy.

---

## Controls

- **E** (use/interact) — pick up the object under the crosshair.
- **E** again — place into the shelf you're facing, or drop it if none is in
  reach.

The HUD prompts contextually (`[E] Pick up Book`, `[E] Put away (Shelf)`,
`[E] Drop`) and shows live progress when idle.

A dropped object **falls to the floor** under gravity instead of hanging where
you released it. This physics is opt-in and cheap: only objects you've actually
dropped are simulated, and only until they land — so a map with thousands of
resting props pays nothing for it (framerate stays the priority). In editor
play mode the landing height is read from the world geometry under the drop; the
lighter standalone player has no such query and settles the object at its
original resting height.

---

## Recipes

**Books back on the shelf.** Scatter `Tidy Object`s (`category: book`) on the
floor. Put one `Tidy Receptacle` (`accepts: book`) above a shelf brush with
`slot_cols` matching how many fit per shelf. Add a `Tidy Goal` (`target: all`).

**Tidy up the museum (sorting).** Give objects different categories
(`fossil`, `painting`, `pot`). Add one receptacle per category, each with its
`accepts` set. Use a single `Tidy Goal` (`category: any, target: all`), or one
goal per category to fire per-section rewards.

**Thousands of objects.** Just place (or procedurally generate) more `Tidy
Object`s — the runtime indexes available objects in a spatial hash, so the
per-frame "what am I looking at" check stays fast no matter how many exist.
Keep `no_collision` on (the default). Give receptacles generous `capacity`.

---

## How it works (for the curious)

- `entities.py` — the three `Thing` subclasses (data only).
- `runtime.py` — `TidySession`: carry/place logic, drop-and-fall physics (only
  in-flight objects are simulated), a `SpatialHash` over available objects,
  receptacle slot maths, goal tracking, and the HUD line.
- `plugin.py` — registration, I/O handlers, and the play lifecycle wiring.
- `assets/book.obj` + `assets/covers/cover_NN.png` — the UV-mapped book model
  and its random covers. Regenerate with `python plugins/tidy/tools/make_books.py`.
- `assets/tidy{object,receptacle,goal}.png` — the entities' own editor icons
  (a book, a bookshelf, a checklist). Regenerate with
  `python plugins/tidy/tools/make_sprites.py`.

Regenerate the demo map with:

```bash
QT_QPA_PLATFORM=offscreen python plugins/tidy/tools/make_example_map.py
```
