# .fiopak end-to-end check

Builds a world in the editor, exports it through the export dialog, and plays
the package in two other places, comparing the engine's dense tables (what
Debug Tables shows) between them.

| Script | Run from | What it does |
| --- | --- | --- |
| `e2e_author.py <out>` | an *author* checkout | Adds assets that exist only there (texture, mixed-case model skin, OBJ+MTL, WAV, cutscene), builds a two-level world through `MainWindow` (boxes, an angled brush, door + trigger I/O, water, lights, monster, model and collectable Props, speaker, cutscene camera, LevelChanger), checks undo/redo, saves, reloads, plays 120 stepped ticks while recording Debug Tables and pixels, then exports `e2e_world.fiopak`. |
| `e2e_fresh.py <package> <out>` | a *pristine* checkout | Opens the package (Tools > Play Game Package path), records the same capture, then checks package-only assets, the packaged cutscene, the level change to the packaged second map, kiosk mode, and that the package is untouched and nothing was extracted. |
| `e2e_android.py <package> <out> <fresh_direct.npz>` | a staged Android runtime (`python -m player.tools.stage_runtime <dir>`) | Opens the package with the player's reader, world and GLES renderer under a real OpenGL ES 3 context, compares the dense tables with the desktop's, draws, plays 600 ticks, and changes level. |

```bash
git worktree add --detach /tmp/author HEAD && git worktree add --detach /tmp/fresh HEAD
(cd /tmp/author && LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python "$OLDPWD/tools/package_e2e/e2e_author.py" /tmp/out)
(cd /tmp/fresh  && LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python "$OLDPWD/tools/package_e2e/e2e_fresh.py" /tmp/out/e2e_world.fiopak /tmp/out)
python tools/oracle_diff.py /tmp/out/author.npz /tmp/out/fresh.npz      # only geo_epoch may differ
python -m player.tools.stage_runtime /tmp/android
(cd /tmp/android && PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless LIBGL_ALWAYS_SOFTWARE=1 SDL_AUDIODRIVER=dummy \
    python "$OLDPWD/tools/package_e2e/e2e_android.py" /tmp/out/e2e_world.fiopak /tmp/out /tmp/out/fresh_direct.npz)
```

`render_table/geo_epoch` is a per-process edit counter for an angled brush's
shape, not world state: it is the one column expected to differ. Both desktop
scripts write to `settings.ini` in the checkout they run in (`git checkout
settings.ini` afterwards). A real device is exercised by the *Android Player
Build* workflow's emulator job, with the showcase world.
