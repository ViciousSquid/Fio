# Fio 2.4 ← MiniWind: Big World three-way merge

Merge base: `4e86e82` (Fio 2.3.1.0) · Fio branch: `2.4.0.0_Latest` · MiniWind: `main`.

## 0. The finding that shapes everything

MiniWind's `engine/world_*.py` is **not a second implementation of Big World.**
It is *this* plugin — `plugins/bigworld/` at the merge base — renamed and moved
into engine core:

| merge base                     | MiniWind                        | normalised code diff |
| ------------------------------ | ------------------------------- | -------------------- |
| `plugins/bigworld/cell.py`     | `engine/cells.py` + `engine/world_cells.py` | rename only |
| `plugins/bigworld/manager.py`  | `engine/world_cells.py`         | rename only          |
| `plugins/bigworld/runtime.py`  | `engine/world_streaming.py`     | 3 real additions     |
| `plugins/bigworld/streaming.py`| `engine/world_streaming_disk.py`| rename only          |
| `plugins/bigworld/persistence.py` | `engine/world_persistence.py`| import path only     |

After normalising the renames (`WorldCellIndex`→`BigWorldManager`,
`WorldCell`→`BigWorldCell`, `WorldStreamingSession`→`BigWorldSession`,
`.cells`→`engine.spatial`), the manager, cell, disk-streaming and persistence
modules diff to **zero** code lines. The divergence is naming and file layout.

So the merge is not "reconcile two designs". It is:

* **reject** the move into engine core (§3: Big World stays a Core Plugin);
* **keep** Fio 2.4's own improvements, which MiniWind's fork predates;
* **upstream** the four things MiniWind genuinely added;
* **leave** MiniWind's gameplay where it is.

## 1. Fio 2.4 changes that must survive (MiniWind's copy is older)

1. **`engine/spatial.py`** — the 512-unit convention extracted out of the plugin
   so the collision grid, the renderer's region cull and Big World read one
   implementation. MiniWind has `engine/cells.py` (the maths) but **no
   `CellIndex`**, and open-codes the hysteresis box scan in `world_cells.py`.
2. **`engine.spatial.authored_hidden` / `PARKED_HIDDEN_KEY` / `PARKED_DISABLED_KEY`**
   — §9's "mapper intent vs parked state" answered at engine level, so the
   collision grid can tell them apart. MiniWind re-declares the marker strings
   locally in `world_streaming.py` and has no `authored_hidden`.
3. **Identity fix in `brush_uuid`/`thing_uuid`.** Fio 2.4 falls back to
   `"#%d" % id(obj)` for an unsaved object; MiniWind returns
   `str(brush.get("id", ""))`, which files *every* id-less brush under the same
   key `""`. That is an identity collision in the middle of §6's hard
   requirement. Fio wins, unconditionally.
4. **`TRANSIENT_KEYS` / `_clear_transient_markers()`** — sweeps `bw_active` off
   the whole world at play-stop, not just off the parked set. MiniWind dropped it.
5. **`tests/test_bigworld_engine_integration.py`** — proves the collision grid
   and Big World agree on every brush, and that parked brushes stay in the grid.
6. **Plugin packaging**: `plugin.py`'s lazy-import discipline (§16), the
   `BigWorldSettings` entity and its property schema, terrain fill/infinite
   terrain streaming, and the debug overlay. MiniWind has no `plugins/bigworld`
   at all, so none of this exists there.

## 2. MiniWind additions that are FIO-WORTHY

| MiniWind                                  | Verdict |
| ----------------------------------------- | ------- |
| Simulation tiers `NEAR/ACTIVE/DISTANT/DORMANT` + tier hysteresis (`world_index.py`) | **Upstream.** Fio 2.4 has cell-residency hysteresis and *no per-object tier at all*. This is the whole of §11/§12 and the only real gap. |
| `_sim_tier` property stamp                | **Upstream.** The decoupling that lets AI/gameplay read a tier without importing the classifier — and read `NEAR` when there is no Big World. |
| Streaming/simulation radius agreement (`_publish_relevance_radii`) | **Upstream.** §13, exactly. |
| `StreamingHost` contract + `notify_visibility_changed` | **Upstream.** Writes down what the session needs of its host. |
| `render_cull.visible_xz_bounds()`         | **Upstream.** Pure GL-free geometry; the docstring is MiniWind-specific, the maths is not. §15: residency is Big World's, visibility is the renderer's. |

## 3. MiniWind material that stays in MiniWind

| MiniWind                                          | Why |
| ------------------------------------------------- | --- |
| `team`/`faction` interning, `team_relation_table`  | Faction policy — §19. |
| `dead` corpse flag, liveness semantics             | Combat state — §19. |
| NumPy actor arrays, `rows_near`, `nearest`, `derived()` | An **AI query accelerator**, not world infrastructure. It answers "who is near whom" for combat and perception. Fio's `CellIndex` is the generic bucketing primitive underneath it; MiniWind may keep its vectorised layer on top. |
| `game/runtime.py` NPC decision scheduling per tier | §11: Fio defines the tiers, MiniWind decides what to do at each. |
| `engine/streaming_debug.py`                        | Duplicate — Fio's overlay already lives in `plugin.py`, guarded by `show_cell_debug`. |

## 4. Accidental fork divergence — discarded

* Big World moved from `plugins/bigworld/` into `engine/` (§3 violation).
* `WorldCellIndex` / `WorldCell` / `WorldStreamingSession` naming.
* `session.cells` vs `session.manager` attribute naming.
* `engine/cells.py` as a second home for the cell maths (`engine/spatial.py` is it).

## 5. Ownership matrix (post-inspection)

| Subsystem          | Fio 2.4                     | MiniWind                        | Final owner  |
| ------------------ | --------------------------- | ------------------------------- | ------------ |
| Spatial cells      | `engine/spatial.py` + `CellIndex` | `engine/cells.py`, no index | **Fio**      |
| UUID identity      | correct, id() fallback      | collides on unsaved objects     | **Fio**      |
| Cell residency     | ref-counted, hysteresis     | same code, renamed              | **Fio BigWorld** |
| Parking            | engine-level `authored_hidden` | local marker strings         | **Fio BigWorld** |
| Simulation tiers   | **absent**                  | complete, gameplay-entangled    | **Fio BigWorld** (new `tiers.py`) |
| Tier→streaming agreement | absent                | `_publish_relevance_radii`      | **Fio BigWorld** |
| Persistent globals | `bw_persistent` + type set  | same code, renamed              | **Fio BigWorld** |
| Disk streaming     | `DiskStreamingSession`      | same code, renamed              | **Fio BigWorld** |
| Camera visible bounds | fixed radius only        | `visible_xz_bounds`             | **Fio** (`engine/render_cull.py`), corner-ray bug fixed |
| Restore-time visibility invalidation | absent      | `notify_visibility_changed()` on restore | **Fio** (`engine/savegame.py`) |
| Parked-brush collision | `authored_hidden`          | tested bare `hidden` — a fall-through bug | **Fio** |
| Actor query accelerator | —                      | NumPy `WorldIndex`              | **MiniWind** |
| NPC / quests / factions / RPG persistence | generic hooks | implementation           | **MiniWind** |

## 6. What this merge changes in Fio

New: `plugins/bigworld/tiers.py`.
Edited: `engine/spatial.py` (tier vocabulary), `engine/render_cull.py`
(`visible_xz_bounds`), `plugins/bigworld/runtime.py` (classifier + host
contract + radius publication), `plugins/bigworld/entities.py` and
`plugins/bigworld/plugin.py` (one new map property).
New tests: `plugins/bigworld/tests/test_bigworld_tiers.py`.

Nothing else is touched. No renderer change, no collision change, no entity
change, no `if bigworld_enabled:` anywhere.

### Why the vocabulary is in `engine/`, not the plugin

`engine/spatial.py` gains four integer constants, a property-key string and a
one-`dict.get` reader. It gains **no code that runs**. The classifier — the only
thing that costs anything — lives in the plugin and is instantiated only by a
live `BigWorldSession`.

That split is what makes §16 hold *while* letting gameplay be written against
tiers. A system that gates on `tier_of(npc) <= TIER_ACTIVE` reads `TIER_NEAR`
on an ordinary map, because nothing stamped the property — i.e. full
simulation, vanilla Fio behaviour, zero Big World runtime. The alternative
(constants in the plugin) would force every consumer to import the plugin
package to name a tier, which is precisely the scattering §3 forbids.


---

# 7. Findings that only surfaced on integration

Three things the static inspection in §1–§5 did not catch, all found by running
the code rather than reading it.

## 7.1 Simulation tiers were nearly a per-frame global scan

The first cut of `TierClassifier` took a sequence of objects and measured each
one's distance from the player. That is the per-frame world walk Big World
exists to abolish: cost scales with total population, which is the one thing
§22 forbids, in the module whose purpose is to prevent it.

It was rebuilt so a tier is a property of a **cell**, and its entities inherit
it. Evaluation is driven by the residency crossing that already recomputes the
active set:

```text
player crosses a cell boundary
        ↓
residency delta (work the manager already does)
        ↓
cell tiers re-evaluated            O(active cells)
        ↓
entities of cells whose band changed are stamped
        ↓
consumers read the stamp
```

There is deliberately **no per-frame entry point**. Both terms are bounded by
the activation radius; a world of ten million objects costs the same per
crossing as one of ten thousand. `test_tiering_does_no_work_without_a_cell_crossing`
and `test_evaluation_scales_with_the_active_set_not_the_world` are the guards.

The price is granularity: a tier is conservative by up to one cell, erring
towards *more* simulation. Where a game wants a sharper boundary it draws its
own — MiniWind already measures per-actor distances for combat, and that is the
right place for it.

## 7.2 `visible_xz_bounds` under-reached in first person

Fio has two play cameras, switchable mid-session: overhead (`overhead_height`,
`overhead_tilt`) and first person. MiniWind's implementation sampled only the
four far-plane **corner** rays and clipped each by ray length. Corner rays leave
the eye at the frustum's widest angle and so are much longer than the view axis:
at a 75° FOV, clipping them to a 4096 ceiling leaves a forward reach of ~2054,
wrongly culling an object dead ahead.

Invisible in a top-down game, where the slab clip dominates long before the
ceiling does. Fio's first-person mode exposes it. The axis ray is what bounds
forward reach, and for a symmetric frustum it passes through the centroid of the
four corners, so that is now sampled alongside them.

The related invariant is that the **camera must never feed residency or tiers**.
In first person the visible region can reach past the activation radius; if it
drove streaming, a camera toggle would change the world's resident set without
the player moving. Residency and tiering both key off `_player_pos()`;
`test_camera_mode_cannot_move_the_resident_set_or_the_tiers` holds that line.

## 7.3 MiniWind's collision grid dropped parked brushes

`engine/physics.py` tested `brush.get('hidden')` when populating the spatial
grid. Big World parks brushes *by hiding them*, so any mid-play grid rebuild
removed every parked brush from collision permanently — a player falling
through the world after walking away and back. Fio 2.4's `authored_hidden`
fixes it; MiniWind's fork predated it. Caught by the vendored
`test_the_collision_grid_keeps_parked_brushes`.

Also fixed at source: the `_sim_tier` stamp was leaking into saved cell deltas,
and is now in `persistence.RUNTIME_KEYS` alongside `bw_active`.

---

# 8. MiniWind after the merge

`engine/cells.py`, `engine/world_cells.py`, `engine/world_streaming.py`,
`engine/world_streaming_disk.py`, `engine/world_persistence.py` and
`engine/streaming_debug.py` are **deleted**. Fio's `plugins/bigworld/` and
`engine/spatial.py` are vendored verbatim — one implementation, one owner.

Consumption is direct, not wrapped. `LogicThread._start_world_streaming` calls
`plugins.bigworld.PLUGIN.on_play_start(self)` — the same lifecycle code Fio
runs, not a reimplementation of it — and `self.streaming` is the session the
plugin built. `engine/savegame.py` reads `logic._bigworld`, the plugin's own
name. `engine/physics.py`, `engine/monster_ai.py` and `game/runtime.py` read the
tier vocabulary from `engine.spatial`.

`engine/world_index.py` **stays**, and stays MiniWind's. Its NumPy actor arrays,
team/faction interning, corpse flags and `team_relation_table` are combat and
perception acceleration — gameplay policy, explicitly not upstreamed (§19). What
it lost is the tier classification it should never have owned: `rebuild()` now
takes no radii and no focus point, and `tier_of()` reads the stamp Fio wrote.

```text
Fio BigWorld  →  cell residency  →  _sim_tier stamp
                                         ↓
                              MiniWind WorldIndex.tier_of()
                                         ↓
                    NPC scheduling, perception, combat (game/runtime.py)
```
