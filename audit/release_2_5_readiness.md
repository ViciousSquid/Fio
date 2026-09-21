# Fio 2.5.0.0 — desktop release-readiness

Scope fixed by the release boundaries: Nuitka-compiled binaries for Windows,
macOS and Linux; `.fiopak` as the world-distribution boundary; the supported
workflow being Tools → Play Game Package → fullscreen Play, with Editor mode as
a setting. Android and `player/` are future work and are **not** assessed as
blockers. 2.4 `.fiopak` compatibility is explicitly not required.

Four conditions were found. **Three are closed:** R1 and R2 are fixed in the
tree, and R4 is validated on the primary Windows-on-ARM reference machine — the
Nuitka-compiled Windows artifact launches and renders there, so the OpenGL 3.3
Core floor is empirically met on the target hardware.

**R3 is the one that remains**, and it is narrower than it was: the compiled
Windows artifact has been manually smoke-tested on the reference machine, so
what is left is the pipeline gap rather than an unknown about the software —
CI still ships a binary nothing has launched.

---

## R1 — Playing a package destroys it — **FIXED**

Severity: data loss, in the one supported workflow.

`save_level()` is `open(self.file_path, 'w')` + `json.dump`. Both package
importers set `self.file_path` to the `.fiopak` they had just extracted —
twenty-five lines below their own comment saying not to:

```python
# Do NOT set file_path to the archive path – saving would corrupt the package.
# Force a "Save As" dialog the first time the user saves.
self.file_path = None
...
self.file_path = filePath          # <- twenty-five lines later
```

Reproduced on a real archive before fixing:

```
before: is_zipfile = True  | size = 292
after:  is_zipfile = False | size =  57
```

The map, every texture, model and sound, and any bundled plugins are replaced
by a bare JSON file. One Ctrl+S after importing a package.

Both paths now leave `file_path` as `None`, so a first save goes through Save
As — which is what the comment always promised. Pinned by
`tests/persistence/test_package_import_safety.py`, mutation-verified against
the pre-fix code.

---

## R2 — Desktop import ignores a package's bundled plugins — **FIXED**

The documented contract (wiki, *.fiopak archive*) is that Fio bundles plugin
code into the archive, "making packages self-contained rather than dependent on
destination machine installations". The exporter honours it:
`plugins.packaging.augment_fiopak` writes `plugins/` into the archive and
records `"plugins": [...]` / `"requires_plugins": true` in `metadata.json`.

The desktop importer does not. `play_game_package` extracts the archive, finds
a map, points ResourceManager at the temp directory, and loads the level. It
never reads `metadata.json` and never loads the bundled `plugins/`.

`plugins.packaging.load_package_plugins(package_root)` exists for exactly this
— its docstring says "Intended for a player/editor opening a package built
elsewhere" — and has **zero callers** anywhere in the tree. The only
implementation of the documented behaviour is in `player/plugin_host.py`, which
is the WIP player, not the shipped desktop path.

What still works: entity-driven plugins auto-enable on load, because
`plugins.integration` wraps `EditorState.load_from_data` with
`auto_enable_for_map`. So a package using Tidy or Big World plays correctly **on
a machine that already has those plugins installed**. What fails is the case the
contract is written for: a package carrying a plugin the destination Fio does
not have. Its entities load as unresolved records — preserved, per the 2.5
unknown-entity policy, but not functional.

**Fixed.** Both import paths now call it, before the map is parsed —
`Thing.from_dict` resolves an entity's class at parse time, so a plugin
arriving afterwards leaves every one of its entities an unresolved record.

It was not a one-line call. `load_package_plugins` could not have worked if
something had called it: `discover_and_load` returns immediately once the
manager has loaded, which it always has by the time the editor opens a package;
and adding the package root to `sys.path` does nothing, because `plugins` is
already imported and its `__path__` is what decides where `plugins.<name>` is
found. Extending that `__path__` is what makes the bundled package importable —
and it is narrower than `sys.path`, so the session-state worry that held this
back turned out to be avoidable rather than merely acceptable: the change is
scoped to one package and there is nothing global to undo.

The collision question is settled by rule rather than by hope: a plugin name
already loaded is kept and the package's copy ignored, so a second package
cannot replace classes the session holds live objects for. That guard is new —
the existing one keyed on the package *directory*, which is a different
question, and a package can carry a plugin under a differently-spelled
directory. `sorted()` over `pkgutil.iter_modules` also only ever worked by
accident: it compares finder objects first, which tie only when there is a
single root.

Covered by `tests/persistence/test_package_plugins.py`, including the one that
matters — a package's entity type resolving to a real class afterwards — and
mutation-verified at all three points.

---

## R3 — The release artifact is never executed

`.github/workflows/build.yml` builds all three targets correctly (the Nuitka
invocations, Qt plugin inclusion, data directories, macOS bundle, ad-hoc
signing and `ditto` packaging are all sound). Its validation step, on every
platform, is:

```
if (Test-Path "build/main.dist/Fio.exe") { "Build successful" }
```

A binary that segfaults on startup, cannot find its assets, or dies on the
first GL call passes this. Nothing launches the thing being shipped, and the
suite that proves 2696 tests pass runs against the *interpreted* tree, not the
compiled one.

The gap matters because compilation genuinely changes behaviour — see the two
compiled-build defects in the appendix, neither of which any test can see.

**Manual target-hardware validation now exists.** The Windows Nuitka artifact
was launched and exercised successfully on the primary Surface Pro 9 5G / SQ3 /
Adreno reference machine. This confirms that the compiled Windows artifact can
start and render on the actual target hardware. Automated compiled-artifact
smoke testing in CI remains absent.

Minimum bar: launch the built binary headless (`xvfb-run` on Linux, offscreen
Qt platform elsewhere), load a map, enter and leave Play mode, and exit non-zero
on an unhandled exception. `workflow_dispatch` is also the only trigger, so the
build is not tied to a tag or release.

**One compiled-build assumption was checked and holds.** Plugin discovery uses
`pkgutil.iter_modules([os.path.dirname(__file__)])`, which looked certain to
fail when `plugins/` is compiled into the binary. Tested by compiling a
reproduction with the same Nuitka flags the release uses:

```
compiled: True
package_dir: .../probe.dist/pkgs
dir exists: False
iter_modules found: [('alpha', True), ('beta', True)]
PLUGINS LOADED: ['alpha', 'beta']
```

Nuitka patches `iter_modules` for compiled packages, so plugins do load in the
shipped binary despite the directory not existing on disk. Recorded because it
is the kind of thing that gets "fixed" later by someone reasoning about it
instead of testing it.

---

## R4 — GL 3.3 Core on the primary reference machine — **VALIDATED**

`main.py` sets a hard floor before `QApplication` exists:

```python
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
```

There is no fallback profile, no `isValid()` check, no `GL_VERSION` probe and
no diagnostic if context creation fails. The failure mode on a machine that
cannot provide 3.3 Core is undefined — a black window, or a crash at the first
GL call.

The stated primary low-power target is a Surface Pro 9 5G (Snapdragon 8cx Gen 3
/ SQ3, Adreno), i.e. Windows-on-ARM, where desktop OpenGL is not a given the way
it is on x86. Whether that machine provides a 3.3 Core context — natively, or
only with Microsoft's OpenCL/OpenGL Compatibility Pack installed — is a fact
about the device that cannot be established from this repository, and I have not
assumed either answer.

**Target-hardware validation is complete.** The branch was tested on the primary
Surface Pro 9 5G / SQ3 / Adreno reference machine in both forms:

- **Interpreted Python build:** launches and renders successfully.
- **Nuitka-compiled Windows build:** launches and renders successfully.

Therefore the stated OpenGL 3.3 Core requirement is empirically compatible with
the primary target hardware and the compiled Windows release artifact. The
previous repository-only uncertainty about whether the SQ3/Adreno environment
could provide the required context is closed for this reference configuration.

No fallback profile or speculative GL compatibility path is being added on the
basis of the former uncertainty.

A future diagnostic could still record `GL_VERSION` / `GL_RENDERER`, but it is
no longer a release-blocking validation condition for the tested target.

Worth noting the groundwork is already there: `engine.shaders.detect_low_power_arm()`
selects cheaper `*_arm` shader variants and names the SQ3 explicitly as a part
that should get them. That is the right preparation for this machine — but it
only takes effect *after* a context exists, so it does not answer R4.

---

## Assessed and cleared

* **PyGLM is required by the supported desktop runtime** — 30 files across
  `engine/` and `editor/` import `glm`, including `engine.physics` and
  `engine.player`. `requirements.txt` is correct for desktop. The unresolved
  PyGLM/python-for-android question is a property of the future Android target
  and has no bearing on the desktop release.
* **Plugin discovery under Nuitka** — tested, works (R3 above).
* **Android player / `player/`** — out of scope by the release boundaries. The
  architectural requirement (the eventual player consumes the Fio engine rather
  than a parallel implementation) held up in the last pass: `PropSession` now
  runs identically in both tiers, and `engine.player` became Qt-free and joined
  the headless boundary.

## Appendix — smaller defects, none release-blocking

| | |
|---|---|
| `generate_and_save_tilemap` spawns `[sys.executable, 'tools/generate_tilemap.py', ...]`. In a compiled build `sys.executable` is `Fio.exe`, so this launches a second editor instead of running the generator. Invisible to every test. |
| The quicksave-and-launch path looks for `game.py`, which does not exist in the tree. Guarded, so it shows a warning — a dead reference to a removed system. |
| `engine.shaders.load_shader_source` and the `SHADER_*_FILES` tables have no callers and point at `engine/shaders/`, which does not exist. Shaders are Python string constants. Dead code. |
| The Linux `.desktop` entry uses `Exec=$SELF/Fio`; `$SELF` is not expanded in desktop entries. |
| `Foundation` (PyObjC) is imported on macOS for the dock icon but is not in `requirements.txt`, so that block always fails silently under a bare `except:`. |
| `pyflakes` is an undeclared test dependency. Where it is absent, 13 use-before-assignment checks skip silently, and the suite still reports green. |

## Both `_restart_application` implementations

`editor/main_window.py` and `editor/SettingsWindow.py` each build
`[sys.executable, sys.argv[0]] + argv[1:]`. Compiled, both resolve to the Fio
binary, so a restart re-launches Fio with its own path as a stray argument.
`main.py` passes `sys.argv` to `QApplication` and never reads `argv[1]`, so this
appears harmless — but it is two copies of the same idiom, neither written with
the compiled case in mind, and worth collapsing when one of them next changes.
