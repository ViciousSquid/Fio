# Quake III source attribution

This plugin includes Python adaptations of gameplay algorithms from
[id Software's Quake III Arena source](https://github.com/id-Software/Quake-III-Arena):

- `code/game/g_client.c`: furthest-half randomized spawn selection.
- `code/game/g_team.c`: two-team flag pickup, return, drop and capture rules.
- `code/game/g_team.h`: team gameplay constants.
- `code/botlib/be_ai_goal.c`: item desirability weighted by travel cost.

Original Quake III source: Copyright (C) 1999–2005 id Software, Inc.
Python adaptations and Fio integration: Copyright (C) 2026 Fio plugin contributors.

The Deathmatch plugin is distributed under **GPL-2.0-or-later**, with the full
license in `LICENSE`. The existing Fio engine's MIT notices are retained.
No Quake III artwork, game data, models, sounds or binaries are included.
