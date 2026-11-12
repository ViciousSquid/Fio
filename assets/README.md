# Asset archives

Fio discovers these category archives in `assets/` at editor startup:

| Archive | Contents |
| --- | --- |
| `editor.fiopak` | Root images: toolbar icons, application icons, splash; `assets/editor/` (the player-start sign) |
| `sprites.fiopak` | `assets/sprites/` |
| `textures.fiopak` | `assets/textures/` and `assets/noise_3d.bin` |
| `models.fiopak` | `assets/models/` |
| `fonts.fiopak` | `assets/fonts/` |
| `music.fiopak` | `assets/music/` (currently no music tracks are shipped) |
| `sounds.fiopak` | `assets/sounds/` (not shipped: sounds are a loose folder) |

These are data-only ZIP archives with project-relative paths, **read in
place: nothing is extracted**. Startup indexes each archive
(`engine/asset_archives.py`); every loader -- textures, sprites, models,
fonts, editor icons, the asset browser -- asks for an asset by its usual path
and gets the archive member's bytes (`asset_bytes`, `asset_source`, and the Qt
helpers in `engine/asset_qt.py`). A loose file at the same path still wins, as
a development override, and a member is also found by a case-insensitive
match. An archive-only installation needs no writable asset directories.
Archives are opened per read and never held, so a repack can replace one while
the editor runs; the next read sees it. Sounds and music keep their streaming
readers (`engine/audio_archives.py`); repack their category after editing
loose audio. Archives are not opened in rendering hot paths: a texture or
model is read once, when it is first loaded.

## Your own packs

Any other `.fiopak` you put in `assets/` is a **user pack**: a ZIP of your
own textures, models, sounds, music or sprites. Its contents appear in the
Asset Browser's Textures, Models and Audio tabs beside the loose files, load
everywhere a loose asset would, and are carried into exported game packages;
nothing is extracted. Arrange it by category folder -- `textures/...`,
`models/...`, `sounds/...`, `music/...`, `sprites/...` -- with or without a
leading `assets/`, or zip one folder that contains them. Anything else in the
pack (and archiver litter such as `__MACOSX/`) is ignored. Loose files and the
built-in archives win over a pack with the same path; packs are searched in
name order. A pack added, replaced or removed while the editor runs is picked
up when `assets/` changes.

Rebuild all categories from the repository root:

```bash
python tools/pack_assets.py
```

Or rebuild one category: `python tools/pack_assets.py sprites`. Sounds ship as
the loose `assets/sounds/` folder, so a bare `pack_assets.py` skips them; name
the category (`pack_assets.py sounds`) to build `sounds.fiopak` anyway. Packing is
deterministic and verifies every payload before replacing an archive. Partial
loose overrides preserve archive-only members; a category without any loose
files preserves its existing archive. Remove both the loose file and its ZIP
entry to intentionally remove an asset. Generated shaders remain outside these
archives and continue to come from `engine/shaders.py`.
