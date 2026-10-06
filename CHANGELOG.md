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

### Fixes

- The Plugins menu's "Add entity (at origin)" entries work again.
- Entering Play no longer raises in `paintGL` before the first full frame.
- A notification is no longer cleared early by an earlier, shorter one.
- Choosing Custom 2 in the Prop panel no longer flips back to Gun 1.
- Big World is no longer dropped when one of its modules is imported before
  plugin discovery.
- The showcase map's cutscene now ships in every build, and its pickup and
  speaker reference files that exist.
- Terrain sized to a world extent no longer gains a chunk past its edge.

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
