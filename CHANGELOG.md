# Changelog

## 3.0.0

3.0.0 is a deliberate clean break from the 2.x line. Compatibility shims for
older maps, settings and interpreters have been removed rather than carried
forward; see **Breaking changes** before upgrading.

### Breaking changes

- **Python:** Fio now requires CPython 3.14 or newer with the GIL enabled
  (previously 3.10–3.12). Free-threaded builds are refused at startup.
- **Cosmetic weapons renamed:** the `cig` weapon is now `custom1`, joined by a
  new `custom2`; both are non-firing. Maps whose pickups name `cig` (or use
  `assets/sprites/cig.png`) must be updated to `custom1`.
- **PathNode:** the legacy `patrol_speed` key is no longer migrated to
  `speed`; maps must use `speed`.
- **Settings:** the old `[Renderer] arm_mode` key is no longer read; use
  `lowpower_mode`.
- **Legacy entities:** maps from before 2.5.10 that still contain `pickup`
  entities load them as preserved, inert unknown entities; re-author them as
  collectable Props.
- **Plugins:** the editor-menu monkey-patch module (`plugins/integration.py`)
  is gone; the Plugins menu is built natively. The logic thread has been split
  into owned runtimes (`player_runtime`, `combat_runtime`, `render_runtime`,
  `session_runtime`, ...), so plugins that reached into `LogicThread`
  attributes directly must move to the runtime that now owns them.
- **Renderer / plugin API 1.6.0 — a major architectural shift from 1.5:**
  `engine/renderer_core.py` is replaced by the `engine.renderer` package, and
  the renderer is a plugin boundary: any object satisfying the
  `engine.renderer.Renderer` protocol can be registered by name and drawn
  through. Plugins written for 1.3–1.5 that don't use the renderer contract
  load and behave unchanged; renderer implementations written for 1.3–1.5
  (the old `cls(texture_loader, grid_size, world_size, config)` factory and
  forward-renderer interface) are not compatible, with no shim.
- **Console:** `r_fog`, `r_water`, `r_glass`, `r_lighting` and `r_deferred`
  (and their unprefixed aliases) are gone; they toggled attributes no
  renderer had.
- **Items:** a collectible Prop that gives a weapon now says
  `collect_type: "item"` with `collect_item` (was `"weapon"` with
  `collect_weapon`); maps are migrated on load. `combat_runtime.gun2_obtained`
  is replaced by `combat_runtime.weapons` (the weapons the player has), and
  `monster_constants.WEAPON_DAMAGE`, `WEAPON_SHOOT_SOUND` and
  `NON_FIRING_WEAPONS` are gone: weapons are described by item definitions
  (`engine.items`). Save games are version 3; version 2 saves still load.

### New

- Cutscenes: a Cutscene Wizard (Tools menu and toolbar), JSON cutscenes under
  `cutscenes/` played by a LogicCamera, with camera keyframes, temporary
  actors, timed I/O events and preview.
- Sprint: hold Shift to move at 1.5x speed.
- HUD styles, opacity and damage fade, with `hudstyle`, `hudopacity` and
  `hudfade` console commands.
- Terrain: persistent AABB CSG subtraction and texture stamping.
- `pos` and bulk `delete` console commands; ammo in the procedural map
  generator.
- Native Windows ARM64 build workflow.
- Renderers swap live, in the editor and in Play: `r_renderer` lists them,
  `r_renderer <name>` switches (`QtGameView.switch_renderer`). A renderer that
  fails to start leaves the current one running.
- Editor view filters (GtkRadiant style): a **Filter** menu shows or hides world
  brushes, movers and doors, triggers, water, glass, fog, terrain, lights, path
  nodes (and their connection lines), monsters, props and models, effects,
  portals, logic entities, speakers, player starts and other entities — in the
  3D view through any renderer, the 2D views, picking and the I/O lines. Play
  ignores them.
- A complete example renderer plugin, `docs/examples/deferred_renderer/`, and
  the wiki's Renderer Development Guide.
- Display modes: **Points** (a laser-scan point cloud, after Scanner Sombre)
  and **Overlay** (the textured frame with every brush triangle drawn over it);
  **Wireframe** now draws true brush edges, and Wireframe and Points are
  coloured by distance from the eye. `r_wireframe` drives the Display box.
- Items: `gun1`, `gun2`, `custom1` and `custom2` are data-driven item
  definitions. **Tools → Custom Items…** makes Custom 1 and Custom 2 weapons
  (hitscan, projectile or melee, with their own damage, range, cooldown,
  pellets, ammunition, sound, noise and sprites) or pickups (health, ammo,
  armor, key or weapon), saved with the map. Props reference items by id and
  follow their definition.
- Weapon slots: number keys 1–4 take a weapon the player has in hand, with a
  brief flash of its sprite bottom-right. Weapons and ammunition carry over a
  LevelChanger.
- Armor, from armor pickups: it absorbs damage before health from every
  source, and shows under the ammo count (beside it in HUD styles 2 and 3).
- `hudtext` console alias for `hudstyle`.
- Debug Tables labels the active renderer (FORWARD, DEFERRED, …) and works
  unchanged with any renderer; it counts shadow-map draws, shows lines a
  renderer publishes in `RenderStats.details`, and highlights a selected
  object's rows (from the viewport or the Scene Hierarchy) with a SELECTION tab
  listing every column of them.

### Fixes

- No GL resource outlives the renderer that owns it: replacing a renderer
  leaked its gizmo, AABB and component-overlay vertex arrays, its instance
  buffers, its portal programs and an instanced program compiled twice.
- "Solid Lit" no longer shows textures, and "Wireframe" no longer draws the
  instanced brushes filled.
- Portals no longer show an editor sprite; the aperture wireframe shows them.
- Debug Tables keeps the Follow Selection chain on screen (it vanished at the
  next refresh) and reports an id that is in no table.
- The Plugins menu's "Add entity (at origin)" entries work again.
- Entering Play no longer raises in `paintGL` before the first full frame.
- A notification is no longer cleared early by an earlier, shorter one.
- Choosing Custom 2 in the Prop panel no longer flips back to Gun 1.
- A legacy weapon Prop (`collect_weapon`) keeps its weapon on load.
- A key pressed in Play before the first frame is drawn no longer raises.
- GL end-to-end tests no longer depend on the order they run in: the
  application font each editor window sets is restored after every test.
- Big World is no longer dropped when one of its modules is imported before
  plugin discovery.
- The showcase map's cutscene now ships in every build, and its pickup and
  speaker reference files that exist.
- Terrain sized to a world extent no longer gains a chunk past its edge.
- Opening the terrain editor no longer raises in `paintGL` until the mouse
  moves over the 3D view.
- The standalone/Android player compiles the terrain shader on GLES (every
  ES 3.00 sampler type now gets a default precision).

### Hardening

- Malformed maps are refused at load with a message naming the field,
  instead of loading and then failing when Play starts.
- Save games are validated on read the same way; a malformed `.fiosave` can
  no longer leave the session unable to leave Play.
- `.fiopak` extraction keeps to the player's asset size budget.

## 2.5.11

### Behaviour changes

- **Prop collection default:** the default `collect_type` for a Prop is now
  `weapon` rather than `health`. Maps that omit an explicit
  `collect_type` therefore collect as a weapon after upgrading to 2.5.11.
  Explicitly authored collection types are unchanged.

### Engine fixes

- Preserve Effect runtime state when authored Effect rows are rebuilt after
  insertions or reordering.
- Share capacity growth between dense Effect and Projectile stores without
  repeating old data into newly allocated rows.
- Use a bounded nearest-enemy kernel crossover for small populations and
  many tiny teams.
- Vectorise moved Effect position synchronisation into the EffectStore.
