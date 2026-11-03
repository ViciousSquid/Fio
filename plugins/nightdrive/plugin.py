"""Night Drive: a complete game layer over Fio's existing world and renderer.

All authored vehicles are ordinary Fio Props. No engine core file is patched.
The plugin owns driving input, traffic and delivery missions only while its map
is running. A normal Fio map continues to use the stock player controller.
"""
from __future__ import annotations

import math
import os
import weakref

from plugins.api import FioPlugin
from engine.change_journal import STATE, touch

from .vehicle import Vehicle, Traffic, circle_hits_rect

ROOT = os.path.dirname(__file__)
MAP_PATH = os.path.join(ROOT, "NightDrive_City.json")
WORLD_LIMIT = 1915.0


def _role(entity):
    return getattr(entity, "properties", {}).get("drive_role", "")


def _pose(entity):
    return (list(entity.pos),
            list(entity.properties.get("rotation", [0, 0, 0])),
            entity.properties.get("tint"))


def _set_pose(entity, x, z, angle):
    """Always assign a new pos list, and journal non-position state explicitly."""
    entity.pos = [float(x), 5.0, float(z)]
    degrees = math.degrees(angle)
    old = entity.properties.get("rotation", [0, 0, 0])
    if abs(float(old[1]) - degrees) > 0.0001:
        entity.properties["rotation"] = [0.0, degrees, 0.0]
        touch(entity, STATE)


class NightDriveSession:
    def __init__(self, logic, editor_view=None):
        self.logic = logic
        self._editor_view = editor_view
        things = list(logic.editor_state.things)
        self.player_prop = next((p for p in things if _role(p) == "player_vehicle"), None)
        self.traffic_props = [p for p in things if _role(p) == "traffic"]
        self.checkpoints = sorted(
            (p for p in things if _role(p) == "delivery"),
            key=lambda p: int(p.properties.get("drive_order", 0))
        )
        self.obstacles = []
        for brush in logic.editor_state.brushes:
            if not brush.get("nightdrive_solid"):
                continue
            x, _, z = (float(v) for v in brush["pos"])
            sx, _, sz = (float(v) for v in brush["size"])
            self.obstacles.append((x - sx / 2, z - sz / 2,
                                   x + sx / 2, z + sz / 2))
        self.snapshots = [(p, _pose(p)) for p in
                          ([self.player_prop] if self.player_prop else []) + self.traffic_props]
        self.camera_before = None
        self.view = None
        self.sprite_before = None
        self.vehicle = None
        self.traffic = []
        self.next_stop = 0
        self.deliveries = 0
        self.score = 0
        self._reset_held = False
        self._impact_cooldown = 0.0
        if self.player_prop:
            x, _, z = self.player_prop.pos
            yaw = self.player_prop.properties.get("rotation", [0, 0, 0])[1]
            self.vehicle = Vehicle(float(x), float(z), math.radians(float(yaw)))
            self.spawn = (self.vehicle.x, self.vehicle.z, self.vehicle.angle)
        for prop in self.traffic_props:
            route = tuple((float(p[0]), float(p[1]))
                          for p in prop.properties.get("drive_route", ()))
            if len(route) < 2:
                continue
            x, _, z = prop.pos
            yaw = prop.properties.get("rotation", [0, 0, 0])[1]
            initial = int(prop.properties.get("drive_next_waypoint", 1)) % len(route)
            self.traffic.append((prop, Traffic(
                Vehicle(float(x), float(z), math.radians(float(yaw))),
                route, float(prop.properties.get("drive_speed", 105)), initial)))

    def start(self):
        camera = self.logic.camera
        self.camera_before = (camera.camera_mode, camera.overhead_height,
                              camera.overhead_tilt, camera.overhead_orientation)
        camera.set_camera_mode("Overhead")
        camera.camera_transition = None
        camera.overhead_height = 560.0
        camera.overhead_tilt = 0.0
        camera.overhead_orientation = "north"
        # Only a display preference. The visible player is our native Fio model
        # Prop, so the pedestrian sprite must not be layered on top of it.
        editor = (getattr(self.logic, "main_window", None) or
                  getattr(self.logic.editor_state, "main_window", None))
        self.view = self._editor_view or getattr(editor, "view_3d", None)
        if self.view is not None:
            self.sprite_before = self.view.overhead_sprite_enabled
            self.view.overhead_sprite_enabled = False
        if self.vehicle:
            self.sync_player()

    def stop(self):
        for prop, (pos, rotation, tint) in self.snapshots:
            prop.pos = pos
            prop.properties["rotation"] = rotation
            if tint is None:
                prop.properties.pop("tint", None)
            else:
                prop.properties["tint"] = tint
            touch(prop, STATE)
        if self.camera_before:
            cam = self.logic.camera
            mode, height, tilt, orientation = self.camera_before
            cam.set_camera_mode(mode)
            cam.overhead_height = height
            cam.overhead_tilt = tilt
            cam.overhead_orientation = orientation
        if self.view is not None and self.sprite_before is not None:
            self.view.overhead_sprite_enabled = self.sprite_before

    def blocked(self, x, z, radius):
        if abs(x) > WORLD_LIMIT - radius or abs(z) > WORLD_LIMIT - radius:
            return True
        # Only building body brushes are marked solid. Road stripes, signs,
        # pavements and roofs never block an arcade car.
        for rect in self.obstacles:
            if circle_hits_rect(x, z, radius, rect):
                return True
        for _prop, traffic in self.traffic:
            v = traffic.vehicle
            if (v.x - x) ** 2 + (v.z - z) ** 2 < (radius + 27.0) ** 2:
                return True
        return False

    def sync_player(self):
        state = self.vehicle
        _set_pose(self.player_prop, state.x, state.z, state.angle)
        player = self.logic.player_runtime.player
        if player is not None:
            # Fio player coordinates are GLM vectors; direct component writes
            # preserve the object used by the native top-down camera.
            player.pos.x = state.x
            player.pos.z = state.z
            player.angle = state.angle
            player.pitch = 0.0

    def reset(self):
        v = self.vehicle
        v.x, v.z, v.angle = self.spawn
        v.speed = 0.0
        self.sync_player()

    def tick(self, ctx):
        if self.vehicle is None:
            return
        if self.logic.session_runtime.world_paused:
            return
        dt = min(0.05, max(0.0, float(ctx.delta)))
        for prop, traffic in self.traffic:
            traffic.update(dt)
            _set_pose(prop, traffic.vehicle.x, traffic.vehicle.z,
                      traffic.vehicle.angle)
        reset = ctx.key_down("r")
        if reset and not self._reset_held:
            self.reset()
            ctx.toast("Vehicle recovered", 1.5)
        self._reset_held = reset

        throttle = int(ctx.key_down("w") or ctx.key_down("up")) - int(
            ctx.key_down("s") or ctx.key_down("down"))
        steering = int(ctx.key_down("d") or ctx.key_down("right")) - int(
            ctx.key_down("a") or ctx.key_down("left"))
        hit = self.vehicle.step(dt, throttle, steering,
                                handbrake=ctx.key_down("space"),
                                collides=self.blocked)
        if hit and self._impact_cooldown <= 0.0:
            self.score = max(0, self.score - 75)
            ctx.toast("Collision! -75", 1.2)
            self._impact_cooldown = 0.8
        self._impact_cooldown = max(0.0, self._impact_cooldown - dt)
        self.sync_player()

        target_text = "CRUISE"
        if self.checkpoints:
            dest = self.checkpoints[self.next_stop]
            dx = float(dest.pos[0]) - self.vehicle.x
            dz = float(dest.pos[2]) - self.vehicle.z
            distance = math.hypot(dx, dz)
            target_text = "DROP %d/%d  %dm" % (
                self.next_stop + 1, len(self.checkpoints), int(distance / 10))
            if distance < 105.0:
                self.score += 500
                self.deliveries += 1
                self.next_stop = (self.next_stop + 1) % len(self.checkpoints)
                ctx.toast("Delivery complete  +500", 2.0)
        # The stock Fio HUD draws this line; no bespoke GPU text or UI thread
        # traffic is introduced for speed/score.
        ctx.set_prompt("NIGHT DRIVE  |  %03d km/h  |  $%05d  |  %s"
                       "  |  SPACE drift  R recover"
                       % (int(abs(self.vehicle.speed) * 0.36), self.score,
                          target_text), priority=2)


class NightDrivePlugin(FioPlugin):
    name = "nightdrive"
    version = "1.0.0"
    api_version = "1.6.0"
    description = "Top-down open-city driving, AI traffic and delivery missions."
    category = "Games"
    enabled = False
    _sessions = weakref.WeakKeyDictionary()
    _loading_view = None

    def map_uses_plugin(self, map_data):
        if not isinstance(map_data, dict):
            return False
        return any(
            isinstance(p, dict) and
            p.get("properties", {}).get("drive_role") == "player_vehicle"
            for p in map_data.get("things", ())
        )

    def _load_demo_map(self, main_window):
        if not main_window.check_unsaved_changes():
            return
        view = getattr(main_window, "view_3d", None)
        self._loading_view = weakref.ref(view) if view is not None else None
        main_window.load_level_file(MAP_PATH)

    def register(self, api):
        api.register_menu_action(
            "Load Night Drive city", self._load_demo_map,
            "Open the bundled GTA 2-style top-down driving level"
        )

    def connect(self, host):
        logic = host.logic

        def drive_input(original, *args, **kwargs):
            # Skip stock WASD on the driving map. In every other level the
            # original Quake-style pedestrian controller is unchanged.
            if self.enabled and logic in self._sessions:
                return None
            return original(*args, **kwargs)

        host.wrap(logic.player_runtime, "update_primary", drive_input)

    def on_play_start(self, logic):
        view = self._loading_view() if self._loading_view is not None else None
        session = NightDriveSession(logic, editor_view=view)
        if session.vehicle is None:
            return
        self._sessions[logic] = session
        session.start()

    def on_tick(self, logic, ctx):
        session = self._sessions.get(logic)
        if session is not None:
            session.tick(ctx)

    def on_play_stop(self, logic):
        session = self._sessions.pop(logic, None)
        if session is not None:
            session.stop()


PLUGIN = NightDrivePlugin()
