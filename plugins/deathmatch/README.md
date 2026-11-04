# Deathmatch for Fio

A free-for-all game mode for Fio's **desktop editor Play mode**, using plugin
API 1.7.0. No core engine edits or additional packages are required.

## Install and play

1. Extract the distribution ZIP into your Fio repository root. Its files belong
   in `plugins/deathmatch/`, alongside `plugins/tidy/`.
2. Start Fio normally and choose **Plugins → deathmatch → Enabled** if needed.
3. Choose **Plugins → deathmatch → Load Demo map**. The normal unsaved-changes
   check runs before replacing your map.
4. Enter Play using Fio's normal Play control. The demo auto-enables the plugin
   and starts a five-player match: you and four bots of mixed difficulty.

Use Fio's normal movement, mouse-look and fire controls. Number keys **1/2**
select the pistol/shotgun. You receive both at spawn, with 24 shotgun rounds.
Hold **Tab** for the full scoreboard. After a match, press **R** to restart,
or choose **Restart Deathmatch** in the Escape pause menu at any time.

The demo ends at **15 frags or five minutes**. Tied frag totals produce a draw.
Deaths respawn after three seconds. New spawns have 1.5 seconds of protection;
firing ends your protection. A kill grants one frag; suicide or a world death
subtracts one from the victim. Repeated damage to a corpse never counts twice.

## Console commands

Open Fio's debug console during a Deathmatch Play session:

These commands are available **only while Plugins → deathmatch → Enabled is
checked**. Disabling Deathmatch removes them from console dispatch and help;
they cannot change a match while the plugin is off.

| Command | Effect |
| --- | --- |
| `addbot` | Add a normal bot with an automatic name. |
| `addbot hard` | Add a hard bot with an automatic name. |
| `addbot "New Challenger" easy` | Add a named bot at the selected difficulty. |
| `removebot` | Remove the last bot in the participant list. |
| `removebot "New Challenger"` | Remove a bot by name (case-insensitive), or use its entity ID. |
| `fraglimit 25` | Change this session's frag limit immediately. |
| `fraglimit 0` | Disable the frag limit; the time limit still applies. |
| `fraglimit` | Show the current frag limit. |

Lowering the limit to a score already reached ends the running match. Commands
also work while paused. They affect the current Play session and persist across
round restarts; they do not change the saved map. On stopping Play, added bots
are removed and removed map-authored bots are restored. Adding bots after a
match ends prepares them for the next round; press R to start it.

## Bot behaviour

- Bots fight every opposing participant, including other bots.
- Field of view, range and real world occlusion govern sight. Bots remember a
  last seen position briefly; hearing gunfire prompts investigation without
  granting visibility through walls.
- A* routes over authored waypoints. The planner checks current brush and door
  clearance before using an edge. Bots explore when they have no target.
- Bots strafe, keep a preferred fighting distance, backpedal at low health,
  and attempt a jump and a short detour when stuck.
- Easy/normal/hard skills change reaction delay, aim error, firing interval and
  damage. Shots trace actual rays, hit the nearest fighter and stop at walls.
- Native Fio Player physics handles gravity, walls, steps, slopes and water.
  Native Monster identity provides billboard sprites and weapon hit detection.
  The plugin reserves `disabled=True` on its bots so stock Monster AI cannot
  move or attack for them as well.

## Build your own arena

Place entities from **Plugins → deathmatch**:

| Entity | Purpose |
| --- | --- |
| Match Settings | Exactly one per arena. Starts the mode. |
| Bot Opponent | One bot per entity; edit skill, health and perception settings. |
| Respawn Point | Shared spawn, placed at **floor/feet height**. Add more than the participant count if possible. |
| Navigation Node | Waypoint at **floor height**, with a unique name. |

Add a normal **PlayerStart** so Fio can enter Play. Deathmatch replaces its
initial position with a shared respawn. Spawn angles use the runtime convention:
**0° = +Z, 90° = +X**, independent of the editor's PlayerStart convention.
Respawn selection prefers unoccupied positions farther from opponents and
behind cover. Positions must have room for the player hull.

Connect navigation nodes using comma-separated **names** in `links`, such as
`West_Corner,Central_Hall`. Links work in both directions. Place enough nodes
around corners and obstacles; keep linked segments on walkable floors. This is
an authored waypoint planner, not an automatic navmesh bake: it does not plan
intentional jumps, ladders or routes across cliffs. Without waypoints, bots
still use direct pursuit and local exploration, with less reliable navigation
around complex geometry.

Match Settings exposes `frag_limit` (default 20), `time_limit` in seconds
(default 300), `respawn_delay`, `spawn_protection`, `seed`, and `show_hud`.
Zero disables either limit; zero for both allows continuous play. The seed
controls bot decisions and spawn tie-breaking for a given sequence of ticks.

Maps without Match Settings stay inert even if the plugin is enabled. Stopping
Play or disabling Deathmatch restores bot properties/positions, the player's
loadout and every installed engine hook. Pauses freeze the match and respawns.
During the player's death countdown, the surviving bots continue fighting.

## Map I/O and events

Match Settings inputs: **Restart**, **EndMatch**.

Outputs: **OnMatchStart**, **OnFrag** (kill feed text), **OnMatchEnd** (winner
name or `Draw`). Bots expose native **OnDamaged** and **OnDeath** outputs.

Other plugins can listen for `deathmatch.start`, `deathmatch.frag`,
`deathmatch.respawn`, and `deathmatch.end` through PluginHost. Frag events use
fighter IDs (`player` for the human); end events include `winner` and `reason`.

## Compatibility and limits

- Targets Fio's desktop editor engine with plugin API 1.7.0, including the
  native `LogicCombat`, `LogicPlayer` and `LogicThread` runtimes. Tested against
  repository commit `fa1d28c` in the supplied workspace. These internal seams
  can require adjustment if Fio changes them.
- The standalone `.fiopak` player/Android bridge currently lacks the necessary
  world combat services. The plugin loads there but logs a message and does
  not start a match. A `.fiopak` is a world container: install Python plugin
  code separately rather than placing it inside the package.
- The supported scoring loadout is the native pistol and shotgun (and native
  hitscan/melee weapons). Native projectiles can damage bots, but the current
  engine discards their shooter identity before reporting monster damage.
  Those deaths count as world deaths, without awarding a guessed frag.
- Single local human versus bots; network multiplayer and split-screen scoring
  are not implemented. Dedicated arena maps are recommended.
- Match scores and AI memory are per Play session. Native save-game loading
  does not restore this plugin's round state; restart the match after loading.

## Verification

From the Fio root, with Fio's desktop dependencies and pytest installed:

```sh
SDL_AUDIODRIVER=dummy QT_QPA_PLATFORM=offscreen PYOPENGL_PLATFORM=egl \
  python -m pytest plugins/deathmatch/tests -q
```

The suite uses real Fio engine objects for combat, physics, respawns, scoring,
perception and routing. It includes an editor Play/HUD/Stop check and an entire
simulated demo round. The dummy audio driver keeps headless runs independent
of an audio device.

The plugin is distributed under the MIT license; see `LICENSE`.
