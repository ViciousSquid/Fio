"""Qt-free host for Fio's authoritative simulation, shared by every player."""
from __future__ import annotations

import copy
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
from engine.items import ITEM_IDS
from .input.state import (ACTION_JUMP, ACTION_CROUCH, ACTION_SPRINT, ACTION_USE, ACTION_FIRE,
                          ACTION_WEAPON)


class WorldState(SimpleNamespace):
    """The scene owner the shared engine and installed plugins read.

    Desktop plugins ask the editor state for the level they are playing
    (Deathmatch hands it to LAN clients). The player has no editor; the
    authored map it loaded is that level.
    """

    def __init__(self, level, **fields):
        super().__init__(**fields)
        self._level = level

    def get_level_data(self):
        return copy.deepcopy(self._level)


class PlayerWorld:
    STEP = 1 / 60

    def __init__(self, data, package=None, spawn=None):
        self.data, self.package = data, package
        self.pending_level = None
        self._elapsed = 0.0
        self._closed = False
        definitions = ItemDefinitions()
        definitions.load(data.get('items'))
        self.state = WorldState(data, brushes=[], things=[], world_epoch=0,
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
        # A brush record is brought to the engine's in-memory layout as the
        # desktop does when it loads a map (EditorState._deserialize_brushes):
        # play state dropped, an angled face's material on its plane only, and
        # the planes in the brush's own frame. A package holds map version 4,
        # whose planes are in world space (brush_placement.export_world_level);
        # left as they were the engine reads them as local and every angled
        # brush is drawn, culled and collided displaced by its own position.
        from engine import brush_faces, brush_placement
        try:
            version = int(data.get('version') or 0)
        except (TypeError, ValueError):
            version = 0
        legacy_faces = version < brush_faces.CANONICAL_MAP_VERSION
        world_planes = version < brush_placement.MAP_VERSION
        for record in data.get('brushes', []):
            brush = dict(record)
            brush_placement.drop_play_state(brush)
            if 'geometry' in brush:
                if legacy_faces:
                    brush_faces.migrate_legacy(brush)
                else:
                    brush_faces.drop_tag_copies(brush)
                if world_planes:
                    brush_placement.localise_legacy(brush)
                else:
                    brush_placement.normalise_loaded(brush)
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
            self.terrain = Terrain()
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
        if inp.just_pressed(ACTION_WEAPON):
            slot = self.next_weapon_slot()
            if slot is not None:
                self.game_state.queue_weapon_slot(slot)
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

    def owned_weapons(self):
        """Weapon slots the player holds, in slot order (desktop keys 1-4)."""
        owned = self.logic.combat_runtime.weapons
        return [index + 1 for index, item in enumerate(ITEM_IDS) if item in owned]

    def next_weapon_slot(self):
        """The slot after the weapon in hand, wrapping; None with one weapon."""
        slots = self.owned_weapons()
        if len(slots) < 2:
            return None
        active = self.logic.combat_runtime.active_weapon
        current = ITEM_IDS.index(active) + 1 if active in ITEM_IDS else 0
        return next((slot for slot in slots if slot > current), slots[0])

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
    def hud_state(self):
        import time
        combat = self.logic.combat_runtime
        held = combat.items.resolve(combat.active_weapon) if combat.active_weapon else None
        return dict(held=held, health=self.logic.player_runtime.player_health,
                    armor=self.logic.player_runtime.player_armor, ammo=combat.player_ammo,
                    ready=combat.shot_ready(time.perf_counter()),
                    flash=time.perf_counter()-combat._last_player_shot_time < .1,
                    cinematic=bool(self.logic.cutscene_runtime.state),
                    overhead=self.logic.camera.is_overhead(),
                    prompt=self.logic.interaction_runtime.current_hud_message)

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
