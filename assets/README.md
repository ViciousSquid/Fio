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

Loose files are intentional development overrides, and take precedence over
the matching ZIP member. No existing loose files are deleted or overwritten.
An archive-only installation does not need write access to its asset directories.
Repack a category after changing loose files to update its archive. Existing
texture/model/sprite caches still prevent archive reads on every rendered frame.

Portable world `.fiopak` packages handled by the desktop editor's Play Package
flow are separate from these seven category archives; that flow still uses a
temporary extraction directory and is not covered by this mounting mechanism.

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
