# Compatibility — map data and saved state

**Map data integrity is a release requirement. An unknown entity must never
simply disappear during load/save.**

## COMPAT-01 — The general hazard (unresolved, needs a decision)

`editor.things.Thing.from_dict` ends:

    print(f"Warning: Unknown thing type '{thing_type}' found in map file.")
    return None

`EditorState.load_from_data` drops the `None`. So **any** entity whose type
token the running build does not recognise is silently deleted, and the next
save makes it permanent. A warning on stdout is not a safeguard: the editor
has a debug console, and a user loading a map in the GUI will never see it.

This is not hypothetical — it is exactly how REG-03 destroyed 42 objects per
map. It will recur for any of:

* a plugin that is missing from the install,
* a plugin that is present but **disabled** at load time,
* a map authored by a newer Fio and opened in an older one,
* any future entity rename whose author forgets `legacy_map_types`.

The mechanisms that exist today are both *opt-in per rename*:
`legacy_map_types` on the receiving class, and `FioPlugin.migrate_map_data`
(restored in this audit). Neither protects a type nobody anticipated.

**Two designs worth considering, for the maintainer to choose between:**

1. **Preserve and round-trip.** Keep the unrecognised record verbatim in a
   side list on `EditorState`, exclude it from the scene, and re-emit it on
   save. The map survives a round trip through a build that cannot render it.
   This is what the release requirement literally asks for.
2. **Refuse the load.** Fail loudly with the list of unknown types and the
   plugins that would supply them, rather than opening a silently-lossy
   document. Safer, less convenient.

Doing nothing means the requirement is stated but not enforced anywhere.

## COMPAT-02 — `tidyobject` → `prop` (was broken, now fixed)

See regressions.md REG-03. A 2.4.2 Tidy map lost all 42 of its objects.
Restored via `TidyPlugin.migrate_map_data`:

    type / properties.type : 'tidyobject' -> 'prop'
    properties.category    -> properties.tidy_category
    properties.tidied      -> dropped (transient runtime state)
    model_path             -> defaulted to BOOK_MODEL if absent

Verified idempotent, and verified not to touch a 2.5-format map. Pinned by
`tests/persistence/test_legacy_map_integrity.py`.

## COMPAT-03 — `collision_size == [0,0,0]` changed meaning

At 2.4.2 the Prop default `collision_size: [0.0, 0.0, 0.0]` meant *no size*;
`no_collision` defaulted to `True` and the baseline sim ignored the value
entirely. From commit 414 ("Treat zero Prop collision size as automatic
bounds"), refined by 455 ("Use real OBJ bounds"), `[0,0,0]` means **derive the
collision box from the mesh**.

So a 2.4.2 map's props, loaded in 2.5 with physics enabled, get mesh-derived
collision where before they got none. This is almost certainly the intended
improvement, but it is a **semantic change to an existing key in saved data**,
not just a new default for new content. Worth a line in the release notes.

## COMPAT-04 — `friction` became live

At 2.4.2, `friction` (default 0.55) was authored on every Prop and **read by
nothing** — the baseline `PropSession._simulate` applied only `linear_damping`
on the vertical axis. From commit 456 it is Coulomb ground friction:
`a = mu * g`. Any 2.4.2 map that set a non-default `friction` will behave
differently, because previously the value was inert. Same for `mass`
(used from 411 for player push) and `angular_damping`.

## COMPAT-05 — `render_mode` added with an implicit default

At 2.4.2 a Prop chose its representation implicitly: `model_path` if set,
otherwise `sprite_path` as a billboard. From commits 465–473 `render_mode`
(`'model'` / `'billboard'`) is authored and **authoritative** (473).

The default applied to a legacy prop is `'model'`. A 2.4.2 prop authored with a
`sprite_path` and no `model_path` therefore depends on that default not
overriding the old implicit rule. **This is the compatibility item most worth
checking against real authored maps** before release — it is the one place
where a legacy prop could render as nothing rather than as a billboard.

## COMPAT-06 — `trigger_poll_interval` snaps rather than validates

`LogicThread._trigger_poll_interval` does:

    allowed = (1.0, 0.5, 0.25)
    return min(allowed, key=lambda interval: abs(interval - value))

An authored `2.0` silently becomes `1.0`; `0.1` becomes `0.25`; the exact
midpoint `0.375` is resolved by iteration order. The docstring says "return a
valid interval", so this is deliberate — but a map can author a cadence it does
not get, with no warning. Consider clamping-with-a-log, or documenting the
three permitted values in the property tooltip.

## COMPAT-07 — Maps shipped in the repository

* `maps/Tidy_Test.json` → `plugins/tidy/Tidy_Test.json` (git: `R073`), rewritten
  to the core-Prop format (533). The plugin's demo-map menu action loads it via
  the editor's existing unsaved-changes dialog.
* `maps/DevTest.json`, `maps/MonsterTest.json`, `maps/Simple_Map_Test.json`
  modified; `maps/dLight2_Test.json` added.
* `assets/Untitled.png` is added at commit 135 ("Add files via upload") and is
  referenced by nothing. Dead weight in the shipped tree.

## Save games

`engine/savegame.py` is **untouched** by all 559 commits, and it persists
`current_hud_message` among its fields. No format change. Not otherwise
affected by this range.

## Repository state — RISK-REPO-1

`origin/2.5.0.0_canary` is a **single orphan commit** ("Initial commit with
newer version") with **no shared ancestry** with `2.4.2.1709_Latest` or with
the 559-commit development branch this audit was performed against.

Consequences:
* `git log`, `git bisect` and `git blame` across the 2.4→2.5 boundary do not
  work on the published branch.
* The published snapshot is *ahead* of the 559-commit branch in several
  respects (it carries `engine/prop_entity.py` and legacy-map compat tests),
  so the two are not simply "history vs squash" — they diverged.
* Force-pushing either over the other loses real work.

The 559-commit history is preserved locally as tag
`2.5.0.0_canary-full-history`. **Pushing that tag is refused by the egress
policy (HTTP 403)**, so it exists only in this working copy; it needs pushing
from a clone with ordinary credentials if it is to survive.
