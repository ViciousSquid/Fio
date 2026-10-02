"""
Logic Wizard
============
A guided QWizard that lets you wire up common I/O scenarios without
touching the raw connection editor.

Built-in scenarios are grouped into four categories:

  Monster Encounters (7)
    Locked Room, Alarm System, Chain Reaction, Death Trap,
    Kill the Lights, Timed Patrol, Ambush Swarm

  Doors & Movers (7)
    Button Door, Timed Door, Locked Door Hint, Pickup Unlocks Path,
    Elevator, Light Switch, Respawning Pickup

  Environment & Audio (5)
    Flickering Light, Ambient Soundscape, Trap Corridor,
    Scripted Reveal, Timed Hazard

  Complex Logic (7)
    Arena Battle, Security System, Puzzle Door (Multi-Switch),
    Gauntlet Sequence, Level Exit Sequence, Relay Chain,
    Disable on Trigger

Usage
-----
  from editor.logic_wizard import LogicWizard
  wiz = LogicWizard(editor_state, graph_scene, parent=window)
  if wiz.exec_() == QDialog.Accepted:
      ...  # connections added to graph_scene; call apply_to_entities() to save
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any

from PyQt5.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QDoubleSpinBox, QCheckBox, QFormLayout,
    QGroupBox, QListWidget, QListWidgetItem,
    QWidget, QSpinBox, QApplication, QMessageBox, QPushButton
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui  import QFont, QFontMetrics

try:
    from .io_system import get_entity_type_for_io, IO_REGISTRY
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False


# ── Scenario catalogue ────────────────────────────────────────────────────────

SCENARIOS: List[Dict[str, Any]] = [

    # ═════════════════════════════════════════════════════════════════════════
    #  MONSTER ENCOUNTERS
    # ═════════════════════════════════════════════════════════════════════════

    {
        "id":    "locked_room",
        "title": "Locked Room",
        "icon":  "🚪",
        "short": "Monster sleeps until a door opens",
        "desc":  (
            "A monster starts dormant (triggered=True) and only wakes "
            "when a door is opened via another trigger or by the player. "
            "Great for ambushes behind unlocked doors."
        ),
        "wiring": [
            ("door", "OnOpen", "monster", "Wake",
             "Door opens → Monster wakes"),
        ],
        "roles": [
            ("door",    "door",    "The door that unlocks the monster"),
            ("monster", "monster", "The monster to wake"),
        ],
        "positions": {
            "door": [0, 0, 0],
            "monster": [0, 0, -256],
        },
        "params": [],
    },
    {
        "id":    "alarm_system",
        "title": "Alarm System",
        "icon":  "🔔",
        "short": "Monster spotting the player triggers an alarm sound",
        "desc":  (
            "When the monster first sees the player it fires OnSeePlayer, "
            "which starts a speaker (alarm, music, etc.). "
            "A second connection stops the alarm when the monster loses sight."
        ),
        "wiring": [
            ("monster", "OnSeePlayer",  "speaker", "PlaySound",
             "Monster sees player → Speaker plays"),
            ("monster", "OnLostPlayer", "speaker", "StopSound",
             "Monster loses sight → Speaker stops"),
        ],
        "roles": [
            ("monster", "monster", "The monster watching for the player"),
            ("speaker", "speaker", "The speaker / alarm to activate"),
        ],
        "params": [],
    },
    {
        "id":    "chain_reaction",
        "title": "Chain Reaction",
        "icon":  "💥",
        "short": "Killing one monster wakes another",
        "desc":  (
            "When the first monster dies (OnDeath) it sends a Wake signal "
            "to a second dormant monster.  Chain as many as you like by "
            "applying the wizard repeatedly."
        ),
        "wiring": [
            ("monster_a", "OnDeath", "monster_b", "Wake",
             "Monster A dies → Monster B wakes"),
        ],
        "roles": [
            ("monster_a", "monster", "The monster that triggers on death"),
            ("monster_b", "monster", "The monster to wake"),
        ],
        "positions": {
            "monster_a": [0, 0, 0],
            "monster_b": [0, 0, -256],
        },
        "params": [],
    },
    {
        "id":    "death_trap",
        "title": "Death Trap",
        "icon":  "⚙",
        "short": "Monster death opens a door or activates a mover",
        "desc":  (
            "Killing a monster opens a door (or fires any connected mover). "
            "Use this for 'kill the guard to open the vault' moments."
        ),
        "wiring": [
            ("monster", "OnDeath", "door", "Open",
             "Monster dies → Door opens"),
        ],
        "roles": [
            ("monster", "monster", "The monster that must be killed"),
            ("door",    "door",    "The door/mover to activate"),
        ],
        "positions": {
            "monster": [0, 0, -128],
            "door": [0, 0, 0],
        },
        "params": [],
    },
    {
        "id":    "kill_lights",
        "title": "Kill the Lights",
        "icon":  "💡",
        "short": "Monster spotting player turns off a light",
        "desc":  (
            "The moment a monster first sees the player it fires OnSeePlayer, "
            "triggering a light to turn off.  Pair with an ambient speaker "
            "for a jump-scare atmosphere."
        ),
        "wiring": [
            ("monster", "OnSeePlayer",  "light", "TurnOff",
             "Monster sees player → Light turns off"),
            ("monster", "OnLostPlayer", "light", "TurnOn",
             "Monster loses sight → Light back on"),
        ],
        "roles": [
            ("monster", "monster", "The monster watching the area"),
            ("light",   "light",   "The light to extinguish"),
        ],
        "positions": {
            "monster": [0, 0, 0],
            "light": [0, 128, 0],
        },
        "params": [],
    },
    {
        "id":    "timed_patrol",
        "title": "Timed Patrol",
        "icon":  "⏱",
        "short": "A timer wakes a dormant monster after N seconds",
        "desc":  (
            "A LogicTimer fires repeatedly (or once) and sends Wake to a "
            "monster.  Set the timer interval in Properties to control the delay. "
            "Use fire_once on the connection to wake only once."
        ),
        "wiring": [
            ("timer", "OnTimer", "monster", "Wake",
             "Timer fires → Monster wakes"),
        ],
        "roles": [
            ("timer",   "logic_timer", "The timer that controls the delay"),
            ("monster", "monster",     "The monster to patrol"),
        ],
        "params": [
            {
                "key":     "fire_once",
                "label":   "Wake only once",
                "type":    "bool",
                "default": True,
                "help":    "If checked the connection fires once; otherwise re-wakes every interval.",
            }
        ],
    },
    {
        "id":    "ambush_swarm",
        "title": "Ambush Swarm",
        "icon":  "👹",
        "short": "A trigger wakes three monsters at once",
        "desc":  (
            "A single trigger fires OnTrigger and fan-outs to Wake on "
            "three dormant monsters simultaneously.  All three rush the "
            "player at once.  Remove roles you don't need, or run the "
            "wizard again to add more."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "monster_a", "Wake",
             "Trigger fires → Monster A wakes"),
            ("trigger", "OnTrigger", "monster_b", "Wake",
             "Trigger fires → Monster B wakes"),
            ("trigger", "OnTrigger", "monster_c", "Wake",
             "Trigger fires → Monster C wakes"),
        ],
        "roles": [
            ("trigger",   "trigger", "The ambush trigger"),
            ("monster_a", "monster", "First monster"),
            ("monster_b", "monster", "Second monster"),
            ("monster_c", "monster", "Third monster"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "monster_a": [-128, 0, -256],
            "monster_b": [0, 0, -256],
            "monster_c": [128, 0, -256],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot ambush",
                "type":    "bool",
                "default": True,
                "help":    "Ambush fires only once.",
            }
        ],
    },

    # ═════════════════════════════════════════════════════════════════════════
    #  DOORS & MOVERS
    # ═════════════════════════════════════════════════════════════════════════

    {
        "id":    "button_door",
        "title": "Button Door",
        "icon":  "🔘",
        "short": "Stepping on a trigger opens a door",
        "desc":  (
            "The simplest classic setup: a floor trigger opens a door "
            "when the player walks over it.  Optionally the door closes "
            "again when the player leaves the trigger."
        ),
        "wiring": [
            ("trigger", "OnStartTouch", "door", "Open",
             "Player enters trigger → Door opens"),
            ("trigger", "OnEndTouch", "door", "Close",
             "Player leaves trigger → Door closes"),
        ],
        "roles": [
            ("trigger", "trigger", "The floor trigger / button"),
            ("door",    "door",    "The door to open"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "door": [0, 0, -128],
        },
        "params": [],
    },
    {
        "id":    "timed_door",
        "title": "Timed Door",
        "icon":  "⏳",
        "short": "Door opens, then auto-closes after a delay",
        "desc":  (
            "A trigger opens a door, then a logic_timer closes it after "
            "a configurable number of seconds.  Good for 'get through "
            "before it shuts' pressure moments.  Set the timer interval "
            "in Properties to control how long the door stays open."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "door",  "Open",
             "Trigger fires → Door opens"),
            ("trigger", "OnTrigger", "timer", "Enable",
             "Trigger fires → Timer starts countdown"),
            ("timer",   "OnTimer",   "door",  "Close",
             "Timer elapses → Door closes"),
            ("timer",   "OnTimer",   "timer", "Disable",
             "Timer elapses → Timer stops itself"),
        ],
        "roles": [
            ("trigger", "trigger",     "The trigger that starts it all"),
            ("door",    "door",        "The door that opens and later closes"),
            ("timer",   "logic_timer", "Timer that controls the close delay"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "door": [0, 0, -128],
            "timer": [192, 0, 0],
        },
        "params": [],
    },
    {
        "id":    "locked_door_hint",
        "title": "Locked Door Hint",
        "icon":  "🔒",
        "short": "Trying a locked door plays a sound hint",
        "desc":  (
            "When the player uses a locked door, instead of silence "
            "a speaker plays a hint sound (rattle, voice line, buzzer). "
            "Pair with a key pickup elsewhere to unlock it."
        ),
        "wiring": [
            ("door",    "OnLockedUse", "speaker", "PlaySound",
             "Player tries locked door → Hint sound plays"),
        ],
        "roles": [
            ("door",    "door",    "The locked door"),
            ("speaker", "speaker", "Speaker that plays the hint"),
        ],
        "positions": {
            "door": [0, 0, 0],
            "speaker": [64, 64, 0],
        },
        "params": [],
    },
    {
        "id":    "pickup_unlocks_path",
        "title": "Pickup Unlocks Path",
        "icon":  "🔑",
        "short": "Collecting a pickup opens a door or moves a blocker",
        "desc":  (
            "When the player picks up an item (key, collectible, etc.) "
            "a door opens or a mover activates, revealing a new path.  "
            "A speaker can optionally play a 'path opened' chime."
        ),
        "wiring": [
            ("pickup",  "OnPickedUp", "door",    "Open",
             "Pickup collected → Door opens"),
            ("pickup",  "OnPickedUp", "speaker", "PlaySound",
             "Pickup collected → Chime plays"),
        ],
        "roles": [
            ("pickup",  "pickup",  "The pickup item to collect"),
            ("door",    "door",    "The door or mover to activate"),
            ("speaker", "speaker", "Optional chime or voice line"),
        ],
        "positions": {
            "pickup": [0, 0, 0],
            "door": [0, 0, -384],
            "speaker": [64, 64, -384],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot",
                "type":    "bool",
                "default": True,
                "help":    "Connection fires only once (pickup can only be collected once anyway).",
            }
        ],
    },
    {
        "id":    "elevator",
        "title": "Elevator",
        "icon":  "🛗",
        "short": "Two triggers move a platform up and down",
        "desc":  (
            "A trigger at the bottom sends the mover to its end position "
            "(up), and a trigger at the top sends it back to the start "
            "(down).  Use for lifts, platforms, or any two-button mover."
        ),
        "wiring": [
            ("trigger_bottom", "OnTrigger", "mover", "Open",
             "Bottom trigger → Mover goes up"),
            ("trigger_top",    "OnTrigger", "mover", "Close",
             "Top trigger → Mover goes down"),
        ],
        "roles": [
            ("trigger_bottom", "trigger", "Trigger at the bottom (call up)"),
            ("trigger_top",    "trigger", "Trigger at the top (send down)"),
            ("mover",          "mover",   "The moving platform"),
        ],
        "positions": {
            "trigger_bottom": [0, 0, 0],
            "mover": [0, 64, 0],
            "trigger_top": [0, 256, 0],
        },
        "params": [],
    },
    {
        "id":    "light_switch",
        "title": "Light Switch",
        "icon":  "🔦",
        "short": "Trigger toggles a light on and off each activation",
        "desc":  (
            "Each time the player activates the trigger it sends Toggle "
            "to a light, flipping it between on and off.  Simple and "
            "reusable — wire multiple lights to the same trigger for "
            "a room-wide switch."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "light", "Toggle",
             "Trigger fires → Light toggles"),
        ],
        "roles": [
            ("trigger", "trigger", "The switch / trigger"),
            ("light",   "light",   "The light to toggle"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "light": [0, 128, -64],
        },
        "params": [],
    },
    {
        "id":    "respawning_pickup",
        "title": "Respawning Pickup",
        "icon":  "♻",
        "short": "Pickup respawns after a timer delay",
        "desc":  (
            "When the player collects the pickup it starts a timer.  "
            "When the timer fires it respawns the pickup and stops "
            "itself.  Set the timer's interval in Properties to control "
            "the respawn delay (e.g. 10 seconds for health, 30 for ammo)."
        ),
        "wiring": [
            ("pickup", "OnPickedUp", "timer",  "Enable",
             "Pickup collected → Timer starts"),
            ("timer",  "OnTimer",    "pickup", "Respawn",
             "Timer fires → Pickup respawns"),
            ("timer",  "OnTimer",    "timer",  "Disable",
             "Timer fires → Timer stops itself"),
        ],
        "roles": [
            ("pickup", "pickup",      "The pickup item to respawn"),
            ("timer",  "logic_timer", "Timer controlling the respawn delay"),
        ],
        "positions": {
            "pickup": [0, 0, 0],
            "timer": [192, 0, 0],
        },
        "params": [],
    },

    # ═════════════════════════════════════════════════════════════════════════
    #  ENVIRONMENT & AUDIO
    # ═════════════════════════════════════════════════════════════════════════

    {
        "id":    "flickering_light",
        "title": "Flickering Light",
        "icon":  "✨",
        "short": "A timer toggles a light on and off repeatedly",
        "desc":  (
            "A LogicTimer repeatedly toggles a light, creating a "
            "flickering or strobing effect.  Set the timer interval in "
            "Properties to control the flicker speed (e.g. 0.3s for "
            "fast flicker, 2s for slow pulse)."
        ),
        "wiring": [
            ("timer", "OnTimer", "light", "Toggle",
             "Timer fires → Light toggles on/off"),
        ],
        "roles": [
            ("timer", "logic_timer", "Timer controlling the flicker rate"),
            ("light", "light",       "The light to flicker"),
        ],
        "params": [],
    },
    {
        "id":    "ambient_soundscape",
        "title": "Ambient Soundscape",
        "icon":  "🎵",
        "short": "Player spawn starts background music/ambience",
        "desc":  (
            "When the player spawns, one or more speakers start playing "
            "ambient audio.  The speaker's own properties control looping "
            "and volume."
        ),
        "wiring": [
            ("spawn",   "OnPlayerSpawn", "speaker", "PlaySound",
             "Player spawns → Ambience starts"),
        ],
        "roles": [
            ("spawn",   "playerstart", "The player start point"),
            ("speaker", "speaker",     "Speaker playing ambient audio"),
        ],
        "params": [],
    },
    {
        "id":    "trap_corridor",
        "title": "Trap Corridor",
        "icon":  "🕳",
        "short": "Trigger removes floor brush and plays a sound",
        "desc":  (
            "When the player steps on a trigger the floor beneath them "
            "is killed (removed), they fall, and a trap sound plays. "
            "The brush must be named so the I/O system can target it."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "floor",   "Kill",
             "Trigger fires → Floor brush removed"),
            ("trigger", "OnTrigger", "speaker", "PlaySound",
             "Trigger fires → Trap sound plays"),
        ],
        "roles": [
            ("trigger", "trigger", "The trap trigger"),
            ("floor",   "brush",   "The floor brush to remove"),
            ("speaker", "speaker", "Sound effect for the trap"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "floor": [0, -64, 0],
            "speaker": [128, 64, 0],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot trap",
                "type":    "bool",
                "default": True,
                "help":    "If checked the trap can only fire once.",
            }
        ],
    },
    {
        "id":    "scripted_reveal",
        "title": "Scripted Reveal",
        "icon":  "🎭",
        "short": "Trigger removes a wall and fades in a light",
        "desc":  (
            "A trigger disables a brush (making a wall disappear) and "
            "fades in a light to reveal a hidden area, secret room, or "
            "dramatic vista.  Set the light's FadeIn parameter to "
            "control how many seconds the fade takes (e.g. '2.0')."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "wall",  "Disable",
             "Trigger fires → Wall disappears"),
            ("trigger", "OnTrigger", "light", "FadeIn",
             "Trigger fires → Light fades in"),
        ],
        "roles": [
            ("trigger", "trigger", "The reveal trigger"),
            ("wall",    "brush",   "The wall brush to remove"),
            ("light",   "light",   "Light that reveals the hidden area"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "wall": [0, 0, -128],
            "light": [0, 64, -192],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot reveal",
                "type":    "bool",
                "default": True,
                "help":    "Reveal happens only once.",
            }
        ],
    },
    {
        "id":    "timed_hazard",
        "title": "Timed Hazard",
        "icon":  "⚠",
        "short": "Timer toggles a mover back and forth in a loop",
        "desc":  (
            "A LogicTimer repeatedly toggles a mover, creating a "
            "looping hazard — crushing ceiling, swinging gate, "
            "retracting bridge.  Set the timer interval in Properties "
            "to control the cycle speed."
        ),
        "wiring": [
            ("timer", "OnTimer", "mover", "Toggle",
             "Timer fires → Mover toggles direction"),
        ],
        "roles": [
            ("timer", "logic_timer", "Timer controlling the cycle"),
            ("mover", "mover",       "The hazard mover"),
        ],
        "positions": {
            "timer": [192, 0, 0],
            "mover": [0, 0, 0],
        },
        "params": [],
    },

    # ═════════════════════════════════════════════════════════════════════════
    #  COMPLEX LOGIC  (multi-entity, multi-step)
    # ═════════════════════════════════════════════════════════════════════════

    {
        "id":    "arena_battle",
        "title": "Arena Battle",
        "icon":  "⚔",
        "short": "Trigger locks doors, wakes monsters; last kill opens exit",
        "desc":  (
            "COMPLEX — 5 connections, 5 roles.\n\n"
            "Walking into the arena trigger locks the entrance and "
            "wakes two monsters.  Each monster's OnDeath sends Trigger "
            "to a logic_gate (set to AND-2).  When both are dead the "
            "gate fires and opens the exit door.\n\n"
            "Set the logic_gate's 'required_inputs' property to 2."
        ),
        "wiring": [
            ("trigger",   "OnTrigger", "entrance", "Lock",
             "Enter arena → Entrance locks"),
            ("trigger",   "OnTrigger", "monster_a", "Wake",
             "Enter arena → Monster A wakes"),
            ("trigger",   "OnTrigger", "monster_b", "Wake",
             "Enter arena → Monster B wakes"),
            ("monster_a", "OnDeath",   "gate",      "Trigger",
             "Monster A dies → Gate gets input 1"),
            ("monster_b", "OnDeath",   "gate",      "Trigger",
             "Monster B dies → Gate gets input 2"),
            ("gate",      "OnTrigger", "exit",      "Open",
             "Both dead → Exit door opens"),
        ],
        "roles": [
            ("trigger",   "trigger",    "Floor trigger at the arena entrance"),
            ("entrance",  "door",       "The entrance door (locks behind you)"),
            ("monster_a", "monster",    "First arena monster"),
            ("monster_b", "monster",    "Second arena monster"),
            ("gate",      "logic_gate", "AND gate (set required_inputs=2)"),
            ("exit",      "door",       "The exit door that opens when all are dead"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "entrance": [0, 0, 64],
            "monster_a": [-128, 0, -256],
            "monster_b": [128, 0, -256],
            "gate": [256, 0, -256],
            "exit": [0, 0, -512],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot",
                "type":    "bool",
                "default": True,
                "help":    "Arena encounter fires only once.",
            }
        ],
    },
    {
        "id":    "security_system",
        "title": "Security System",
        "icon":  "🚨",
        "short": "Trigger kills lights, wakes guard, sounds alarm, locks exit",
        "desc":  (
            "COMPLEX — 4 connections, 5 roles.\n\n"
            "A security trigger (laser tripwire, pressure plate) sets "
            "off a full lockdown: lights go out, an alarm speaker starts, "
            "a guard monster wakes, and the exit door locks.  Great for "
            "stealth-failure punishment."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "light",   "TurnOff",
             "Alarm tripped → Lights out"),
            ("trigger", "OnTrigger", "speaker", "PlaySound",
             "Alarm tripped → Alarm sounds"),
            ("trigger", "OnTrigger", "guard",   "Wake",
             "Alarm tripped → Guard wakes"),
            ("trigger", "OnTrigger", "exit",    "Lock",
             "Alarm tripped → Exit locks"),
        ],
        "roles": [
            ("trigger", "trigger", "The security tripwire / trigger"),
            ("light",   "light",   "Room light to kill"),
            ("speaker", "speaker", "Alarm speaker"),
            ("guard",   "monster", "Guard monster to wake"),
            ("exit",    "door",    "Exit door to lock"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "light": [0, 128, -128],
            "speaker": [64, 64, 0],
            "guard": [0, 0, -256],
            "exit": [0, 0, -384],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot alarm",
                "type":    "bool",
                "default": True,
                "help":    "Security system fires only once.",
            }
        ],
    },
    {
        "id":    "puzzle_door_multi_switch",
        "title": "Puzzle Door (Multi-Switch)",
        "icon":  "🧩",
        "short": "Two triggers must both be activated to open a door",
        "desc":  (
            "COMPLEX — 3 connections, 4 roles.\n\n"
            "Two separate triggers each send Trigger to a logic_gate "
            "configured as AND-2.  The door only opens when both "
            "triggers have been activated.\n\n"
            "Set the logic_gate's 'required_inputs' property to 2.  "
            "To make it a 3-switch puzzle, run the wizard again adding "
            "a third trigger to the same gate and set required_inputs=3."
        ),
        "wiring": [
            ("trigger_a", "OnTrigger", "gate", "Trigger",
             "Switch A activated → Gate input 1"),
            ("trigger_b", "OnTrigger", "gate", "Trigger",
             "Switch B activated → Gate input 2"),
            ("gate",      "OnTrigger", "door", "Open",
             "All switches hit → Door opens"),
        ],
        "roles": [
            ("trigger_a", "trigger",    "First switch / trigger"),
            ("trigger_b", "trigger",    "Second switch / trigger"),
            ("gate",      "logic_gate", "AND gate (set required_inputs=2)"),
            ("door",      "door",       "The door that opens"),
        ],
        "positions": {
            "trigger_a": [-128, 0, 0],
            "trigger_b": [128, 0, 0],
            "gate": [0, 0, -128],
            "door": [0, 0, -256],
        },
        "params": [],
    },
    {
        "id":    "gauntlet_sequence",
        "title": "Gauntlet Sequence",
        "icon":  "🏃",
        "short": "Sequential rooms: open door → wake monster → kill → next door",
        "desc":  (
            "COMPLEX — 4 connections, 4 roles.\n\n"
            "A relay starts the sequence by opening door A and waking "
            "a monster.  When the monster dies, door B opens.  Chain "
            "this wizard multiple times to build a full gauntlet of "
            "sequential encounters.\n\n"
            "To start the sequence from a trigger, wire the trigger's "
            "OnTrigger → relay's Trigger input manually or with the "
            "Button Door scenario first."
        ),
        "wiring": [
            ("relay",   "OnTrigger", "door_a",  "Open",
             "Relay fires → Door A opens (enter the room)"),
            ("relay",   "OnTrigger", "monster", "Wake",
             "Relay fires → Monster wakes"),
            ("monster", "OnDeath",   "door_b",  "Open",
             "Monster killed → Door B opens (exit the room)"),
            ("monster", "OnDeath",   "door_a",  "Close",
             "Monster killed → Door A closes behind you"),
        ],
        "roles": [
            ("relay",   "logic_relay", "Relay that kicks off this room"),
            ("door_a",  "door",        "Entrance door (opens then closes)"),
            ("monster", "monster",     "The monster guarding this room"),
            ("door_b",  "door",        "Exit door (opens on kill)"),
        ],
        "positions": {
            "relay": [192, 0, 128],
            "door_a": [0, 0, 0],
            "monster": [0, 0, -192],
            "door_b": [0, 0, -384],
        },
        "params": [
            {
                "key":     "fire_once",
                "label":   "One-shot",
                "type":    "bool",
                "default": True,
                "help":    "Each room in the gauntlet fires only once.",
            }
        ],
    },
    {
        "id":    "level_exit_sequence",
        "title": "Level Exit Sequence",
        "icon":  "🏁",
        "short": "Trigger plays a sound then changes level after a delay",
        "desc":  (
            "COMPLEX — 2 connections, 3 roles.\n\n"
            "When the player hits an exit trigger, a speaker plays a "
            "transition sound (fanfare, door slam) and after a short "
            "delay the level changer fires.  Set the delay on the "
            "Options page to control the pause between sound and load "
            "(e.g. 1.5 seconds for a fanfare to play out)."
        ),
        "wiring": [
            ("trigger", "OnTrigger",   "speaker",      "PlaySound",
             "Exit trigger → Transition sound"),
            ("trigger", "OnTrigger",   "levelchanger", "ChangeLevel",
             "Exit trigger → Level changes (after delay)"),
        ],
        "roles": [
            ("trigger",      "trigger",      "The exit trigger"),
            ("speaker",      "speaker",      "Transition sound effect"),
            ("levelchanger", "levelchanger", "The level changer entity"),
        ],
        "positions": {
            "trigger": [0, 0, 0],
            "speaker": [64, 64, 0],
            "levelchanger": [0, 0, -128],
        },
        "params": [],
    },
    {
        "id":    "relay_chain",
        "title": "Relay Chain",
        "icon":  "🔗",
        "short": "Relays fire in sequence with delays for scripted events",
        "desc":  (
            "COMPLEX — 2 connections, 3 roles.\n\n"
            "Relay A fires and triggers Relay B, which fires and "
            "triggers Relay C.  Set delays on each connection to "
            "create timed sequences (e.g. lights turning on one by "
            "one, doors opening in order, sounds playing in sequence).\n\n"
            "Wire each relay's OnTrigger to whatever effect you want "
            "at that step — this wizard just builds the chain itself."
        ),
        "wiring": [
            ("relay_a", "OnTrigger", "relay_b", "Trigger",
             "Relay A fires → Relay B fires"),
            ("relay_b", "OnTrigger", "relay_c", "Trigger",
             "Relay B fires → Relay C fires"),
        ],
        "roles": [
            ("relay_a", "logic_relay", "First relay (start of chain)"),
            ("relay_b", "logic_relay", "Second relay"),
            ("relay_c", "logic_relay", "Third relay (end of chain)"),
        ],
        "positions": {
            "relay_a": [0, 0, 0],
            "relay_b": [192, 0, 0],
            "relay_c": [384, 0, 0],
        },
        "params": [],
    },
    {
        "id":    "disable_on_trigger",
        "title": "Disable on Trigger",
        "icon":  "🚫",
        "short": "Trigger fires once then disables its own collision",
        "desc":  (
            "A trigger fires its OnTrigger output and simultaneously "
            "sends Disable to itself, making it non-solid so the "
            "player can never re-trigger it.  Stronger than fire_once "
            "because the trigger volume itself disappears.\n\n"
            "Wire the OnTrigger to whatever you want to happen — "
            "this wizard just adds the self-disable."
        ),
        "wiring": [
            ("trigger", "OnTrigger", "trigger", "Disable",
             "Trigger fires → Trigger disables itself"),
        ],
        "roles": [
            ("trigger", "trigger", "The self-disabling trigger"),
        ],
        "params": [],
    },
]


# ── Wizard stylesheet ─────────────────────────────────────────────────────────
#
# No font-size: Npx values anywhere.  All text inherits from QApplication.font()
# so it respects the user's font_size setting in Settings and OS DPI scaling.

WIZARD_STYLE = """
QWizard, QWizardPage {
    background: #1e1e24;
    color: #ddd;
    font-family: 'Segoe UI', Arial;
}
QLabel { color: #ccc; }
QComboBox {
    background: #2e2e38;
    border: 1px solid #555;
    color: #eee;
    padding: 4px;
    min-width: 180px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView { background: #2e2e38; color: #eee; }
QDoubleSpinBox, QSpinBox {
    background: #2e2e38;
    border: 1px solid #555;
    color: #eee;
    padding: 3px;
}
QCheckBox { color: #ccc; spacing: 6px; }
QGroupBox {
    color: #bbb;
    border: 1px solid #444;
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #aaa;
}
QListWidget {
    background: #252530;
    border: 1px solid #444;
    color: #ddd;
    outline: none;
}
QListWidget::item {
    padding: 8px 10px;
    border-bottom: 1px solid #333;
}
QListWidget::item:selected {
    background: #2a3a5a;
    color: #fff;
    border-left: 3px solid #4a90d9;
}
QListWidget::item:hover { background: #2a2a38; }
QPushButton {
    background: #3a3a44;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 6px 14px;
    color: #ddd;
}
QPushButton:hover { background: #4a4a55; }
"""


# ── Page 1 — Scenario selection ───────────────────────────────────────────────

class ScenarioPage(QWizardPage):
    """Choose a scenario from the built-in catalogue."""

    PAGE_ID = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Choose a Scenario")
        self.setSubTitle(
            "Select a pre-built wiring pattern. "
            "You will pick the specific entities on the next page."
        )
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self._list = QListWidget()
        self._list.setIconSize(QSize(32, 32))
        self._list.setMinimumHeight(180)
        self._list.currentRowChanged.connect(self._on_select)

        for sc in SCENARIOS:
            item = QListWidgetItem(f"  {sc['icon']}  {sc['title']}")
            item.setData(Qt.UserRole, sc["id"])
            item.setToolTip(sc["desc"])
            self._list.addItem(item)

        layout.addWidget(self._list, stretch=2)

        # Description box — scrolls internally so it never squashes the list
        self._desc_box = QGroupBox("Description")
        desc_lay = QVBoxLayout(self._desc_box)
        self._desc_lbl = QLabel()
        self._desc_lbl.setWordWrap(True)
        # Colour only — no font-size so it inherits from QApplication.font()
        self._desc_lbl.setStyleSheet("color: #aaa; padding: 4px;")
        desc_lay.addWidget(self._desc_lbl)
        self._desc_box.setMaximumHeight(120)
        layout.addWidget(self._desc_box, stretch=0)

        # Wiring preview
        self._wire_box = QGroupBox("Connections that will be created")
        wire_lay = QVBoxLayout(self._wire_box)
        self._wire_lbl = QLabel()
        self._wire_lbl.setWordWrap(True)
        # Use a monospace variant of the app font — no hardcoded point size
        mono = QFont(QApplication.font())
        mono.setFamily("Courier New")
        self._wire_lbl.setFont(mono)
        self._wire_lbl.setStyleSheet("color: #80c0ff;")
        wire_lay.addWidget(self._wire_lbl)
        self._wire_box.setMaximumHeight(160)
        layout.addWidget(self._wire_box, stretch=0)

        self._list.setCurrentRow(0)

        self.registerField("scenario_id*",
                           self._list, "currentRow",
                           self._list.currentRowChanged)

    def _on_select(self, row: int):
        if row < 0 or row >= len(SCENARIOS):
            return
        sc = SCENARIOS[row]
        self._desc_lbl.setText(sc["short"])
        self._desc_box.setToolTip(sc["desc"])
        wires = "\n".join(
            f"  {w[0]}.{w[1]}  →  {w[2]}.{w[3]}"
            for w in sc["wiring"]
        )
        self._wire_lbl.setText(wires)

    def selected_scenario(self) -> Optional[Dict]:
        row = self._list.currentRow()
        if 0 <= row < len(SCENARIOS):
            return SCENARIOS[row]
        return None

    def isComplete(self) -> bool:
        return self._list.currentRow() >= 0


# ── Page 2 — Entity selection ─────────────────────────────────────────────────

class EntitiesPage(QWizardPage):
    """Pick the entity for each role in the chosen scenario."""

    PAGE_ID = 1

    # Maps IO type → (is_brush, factory_info)
    # For Things: (False, 'ClassName')
    # For Brushes: (True, {flag_key: True, ...})
    _TYPE_FACTORIES = {
        'light':         (False, 'Light'),
        'speaker':       (False, 'Speaker'),
        'monster':       (False, 'Monster'),
        'pickup':        (False, 'Pickup'),
        'logic_relay':   (False, 'LogicRelay'),
        'logic_gate':    (False, 'LogicGate'),
        'logic_timer':   (False, 'LogicTimer'),
        'playerstart':   (False, 'PlayerStart'),
        'levelchanger':  (False, 'LevelChanger'),
        'door':          (True,  {'is_door': True}),
        'mover':         (True,  {'is_mover': True}),
        'trigger':       (True,  {'is_trigger': True}),
        'brush':         (True,  {}),
    }

    def __init__(self, editor_state, parent=None):
        super().__init__(parent)
        self.editor_state = editor_state
        self.setTitle("Pick Entities")
        self.setSubTitle(
            "Choose the specific entity for each role. "
            "Only compatible entity types are shown."
        )
        self._combos: Dict[str, QComboBox] = {}
        self._creation_count = 0  # offset counter so created entities don't overlap
        self._layout = QFormLayout()
        layout = QVBoxLayout(self)
        self._role_group = QGroupBox("Roles")
        self._role_group.setLayout(self._layout)
        layout.addWidget(self._role_group)
        layout.addStretch()

    def initializePage(self):
        """Rebuild combo boxes for the chosen scenario each time this page is shown."""
        wiz = self.wizard()
        sc  = wiz.page(ScenarioPage.PAGE_ID).selected_scenario()
        if not sc:
            return

        # Clear previous widgets
        while self._layout.count():
            child = self._layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._combos.clear()
        self._creation_count = 0

        self.setSubTitle(
            f"Scenario: {sc['icon']}  {sc['title']}\n"
            "Choose an entity for each role below."
        )

        for role_id, required_type, role_desc in sc["roles"]:
            combo = QComboBox()
            names = self._names_of_type(required_type)
            if not names:
                # No entities of this type — offer to create one
                row_widget = QWidget()
                row_lay = QHBoxLayout(row_widget)
                row_lay.setContentsMargins(0, 0, 0, 0)
                combo.addItem(f"⚠  No '{required_type}' entities")
                combo.setEnabled(False)
                row_lay.addWidget(combo, stretch=1)

                create_btn = QPushButton(f"+ Create {required_type}")
                create_btn.setToolTip(
                    f"Create a new {required_type} entity and add it to the scene."
                )
                # Capture role_id and required_type for the lambda
                create_btn.clicked.connect(
                    lambda _checked, r=role_id, t=required_type: self._create_entity(r, t)
                )
                row_lay.addWidget(create_btn, stretch=0)
            else:
                row_widget = combo
                for n in names:
                    combo.addItem(n)

            self._combos[role_id] = combo

            lbl = QLabel(
                f"<b>{role_desc}</b><br>"
                f"<small style='color:#888;'>type: {required_type}</small>"
            )
            lbl.setWordWrap(True)
            self._layout.addRow(lbl, row_widget)

    def _create_entity(self, role_id: str, required_type: str):
        """Create a new entity of the required type and refresh the combo."""
        import uuid as _uuid
        factory = self._TYPE_FACTORIES.get(required_type)
        if not factory:
            QMessageBox.warning(
                self, "Cannot create",
                f"Don't know how to create entities of type '{required_type}'."
            )
            return

        is_brush, info = factory

        # Use scenario-defined position if available, otherwise generic offset
        wiz = self.wizard()
        sc = wiz.page(ScenarioPage.PAGE_ID).selected_scenario() if wiz else None
        positions = sc.get("positions", {}) if sc else {}

        if role_id in positions:
            pos = list(positions[role_id])
        else:
            pos = [self._creation_count * 128.0, 0.0, 0.0]
        self._creation_count += 1

        if is_brush:
            # Default shapes per brush type
            #   door:    tall thin rectangle (walk-through shape)
            #   trigger: flat floor plate
            #   mover:   medium platform
            #   brush:   standard cube
            size_defaults = {
                'door':    [96.0, 192.0, 16.0],
                'trigger': [128.0, 16.0, 128.0],
                'mover':   [128.0, 16.0, 128.0],
                'brush':   [64.0, 64.0, 64.0],
            }
            size = list(size_defaults.get(required_type, [64.0, 64.0, 64.0]))

            new_brush = {
                'id': str(_uuid.uuid4()),
                'name': f"{required_type}_{len(self.editor_state.brushes) + 1}",
                'pos': pos,
                'size': size,
                '_io_connections': [],
            }
            new_brush.update(info)

            # Doors need movement defaults
            if required_type == 'door':
                new_brush.setdefault('door_direction', 'up')
                new_brush.setdefault('door_distance', 128.0)
                new_brush.setdefault('door_speed', 64.0)
                new_brush.setdefault('door_lip', 0.0)
                new_brush.setdefault('original_pos', list(pos))

            self.editor_state.brushes.append(new_brush)
            created_name = new_brush['name']
        else:
            # Create a Thing subclass by class name
            from . import things as _things
            cls = getattr(_things, info, None)
            if cls is None:
                QMessageBox.warning(
                    self, "Cannot create",
                    f"Thing class '{info}' not found."
                )
                return
            new_thing = cls(pos=list(pos))
            self.editor_state.things.append(new_thing)
            created_name = new_thing.properties.get('name', '?')

        # Refresh: update the combo for this role
        combo = self._combos.get(role_id)
        if combo:
            combo.clear()
            names = self._names_of_type(required_type)
            for n in names:
                combo.addItem(n)
            combo.setEnabled(True)
            # Select the newly created entity
            idx = combo.findText(created_name)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        # Re-check page completeness
        self.completeChanged.emit()

    def _names_of_type(self, required_type: str) -> List[str]:
        """Return all entity names whose IO type matches required_type."""
        results = []
        for t in self.editor_state.things:
            etype = get_entity_type_for_io(t) if IO_AVAILABLE else ""
            if required_type in (etype, etype.replace("_", "")):
                name = t.properties.get("name", "")
                if name:
                    results.append(name)
        for b in self.editor_state.brushes:
            btype = b.get("type", "")
            if required_type in (btype, btype.replace("_", "")):
                name = b.get("name", "")
                if name:
                    results.append(name)
        # Also check brush flags (door/mover/trigger are brushes with flags)
        if required_type in ('door', 'mover', 'trigger'):
            flag = f'is_{required_type}'
            for b in self.editor_state.brushes:
                if b.get(flag):
                    name = b.get("name", "")
                    if name and name not in results:
                        results.append(name)
        return results

    def entity_for_role(self, role_id: str) -> str:
        combo = self._combos.get(role_id)
        if combo and combo.isEnabled():
            return combo.currentText()
        return ""

    def isComplete(self) -> bool:
        return all(
            cb.isEnabled() and cb.currentText()
            for cb in self._combos.values()
        )


# ── Page 3 — Options ──────────────────────────────────────────────────────────

class OptionsPage(QWizardPage):
    """Fine-tune delay, fire-once, and any scenario-specific parameters."""

    PAGE_ID = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Connection Options")
        self.setSubTitle("Fine-tune the behaviour of the connections.")
        self._param_widgets: Dict[str, Any] = {}
        self._form = QFormLayout()
        layout = QVBoxLayout(self)

        # Global options — always shown
        global_grp  = QGroupBox("Global Options")
        global_form = QFormLayout(global_grp)

        self._delay_spin = QDoubleSpinBox()
        self._delay_spin.setRange(0.0, 60.0)
        self._delay_spin.setSingleStep(0.25)
        self._delay_spin.setSuffix(" sec")
        self._delay_spin.setValue(0.0)
        global_form.addRow("Trigger delay:", self._delay_spin)

        self._fire_once_chk = QCheckBox(
            "Connections fire only once per play session")
        self._fire_once_chk.setChecked(False)
        global_form.addRow("", self._fire_once_chk)

        layout.addWidget(global_grp)

        # Scenario-specific params (shown only when the scenario has params)
        self._specific_grp = QGroupBox("Scenario Options")
        self._specific_grp.setLayout(self._form)
        layout.addWidget(self._specific_grp)
        layout.addStretch()

    def initializePage(self):
        wiz = self.wizard()
        sc  = wiz.page(ScenarioPage.PAGE_ID).selected_scenario()
        if not sc:
            return

        # Clear previous scenario-specific widgets
        while self._form.count():
            child = self._form.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._param_widgets.clear()

        params = sc.get("params", [])
        self._specific_grp.setVisible(bool(params))

        for p in params:
            if p["type"] == "bool":
                w = QCheckBox(p["help"])
                w.setChecked(p["default"])
                self._form.addRow(p["label"] + ":", w)
                self._param_widgets[p["key"]] = w
            elif p["type"] == "float":
                w = QDoubleSpinBox()
                w.setValue(p["default"])
                w.setRange(p.get("min", 0.0), p.get("max", 999.0))
                w.setSuffix(p.get("suffix", ""))
                self._form.addRow(p["label"] + ":", w)
                self._param_widgets[p["key"]] = w

    def global_delay(self)     -> float: return self._delay_spin.value()
    def global_fire_once(self) -> bool:  return self._fire_once_chk.isChecked()

    def param_value(self, key: str) -> Any:
        w = self._param_widgets.get(key)
        if w is None:
            return None
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            return w.value()
        return None


# ── Page 4 — Summary ──────────────────────────────────────────────────────────

class SummaryPage(QWizardPage):
    """Preview exactly which connections will be added before committing."""

    PAGE_ID = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Summary")
        self.setSubTitle(
            "Review the connections that will be added to the Logic Graph.")
        layout = QVBoxLayout(self)

        self._summary_lbl = QLabel()
        self._summary_lbl.setWordWrap(True)
        # Use a monospace variant of the app font for the connection list.
        # No font-size: Npx — size is inherited from QApplication.font().
        mono = QFont(QApplication.font())
        mono.setFamily("Courier New")
        self._summary_lbl.setFont(mono)
        self._summary_lbl.setStyleSheet("""
            background: #1a1a22;
            border: 1px solid #3a3a50;
            border-radius: 4px;
            padding: 14px;
            color: #b0e0ff;
        """)
        layout.addWidget(self._summary_lbl)

        self._note_lbl = QLabel(
            "ℹ  Connections will be saved directly to entities if the Logic "
            "Graph is not open, or added to the graph for review otherwise."
        )
        self._note_lbl.setWordWrap(True)
        # Colour and spacing only — no font-size
        self._note_lbl.setStyleSheet("color: #a0a060; padding-top: 8px;")
        layout.addWidget(self._note_lbl)
        layout.addStretch()

    def initializePage(self):
        wiz = self.wizard()
        sc  = wiz.page(ScenarioPage.PAGE_ID).selected_scenario()
        ep  = wiz.page(EntitiesPage.PAGE_ID)
        op  = wiz.page(OptionsPage.PAGE_ID)
        if not sc or not ep or not op:
            return

        delay     = op.global_delay()
        fire_once = op.global_fire_once()

        lines = [f"<b>{sc['icon']}  {sc['title']}</b>", ""]
        for wire in sc["wiring"]:
            src_role, out_pin, dst_role, in_pin, _desc = wire
            src_name = ep.entity_for_role(src_role)
            dst_name = ep.entity_for_role(dst_role)

            fo = fire_once
            # Let scenario-level fire_once override the global one if present
            if "fire_once" in [p["key"] for p in sc.get("params", [])]:
                v = op.param_value("fire_once")
                if v is not None:
                    fo = bool(v)

            line = (
                f"  {src_name}<b>.{out_pin}</b>"
                f"  →  "
                f"{dst_name}<b>.{in_pin}</b>"
            )
            extras = []
            if delay > 0.0:
                extras.append(f"delay {delay:.2f}s")
            if fo:
                extras.append("fire once")
            if extras:
                line += f"  <span style='color:#888'>({', '.join(extras)})</span>"
            lines.append(line)

        self._summary_lbl.setText("<br>".join(lines))


# ── Wizard ────────────────────────────────────────────────────────────────────

class LogicWizard(QWizard):
    """
    Guided wizard for adding I/O connections to the Logic Graph.

    Parameters
    ----------
    editor_state : EditorState
        The live editor state (things, brushes).
    graph_scene : LogicGraphScene
        The scene to add connections to.  The caller must call
        graph_scene.apply_to_entities() (or press Apply in the graph window)
        to persist the connections to the map.
    """

    def __init__(self, editor_state, graph_scene, parent=None):
        super().__init__(parent)
        self.editor_state = editor_state
        self.graph_scene  = graph_scene

        self.setWindowTitle("Logic Wizard")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setMinimumSize(640, 540)
        self.setStyleSheet(WIZARD_STYLE)
        self.setOption(QWizard.HaveHelpButton,         False)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.FinishButton, "Add to Graph")
        self.setButtonText(QWizard.NextButton,   "Next  ›")
        self.setButtonText(QWizard.BackButton,   "‹  Back")

        self._p_scenario = ScenarioPage(self)
        self._p_entities = EntitiesPage(editor_state, self)
        self._p_options  = OptionsPage(self)
        self._p_summary  = SummaryPage(self)

        self.setPage(ScenarioPage.PAGE_ID, self._p_scenario)
        self.setPage(EntitiesPage.PAGE_ID, self._p_entities)
        self.setPage(OptionsPage.PAGE_ID,  self._p_options)
        self.setPage(SummaryPage.PAGE_ID,  self._p_summary)

        self.accepted.connect(self._apply)

    def _apply(self):
        sc = self._p_scenario.selected_scenario()
        ep = self._p_entities
        op = self._p_options
        if not sc:
            return

        delay            = op.global_delay()
        fire_once_global = op.global_fire_once()
        added_graph      = 0
        added_direct     = 0

        for wire in sc["wiring"]:
            src_role, out_pin, dst_role, in_pin, _desc = wire
            src_name = ep.entity_for_role(src_role)
            dst_name = ep.entity_for_role(dst_role)
            if not src_name or not dst_name:
                continue

            fo = fire_once_global
            if "fire_once" in [p["key"] for p in sc.get("params", [])]:
                v = op.param_value("fire_once")
                if v is not None:
                    fo = bool(v)

            # Try the graph scene first (if it has nodes for these entities)
            ok = self.graph_scene.add_connection_by_name(
                src_name, out_pin,
                dst_name, in_pin,
                param="", delay=delay, fire_once=fo,
            )
            if ok:
                added_graph += 1
            else:
                # Fallback: write the connection directly to the entity
                if self._apply_direct(src_name, dst_name,
                                      out_pin, in_pin,
                                      delay, fo):
                    added_direct += 1

        added = added_graph + added_direct
        if added:
            if added_direct and not added_graph:
                # All connections went direct — no need to press Apply
                QMessageBox.information(
                    self.parentWidget(),
                    "Wizard complete",
                    f"{added} connection(s) saved directly to entities.\n\n"
                    "Open the Logic Graph to review them visually.",
                )
            else:
                QMessageBox.information(
                    self.parentWidget(),
                    "Wizard complete",
                    f"{added} connection(s) added.\n\n"
                    "Press ✔ Apply in the Logic Graph Editor to save them to the map.",
                )
        else:
            QMessageBox.warning(
                self.parentWidget(),
                "Nothing added",
                "No connections could be created.\n"
                "Make sure the selected entities have the required pins available\n"
                "and that entity names are not empty.",
            )

    def _apply_direct(self, src_name, dst_name, out_pin, in_pin,
                      delay, fire_once) -> bool:
        """Write a connection directly to the source entity, bypassing the graph."""
        src = self.editor_state.find_entity_by_name(src_name)
        dst = self.editor_state.find_entity_by_name(dst_name)
        if src is None or dst is None:
            return False

        # Resolve target ID for stable reference
        target_id = self.editor_state.get_entity_id(dst)

        try:
            from .io_system import OutputConnection, add_connection
            conn = OutputConnection(
                output_name=out_pin,
                target_name=dst_name,
                input_name=in_pin,
                parameter="",
                delay=delay,
                fire_once=fire_once,
                target_id=target_id,
            )
            add_connection(src, conn)
            return True
        except ImportError:
            # No I/O system — store as raw dict
            if hasattr(src, 'properties'):
                conns = src.properties.setdefault('_io_connections', [])
            else:
                conns = src.setdefault('_io_connections', [])
            conns.append({
                'output': out_pin,
                'target': dst_name,
                'target_id': target_id,
                'input': in_pin,
                'parameter': '',
                'delay': delay,
                'fire_once': fire_once,
            })
            return True
