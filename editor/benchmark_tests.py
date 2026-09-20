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
            "live_io_1000": 2.0,
            "live_1000_brushes": 3.0,
            "live_10000_brushes": 3.0,
            "live_100000_brushes": 2.0,
            "monster_chaos_witness": 15.0,
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
                self._append(
                    "  Live brush scene: created %d real brushes with varied dimensions."
                    % brush_count
                )
            elif label == "monster_chaos_witness":
                cooperative_yield = lambda: self._live_cooperative_yield(label)
                data, chaos_info = bench.make_monster_chaos_witness_world(
                    seed="43",
                    monster_count=50,
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
                logic.god_mode = True
                logic.notarget = True

                self._monster_chaos_aggro_injected = False
                self._monster_chaos_fighters = []
                self._monster_chaos_aggro_delay = 2.0
                self._monster_chaos_info = dict(chaos_info)
                self._show_monster_chaos_overlay(10.0)
                self._append(
                    "  Monster chaos witness: seed 43, 50 mixed monsters (30 human / 20 flying) in two hostile teams, "
                    "PathNode '%s'. All monsters are converging; infighting "
                    "will be injected after %.1f seconds."
                    % (
                        self._html_escape(
                            self._monster_chaos_info.get(
                                "pathnode_name", "ChaosPathNode"
                            )
                        ),
                        self._monster_chaos_aggro_delay,
                    )
                )

                self._current = ("monster_chaos_witness", 10.0, None)
                self._phase_started = time.perf_counter()
                self._measurement_deadline = self._phase_started + 10.0
                self._measurement_watchdog_deadline = (
                    self._phase_started + self._live_stress_timeout_for(label)
                )
                self._measurement_active = True
                self._sysmon_samples = []
                self._last_sysmon_sample = 0.0
                view.update()
                QApplication.processEvents()
                self._timer.start()

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
    
                self._append(
                    "  Live I/O: firing OnTrigger through %d real LogicRelay entities..."
                    % len(relays)
                )
    
                import editor.io_system as _io_system
                old_debug = _io_system.IO_DEBUG_ENABLED
                old_limit = sys.getrecursionlimit()

                _io_system.IO_DEBUG_ENABLED = False
                sys.setrecursionlimit(max(old_limit, 10000))
                try:
                    io_manager.reset()
                    start = time.perf_counter()
                    io_manager.fire_output(first, "OnTrigger")
                    completed = True
                    self._live_io_elapsed = time.perf_counter() - start
                finally:
                    _io_system.IO_DEBUG_ENABLED = old_debug
                    sys.setrecursionlimit(old_limit)

                if not completed or self._live_stress_timeout:
                    self._abort_live_stress(
                        self._live_stress_timeout_reason or
                        ("%s exceeded its %.1f s live benchmark timeout. "
                         "The benchmark was stopped without terminating Fio."
                         % (label, self._live_stress_timeout_for(label)))
                    )
                    return
    
                self._append(
                    "  Live I/O: completed %d LogicRelay hops in %.3f ms."
                    % (len(relays), self._live_io_elapsed * 1000.0)
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


