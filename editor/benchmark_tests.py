"""Individual live benchmark workloads.

The workloads still operate on the real Fio MainWindow.
"""

import math
import os
import sys
import time

from PyQt5.QtWidgets import QApplication


class BenchmarkTests:
    # Keep the ordinary live monster tiers from making a low-power editor
    # unresponsive. The limit is configurable for faster machines.
    LIVE_MONSTER_SAFE_LIMIT = 64

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
        return {"procedural_50_monsters": 4.0, "procedural_100_monsters": 4.0, "procedural_500_monsters": 4.0, "procedural_1000_monsters": 5.0, "live_io_1000": 2.0, "live_1000_brushes": 3.0, "live_10000_brushes": 3.0, "live_100000_brushes": 2.0, "monster_apocalypse": 4.0}.get(label, 3.0)
    
    
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


    def _run_monster_capacity_probe(self, bench, window, view):
        """Find the highest live monster count that remains responsive."""
        timeout_s = self._live_stress_timeout_for("monster_capacity")
        deadline = time.perf_counter() + timeout_s
        last_good = 0
        first_bad = None
        candidate = 16

        self._append(
            "  Monster capacity: probing the live MainWindow until a workload "
            "exceeds the %.0f s responsiveness limit." % timeout_s
        )

        def probe(count):
            window.state.clear_scene()
            window.update_all_ui()
            window.update_views()
            view.sysmon.reset_metrics()
            view.update()
            QApplication.processEvents()

            def yield_hook():
                QApplication.processEvents()
                if time.perf_counter() >= deadline:
                    raise TimeoutError(
                        "monster capacity probe at %d monsters exceeded the %.0f s "
                        "responsiveness limit" % (count, timeout_s)
                    )

            data = bench._generate_procedural_map(
                monsters=count,
                relay_count=32,
                seed=bench.BENCHMARK_MAP_SEED,
                live_monster=True,
                yield_hook=yield_hook,
            )
            bench.load_live_benchmark_world(window, data, yield_hook=yield_hook)
            QApplication.processEvents()
            bench.prepare_live_monster_test(window)
            QApplication.processEvents()

            stable_until = min(deadline, time.perf_counter() + 2.0)
            while time.perf_counter() < stable_until:
                QApplication.processEvents()
                view.update()
                time.sleep(0.01)

        try:
            while first_bad is None and candidate <= 4096:
                self._append("  Testing %d live monsters..." % candidate)
                try:
                    probe(candidate)
                    last_good = candidate
                    self._append("  PASS — %d monsters remained responsive." % candidate)
                    candidate *= 2
                except (TimeoutError, MemoryError):
                    first_bad = candidate
                    self._append("  LIMIT — %d monsters exceeded the responsiveness limit." % candidate)
                finally:
                    try:
                        if view.play_mode:
                            bench.finish_live_monster_test(window)
                    except Exception:
                        pass
                    QApplication.processEvents()

            if first_bad is None:
                first_bad = candidate

            low = last_good + 1
            high = first_bad - 1
            while low <= high:
                mid = (low + high) // 2
                self._append("  Binary search: testing %d monsters..." % mid)
                try:
                    probe(mid)
                    last_good = mid
                    low = mid + 1
                    self._append("  PASS — %d monsters." % mid)
                except (TimeoutError, MemoryError):
                    high = mid - 1
                    self._append("  FAIL — %d monsters." % mid)
                finally:
                    try:
                        if view.play_mode:
                            bench.finish_live_monster_test(window)
                    except Exception:
                        pass
                    QApplication.processEvents()

            self._timer.stop()
            self._measurement_active = False
            self._live_stress_active = False
            self._stop_live_stress_monitor()
            self._results.append({
                "test": "monster_capacity",
                "status": "passed",
                "benchmark_live": True,
                "monster_capacity": int(last_good),
                "timeout_seconds": float(timeout_s),
            })
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)
            self.output.append(
                '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
                '<div style="font-size:15px; font-weight:bold; color:#eeeeee;">Monster capacity</div>'
                '<div style="color:#aaa; margin-top:4px;">Live MainWindow / real LogicThread / real renderer</div>'
                '<table cellspacing="0" cellpadding="0" style="margin-top:10px;">'
                '<tr><td width="24" rowspan="2" bgcolor="#63d471"></td>'
                '<td height="2" bgcolor="#63d471"></td></tr>'
                '<tr><td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">Maximum live monsters:</span>'
                '<span style="font-size:42px; font-weight:bold; color:#ff9a32; margin-left:12px;">%d</span>'
                '</td></tr></table>'
                '<div style="color:#aaa; padding:4px 0;">Highest tested count that remained responsive within %.0f s.</div>'
                '</div>' % (last_good, timeout_s)
            )
            self.status_label.setText("Monster capacity: %d live monsters." % last_good)
            QApplication.processEvents()
            self._begin_next()
        except Exception:
            self._live_stress_active = False
            self._stop_live_stress_monitor()
            raise


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
            # Every live stress test gets a genuinely fresh editor scene.
            # Do not merely replace EditorState's lists: clear_scene() also
            # invalidates the I/O/entity caches and resets the scene-owned
            # runtime state before the next generated workload is loaded.
            # This prevents entities/geometry from a previous stress test
            # surviving into the next one (especially the 1000-relay I/O test
            # immediately before the monster workloads).
            window.state.clear_scene()
            window.update_all_ui()
            window.update_views()
            view.sysmon.reset_metrics()
            view.update()
            QApplication.processEvents()
            self._append(
                "  Created a fresh empty benchmark map before loading %s."
                % label
            )
    
            if label in ("live_1000_brushes", "live_10000_brushes", "live_100000_brushes"):
                brush_count = {
                    "live_1000_brushes": 1000,
                    "live_10000_brushes": 10000,
                    "live_100000_brushes": 100000,
                }[label]
                # Use Fio's NumPy-assisted scene builder and load the resulting
                # level data into the existing EditorState.
                cooperative_yield = lambda: self._live_cooperative_yield(label)
                data = bench._make_brush_stress_scene(
                    brush_count,                    yield_hook=cooperative_yield,
                )
                bench.load_live_benchmark_world(
                    window,
                    data,
                    yield_hook=cooperative_yield,
                )
                QApplication.processEvents()
                self._append(
                    "  Live brush scene: created %d real brushes in the existing "
                    "EditorState; 2D/3D views refreshed." % brush_count
                )
    
            elif label == "monster_capacity":
                self._run_monster_capacity_probe(bench, window, view)
                return

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
                io_deadline = time.perf_counter() + self._live_stress_timeout_for(label)
    
                def _yield_live_io():
                    # Return to Qt periodically so the independent monitor can
                    # deliver its timeout and the main thread can abort cleanly.
                    QApplication.processEvents()
                    if self._live_stress_timeout:
                        return False
                    if time.perf_counter() >= io_deadline:
                        self._live_stress_timeout = True
                        self._live_stress_timeout_reason = (
                            "%s exceeded its %.1f s live benchmark timeout. "
                            "The benchmark was stopped without terminating Fio."
                            % (label, self._live_stress_timeout_for(label))
                        )
                        return False
                    return True
    
                _io_system.IO_DEBUG_ENABLED = False
                sys.setrecursionlimit(max(old_limit, 10000))
                try:
                    io_manager.reset()
                    io_manager.set_dispatch_yield_hook(_yield_live_io, interval=16)
                    start = time.perf_counter()
                    completed = io_manager.fire_output(
                        first, "OnTrigger", _iterative=True
                    )
                    self._live_io_elapsed = time.perf_counter() - start
                finally:
                    io_manager.set_dispatch_yield_hook(None)
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
            view.sysmon.reset_metrics()
            view.sysmon.begin_benchmark_capture()
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
    
