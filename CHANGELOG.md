# Changelog

## 3.0.0

3.0.0 is a deliberate clean break from the 2.x line. Compatibility shims for
older maps, settings and interpreters have been removed rather than carried
forward; see **Breaking changes** before upgrading.

### Breaking changes

- **Jump height:** the player jumps at Quake 2's 270 u/s (about 46 units,
  about 64 with the step-up) instead of 355 (about 79). A ledge between 64
  and 79 units that was reachable is not any more.
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
- **Benchmark plugin removed:** `plugins/benchmark` and everything that
  referred to it are gone.
- **Brush tool, 2D views:** a plain click on a brush no longer selects it;
  it starts drawing a new brush there, as on empty space. Shift+click
  selects a brush. Entities, resize handles and the Select tool are
  unchanged.
- **Esc in Play opens the pause menu** instead of leaving Play. Exit to
  Editor (Quit in a played package) is in the menu; with the player dead,
  Esc still leaves Play.

### Player physics: Quake 2

Gameplay moves and falls as in Quake 2, on Fio's one physics:

- Gravity is Quake 2's 800 and has one authority, the live PhysicsWorld
  (``engine.physics.world_gravity``) that Big World already relies on. The
  player, monsters and dropped props read it from there (they had 800, 500
  and 900 of their own; physics bodies 900), so ``phys_gravity`` now changes
  how everything falls.
- The player keeps Fio's collision (spatial-grid colliders, box resolution,
  angled-brush capsules, terrain, water) and follows Quake 2's movement rules:
  walk 200, run (sprint, Quake 2's +speed) 300, duck 100, with diagonals
  adding up as in Quake 2 (283 walking); acceleration 10 on the ground and 1
  in the air, friction 6 with stop speed 100; the forward wish shortened by a
  third of the view pitch.
- Jump 270 (about 46 units). Jump must be released between jumps; a press in
  the air jumps on landing; a landing faster than 200 u/s holds off the next
  jump for 144 ms (200 ms past 400 u/s).
- Steps up to 18 units are taken in the air as well as on the ground, so a
  jump lands on a ledge a step above its peak (about 64 units).
- Crouch ducks: the hull halves from the top, so the player fits under
  50-unit gaps, and stands back up only where there is room.
- A surface steeper than about 45 degrees is not ground.

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
- PlayerStarts have an explicit **Primary** flag (one per level: the first
  created, movable and undoable, promoted on delete) and Play starts there.
  LevelChangers gain **Destination Spawn**: a named PlayerStart in the target
  map (chosen from that map's starts) or its primary. A missing named start
  stops the level change with an authoring error instead of spawning
  elsewhere. The 2D views label PlayerStarts with their name and "Primary".
- Debug Tables labels the active renderer (FORWARD, DEFERRED, …) and works
  unchanged with any renderer; it counts shadow-map draws, shows lines a
  renderer publishes in `RenderStats.details`, and highlights a selected
  object's rows (from the viewport or the Scene Hierarchy) with a SELECTION tab
  listing every column of them.
- A procedurally generated level's LevelChanger, used with E, generates a
  new random level with the same generator settings (and a fresh seed) and
  sends the player there. An untouched generated level is replaced without
  an autosave prompt.
- Loading a large level (4 MB and up) shows a five-pixel strip of orange and
  green stripes scrolling across the top of the editor. It runs as its own
  small process, so it keeps moving while the editor is busy parsing and
  building the level, and it can never outlive the editor.
- Up recalls the last command in the Play console (`) as it does in the
  Debug Console; the two share one command history.
- Pause menu (Esc in Play): Resume, Save Game and Load Game (three slots,
  `saves/slot1.fiosave` to `slot3.fiosave`), Options (Sound Volume, Music
  Volume, and Video: Fullscreen, Borderless or Windowed), plugins' items,
  and Exit, in a floating window like SysMon's (drag it by its title bar;
  [X] resumes). The game pauses and is blurred behind it (fading in over
  a second, and out again over the resumed game), and the cursor is free. The volumes and Video are the settings of Settings > Play Modes, so
  the two always agree; Settings gains Sound volume and Music volume. Music
  is whatever plays from `assets/music`; every other game sound is sound.
  Plugin API 1.7.0 adds `register_pause_menu_item`, `pause_menu_opened` /
  `pause_menu_closed` events and `main_window.open_pause_menu()` /
  `close_pause_menu()`; set in the bundled HornetDisplay font.
- `quit` console command: closes Fio at once, without asking (unsaved
  changes are lost). Map logic cannot run it.
- Asset Browser **Audio** tab: `assets/sounds` or `assets/music` (the
  orange link toggles them, as on the Maps tab), each with the folder panel
  for its subfolders. Double-click a sound, or **Give to Speaker**, to set it
  on the selected Speakers. Speakers play only from those two folders: a
  sound file anywhere else is refused, in the editor and at run time. Music
  files (`.ogg`, `.mp3`, `.wav`) play like sounds, at the music volume.
- Animated GIF textures: a GIF goes on a brush or face like a PNG or JPG
  (Fit, Natural, scale and stretch in the Surface Inspector included) and
  animates in Play at its own frame times, holding while the game is paused;
  the editor shows its first frame. In the Asset Browser its thumbnail is
  still, and an animated GIF has a small orange arrow that plays the
  animation on the thumbnail (click again to stop). Renderers may implement
  the optional `animate_textures(clock)`.
- **Visgroups & Cordon** (Hammer's Visgroups and Cordon tool in one window),
  from the button above the Scene Hierarchy's search bar. *User*: named
  groups of brushes and entities, each shown or hidden by its checkbox; new
  from the selection, add or remove the selection, select members, rename,
  delete. *Auto*: every kind of object (the Filter menu, kept in step).
  *Cordon*: a box -- from the selection or typed -- outside which nothing is
  shown, drawn in the 2D views. They hide objects in every editor view and
  in what the renderer is given, never in Play; visgroups and the cordon
  are saved with the map. The padlock keeps the window on top.
- Asset Browser: hovering a texture shows its type and size ("PNG
  512x512"; an animated GIF adds "animated"), read from the file header.

### Fixes

- `LowPoly_Tree_v1.obj` no longer lies on its side when inserted: it was
  exported Z-up while Fio (and the other bundled models) are Y-up. The file
  is now Y-up, and the maps that stood it up with a 270 degree rotation no
  longer need it (their trees look exactly as before).
- A portal fading in or out (Enable, Disable, Toggle) changes its opacity:
  its view and rim show at the fade's opacity over the scene behind the
  aperture. It darkened to black instead, then popped to the scene behind
  at the end of a fade-out. The console's `portal_enable` / `portal_disable`
  fade too (a console-enabled portal stayed black).
- Settings > Display > Big toolbar buttons resizes the toolbar buttons (the
  small setting changed nothing), and applies at once instead of asking for
  a restart.
- Settings > Play Modes > Display Mode is applied: the game window (F12) was
  always full screen, whatever was chosen.
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
- A LevelChanger used with the Use key resolves its target map as its
  ChangeLevel input does (a bare name gets `maps/` and `.json`).
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
- Unsaved edits are no longer lost when the level is replaced. Recent Files,
  the console `map` / `load` commands and the procedural generator ask first,
  as File > Open does (regenerating replaces the generator's own untouched
  preview without asking). A level change during Play cannot stop to ask, so
  the edited level is written to `maps/<name>_autosave.json` first, as edited
  rather than as played when "Restore the world when leaving Play" is on.
- In the editor's Play Game Package, a LevelChanger or the `map` command
  changes to the package's own map rather than one in the editor's `maps/`
  folder, so multi-level packages work away from the author's machine.
- The `fps` console command no longer raises.
- The terrain panel re-arms the terrain brush whenever it is shown again.
- The About dialog finds `editor/version.txt` from any working directory.
- A Debug Console being torn down no longer raises on a late log message.
- Export Game Package bundles the cutscenes its maps play (and the assets of
  actors those cutscenes spawn), and a played package's cutscenes are found
  inside the package. A cutscene reference outside `cutscenes/` is reported
  and left out.
- A level change the game asks for (a LevelChanger, a map's `map` command)
  into a map with no Player Start is refused with a toast and Play carries on,
  as for a missing destination spawn. Opening such a map yourself during Play
  ends Play with a toast instead of a dialog that blocked the game loop.
- GLB models with indices load again: under NumPy 2 every indexed GLB (nearly
  every glTF export) raised `TypeError` while building its buffers or its
  2D-view wireframe.
- OBJ material groups are contiguous: an OBJ that returns to an earlier
  material (`usemtl A`, `B`, `A` -- the bundled `Tree low.obj` does) drew
  overlapping index ranges, so some triangles were drawn twice with the wrong
  material.
- A non-numeric coordinate in an OBJ or MTL loads as zero instead of failing
  the whole model.

### Hardening

- Malformed maps are refused at load with a message naming the field,
  instead of loading and then failing when Play starts.
- Save games are validated on read the same way; a malformed `.fiosave` can
  no longer leave the session unable to leave Play.
- `.fiopak` extraction keeps to the player's asset size budget.
- A `.fiopak` manifest or map over 64 MiB is refused before it is inflated.
- settings.ini is written atomically, so a failed write cannot truncate it.
- Map logic can no longer run `vsync` / `r_vsync`, and a map's `fps` toggle
  no longer writes settings.ini.
- 20 test modules that skipped without PyQt5 but were not marked `qt` ran in
  no tier; they are marked, and the suite-integrity check refuses another.
- GLB: external buffers must be relative paths to regular files (an absolute
  path, a scheme or `/dev/zero` is not read); models are capped at 256 MiB;
  a primitive with out-of-range indices or negative offsets is dropped
  instead of reaching the GPU.
- OBJ/MTL: only regular files up to 256 MiB are read, so `mtllib /dev/zero`
  no longer hangs the loader.
- A terrain heightmap that is not a plain 2-D numeric `.npy` (corrupt, truncated,
  an `.npz`) or a malformed sculpt entry is dropped rather than failing the
  map load.
- CI workflows default to a read-only `GITHUB_TOKEN`; only the release job can
  write to the repository.

### Performance

- GLB accessors, vertex interleaving and triangle lists are decoded in bulk
  with NumPy instead of per-element Python loops (a 200k-vertex model parses
  in ~13 ms instead of ~490 ms).

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
