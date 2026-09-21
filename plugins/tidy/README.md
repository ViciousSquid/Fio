# Tidy

Tidy adds **put-it-away gameplay** to Fio without introducing a second object or
physics system.

A tidyable object is an ordinary core **Prop** with a non-empty
`tidy_category`. The core Prop system remains responsible for pickup,
carrying, dropping, and physics. The Tidy plugin adds the rules for where that
Prop can be put, tracks progress, and provides receptacles and goals.

Typical uses include:

- returning books to a shelf;
- sorting museum objects into the correct display;
- clearing loose objects from a room;
- building simple "tidy everything" or "sort everything" objectives.

## Quick start

Open **`maps/Tidy_Test.json`** and press **Play**.

Look at a book and press **E** to pick it up. Turn toward the shelf and press
**E** again to put it away. The HUD tracks the number of objects tidied.

The demo contains **42 core Props** carrying Tidy metadata. There is no special
"Tidy Object" entity to place.

## Mapper workflow

### 1. Create a normal Prop

Place a **Prop** using Fio's normal Prop workflow.

Set:

| Property | Purpose |
|---|---|
| `tidy_category` | The logical category of the object, such as `book`, `fossil`, or `pot`. A non-empty value makes the Prop tidyable. |

Everything else remains a normal core Prop property. Use the normal Prop
controls for its model, collision, mass, friction, pickup behaviour, and other
physical properties.

For example:

```text
type = prop
tidy_category = book
```

A plain Prop with no `tidy_category` is completely unaffected by Tidy.

### 2. Add a Tidy Receptacle

Place **Tidy Receptacle (shelf/bin)** from **Plugins ▸ tidy**.

The receptacle is an invisible gameplay volume/anchor. Put it where the objects
should be arranged, normally just above a shelf, table, tray, or other visible
piece of level geometry.

| Property | Purpose |
|---|---|
| `accepts` | Category accepted by the receptacle, or `any`. |
| `capacity` | Maximum number of Props it can hold. |
| `slot_cols` | Number of slots across before a new row begins. |
| `slot_spacing` | `[x, y, z]` spacing between slots. |
| `slot_offset` | `[x, y, z]` offset of the first slot from the receptacle origin. |
| `reach` | Maximum distance at which the player can place a held Prop. |
| `disabled` | Prevents new objects from being placed here. |

When a valid Prop is placed, Tidy moves it to the next free slot.

### 3. Add a Tidy Goal

Place **Tidy Goal** from **Plugins ▸ tidy**.

| Property | Purpose |
|---|---|
| `target` | `all` or a numeric number of objects to tidy. |
| `category` | Restrict the goal to one Tidy category, or use `any`. |
| `show_hud` | Show the live tidy counter. |
| `disabled` | Temporarily stop the goal from counting toward completion. |

A goal can be used purely as a progress tracker, or its `OnComplete`
output can drive the rest of the level.

## I/O

Tidy extends the normal core Prop I/O rather than replacing it.

### Prop

Core Prop I/O remains available:

**Inputs**

- `Enable`
- `Disable`
- `Drop`
- `Wake`

Tidy adds:

- `Reset` — return the tidyable Prop to its authored position.

**Outputs**

- `OnPickedUp`
- `OnDropped`
- `OnRest`

Tidy adds:

- `OnTidied` — fired when the Prop is successfully placed into a Tidy
  receptacle.

### Tidy Receptacle

**Inputs**

- `Reset` — remove everything from the receptacle and return the contained
  Props to their authored positions.
- `Enable`
- `Disable`

**Outputs**

- `OnObjectPlaced` — parameter is the new number of objects in the receptacle.
- `OnFull` — fired when the receptacle reaches capacity.

### Tidy Goal

**Inputs**

- `Enable`
- `Disable`

**Outputs**

- `OnProgress` — parameter is `done/need`.
- `OnComplete` — fired once when the configured target is reached.

Example:

```text
TidyGoal.OnComplete -> LevelChanger.Enable
```

The same output can drive a door, light, speaker, message, or any other Fio
entity with a compatible input.

## Player interaction

With a tidyable Prop under the crosshair:

**E** picks it up using the normal core Prop interaction.

While carrying it, **E** attempts to place it into the valid receptacle being
aimed at. If no suitable receptacle is in range, the normal core Prop drop
behaviour is used instead.

When a Prop is placed successfully, Tidy:

1. consumes the core Prop drop;
2. snaps the Prop to the next receptacle slot;
3. temporarily disables pickup for that stowed Prop;
4. updates tidy progress;
5. fires `OnTidied`, `OnObjectPlaced`, `OnProgress`, and `OnFull` as
   appropriate.

Normal dropped Props continue to use the core physics system.

## Categories and sorting

Categories are just metadata. Tidy does not prescribe a fixed list.

For example:

```text
Books       -> tidy_category = book
Fossils     -> tidy_category = fossil
Pots        -> tidy_category = pot
Paintings   -> tidy_category = painting
```

Then create matching receptacles:

```text
Book shelf  -> accepts = book
Fossil case -> accepts = fossil
Pot shelf   -> accepts = pot
```

A receptacle can instead use `accepts = any` when category-specific sorting is
not required.

Goals can also be category-specific. A museum level could have one goal for
all fossils and another for all pots, for example.

## Performance and architecture

Tidy deliberately stays out of the systems that already belong to core Fio.

**Core Prop owns:**

- pickup and carrying;
- ordinary dropping;
- physical simulation;
- collision and wake/rest behaviour;
- Prop spatial interaction.

**Tidy owns:**

- `tidy_category` metadata;
- receptacles and slot placement;
- tidy progress;
- goal completion;
- Tidy-specific I/O.

There is no `TidyObject` entity, no duplicate pickup implementation, no
second Prop physics simulation, and no second Prop spatial hash.

This keeps a large collection of ordinary Props in the core runtime while Tidy
only maintains its own small amount of state: the tidyable Props, receptacles,
goals, and their placement/progress bookkeeping.

## Plugin activation

Tidy is **disabled by default**.

A map automatically uses Tidy when it contains:

- a Tidy Receptacle;
- a Tidy Goal; or
- a core Prop with a non-empty `tidy_category`.

This means a normal Fio map containing ordinary Props does not pay for Tidy
gameplay just because the plugin exists.

For authoring a new map, Tidy can also be enabled manually from
**Plugins ▸ tidy**.

## Demo and assets

The reference map is:

```text
maps/Tidy_Test.json
```

The example generator is:

```bash
QT_QPA_PLATFORM=offscreen python plugins/tidy/tools/make_example_map.py
```

Tidy's bundled book assets live under:

```text
plugins/tidy/assets/
```

The book model and cover textures can be regenerated with:

```bash
python plugins/tidy/tools/make_books.py
```

The receptacle and goal editor icons can be regenerated with:

```bash
python plugins/tidy/tools/make_sprites.py
```

## Implementation

`plugins/tidy/entities.py` contains the two Tidy-owned entities:

- `TidyReceptacle`
- `TidyGoal`

`plugins/tidy/runtime.py` contains `TidySession`, which handles receptacle
selection, placement, temporary stowed-object state, progress, goals, and HUD
updates.

`plugins/tidy/plugin.py` registers the metadata and entities, installs the
Tidy I/O handlers, and connects Tidy to the core Prop drop path.

The important boundary is intentional: **Prop is the object; Tidy is what
happens when that object is put away.**
