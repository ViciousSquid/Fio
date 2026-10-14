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

These are data-only ZIP archives with project-relative paths. Loose non-audio
files stay as development overrides. Startup restores missing files, byte for
byte, before constructing the splash or editor toolbar; existing files are
never overwritten. An archive-only installation therefore needs writable
asset directories. Sounds and music retain their existing archive-first native
readers and are not extracted; repack their category after editing loose audio.
Archives are not opened in rendering hot paths.

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
