# Asset archives

Fio discovers these category archives in `assets/` at editor startup:

| Archive | Contents |
| --- | --- |
| `editor.fiopak` | Root images: toolbar icons, application icons, splash |
| `sprites.fiopak` | `assets/sprites/` |
| `textures.fiopak` | `assets/textures/` and `assets/noise_3d.bin` |
| `models.fiopak` | `assets/models/` |
| `fonts.fiopak` | `assets/fonts/` |
| `music.fiopak` | `assets/music/` (currently no music tracks are shipped) |
| `sounds.fiopak` | `assets/sounds/` |

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

Rebuild all categories from the repository root:

```bash
python tools/pack_assets.py
```

Or rebuild one category: `python tools/pack_assets.py sprites`. Packing is
deterministic and verifies every payload before replacing an archive. Partial
loose overrides preserve archive-only members; a category without any loose
files preserves its existing archive. Remove both the loose file and its ZIP
entry to intentionally remove an asset. Generated shaders remain outside these
archives and continue to come from `engine/shaders.py`.
