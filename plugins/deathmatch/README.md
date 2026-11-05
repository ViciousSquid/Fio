# Deathmatch for Fio

A Deathmatch, Team Deathmatch and Capture the Flag plugin for Fio's **desktop editor Play mode**, using plugin
API 1.7.0. No additional packages are required. The accompanying core changes
in `engine/prop_runtime.py` and `engine/logic_combat.py` provide actor-aware
pickup collection and projectile damage ownership; install those changes too.

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

## LAN multiplayer GUI

Install the same Fio checkout and this plugin on each computer, then open
**Plugins → deathmatch → LAN Multiplayer…**.

- **Host:** enter your player/game names, select Deathmatch, Capture the Flag,
  or Team Deathmatch, choose the bundled arena or your current map, set the
  appropriate score limit and click **Host game**.
- **Join:** click **Refresh LAN games**, select a discovered game and click
  **Join selected game**. The list shows game type, player count and address.
  Alternatively enter the host's LAN address and click **Join address**.
- Choose red/blue or auto team assignment before joining. Up to eight human
  connections are supported; bots can be added by the host's console.

Discovery uses **UDP 27960**, and play uses **TCP 27961**. Permit those ports on
the host's local firewall. Discovery broadcasts stay on the local subnet;
direct address joining works when broadcasts are unavailable. This is LAN
play, without Internet matchmaking. Peers must already have the referenced
assets/plugins installed; the handshake transfers JSON map data, not code or
asset files.

The host simulates remote movement, combat, pickups, respawns and flags. Clients
send movement/look/fire/use inputs and receive actor, inventory, pickup, score
and flag snapshots, with local movement prediction and server correction.
Host pauses and round results propagate to clients. The host controls restart
and match console commands; client commands cannot change authoritative scores.
Disconnects remove the remote player and drop any carried flag. Stopping Play
closes the LAN session and restores the edited map.

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

For illustrated routing examples and node placement advice, see the
[bot navigation guide](NAVIGATION_GUIDE.md).

`botdebug` opens a floating live overview and one tab per bot, including
ordinary monsters. It is a core editor command and also works while the
Deathmatch plugin is disabled. Use `botdebug off` to close it.

- Bots fight every opposing participant, including other bots.
- Field of view, range and real world occlusion govern sight. Bots remember a
  last seen position briefly; hearing gunfire prompts investigation without
  granting visibility through walls.
- A* routes over authored waypoints. The planner checks current brush and door
  clearance before using an edge. Bots explore when they have no target.
- Bots strafe, keep a preferred fighting distance, backpedal at low health,
  and attempt a jump and a short detour when stuck.
- Easy/normal/hard skills change reaction delay, aim error and firing interval.
  Bots start with a pistol, seek useful weapon/ammo Props, and switch weapons
  by range, ammunition and damage. Collected weapons use their compiled firing
  mode (hitscan, melee or projectile), damage, pellets, spread and cooldown.
- Wounded bots seek health, including during a retreat. Native health, ammo,
  weapon, armor and key Props share consumption, use/walk-over activation,
  OnCollected I/O and respawns with the human player. Full-health bots leave
  health for others. Each life resets the bot's inventory.
- Native Fio Player physics handles gravity, walls, steps, slopes and water.
  Native Monster identity provides billboard sprites and weapon hit detection.
  The plugin reserves `disabled=True` on its bots so stock Monster AI cannot
  move or attack for them as well.

## Teams and Capture the Flag

Choose `deathmatch`, `teamdeathmatch`, or `ctf` in Match Settings. Team modes
use red and blue; set `player_team`, bot `team` (`auto` balances), and spawn
`team` (`any` is a fallback). `addbot "Ranger" hard blue` assigns a team.
Friendly fire is off by default. Blue bots use Fio's flying monster `variant1`,
red bots its base flying sprite; movement still uses player physics.

**Plugins → deathmatch → Load CTF map** loads a playable two-base arena with
visible red and blue flag sprites, team spawns, navigation and pickups. To
author CTF, place exactly one red and one blue **CTF Flag** at floor height.
Take the enemy flag to your base while your own flag is home to capture.
Carriers drop flags on death or removal; touch your dropped flag to return it.
Unattended flags return after `flag_return_time` (default 40 seconds, from
Quake III). `capture_limit` defaults to 10; zero disables it. Frag limits
do not end CTF games. Team Deathmatch uses combined team frags and penalizes
team kills/suicides. The scoreboard shows team scores and flag status.

Bots attack, escort carriers, recover their own flag and defend base. The
spawn selector adapts Quake III's randomized furthest-half algorithm, avoiding
occupied spawns and preferring the participant's team spawns.

## Build your own arena

Place entities from **Plugins → deathmatch**:

| Entity | Purpose |
| --- | --- |
| Match Settings | Exactly one per arena. Starts the mode. |
| Bot Opponent | One bot per entity; edit skill, health and perception settings. |
| Respawn Point | Shared spawn, placed at **floor/feet height**. Add more than the participant count if possible. |
| Bot Navigation Node | Marker at any convenient height, with a unique name and arrival `radius`. |

Add a normal **PlayerStart** so Fio can enter Play. Deathmatch replaces its
initial position with a shared respawn. Spawn angles use the runtime convention:
**0° = +Z, 90° = +X**, independent of the editor's PlayerStart convention.
Respawn selection prefers unoccupied positions farther from opponents and
behind cover. Positions must have room for the player hull.

New **Bot Navigation Nodes** use a teal diamond marker and automatically connect
to up to six reachable neighbours within `link_range` (default 512 units).
Fine-tune their comma-separated `links` afterwards; Ctrl-K dragging adds a link.
Links work in both directions. Placement preserves existing manual edits, and
loading/playing a map does not regenerate links. Moving a node keeps its links
for review. `auto_link` controls its placement-time behaviour.

Marker height is flexible: standing surfaces and route connectivity determine
the actual walking height. Bots arrive within the node's `radius` when the next
segment is safe. Live validation rejects walls, unsupported gaps and closed or
partly open doors. Bots can wait for, board, ride and disembark active cyclic
linear and path-driven platforms with clear, supported approaches. The mover's
ordinary PathNodes remain distinct from the bot's teal navigation nodes.

This is a waypoint planner, not an automatic navmesh bake. It does not plan
intentional jumps, ladders, rotating/scripted movers or routes across cliffs.
Without waypoints, bots still use direct pursuit and local exploration, with
less reliable navigation around complex geometry. See the illustrated guide
for door, platform and node placement examples. Ctrl-K also creates native
PathNode chains and appropriate entity I/O links.

Match Settings exposes `frag_limit` (default 50), `time_limit` in seconds
(default 1800, or 30 minutes), `respawn_delay`, `spawn_protection`, `seed`, and `show_hud`.
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
- Native projectile damage retains its owner for plugin scoring. Bot and human
  projectile kills therefore receive the same credit as hitscan kills.
- LAN arenas replicate players, bots, pickup availability and flag state.
  Arbitrary map scripts, moving world geometry, carried physics Props and
  split-screen scoring are not synchronized; use dedicated arena maps.
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

The LAN tests also use real UDP/TCP loopback sockets and two native Fio logic
sessions, including remote movement, shooting, pickup consumption, pause,
respawn, disconnect and CTF capture replication. Run them with local socket
access enabled. Cross-machine play and a full OpenGL visual playtest still need
to be checked on the target LAN/graphics setup.

The plugin includes Quake III-derived gameplay algorithms and is distributed
under **GPL-2.0-or-later**. See `LICENSE` and `NOTICE.md` for the original id
Software attribution and source references. Fio's existing MIT notices remain.

CTF flags use transparent red/blue billboards and overlay a smaller flag icon on
the carrier. Each flag always emits its colour of light at home, while dropped
and while carried. `dflaglight` / `dflaglight on` enables shadow casting for both
flag lights in your own view; `dflaglight off` keeps the lights on without shadows.
This setting is per-client, is never replicated, and survives round restarts. `botdebug` refreshes
twice per second (500 ms).
