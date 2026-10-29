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

These are data-only ZIP archives with project-relative paths. Fio validates
their member indexes on startup and reads each file on demand without unpacking
it into the project directory. All seven categories — including sprites,
textures, models, fonts and editor icons — use archive-backed resource readers.
Sound and music continue to use their dedicated direct archive readers.

On startup, Fio creates the loose development directories `assets/sprites/`,
`assets/textures/`, `assets/models/`, `assets/fonts/`, `assets/sounds/`
and `assets/music/` if they do not already exist. They are **blank workspaces**:
no archive members are copied into them. The `editor.fiopak` category stores
root-level images in `assets/`, so it needs no separate `assets/editor/` folder.

Loose files are intentional development overrides, and take precedence over
matching category archive members. Existing files are never deleted or
overwritten. Read-only installations can still mount and load their archives
if directory creation is denied; adding loose content requires write access.
Repack a category after changing loose files to update its archive. Existing
texture/model/sprite caches prevent archive reads on every rendered frame.

Portable world `.fiopak` packages launched through Play Game Package are
also mounted and read without temporary extraction. A portable package's
resource paths take priority during play, keeping its assets self-contained.

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
