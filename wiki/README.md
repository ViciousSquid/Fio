# Wiki pages to publish

Pages written or updated for this release, ready to paste into the wiki.

| File | Wiki page | What |
| :--- | :--- | :--- |
| `Soma.md` | **Soma** (new) | The physics engine: what bodies do, barrels, Prop physics properties, how it is built, measurements, the plugin surface, swapping it out |
| `Plugin-API.md` | **Plugin API** (replace) | The full reference at API 2.0 — a copy of `plugins/API.md`. New: the physics engine (Soma), player state, LogicState, flags, physics events |
| `Changelog-API-2.0.md` | **Changelog** (add entry) | The 2.0 entry |

Pages that mention these and may want a line or a link:

- **Prop** — the physics properties table (see `Soma.md`), `topples` / "Falls over like a barrel", and that physics Props now collide with each other.
- **Console commands** — `phys_engine [name]` (list or choose the physics engine for the next Play).
- **Settings** — *Enable physics* now also switches the physics engine off altogether.
- **Fio Player (Android)** — the Android app now runs the desktop's engine unmodified (no overlay patch).
- **Big World** — bodies in distant cells are held, and tier stamps are journalled.
