"""Render-state projection delegated from LogicThread.

Owns frustum math, HUD render-state fading, and the double-buffered
RenderTable/EntityTable projection. LogicThread remains the simulation
orchestrator; the published render state is owned by ThreadedGameState.
"""

from __future__ import annotations

import math
import time

import glm
import numpy as np

from .change_journal import JOURNAL, STATE
from .entity_table import ENT_PROP
from .logic_combat import NO_PROJECTILES as _NO_PROJECTILES
from .view_distance import ViewDistance


class LogicRender:
    """Render-facing projection and culling for LogicThread."""

    def __init__(self, logic):
        self.logic = logic
        self.view_distance = ViewDistance()
        self.culling_enabled = True
        self._last_edited = {}

        # Health HUD fade is render-owned state. LogicThread supplies the
        # authoritative health value; this runtime owns the projection timing
        # and display alpha.
        self.hud_fade_enabled = True
        self._hud_health_fade_in_duration = 1.5
        self._hud_health_fade_out_duration = 4.0
        self._hud_health_alpha = 0.5
        self._hud_health_last_value = None
        self._hud_health_fade_started = None
        self._hud_health_fade_from = 0.5
        self._hud_health_fade_phase = "idle"

        # Cinematic HUD fade is also render-owned state.
        self._hud_cinematic_last_active = False
        self._hud_cinematic_fade_started = None

    def extract_frustum_planes(self, proj_view: glm.mat4):
        m = proj_view
        planes = []
        planes.append(
            self.normalize_plane(
                m[0][3] + m[0][0],
                m[1][3] + m[1][0],
                m[2][3] + m[2][0],
                m[3][3] + m[3][0],
            )
        )
        planes.append(
            self.normalize_plane(
                m[0][3] - m[0][0],
                m[1][3] - m[1][0],
                m[2][3] - m[2][0],
                m[3][3] - m[3][0],
            )
        )
        planes.append(
            self.normalize_plane(
                m[0][3] + m[0][1],
                m[1][3] + m[1][1],
                m[2][3] + m[2][1],
                m[3][3] + m[3][1],
            )
        )
        planes.append(
            self.normalize_plane(
                m[0][3] - m[0][1],
                m[1][3] - m[1][1],
                m[2][3] - m[2][1],
                m[3][3] - m[3][1],
            )
        )
        planes.append(
            self.normalize_plane(
                m[0][3] + m[0][2],
                m[1][3] + m[1][2],
                m[2][3] + m[2][2],
                m[3][3] + m[3][2],
            )
        )
        planes.append(
            self.normalize_plane(
                m[0][3] - m[0][2],
                m[1][3] - m[1][2],
                m[2][3] - m[2][2],
                m[3][3] - m[3][2],
            )
        )
        return planes

    @staticmethod
    def normalize_plane(a, b, c, d):
        length = math.sqrt(a * a + b * b + c * c)
        if length < 1e-8:
            return (0, 0, 0, 0)
        return (a / length, b / length, c / length, d / length)

    @staticmethod
    def aabb_in_frustum(planes, center, half_size):
        for plane in planes:
            a, b, c, d = plane
            px = center[0] + half_size[0] if a >= 0 else center[0] - half_size[0]
            py = center[1] + half_size[1] if b >= 0 else center[1] - half_size[1]
            pz = center[2] + half_size[2] if c >= 0 else center[2] - half_size[2]
            if a * px + b * py + c * pz + d < 0:
                return False
        return True

    def aabb_in_frustum_batch(self, planes, centers, halves):
        """Vectorized equivalent of calling aabb_in_frustum for every box."""
        c = np.asarray(centers, dtype=np.float64).reshape(-1, 3)
        h = np.asarray(halves, dtype=np.float64).reshape(-1, 3)
        if c.size == 0:
            return np.ones(len(centers), dtype=bool)
        return self.aabb_in_frustum_bounds(
            planes, np.concatenate((c, h), axis=1)
        )

    @staticmethod
    def aabb_in_frustum_bounds(planes, bounds):
        """Evaluate positive-vertex AABB/frustum tests as one NumPy product."""
        p = np.asarray(planes, dtype=np.float64)
        normals = p[:, :3]
        weights = np.concatenate((normals, np.abs(normals)), axis=1)
        return (weights @ bounds.T >= -p[:, 3:]).all(axis=0)

    def set_hud_fade_enabled(self, enabled: bool):
        """Enable or disable the damage-driven health HUD fade."""
        logic = self.logic
        with logic._tick_lock:
            enabled = bool(enabled)
            if enabled == self.hud_fade_enabled:
                return
            self.hud_fade_enabled = enabled
            self._hud_health_alpha = 0.5 if enabled else 1.0
            self._hud_health_fade_started = None
            self._hud_health_fade_from = self._hud_health_alpha
            self._hud_health_fade_phase = "idle"
            self._hud_health_last_value = logic.player_runtime.player_health

    def update_hud_health_alpha(self, now: float) -> float:
        """Advance the health HUD fade state machine and return its alpha."""
        logic = self.logic

        if not self.hud_fade_enabled:
            self._hud_health_alpha = 1.0
            self._hud_health_fade_started = None
            self._hud_health_fade_from = 1.0
            self._hud_health_fade_phase = "idle"
            self._hud_health_last_value = logic.player_runtime.player_health
            return self._hud_health_alpha

        def _sample(at):
            phase = self._hud_health_fade_phase
            if phase == "in":
                started = self._hud_health_fade_started
                if started is None:
                    self._hud_health_alpha = 1.0
                    self._hud_health_fade_from = 1.0
                    self._hud_health_fade_started = at
                    self._hud_health_fade_phase = "out"
                    return self._hud_health_alpha

                elapsed = max(0.0, at - started)
                if elapsed + 1e-12 < self._hud_health_fade_in_duration:
                    t = elapsed / self._hud_health_fade_in_duration
                    self._hud_health_alpha = (
                        self._hud_health_fade_from
                        + (1.0 - self._hud_health_fade_from) * t
                    )
                    return self._hud_health_alpha

                self._hud_health_alpha = 1.0
                self._hud_health_fade_from = 1.0
                self._hud_health_fade_phase = "out"
                out_elapsed = elapsed - self._hud_health_fade_in_duration
            elif phase == "out":
                started = self._hud_health_fade_started
                if started is None:
                    self._hud_health_alpha = 0.5
                    self._hud_health_fade_phase = "idle"
                    return self._hud_health_alpha
                out_elapsed = max(
                    0.0,
                    at
                    - started
                    - self._hud_health_fade_in_duration,
                )
            else:
                self._hud_health_alpha = 0.5
                return self._hud_health_alpha

            if out_elapsed + 1e-12 >= self._hud_health_fade_out_duration:
                self._hud_health_alpha = 0.5
                self._hud_health_fade_started = None
                self._hud_health_fade_phase = "idle"
                return self._hud_health_alpha

            t = max(
                0.0,
                min(
                    1.0,
                    out_elapsed / self._hud_health_fade_out_duration,
                ),
            )
            self._hud_health_alpha = 1.0 - (0.5 * t)
            return self._hud_health_alpha

        health = logic.player_runtime.player_health
        health_changed = (
            self._hud_health_last_value is not None
            and health != self._hud_health_last_value
        )

        if health_changed:
            _sample(now)
            self._hud_health_last_value = health
            self._hud_health_fade_started = now
            self._hud_health_fade_from = self._hud_health_alpha
            self._hud_health_fade_phase = "in"
        elif self._hud_health_last_value is None:
            self._hud_health_last_value = health

        return _sample(now)

    def peer_render_dirty(self, own_dirty, peer_table, snapshot_epoch):
        """Return the peer table's dirty objects when it can repair a rebuild."""
        logic = self.logic
        if own_dirty is not None or peer_table is None:
            return None
        peer_epoch = getattr(peer_table, "_epoch", None)
        if peer_epoch is None:
            return None
        return logic.editor_state.render_dirty_since(
            peer_epoch, through_epoch=snapshot_epoch
        )[1]

    def prepare_render_state(self):
        logic = self.logic
        started = time.perf_counter()
        write_state = logic.game_state.get_write_state()
        write_state.is_play_mode = logic.session_runtime.play_mode

        if logic.session_runtime.play_mode and logic.player_runtime.player:
            cs = logic.cutscene_runtime.state
            if cs and "cam_pos" in cs:
                cam_pos = glm.vec3(*cs["cam_pos"])
                cam_angle = cs.get("cam_angle", 0.0)
                cam_pitch = cs.get("cam_pitch", 0.0)
                direction = glm.vec3(
                    math.cos(cam_angle) * math.cos(cam_pitch),
                    math.sin(cam_pitch),
                    math.sin(cam_angle) * math.cos(cam_pitch),
                )
                view_matrix = glm.lookAt(
                    cam_pos,
                    cam_pos + direction,
                    glm.vec3(0, 1, 0),
                )
                write_state.player_pos = cam_pos
                write_state.player_angle = cam_angle
                write_state.player_pitch = cam_pitch
                fov = cs["fov"] if cs.get("fov") else 90.0
            else:
                player_pos = glm.vec3(
                    logic.player_runtime.player.pos.x,
                    logic.player_runtime.player.pos.y,
                    logic.player_runtime.player.pos.z,
                )
                player_angle = logic.player_runtime.player.angle
                player_pitch = logic.player_runtime.player.pitch
                camera_height = logic.player_runtime.player.camera_height
                ct = logic.camera.camera_transition
                if ct:
                    dur = ct["duration"]
                    t = (
                        1.0
                        if dur <= 0.0
                        else max(
                            0.0,
                            min(1.0, ct["elapsed"] / dur),
                        )
                    )
                    t = t * t * (3.0 - 2.0 * t)
                    a = logic.camera._camera_for_mode(
                        ct["from_overhead"],
                        player_pos,
                        player_angle,
                        player_pitch,
                        camera_height,
                    )
                    b = logic.camera._camera_for_mode(
                        ct["to_overhead"],
                        player_pos,
                        player_angle,
                        player_pitch,
                        camera_height,
                    )
                    cam_pos = a[0] + (b[0] - a[0]) * t
                    direction = a[1] + (b[1] - a[1]) * t
                    if glm.length(direction) < 1e-8:
                        direction = b[1]
                    direction = glm.normalize(direction)
                    up_vec = logic.camera._safe_up(
                        direction,
                        a[2] + (b[2] - a[2]) * t,
                    )
                    view_matrix = glm.lookAt(
                        cam_pos,
                        cam_pos + direction,
                        up_vec,
                    )
                    fov = a[3] + (b[3] - a[3]) * t
                elif logic.camera.is_overhead():
                    cam_pos, direction, up_vec = logic.camera._overhead_camera(
                        player_pos,
                        player_angle,
                    )
                    view_matrix = glm.lookAt(
                        cam_pos,
                        cam_pos + direction,
                        up_vec,
                    )
                    fov = logic.camera.frustum_fov
                else:
                    cam_pos = player_pos + glm.vec3(
                        0,
                        camera_height,
                        0,
                    )
                    direction = glm.vec3(
                        math.sin(player_angle) * math.cos(player_pitch),
                        math.sin(player_pitch),
                        math.cos(player_angle) * math.cos(player_pitch),
                    )
                    view_matrix = glm.lookAt(
                        cam_pos,
                        cam_pos + direction,
                        glm.vec3(0, 1, 0),
                    )
                    fov = logic.camera.frustum_fov
                write_state.player_pos = player_pos
                write_state.player_angle = player_angle
                write_state.player_pitch = player_pitch
        else:
            editor_camera = logic.camera.get_editor_camera()
            write_state.editor_camera_pos = glm.vec3(editor_camera.pos)
            write_state.editor_camera_yaw = editor_camera.yaw
            write_state.editor_camera_pitch = editor_camera.pitch
            write_state.editor_camera_fov = editor_camera.fov
            view_matrix = editor_camera.get_view_matrix()
            fov = editor_camera.fov

        write_state.camera_view_matrix = view_matrix

        cinematic_active = bool(logic.cutscene_runtime.state)
        now = time.perf_counter()
        if cinematic_active:
            self._hud_cinematic_last_active = True
            self._hud_cinematic_fade_started = None
            hud_alpha = 0.0
        elif self._hud_cinematic_last_active:
            self._hud_cinematic_last_active = False
            self._hud_cinematic_fade_started = now
            hud_alpha = 0.0
        elif self._hud_cinematic_fade_started is not None:
            hud_alpha = min(
                1.0,
                max(
                    0.0,
                    (now - self._hud_cinematic_fade_started) / 4.0,
                ),
            )
            if hud_alpha >= 1.0:
                self._hud_cinematic_fade_started = None
        else:
            hud_alpha = 1.0

        health_hud_alpha = self.update_hud_health_alpha(now)

        write_state.cinematic_camera_active = cinematic_active
        write_state.hud_alpha = hud_alpha
        write_state.hud_health_alpha = health_hud_alpha
        write_state.player_health = logic.player_runtime.player_health
        write_state.player_max_health = logic.player_runtime.player_max_health
        write_state.player_dead = logic.player_runtime.player_dead
        write_state.player_ammo = max(0, int(logic.combat_runtime.player_ammo))
        if logic.session_runtime.play_mode and logic.player_runtime.player and not logic.cutscene_runtime.state:
            write_state.player_underwater = bool(logic.player_runtime.player.eye_underwater)
            write_state.underwater_tint = list(logic.player_runtime.player.water_tint)
        else:
            write_state.player_underwater = False
        write_state.collected_keys = set(logic.player_runtime.collected_keys)
        write_state.hud_message = logic.interaction_runtime.current_hud_message
        write_state.hud_prompt_key = logic.interaction_runtime.current_hud_key_name
        write_state.active_weapon = logic.combat_runtime.active_weapon
        write_state.weapon_switch_serial = logic.combat_runtime.weapon_switch_serial
        write_state.player_armor = logic.player_runtime.player_armor
        write_state.muzzle_flash_active = logic.combat_runtime.muzzle_flash_active
        write_state.shot_ready = logic.combat_runtime.shot_ready(time.perf_counter())
        write_state.camera_transition_active = bool(logic.camera.camera_transition)

        if logic.session_runtime.play_mode and logic.combat_runtime._monster_projectiles:
            logic.combat_runtime._publish_projectile_render_snapshot()
        else:
            logic.combat_runtime.projectile_positions = _NO_PROJECTILES
        write_state.projectiles = logic.combat_runtime.projectile_positions
        write_state.monster_debug_active = logic.monster_ai.monster_debug_active
        write_state.monster_debug_rays = list(
            logic.monster_ai._debug_rays
        )

        current_time = time.perf_counter()
        write_state.bullet_marks = [
            {
                "pos": [m["pos"].x, m["pos"].y, m["pos"].z],
                "alpha": max(
                    0.0,
                    1.0
                    - (current_time - m["time"]) / logic.combat_runtime.BULLET_FADE_TIME,
                ),
            }
            for m in logic.combat_runtime.bullet_marks
            if current_time - m["time"] < logic.combat_runtime.BULLET_FADE_TIME
        ]

        far = (
            self.view_distance.far_plane
        )
        projection = glm.perspective(
            glm.radians(fov),
            logic.camera.frustum_aspect,
            1.0,
            far,
        )
        proj_view = projection * view_matrix
        frustum_planes = self.extract_frustum_planes(proj_view)

        brushes = logic.editor_state.brushes

        table = write_state.render_table
        etable = write_state.entity_table

        render_dirty_snapshot = logic.editor_state.render_dirty_snapshot()
        snapshot_epoch, _current_dirty = render_dirty_snapshot
        table_epoch = getattr(table, "_epoch", None)
        world_epoch, render_dirty = logic.editor_state.render_dirty_since(
            table_epoch,
            through_epoch=snapshot_epoch,
        )
        if (
            len(brushes) != table.count
            and any(b.get("id") is None for b in brushes)
        ):
            logic.editor_state.ensure_entity_ids()

        edited = (
            ()
            if logic.session_runtime.play_mode
            else logic.editor_state.edited_objects()
        )
        edited_ids = {id(obj): obj for obj in edited}
        left = [
            obj
            for oid, obj in self._last_edited.items()
            if oid not in edited_ids
        ]
        if left:
            JOURNAL.record_many(left, STATE)
        self._last_edited = edited_ids
        peer = logic.game_state.peer_state()
        peer_table = (
            peer.render_table if peer is not write_state else None
        )
        table.begin_frame(
            brushes,
            world_epoch,
            dirty_objects=render_dirty,
            edited=edited,
            peer=peer_table,
            peer_dirty=self.peer_render_dirty(
                render_dirty,
                peer_table,
                snapshot_epoch,
            ),
        )
        if logic.session_runtime.play_mode:
            logic.mover_runtime._movers().publish(logic, table)

        refs = table.refs
        total_count = table.count

        keep, all_slots = table.shown()
        # The editor's Filter menu: filtered brushes are not published, so no
        # renderer is asked to draw them.
        view_filters = logic.editor_state.view_filters
        filtering = view_filters.active and not logic.session_runtime.play_mode
        if filtering and total_count:
            keep = keep & ~view_filters.hidden_brush_rows(
                table.class_bits[:total_count], table)
            all_slots = np.flatnonzero(keep)
        if self.culling_enabled and total_count:
            visible_slots = np.flatnonzero(
                keep
                & self.aabb_in_frustum_bounds(
                    frustum_planes,
                    table.bounds[:total_count],
                )
            )
        else:
            visible_slots = all_slots

        culled_count = total_count - len(visible_slots)

        write_state.render_table = table
        write_state.render_refs = refs
        write_state.visible_brush_slots = visible_slots
        write_state.all_brush_slots = all_slots
        write_state.total_brushes = total_count
        write_state.culled_brushes = culled_count

        things = logic.editor_state.things
        etable = write_state.entity_table
        peer_etable = (
            peer.entity_table if peer is not write_state else None
        )
        thing_hidden = etable.begin_frame(
            things,
            world_epoch,
            dirty_objects=render_dirty,
            effect_runtime=logic.session_runtime.play_mode,
            effect_store=logic.session_runtime.effect_store,
            peer=peer_etable,
            peer_dirty=self.peer_render_dirty(
                render_dirty,
                peer_etable,
                snapshot_epoch,
            ),
        )
        erefs = etable.refs
        entity_things = etable.things
        thing_count = etable.count

        logic.editor_state.clear_render_dirty(render_dirty_snapshot)

        visible_thing_slots = etable.all_slots
        if filtering and thing_count:
            # Filtered entities are left out of the rows this view draws;
            # a filtered light still lights the editor, as in Radiant.
            filtered = view_filters.hidden_entity_rows(etable)
            visible_thing_slots = visible_thing_slots[
                ~filtered[visible_thing_slots]]
        collected = logic.prop_runtime.collected_ids
        if logic.session_runtime.play_mode and collected:
            prop_slots = np.flatnonzero(
                (etable.class_bits[:thing_count] & ENT_PROP) != 0
            )
            dropped = [
                int(i)
                for i in prop_slots
                if id(entity_things[int(i)]) in collected
            ]
            if dropped:
                keep_things = np.ones(thing_count, dtype=bool)
                keep_things[dropped] = False
                visible_thing_slots = np.flatnonzero(keep_things)

        write_state.has_portals = bool(len(etable.portal_slots))
        write_state.entity_table = etable
        write_state.entity_refs = erefs
        write_state.visible_thing_slots = visible_thing_slots
        write_state.thing_hidden = thing_hidden
        write_state.timestamp = time.perf_counter()

        if logic.session_runtime.play_mode and logic.player_runtime.player2:
            p2_pos = glm.vec3(logic.player_runtime.player2.pos)
            p2_cam = p2_pos + glm.vec3(
                0,
                logic.player_runtime.player2.camera_height,
                0,
            )
            p2_angle = logic.player_runtime.player2.angle
            p2_pitch = logic.player_runtime.player2.pitch
            p2_dir = glm.vec3(
                math.sin(p2_angle) * math.cos(p2_pitch),
                math.sin(p2_pitch),
                math.cos(p2_angle) * math.cos(p2_pitch),
            )
            write_state.player2_pos = p2_pos
            write_state.player2_angle = p2_angle
            write_state.player2_pitch = p2_pitch
            write_state.player2_view_matrix = glm.lookAt(
                p2_cam,
                p2_cam + p2_dir,
                glm.vec3(0, 1, 0),
            )
            write_state.player2_health = logic.player_runtime.player2_health
            write_state.player2_max_health = logic.player_runtime.player2_max_health
            write_state.player2_dead = logic.player_runtime.player2_dead
            write_state.player2_underwater = bool(logic.player_runtime.player2.eye_underwater)
            write_state.splitscreen_active = True
        else:
            write_state.splitscreen_active = False

        write_state.level_complete_ui = logic.interaction_runtime.level_complete_ui
        write_state.prepare_ms = (time.perf_counter() - started) * 1000.0
