"""Benchmark test implementations for Fio's live benchmark runner.

BenchmarkRunner owns sequencing and lifecycle; BenchmarkTests owns test-specific
workloads and preparation helpers.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import traceback

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


class BenchmarkTests:
    PLAYER_AREA_DEFAULT_RADIUS = 256.0
    PLAYER_AREA_MAX_RADIUS = 2048.0

    def __init__(self, runner):
        object.__setattr__(self, "runner", runner)

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def __setattr__(self, name, value):
        if name == "runner":
            object.__setattr__(self, name, value)
        else:
            setattr(self.runner, name, value)

    def _test_duration(self, label):
        # The current-world path is prepared before the duration is requested.
        # Use the prepared local sweep duration so empty regions outside the
        # actual play area cannot stretch the measurement.
        if label in ("current_world_phase1", "current_world_phase2"):
            base = float(self._requested_duration) if self._requested_duration is not None else self._player_area_sweep_duration()
            return base if label.endswith("phase1") else base * 0.5
        if label in ("current_world", "current_world_phase1", "current_world_phase2", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            if self._requested_duration is not None:
                return float(self._requested_duration)
            return self._player_area_sweep_duration()
        return {
            "live_io_1000": 4.0,
            "live_1000_brushes": 6.0,
            "live_10000_brushes": 6.0,
            "live_100000_brushes": 4.0,
            "monster_chaos_witness": 35.0,
        }.get(label, 3.0)
    
    
    PLAYER_AREA_DEFAULT_RADIUS = 256.0
    PLAYER_AREA_MAX_RADIUS = 2048.0
    

    def _current_world_bounds(self):
        """Return geometric X/Z bounds of the loaded world for choosing an orbit radius."""
        from engine.constants import brush_aabb_bounds
        from engine.spatial import authored_hidden
    
        brushes = getattr(self.main_window.state, "brushes", [])
        bounds = []
        for brush in brushes:
            if not isinstance(brush, dict):
                continue
            if brush.get("is_trigger") or brush.get("is_fog"):
                continue
            if authored_hidden(brush):
                continue
            try:
                if brush.get("_collision_mode") == "mesh" and brush.get("_mesh_bounds"):
                    min_b, max_b = brush["_mesh_bounds"]
                    lo_x, lo_z = float(min_b[0]), float(min_b[2])
                    hi_x, hi_z = float(max_b[0]), float(max_b[2])
                else:
                    lo_x, _lo_y, lo_z, hi_x, _hi_y, hi_z = brush_aabb_bounds(brush)
                    lo_x, lo_z = float(lo_x), float(lo_z)
                    hi_x, hi_z = float(hi_x), float(hi_z)
                bounds.append((lo_x, lo_z, hi_x, hi_z))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
    
        if not bounds:
            camera = self.main_window.view_3d.camera
            x = float(camera.pos.x)
            z = float(camera.pos.z)
            return x - 128.0, x + 128.0, z - 128.0, z + 128.0
    
        return (
            min(item[0] for item in bounds),
            max(item[2] for item in bounds),
            min(item[1] for item in bounds),
            max(item[3] for item in bounds),
        )
    

    def _find_player_start(self):
        """Return the first usable PlayerStart as (x, y, z, yaw_degrees)."""
        from editor.things import PlayerStart
    
        for thing in getattr(self.main_window.state, "things", []):
            if not isinstance(thing, PlayerStart):
                continue
            pos = getattr(thing, "pos", None)
            if pos is None or len(pos) < 3:
                return None, "PlayerStart has no usable position"
            try:
                x = float(pos[0])
                y = float(pos[1])
                z = float(pos[2])
                angle = float(thing.properties.get("angle", 0.0))
            except (TypeError, ValueError, IndexError):
                return None, "PlayerStart has invalid coordinates or angle"
            if not all(math.isfinite(v) for v in (x, y, z, angle)):
                return None, "PlayerStart has non-finite coordinates or angle"
            return (x, y, z, angle), None
    
        return None, "no usable PlayerStart"

    def _player_area_sweep_duration(self):
        """Return the prepared PlayerStart orbit duration."""
        path = getattr(self, "_player_area_camera_path", None)
        if path is not None:
            return max(6.0, min(12.0, float(path["duration"])))
    
        min_x, max_x, min_z, max_z = self._current_world_bounds()
        radius = min(self.PLAYER_AREA_MAX_RADIUS, max(self.PLAYER_AREA_DEFAULT_RADIUS,
                                                        0.25 * max(max_x - min_x, max_z - min_z)))
        circumference = 2.0 * math.pi * radius
        return max(6.0, min(12.0, circumference / 250.0))
    

    def _prepare_player_area_sweep(self):
        """Prepare a pure PlayerStart-centred camera orbit; no collision is performed."""
        player_start, start_error = self._find_player_start()
        fallback = player_start is None
    
        min_x, max_x, min_z, max_z = self._current_world_bounds()
        camera = self.main_window.view_3d.camera
        if fallback:
            anchor_x = float(camera.pos.x)
            anchor_y = float(camera.pos.y)
            anchor_z = float(camera.pos.z)
            phase2_start_yaw = float(camera.yaw)
            fallback_reason = start_error or "no usable PlayerStart"
        else:
            anchor_x, anchor_y, anchor_z, phase2_start_yaw = player_start
            fallback_reason = None
    
        map_span = max(max_x - min_x, max_z - min_z)
        radius = min(
            self.PLAYER_AREA_MAX_RADIUS,
            max(self.PLAYER_AREA_DEFAULT_RADIUS, map_span * 0.25),
        )
        travel_distance = 2.0 * math.pi * radius
        duration = max(6.0, min(12.0, travel_distance / 250.0))
    
        self._player_area_camera_path = {
            "fallback": fallback,
            "center_x": float(anchor_x),
            "center_y": float(anchor_y),
            "center_z": float(anchor_z),
            "radius": float(radius),
            "phase2_start_yaw": float(phase2_start_yaw),
            "pitch": float(camera.pitch),
            "duration": float(duration),
        }
        self._player_area_sweep_metadata = {
            "mode": "player-start orbit" if not fallback else "camera-position fallback",
            "anchor_source": "PlayerStart" if not fallback else "camera position fallback",
            "fallback": fallback,
            "fallback_reason": fallback_reason,
            "bounds": (float(min_x), float(max_x), float(min_z), float(max_z)),
            "duration_s": float(duration),
            "travel_distance": float(travel_distance),
            "collision_disabled": True,
        }
    
        if fallback:
            self._append(
                "Camera sweep: CAMERA-POSITION FALLBACK for %.1f s — %s; collision disabled."
                % (duration, fallback_reason)
            )
        else:
            self._append(
                "Camera sweep: PLAYERSTART ORBIT for %.1f s — PlayerStart at "
                "(%.1f, %.1f, %.1f); collision disabled."
                % (duration, anchor_x, anchor_y, anchor_z)
            )
    

    def _advance_player_area_sweep(self):
        """Orbit around PlayerStart once, looking at PlayerStart, with no collision."""
        path = getattr(self, "_player_area_camera_path", None)
        if path is None:
            return
        elapsed = time.perf_counter() - self._phase_started
        duration = float(path["duration"])
        progress = min(1.0, max(0.0, elapsed / max(duration, 0.001)))
        angle = progress * 2.0 * math.pi
        x = path["center_x"] + path["radius"] * math.sin(angle)
        z = path["center_z"] + path["radius"] * math.cos(angle)
        yaw = math.degrees(            math.atan2(path["center_x"] - x, path["center_z"] - z)
        )
        self._set_benchmark_camera(
            x, z, path["center_y"], yaw, path["pitch"]
        )
    

    def _advance_player_area_rotation(self):
        """Rotate in place at PlayerStart through exactly 360 degrees, no collision."""
        path = getattr(self, "_player_area_camera_path", None)
        if path is None:
            return
        elapsed = time.perf_counter() - self._phase_started
        duration = float(self._test_duration("current_world_phase2"))
        progress = min(1.0, max(0.0, elapsed / max(duration, 0.001)))
        yaw = path["phase2_start_yaw"] + progress * 360.0
        self._set_benchmark_camera(
            path["center_x"], path["center_z"], path["center_y"],
            yaw, path["pitch"]
        )
    

    def _set_benchmark_camera(self, x, z, y, yaw, pitch):
        camera = self.main_window.view_3d.camera
        import glm
        position = glm.vec3(float(x), float(y), float(z))
        logic_thread = getattr(self.main_window.view_3d, "logic_thread", None)
        if logic_thread is not None and getattr(self.main_window.view_3d, "use_threading", False):
            logic_thread.set_editor_camera(position, yaw, pitch, camera.fov)
        else:
            camera.pos = position
            camera.yaw = yaw
            camera.pitch = pitch
    
    

    def _live_cooperative_yield(self, label):
        """Yield from long live-test batches without leaving the Qt thread."""
        self._monitor_beat(label, deadline=self._preparation_deadline)
        monitor_failed, monitor_reason = self._monitor_failed()
        if monitor_failed:
            raise TimeoutError(monitor_reason)
        if time.perf_counter() > self._preparation_deadline:
            elapsed = time.perf_counter() - self._phase_started
            raise TimeoutError(
                "%s exceeded the %.0f s preparation limit after %.1f s."
                % (label, self._preparation_timeout_s, elapsed)
            )
        self.status_label.setText(
            "Preparing: %s — %.1f s elapsed (%.0f s limit)"
            % (
                label,
                time.perf_counter() - self._phase_started,
                self._preparation_timeout_s,
            )
        )
        QApplication.processEvents()
    
    

    def _prepare_editor_windowed_map(self):
        """Load a medium procedural map for the live editor-window benchmarks."""
        cooperative_yield = lambda: self._live_cooperative_yield("editor_windowed_map")
        import random

        random.seed(self._bench.BENCHMARK_MAP_SEED)
        data = self._bench.create_map_data(
            {
                "world_width": 2048,
                "world_height": 2048,
                "min_room": 192,
                "max_room": 384,
                "room_count": 12,
                "wall_tex": "default.png",
                "floor_tex": "default.png",
                "enable_floors": True,
                "floor_height": 256,
                "floor_room_count": 3,
                "spawn_monsters": False,
                "monster_count": 0,
                "spawn_health": False,
            },
            yield_hook=cooperative_yield,
        )
        self._bench.load_live_benchmark_world(
            self.main_window,
            data,
            yield_hook=cooperative_yield,
        )
        QApplication.processEvents()
        self._append(
            "  Editor window scene: generated a medium procedural 2048x2048 map "
            "with 12 rooms for the live 3D view."
        )


    def _spawn_monster_chaos_entity(self, logic, rng, team, position, spawn_index, monster_type=None):
        """Insert one live Monster and rebuild the same cache used by LogicSpawner."""
        from editor.things import Monster

        if monster_type is None:
            monster_type = rng.choice(("human", "flying"))

        props = {
            "name": "MonsterChaos_%03d" % int(spawn_index),
            "monster_id": int(spawn_index),
            "monster_type": str(monster_type),
            "health": 100,
            "damage": 10,
            "awake": True,
            "wake_on_sight": True,
            "dead": False,
            "team": str(team),
            "variant": "<None>",
            "patrol": False,
            "is_shooting": False,
        }
        if monster_type == "flying":
            position = [float(position[0]), max(160.0, float(position[1])), float(position[2])]
        else:
            position = [float(position[0]), max(96.0, float(position[1])), float(position[2])]

        monster = Monster(pos=position, properties=props)
        with logic._monster_lock:
            logic.editor_state.things.append(monster)
            logic._build_entity_caches()
        return monster

    def _monster_chaos_random_position(self, rng, monster_type):
        """Choose a safe point inside the 1024^3 witness room."""
        x = rng.uniform(-400.0, 400.0)
        z = rng.uniform(-400.0, 400.0)
        if monster_type == "flying":
            y = rng.uniform(160.0, 320.0)
        else:
            y = 128.0
        return [x, y, z]

    def _face_monster_chaos_camera(self, logic, monster):
        """Aim the Play camera at the first witness monster."""
        player = getattr(logic, "player", None)
        if player is None:
            raise RuntimeError("Monster chaos witness has no live player")

        dx = float(monster.pos[0]) - float(player.pos.x)
        dy = float(monster.pos[1]) - (float(player.pos.y) + float(player.camera_height))
        dz = float(monster.pos[2]) - float(player.pos.z)
        horizontal = max(1e-6, math.hypot(dx, dz))
        player.angle = math.atan2(dx, dz)
        player.pitch = math.atan2(dy, horizontal)

        camera = getattr(self.main_window.view_3d, "camera", None)
        if camera is not None:
            camera.pos = getattr(camera, "pos", player.pos)
            camera.yaw = math.degrees(player.angle)
            camera.pitch = math.degrees(player.pitch)

    def _run_live_stress_test(self, label, value):

        """Prepare a live stress test; _tick drives the real workload."""
        bench = self._bench
        window = self.main_window
        view = window.view_3d
    
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = True
        self._live_stress_phase = "prepare"
        self._live_stress_label = label
        self._live_stress_value = value
        self._live_io_elapsed = None
        self._live_io_samples = []
        self._live_io_fires = 0
        self._live_io_hops = 0
        self._live_io_next_fire = 0.0
        self._live_io_manager = None
        self._live_io_source = None
        self._live_stress_timeout = False
        self._live_stress_timeout_reason = ""
        self._start_live_stress_monitor(label)
    
        try:
            # Every live stress test starts from a genuinely empty live
            # scene, including terrain and derived renderer/runtime state.
            cooperative_yield = lambda: self._live_cooperative_yield(label)
            self._clear_live_benchmark_scene(label, yield_hook=cooperative_yield)
            self._append("  Preparation stage: generating live workload...")
            cooperative_yield()
    
            if label in ("live_1000_brushes", "live_10000_brushes", "live_100000_brushes"):
                brush_count = {
                    "live_1000_brushes": 1000,
                    "live_10000_brushes": 10000,
                    "live_100000_brushes": 100000,
                }[label]
                # Use Fio's NumPy-assisted scene builder and load the resulting
                # level data into the existing EditorState.
                data = bench._make_brush_stress_scene(
                    brush_count,                    yield_hook=cooperative_yield,
                )
                bench.load_live_benchmark_world(
                    window,
                    data,
                    yield_hook=cooperative_yield,
                )
                # Do not recenter the camera here; the scene itself must remain
                # visible through the real 2D and 3D editor views during preparation.
                QApplication.processEvents()
                self._prepare_player_area_sweep()
                self._append(
                    "  Live brush scene: created %d real brushes with varied dimensions; "
                    "camera sweep will exercise culling and dense RenderTable key sorting."
                    % brush_count
                )
            elif label == "monster_chaos_witness":
                cooperative_yield = lambda: self._live_cooperative_yield(label)
                data, chaos_info = bench.make_monster_chaos_witness_world(
                    seed="43",
                    monster_count=40,
                    yield_hook=cooperative_yield,
                )
                bench.load_live_benchmark_world(
                    window,
                    data,
                    yield_hook=cooperative_yield,
                )
                QApplication.processEvents()

                window.raise_()
                window.activateWindow()
                QApplication.processEvents()

                if not view.play_mode:
                    window.enter_play_mode()
                    QApplication.processEvents()
                if not view.play_mode:
                    raise RuntimeError(
                        "Fio failed to enter Play Mode for monster chaos witness"
                    )

                logic = getattr(view, "logic_thread", None)
                if logic is None:
                    raise RuntimeError(
                        "Monster chaos witness has no live LogicThread"
                    )

                # God mode protects the benchmark player while the real MonsterAI
                # remains fully active and is allowed to target opposing teams.
                logic.god_mode = True
                logic.notarget = False
                self._install_monster_chaos_ai_counter(logic)

                import random
                rng = random.Random("43")
                self._monster_chaos_rng = rng
                self._monster_chaos_spawn_index = 0
                self._monster_chaos_total = 0
                self._monster_chaos_phase = "team1"
                self._monster_chaos_info = dict(chaos_info)
                self._monster_chaos_measurement_started = 0.0
                self._monster_chaos_measurement_deadline = 0.0
                self._monster_chaos_ai_counting = False

                first_monster = None
                team1_z = (-64.0, -32.0, 0.0, 32.0, 64.0)
                for z in team1_z:
                    monster_type = rng.choice(("human", "flying"))
                    monster_y = 128.0 if monster_type == "human" else 192.0
                    monster = self._spawn_monster_chaos_entity(
                        logic,
                        rng,
                        "team1",
                        [384.0 + rng.uniform(-8.0, 8.0), monster_y, z],
                        self._monster_chaos_spawn_index,
                        monster_type=monster_type,
                    )
                    self._monster_chaos_spawn_index += 1
                    self._monster_chaos_total += 1
                    if first_monster is None:
                        first_monster = monster

                if first_monster is None:
                    raise RuntimeError("Monster chaos witness failed to create its first monster")
                self._face_monster_chaos_camera(logic, first_monster)

                self._append(
                    "  Monster witness: 1024x1024x1024 room, PlayerStart at one end, "
                    "5 team1 monsters staged at the opposite end; god mode enabled."
                )
                self._append(
                    "  Population schedule: +5 team2 monsters at 0.5s, then +1 random-team "
                    "monster every 0.25s until 40."
                )
            elif label == "live_io_1000":
                cooperative_yield = lambda: self._live_cooperative_yield(label)
                data = bench._generate_procedural_map(
                    monsters=0,
                    relay_count=1000,
                    seed=bench.BENCHMARK_MAP_SEED,
                    yield_hook=cooperative_yield,
                )
                bench.load_live_benchmark_world(
                    window,
                    data,
                    yield_hook=cooperative_yield,
                )
                QApplication.processEvents()
                if not view.play_mode:
                    window.enter_play_mode()
                    QApplication.processEvents()
    
                io_manager = getattr(view.logic_thread, "io_manager", None)
                if io_manager is None:
                    raise RuntimeError("live Fio LogicThread has no IOManager")
                relays = [
                    thing for thing in window.state.things
                    if str(thing.properties.get("name", "")).startswith("BenchmarkRelay_")
                ]
                if not relays:
                    raise RuntimeError("live I/O benchmark generated no LogicRelay entities")
                first = min(
                    relays,
                    key=lambda thing: int(
                        str(thing.properties.get("name", "BenchmarkRelay_0")).rsplit("_", 1)[1]
                    ),
                )
    
                # Keep the source and live dispatcher for repeated timed fires.
                # Each burst is a real 1,000-entity serialized LogicRelay chain
                # owned by the running LogicThread; the measurement phase below
                # repeatedly exercises it while Fio is otherwise running normally.
                self._live_io_manager = io_manager
                self._live_io_source = first
                self._live_io_hops = max(0, len(relays) - 1)
                self._live_io_next_fire = 0.0
                self._live_io_samples = []
                self._live_io_fires = 0

                self._append(
                    "  Live I/O: prepared %d real LogicRelay entities; "
                    "the source chain will be fired repeatedly during measurement."
                    % len(relays)
                )
    
            else:
                raise ValueError("unknown live stress benchmark: %s" % label)
    
            duration = self._test_duration(label)
            self._current = (label, float(duration), None)
            self._phase_started = time.perf_counter()
            self._measurement_deadline = self._phase_started + float(duration)
            self._measurement_watchdog_deadline = (
                self._phase_started + self._live_stress_timeout_for(label)
            )
            self._measurement_active = True
            if label == "monster_chaos_witness":
                self._monster_chaos_phase = "team2_wait"
                self._monster_chaos_next_spawn = self._phase_started + 0.5
                self._monster_chaos_measurement_started = 0.0
                self._monster_chaos_measurement_deadline = 0.0
                self._monster_chaos_ai_counting = False
            view.update()
            QApplication.processEvents()
            self._timer.start()
        except Exception:
            self._live_stress_active = False
            self._stop_live_stress_monitor()
            if view.play_mode:
                try:
                    if label.startswith(("procedural_", "monster_")):
                        bench.finish_live_monster_test(window)
                    else:
                        window._exit_play_mode()
                except Exception:
                    pass
            raise
    


import configparser
from datetime import datetime, timezone
import html
import os
import platform
import subprocess
import sys
import math
import traceback
from PyQt5.QtWidgets import QApplication, QFileDialog

def _execution_environment():
    """Return a human-readable Python/CPU execution mode."""
    import platform
    process_arch = platform.machine() or "unknown"
    host_arch = process_arch
    translation = "none detected"
    if sys.platform == "win32":
        try:
            import ctypes
            process = ctypes.windll.kernel32.GetCurrentProcess()
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            fn = ctypes.windll.kernel32.IsWow64Process2
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
            fn.restype = ctypes.c_bool
            if fn(process, ctypes.byref(process_machine), ctypes.byref(native_machine)):
                names = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}
                process_arch = names.get(process_machine.value, "0x%04X" % process_machine.value)
                host_arch = names.get(native_machine.value, "0x%04X" % native_machine.value)
                if (native_machine.value == 0xAA64 and process_machine.value in (0x014C, 0x8664)):
                    translation = "Microsoft Prism / Windows on ARM emulation"
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            translated = ctypes.c_int(0)
            size = ctypes.c_size_t(ctypes.sizeof(translated))
            if libc.sysctlbyname(b"sysctl.proc_translated", ctypes.byref(translated), ctypes.byref(size), None, 0) == 0 and translated.value == 1:
                translation = "Apple Rosetta 2"
                host_arch = "ARM64"
        except Exception:
            pass
    return "Python: %s | Host CPU: %s | Translation: %s" % (process_arch, host_arch, translation)


