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

### Tidyable Prop
A Tidy object is an ordinary core **Prop**. Set `tidy_category` to a non-empty
value such as `book`, `cup`, or `bone`; Tidy then adds placement/progress
behaviour without replacing the Prop pickup, carry, drop, or physics runtime.

| Property | Meaning |
|----------|---------|
| `tidy_category` | Logical Tidy group. A receptacle only takes Props whose category it accepts. |
| `model_path` | Standard core-Prop model; the demo uses `plugins/tidy/assets/book.obj`. |
| `texture` | Standard per-instance texture override, useful for bundled book covers. |
| `physics_enabled` | Core Prop physics setting. Tidy does not implement a second physics system. |
| `pickup_enabled` | Core Prop pickup setting. Tidy temporarily disables this while a Prop is stowed. |

Core Prop outputs `OnPickedUp`, `OnDropped`, and `OnRest` remain available; Tidy
adds the `Reset` input and `OnTidied` output.
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

Ordinary dropped objects use the core Prop physics/runtime. Tidy does not contain
a second falling simulation. When a core Prop is dropped over a valid Tidy
receptacle, Tidy consumes that drop and snaps the Prop into the next slot.

---

## Recipes

**Books back on the shelf.** Scatter `Tidy Object`s (`category: book`) on the
floor. Put one `Tidy Receptacle` (`accepts: book`) above a shelf brush with
`slot_cols` matching how many fit per shelf. Add a `Tidy Goal` (`target: all`).

**Tidy up the museum (sorting).** Give Props different Tidy categories
(`fossil`, `painting`, `pot`). Add one receptacle per category, each with its
`accepts` set. Use a single `Tidy Goal` (`category: any, target: all`), or one
goal per category to fire per-section rewards.

**Thousands of objects.** Just place (or procedurally generate) more core `Prop`s with `tidy_category` set.
Tidy does not maintain a second spatial hash for props; core Prop interaction
handles pickup/carry/drop, while Tidy only scans its usually-small receptacle set.

---

## How it works (for the curious)

- `entities.py` — the two Tidy-owned `Thing` subclasses (receptacle and goal).
- `runtime.py` — `TidySession`: receptacle placement, temporary Prop state,
  progress/goal tracking, and the HUD line. Core Prop owns carry/drop/physics.
- `plugin.py` — registration, I/O handlers, and the play lifecycle wiring.
- `assets/book.obj` + `assets/covers/cover_NN.png` — the UV-mapped book model
  and its random covers. Regenerate with `python plugins/tidy/tools/make_books.py`.
- `assets/tidy{receptacle,goal}.png` — the entities' own editor icons
  (a book, a bookshelf, a checklist). Regenerate with
  `python plugins/tidy/tools/make_sprites.py`.

Regenerate the demo map with:

```bash
QT_QPA_PLATFORM=offscreen python plugins/tidy/tools/make_example_map.py
```
