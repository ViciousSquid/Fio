"""
Player runtime for LogicThread.

Owns primary-player and split-screen Player 2 movement/input mechanics plus
the player's water-state sound feedback. LogicThread remains the tick-order
orchestrator.
"""

import math
import threading
import glm

from .physics import world_gravity

# Qt key values used by the engine's input state.
KEY_W = 0x57
KEY_S = 0x53
KEY_A = 0x41
KEY_D = 0x44
KEY_SPACE = 0x20
KEY_C = 0x43
# Qt::Key_Shift; kept numeric so the engine remains Qt-free.
KEY_SHIFT = 0x01000020

_WATER_LOUDNESS = 0.7


class LogicPlayer:
    """Runtime mechanics for the engine's player actors."""

    WATERWALK_INTERVAL = 0.45

    def __init__(self, logic):
        self.logic = logic
        self.player = None
        self.player2 = None
        self._player_was_in_water = False
        self._waterwalk_timer = 0.0
        self.collected_keys = set()
        self.p2_turn_sensitivity = 10.0
        self.god_mode = False
        self.buddha_mode = False
        self.notarget = False
        self.player_health = 100
        self.player_max_health = 100
        #: Armor points, taken by damage before health (see
        #: LogicTriggers._apply_player_damage); given by armor pickups.
        self.player_armor = 0
        self.player_max_armor = 100
        self.player_dead = False
        self.damage_lock = threading.Lock()
        self.player2_health = 100
        self.player2_max_health = 100
        self.player2_dead = False

    def give_armor(self, amount):
        """Add *amount* armor, up to ``player_max_armor``."""
        self.player_armor = min(self.player_max_armor,
                                self.player_armor + max(0, int(amount)))

    def update_primary(self, delta, keys, mouse_dx, mouse_dy):
        """Apply primary-player look, movement and physics for one tick."""
        logic = self.logic
        player = self.player
        if not player:
            return

        sensitivity = 0.002
        player.angle -= mouse_dx * sensitivity
        player.pitch -= mouse_dy * sensitivity
        player.pitch = max(-1.5, min(1.5, player.pitch))

        move_dir = glm.vec3(0)
        if KEY_W in keys:
            move_dir.z += 1
        if KEY_S in keys:
            move_dir.z -= 1
        if KEY_A in keys:
            move_dir.x += 1
        if KEY_D in keys:
            move_dir.x -= 1

        jump = KEY_SPACE in keys
        crouch = KEY_C in keys
        sprint = KEY_SHIFT in keys

        player.update(
            delta,
            move_dir,
            jump,
            crouch,
            logic.collision_runtime._collision_brushes_cache,
            logic.mover_runtime._mover_brush_list,
            logic.mover_runtime._door_brush_list,
            logic.world_runtime.terrain,
            spatial_grid=logic.session_runtime.spatial_grid,
            sprint=sprint,
            gravity=world_gravity(logic.session_runtime.physics_world),
        )

    def update_player2(self, delta):
        """Apply split-screen Player 2 input, look and physics for one tick."""
        logic = self.logic
        if not self.player2 or self.player2_dead:
            return

        p2 = logic.game_state.get_p2_input()
        p2_dir = glm.vec3(float(p2['move_x']), 0.0, float(p2['move_z']))

        turn_input = float(p2['look_dx'])
        self.player2.angle -= turn_input * self.p2_turn_sensitivity * delta
        self.player2.pitch -= float(p2['look_dy']) * 0.002
        self.player2.pitch = max(-1.5, min(1.5, self.player2.pitch))

        self.player2.update(
            delta,
            p2_dir,
            bool(p2['jump']),
            False,
            logic.collision_runtime._collision_brushes_cache,
            logic.mover_runtime._mover_brush_list,
            logic.mover_runtime._door_brush_list,
            logic.world_runtime.terrain,
            spatial_grid=logic.session_runtime.spatial_grid,
            gravity=world_gravity(logic.session_runtime.physics_world),
        )

    def update_water_sounds(self, delta):
        """Queue sounds and monster-noise events from player water state."""
        logic = self.logic
        player = self.player
        if not player:
            return

        in_water = bool(player.in_water)

        if in_water and not self._player_was_in_water:
            logic.game_state.queue_sound(
                {'file': 'enterwater.wav', 'volume': 1.0})
            logic.combat_runtime._emit_noise_event(
                player.pos,
                source='water_enter',
                loudness=_WATER_LOUDNESS,
            )
            self._waterwalk_timer = 0.0
        elif not in_water and self._player_was_in_water:
            logic.game_state.queue_sound(
                {'file': 'exitwater.wav', 'volume': 1.0})
            logic.combat_runtime._emit_noise_event(
                player.pos,
                source='water_exit',
                loudness=_WATER_LOUDNESS,
            )

        self._player_was_in_water = in_water

        wading = (
            in_water
            and not player.swimming
            and player.on_ground
        )
        horiz_speed = math.hypot(
            player.velocity.x,
            player.velocity.z,
        )
        if wading and horiz_speed > 20.0:
            self._waterwalk_timer -= delta
            if self._waterwalk_timer <= 0.0:
                logic.game_state.queue_sound(
                    {'file': 'waterwalk.wav', 'volume': 0.8})
                self._waterwalk_timer = self.WATERWALK_INTERVAL
        else:
            self._waterwalk_timer = 0.0
