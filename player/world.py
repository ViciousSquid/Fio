"""Qt-free host for Fio's authoritative simulation, shared by every player."""
from __future__ import annotations

import math
from types import SimpleNamespace

import glm

from engine.entities import Thing
from player.runtime.fiopak import PackageError
from engine.io_system import OutputConnection
from engine.items import ItemDefinitions
from engine.logic_thread import LogicThread
from engine.player import Player
from engine.player_starts import resolve_start_record
from engine.threaded_game_state import ThreadedGameState
from .input.state import ACTION_JUMP, ACTION_CROUCH, ACTION_SPRINT, ACTION_USE, ACTION_FIRE


class PlayerWorld:
    STEP = 1 / 60

    def __init__(self, data, package=None, spawn=None):
        self.data, self.package = data, package
        self.pending_level = None
        self._elapsed = 0.0
        self._closed = False
        definitions = ItemDefinitions()
        definitions.load(data.get('items'))
        self.state = SimpleNamespace(brushes=[], things=[], world_epoch=0,
                                     item_definitions=definitions, view_filters={})
        self.game_state = ThreadedGameState()
        self.logic = LogicThread(self.game_state, self.state, threaded_ai=False)
        self.logic.package = package
        self.logic.request_level_change = self.request_level_change
        manager = self.logic.plugins
        required = set(data.get('required_plugins', []) or [])
        if package is not None:
            required.update(package.required_plugins)
        for name in required:
            plugin = manager.find_plugin(name) if manager else None
            if plugin is None:
                raise PackageError(f"This world requires an installed plugin: {name}")
            manager.set_enabled(plugin, True)
            config = data.get('plugin_config', {}).get(name)
            if isinstance(config, dict):
                plugin.config.update(config)
        if manager:
            manager.auto_enable_for_map(data)
        for record in data.get('brushes', []):
            brush = dict(record)
            brush['_io_connections'] = [OutputConnection.from_dict(c) for c in brush.get('io_connections', [])]
            self.state.brushes.append(brush)
        for record in data.get('things', []):
            thing = Thing.from_dict(record)
            if thing is None:
                raise PackageError(f"Unknown entity type: {record.get('type')}")
            thing._asset_source = package
            self.state.things.append(thing)
        self.brushes = self.state.brushes
        self.terrain = None
        terrain_data = data.get('terrain_data')
        if terrain_data and terrain_data.get('enabled', True):
            from engine.terrain import Terrain
            self.terrain = Terrain(defer_gl=True)
            self.terrain.from_dict(terrain_data)
            self.logic.world_runtime.terrain = self.terrain
        start = resolve_start_record(data, spawn) or (None if spawn else {})
        if start is None:
            raise PackageError(f"PlayerStart not found: {spawn}")
        pos = start.get('pos', [0, 128, 0])
        authored_angle = float(start.get('properties', {}).get('angle', 0))
        # Same authored-angle conversion as desktop QtGameView.
        self.player = Player(pos[0], pos[2], math.radians(270 - authored_angle))
        self.player.pos = glm.vec3(*pos)
        self.logic.player_runtime.player = self.player
        self.logic.session_runtime.apply_play_mode(True)
        self.grid = self.logic.session_runtime.spatial_grid

    def request_level_change(self, target, spawn=''):
        self.pending_level = (target, spawn)

    def step(self, dt, inp, yaw, pitch):
        self.player.angle = math.radians(90 - yaw)
        self.player.pitch = math.radians(pitch)
        keys = set()
        for pressed, key in ((inp.move_y > .1, 0x57), (inp.move_y < -.1, 0x53),
                             (inp.move_x < -.1, 0x41), (inp.move_x > .1, 0x44),
                             (inp.is_down(ACTION_JUMP), 0x20),
                             (inp.is_down(ACTION_CROUCH), 0x43),
                             (inp.is_down(ACTION_SPRINT), 0x01000020)):
            if pressed:
                keys.add(key)
        self.game_state.set_keys(keys)
        self.game_state.set_movement_axes(-inp.move_x, inp.move_y)
        if inp.just_pressed(ACTION_USE):
            self.game_state.set_use_key_pressed()
            complete = self.logic.interaction_runtime.level_complete_ui
            if complete and complete.get('target_map'):
                self.request_level_change(complete['target_map'], complete.get('destination_spawn', ''))
        self._elapsed = min(self._elapsed + max(0, dt), .15)
        while self._elapsed + 1e-9 >= self.STEP:
            if inp.is_down(ACTION_FIRE):
                self.game_state.queue_shot()
            self.logic._tick(self.STEP)
            # A single simulation owner prevents background threads from ticking
            # a paused mobile world or racing a streamed level transition.
            self.logic.monster_ai.update(self.STEP)
            self._elapsed -= self.STEP
        return [self.player.pos.x, self.player.pos.y + self.player.view_height, self.player.pos.z]

    def camera(self):
        cinematic = self.logic.cutscene_runtime.state
        if cinematic and 'cam_pos' in cinematic:
            eye = glm.vec3(*cinematic['cam_pos'])
            yaw, pitch = cinematic.get('cam_angle', 0), cinematic.get('cam_pitch', 0)
            direction = glm.vec3(math.cos(yaw) * math.cos(pitch), math.sin(pitch), math.sin(yaw) * math.cos(pitch))
            return eye, direction, glm.vec3(0, 1, 0), cinematic.get('fov') or 90
        camera = self.logic.camera
        return camera._camera_for_mode(camera.is_overhead(), self.player.pos,
                                       self.player.angle, self.player.pitch, self.player.view_height)

    def render_map(self):
        return {'brushes': self.brushes, 'things': [{'type': t.properties.get('type'), 'pos': list(t.pos), 'properties': t.properties} for t in self.state.things]}

    @property
    def hud(self):
        runtime = self.logic.player_runtime
        prompt = self.logic.interaction_runtime.current_hud_message
        if self.logic.interaction_runtime.level_complete_ui:
            prompt = 'Level complete — press Use to continue'
        if runtime.player_dead:
            prompt = 'You died — reopen this world to restart'
        return f'Health {runtime.player_health}   Armor {runtime.player_armor}   Ammo {self.logic.combat_runtime.player_ammo}\n{prompt}'

    def close(self):
        if not self._closed:
            self._closed = True
            self.logic.session_runtime.apply_play_mode(False)
