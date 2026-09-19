"""Tools > Benchmark dialog.

The current-world and window-presentation benchmarks run inside the already-running
Fio MainWindow. Deliberately risky stress workloads run in an isolated worker
process so a pathological renderer, world-generation, I/O, or Play Mode test can
be terminated without wedging the Qt GUI. The benchmark does not depend on pytest.
"""

import os
import sys
import json
import platform
import subprocess
import math
import configparser
from datetime import datetime, timezone
import html

from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QApplication, QFileDialog, QTextBrowser, QToolButton, QWidget, QScrollArea, QProgressBar
from PyQt5.QtCore import QTimer, Qt
import copy
import time
import traceback
import threading
import tempfile


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
                if (native_machine.value == 0xAA64 and
                        process_machine.value in (0x014C, 0x8664)):
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

    return "Python: %s | Host CPU: %s | Translation: %s" % (
        process_arch, host_arch, translation
    )


class BenchmarkDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.root_dir = os.path.abspath(main_window.root_dir)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._bench = None
        self._queue = []
        self._current = None
        self._phase_started = 0.0
        self._original_level_data = None
        self._original_play_mode = False
        self._original_unsaved_changes = False
        self._original_camera = None
        self._running = False
        self._restoring = False
        self._results = []
        self._measurement_active = False
        self._measurement_deadline = 0.0
        self._measurement_watchdog_deadline = 0.0
        self._preparation_deadline = 0.0
        self._preparation_timeout_s = 30.0
        self._measurement_watchdog_extra_s = 30.0
        self._original_window_flags = None
        self._original_window_geometry = None
        self._original_window_state = None
        self._original_window_fullscreen = False
        self._benchmark_window_mode = None
        self._monitor_thread = None
        self._monitor_stop = None
        self._monitor_lock = None
        self._monitor_heartbeat = 0.0
        self._monitor_phase = ""
        self._monitor_deadline = 0.0
        self._monitor_timeout = False
        self._monitor_timeout_reason = ""
        self._worker_process = None
        self._worker_result_path = None
        self._worker_label = None
        self._worker_value = None
        self._worker_deadline = 0.0
        self._worker_active = False
        self._worker_finished = False
        self._worker_exit_code = None

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.throbber = QProgressBar()
        self.throbber.setRange(0, 0)
        self.throbber.setTextVisible(False)
        self.throbber.setFixedHeight(6)
        self.throbber.setVisible(False)
        self.throbber.setStyleSheet(
            "QProgressBar { border: 0; background: #292929; border-radius: 3px; }"
            "QProgressBar::chunk { background: #63d471; border-radius: 3px; }"
        )
        layout.addWidget(self.throbber)

        stress_toggle = QToolButton()
        stress_toggle.setText("See tests")
        stress_toggle.setCheckable(True)
        stress_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        stress_toggle.setArrowType(Qt.DownArrow)
        stress_toggle.setAutoRaise(True)
        layout.addWidget(stress_toggle)

        def update_stress_arrow(expanded):
            stress_toggle.setArrowType(Qt.UpArrow if expanded else Qt.DownArrow)

        stress_toggle.toggled.connect(update_stress_arrow)

        self.stress_options = QWidget()
        stress_layout = QVBoxLayout(self.stress_options)
        stress_layout.setContentsMargins(12, 0, 0, 0)

        self.additional_tests = QCheckBox(
            "Additional stress tests (I/O, renderer, gameplay)"
        )
        self.additional_tests.setToolTip(
            "Run deliberately heavy workloads against the live Fio renderer, procedural world and I/O system."
        )
        stress_layout.addWidget(self.additional_tests)

        self.brush_1000 = QCheckBox("Renderer scene: 1,000 brushes")
        self.brush_10000 = QCheckBox("Renderer scene: 10,000 brushes")
        self.brush_100000 = QCheckBox("Renderer scene: 100,000 brushes")
        self.io_chain_1000 = QCheckBox("I/O chain: 1,000 entities")
        self.monsters_100 = QCheckBox("Procedural room: 100 monsters")
        self.monsters_500 = QCheckBox("Procedural room: 500 monsters")
        self.monsters_1000 = QCheckBox("Procedural room: 1,000 monsters")
        self.monster_apocalypse = QCheckBox("FINAL TEST: maximum procedural monster apocalypse (1000 monsters + 1000 relays)")
        self.borderless_window = QCheckBox("Window mode: borderless maximized")
        self.fullscreen_window = QCheckBox("Window mode: true fullscreen")
        self.editor_windowed_1280 = QCheckBox("Editor mode: windowed 1280×720 (3D view pane)")
        self.editor_windowed_1920 = QCheckBox("Editor mode: windowed 1920×1080 (3D view pane)")
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
            self.monster_apocalypse,
            self.borderless_window,
            self.fullscreen_window,
            self.editor_windowed_1280,
            self.editor_windowed_1920,
        ):
            checkbox.setToolTip(
                "Run this deliberately large workload in addition to the standard stress tests."
            )
            stress_layout.addWidget(checkbox)

        self.stress_options.setVisible(False)
        stress_scroll = QScrollArea()
        stress_scroll.setWidgetResizable(True)
        stress_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        stress_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        stress_scroll.setMaximumHeight(220)
        stress_scroll.setWidget(self.stress_options)
        stress_toggle.toggled.connect(stress_scroll.setVisible)
        stress_scroll.setVisible(False)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(self._select_all_stress_tests)
        self.select_all_button.setVisible(False)
        stress_toggle.toggled.connect(self.select_all_button.setVisible)
        layout.addWidget(self.select_all_button)
        layout.addWidget(stress_scroll)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setStyleSheet(
            "QTextBrowser { font-family: Consolas, monospace; background: #171717; border: 1px solid #444; }"
        )
        layout.addWidget(self.output, 1)

        self.export_button = QPushButton("Export HTML Report…")
        self.export_button.setEnabled(False)
        self.export_button.setVisible(False)
        self.export_button.clicked.connect(self._export_results)
        layout.addWidget(self.export_button)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.setStyleSheet(
            "QPushButton { background: #2e9d4d; color: white; font-weight: bold; "
            "border: 1px solid #3fbd63; padding: 7px 16px; border-radius: 3px; }"
            "QPushButton:hover { background: #39b85b; }"
            "QPushButton:pressed { background: #257f3e; }"
            "QPushButton:disabled { background: #3b5a42; color: #b8c4ba; border-color: #49634f; }"
        )
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)

        benchmark_description = QLabel(
            "analyse the <b>currently loaded</b> project"
        )
        layout.addWidget(benchmark_description)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _select_all_stress_tests(self):
        for checkbox in (self.additional_tests, self.brush_1000, self.brush_10000, self.brush_100000, self.io_chain_1000, self.monsters_100, self.monsters_500, self.monsters_1000, self.monster_apocalypse, self.borderless_window, self.fullscreen_window, self.editor_windowed_1280, self.editor_windowed_1920):
            checkbox.setChecked(True)

    def _set_controls_enabled(self, enabled):
        self.run_button.setEnabled(enabled)
        self.additional_tests.setEnabled(enabled)
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
            self.monster_apocalypse,
            self.borderless_window,
            self.fullscreen_window,
            self.editor_windowed_1280,
            self.editor_windowed_1920,
        ):
            checkbox.setEnabled(enabled)

    def _append(self, text):
        self.output.append(text)
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _append_test_separator(self, label):
        self.output.append('<div style="border-top:2px solid #ff8a00; margin:14px 0 8px 0; padding-top:8px;"><span style="color:#ffb15a; font-weight:bold;">TEST: %s</span></div>' % label)

    def _test_duration(self, label):
        # The current-world path is prepared before the duration is requested.
        # Use the prepared local sweep duration so empty regions outside the
        # actual play area cannot stretch the measurement.
        if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            return self._player_area_sweep_duration()
        return {"procedural_100_monsters": 4.0, "procedural_500_monsters": 4.0, "procedural_1000_monsters": 5.0, "live_io_1000": 2.0, "live_1000_brushes": 3.0, "live_10000_brushes": 3.0, "live_100000_brushes": 2.0, "monster_apocalypse": 4.0}.get(label, 3.0)


    PLAYER_AREA_CELL_SIZE = 64.0
    PLAYER_AREA_MAX_CELLS = 16384
    PLAYER_AREA_CAMERA_MARGIN = 8.0
    PLAYER_AREA_SPIRAL_TURNS = 1.25

    def _current_world_bounds(self):
        """Return the geometric bounds used only by the fallback path."""
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
        """Return the same first PlayerStart the game uses for spawning."""
        from editor.things import PlayerStart

        for thing in getattr(self.main_window.state, "things", []):
            if not isinstance(thing, PlayerStart):
                continue
            pos = getattr(thing, "pos", None)
            if pos is None or len(pos) < 3:
                return None, "PlayerStart has no usable position"
            try:
                x = float(pos[0])
                z = float(pos[2])
            except (TypeError, ValueError, IndexError):
                return None, "PlayerStart has invalid coordinates"
            if not (math.isfinite(x) and math.isfinite(z)):
                return None, "PlayerStart has non-finite coordinates"
            return (x, z), None

        return None, "no usable PlayerStart"

    @staticmethod
    def _clone_benchmark_player(x, y, z, angle):
        """Create a lightweight Player state for one flood edge."""
        from engine.player import Player
        import glm

        player = Player(float(x), float(z), angle=float(angle), physics_enabled=True)
        player.pos = glm.vec3(float(x), float(y), float(z))
        player.velocity = glm.vec3(0.0, 0.0, 0.0)
        player.on_ground = True
        player.ground_object = None
        return player

    def _flood_player_area(self, start_x, start_z):
        """Flood reachable coarse cells using the real Player collision simulation.

        Every edge is one coarse player-movement step. The probe uses the same
        Player physics, SpatialGrid and terrain collision as Play Mode.
        """
        from engine.physics import SpatialGrid
        import glm

        view = self.main_window.view_3d
        state = self.main_window.state
        logic_thread = getattr(view, "logic_thread", None)

        collision_brushes = list(getattr(state, "brushes", []))
        if logic_thread is not None:
            collision_brushes.extend(
                getattr(logic_thread, "_model_collision_brushes", []) or []
            )

        grid = SpatialGrid(cell_size=512.0)
        grid.populate(collision_brushes)
        terrain = getattr(view, "terrain", None)

        cell_size = float(self.PLAYER_AREA_CELL_SIZE)
        max_cells = int(self.PLAYER_AREA_MAX_CELLS)
        move_delta = cell_size / 200.0
        tolerance = cell_size * 0.35

        origin = self._clone_benchmark_player(start_x, 100.0, start_z, 0.0)
        settled = False
        zero_move = glm.vec3(0.0, 0.0, 0.0)
        for _ in range(12):
            origin.update(
                0.1,
                zero_move,
                False,
                False,
                collision_brushes,
                terrain=terrain,
                spatial_grid=grid,
            )
            if origin.on_ground or origin.swimming:
                settled = True
                break

        if not settled or origin.pos.y <= -1990.0:
            return None, "player start could not settle onto a playable surface"

        reachable = {(0, 0): (float(origin.pos.x), float(origin.pos.y), float(origin.pos.z))}
        queue = [(0, 0)]
        queue_index = 0
        directions = ((1, 0), (-1, 0), (0, 1), (0, -1))

        while queue_index < len(queue):
            ix, iz = queue[queue_index]
            queue_index += 1

            if len(reachable) >= max_cells:
                return None, (
                    "playable flood exceeded the %d-cell safety limit before "
                    "the reachable region was closed" % max_cells
                )

            current_x, current_y, current_z = reachable[(ix, iz)]

            for dix, diz in directions:
                key = (ix + dix, iz + diz)
                if key in reachable:
                    continue

                target_x = start_x + key[0] * cell_size
                target_z = start_z + key[1] * cell_size
                angle = math.atan2(float(dix), float(diz))
                probe = self._clone_benchmark_player(
                    current_x, current_y, current_z, angle
                )
                probe.update(
                    move_delta,
                    glm.vec3(0.0, 0.0, 1.0),
                    False,
                    False,
                    collision_brushes,
                    terrain=terrain,
                    spatial_grid=grid,
                )

                if not (
                    math.isfinite(float(probe.pos.x))
                    and math.isfinite(float(probe.pos.y))
                    and math.isfinite(float(probe.pos.z))
                ):
                    continue
                if probe.pos.y <= -1990.0:
                    continue
                if not (probe.on_ground or probe.swimming):
                    continue
                if abs(float(probe.pos.x) - target_x) > tolerance:
                    continue
                if abs(float(probe.pos.z) - target_z) > tolerance:
                    continue
                if probe._check_overlap(collision_brushes):
                    continue

                reachable[key] = (
                    float(probe.pos.x),
                    float(probe.pos.y),
                    float(probe.pos.z),
                )
                queue.append(key)

        xs = [start_x + key[0] * cell_size for key in reachable]
        zs = [start_z + key[1] * cell_size for key in reachable]
        half = cell_size * 0.5
        return {
            "bounds": (
                min(xs) - half,
                max(xs) + half,
                min(zs) - half,
                max(zs) + half,
            ),
            "cell_size": cell_size,
            "cell_count": len(reachable),
            "cells": set(reachable.keys()),
            "points": dict(reachable),
        }, None

    def _player_area_sweep_duration(self):
        """Return the prepared player-area sweep duration."""
        path = getattr(self, "_player_area_camera_path", None)
        if path is not None:
            return max(6.0, min(12.0, float(path["duration"])))

        min_x, max_x, min_z, max_z = self._current_world_bounds()
        diagonal = math.hypot(max_x - min_x, max_z - min_z)
        return max(6.0, min(12.0, diagonal / 250.0))

    def _vsync_metadata(self):
        """Return the Fio display VSync setting used by the Qt application."""
        settings_path = os.path.join(self.root_dir, "settings.ini")
        configured = True
        source = "default (settings.ini missing or Display.vsync absent)"
        try:
            parser = configparser.ConfigParser()
            parser.read(settings_path, encoding="utf-8")
            if parser.has_option("Display", "vsync"):
                configured = parser.getboolean("Display", "vsync")
                source = "settings.ini [Display] vsync"
        except (configparser.Error, ValueError, OSError):
            source = "default (could not read settings.ini [Display].vsync)"

        return {
            "enabled": bool(configured),
            "swap_interval": 1 if configured else 0,
            "source": source,
        }

    def _git_commit(self):
        try:
            return subprocess.check_output(["git", "-C", self.root_dir, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
        except Exception:
            return "unknown"

    @staticmethod
    def _html_escape(value):
        return html.escape(str(value), quote=True)

    def _benchmark_results_html(self, version):
        timestamp = datetime.now(timezone.utc).isoformat()
        environment = self._html_escape(_execution_environment())
        platform_name = self._html_escape(platform.platform())
        python_version = self._html_escape(platform.python_version())
        cpu = self._html_escape(platform.processor() or "unknown")
        commit = self._html_escape(self._git_commit())
        vsync = self._vsync_metadata()
        vsync_state = "Enabled" if vsync["enabled"] else "Disabled"
        vsync_interval = int(vsync["swap_interval"])
        vsync_source = self._html_escape(vsync["source"])
        version = self._html_escape(version)

        cards = []
        for result in self._results:
            label = self._html_escape(result.get("test", "benchmark"))
            if result.get("aborted"):
                reason = self._html_escape(result.get("abort_reason", "No reason supplied"))
                status_text = (
                    "ABORTED — timeout"
                    if "timeout" in str(result.get("abort_reason", "")).lower()
                    else "ABORTED — worker failure"
                )
                cards.append(
                    '<section class="result aborted">'
                    '<h2>%s</h2>'
                    '<div class="abort">%s</div>'
                    '<p>%s</p>'
                    '</section>' % (label, self._html_escape(status_text), reason)
                )
                continue

            description = self._html_escape(result.get("description", result.get("test", "")))
            fps = result.get("average_fps")
            mean_ms = result.get("average_frame_time_ms", result.get("mean_ms"))
            p95_ms = result.get("p95_frame_time_ms", result.get("p95_ms"))
            resolution = self._html_escape(result.get("resolution", ""))
            brushes = result.get("brush_count", result.get("brushes"))
            entities = result.get("entity_count", result.get("entities"))

            metrics = []
            if resolution:
                metrics.append("Resolution: %s" % resolution)
            if fps is not None:
                metrics.append("Average FPS: %.2f (from captured frame time)" % float(fps))
            if "wall_clock_fps" in result:
                metrics.append("Wall-clock FPS: %.2f (captured frames / measurement duration)" % float(result["wall_clock_fps"]))
            if mean_ms is not None:
                metrics.append("Average frame: %.2f ms" % float(mean_ms))
            if p95_ms is not None:
                metrics.append("p95: %.2f ms" % float(p95_ms))
            if brushes is not None:
                metrics.append("Brushes: %s" % self._html_escape(brushes))
            if entities is not None:
                metrics.append("Entities: %s" % self._html_escape(entities))
            if "hops_per_second" in result:
                metrics.append("I/O: %.0f hops/s" % float(result["hops_per_second"]))
            if "clip_operations_per_second" in result:
                metrics.append("CSG: %.0f clip operations/s" % float(result["clip_operations_per_second"]))

            fps_html = ""
            if fps is not None:
                fps_html = '<div class="fps">%.2f <span>FPS</span></div>' % float(fps)

            extra = []
            sysmon = result.get("sysmon") or {}
            visible_tris = result.get("average_visible_tris", sysmon.get("average_visible_tris"))
            total_tris = result.get("average_total_tris", sysmon.get("average_total_tris"))
            culled_tris = result.get("average_culled_tris", sysmon.get("average_culled_tris"))
            culling_efficiency = result.get("culling_efficiency", sysmon.get("culling_efficiency"))
            if visible_tris is not None:
                extra.append("Average visible triangles: %.0f" % float(visible_tris))
            if total_tris is not None:
                extra.append("Average total triangles: %.0f" % float(total_tris))
            if culled_tris is not None:
                extra.append("Average culled triangles: %.0f" % float(culled_tris))
            if culling_efficiency is not None:
                extra.append("Culling efficiency: %.1f%%" % float(culling_efficiency))
            if "one_percent_low_fps" in result:
                extra.append("1% low: %.2f FPS" % float(result["one_percent_low_fps"]))
            if "zero_point_one_percent_low_fps" in result:
                extra.append("0.1% low: %.2f FPS" % float(result["zero_point_one_percent_low_fps"]))
            vram_source = result if ("vram_used_mb" in result or "vram_total_mb" in result) else sysmon
            if vram_source:
                extra.append("VRAM: %s" % self._html_escape(self._format_vram(vram_source)))
            if "final_camera_pos" in result:
                extra.append("Final camera position: %s" % self._html_escape(result["final_camera_pos"]))
            if result.get("camera_sweep_mode"):
                extra.append(
                    "Camera sweep: %s" % self._html_escape(
                        result["camera_sweep_mode"]
                    )
                )
                extra.append(
                    "Sweep anchor: %s" % self._html_escape(
                        result.get("camera_sweep_anchor", "unknown")
                    )
                )
                if result.get("camera_sweep_fallback"):
                    extra.append(
                        "Sweep fallback: %s" % self._html_escape(
                            result.get("camera_sweep_fallback_reason", "unknown")
                        )
                    )
                if result.get("camera_sweep_reachable_cells") is not None:
                    extra.append(
                        "Reachable flood cells: %s" % self._html_escape(
                            result["camera_sweep_reachable_cells"]
                        )
                    )

            details_html = ""
            if extra:
                details_html = '<div class="details">%s</div>' % "<br>".join(self._html_escape(item) for item in extra)

            metrics_html = "<span> • </span>".join(self._html_escape(item) for item in metrics)
            cards.append(
                '<section class="result">'
                '<div class="result-head"><div><h2>%s</h2><p>%s</p></div>%s</div>'
                '<div class="metrics">%s</div>%s'
                '</section>' % (label, description, fps_html, metrics_html, details_html)
            )

        return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fio Benchmark Report</title>
<style>
body { margin:0; padding:32px; background:#111; color:#ddd; font-family:Segoe UI,Arial,sans-serif; }
main { max-width:1100px; margin:0 auto; }
h1 { margin:0 0 8px; color:#eee; font-size:30px; }
h2 { margin:0; color:#eee; font-size:18px; }
p { margin:5px 0 0; color:#aaa; }
.meta { margin:0 0 24px; padding:16px; background:#191919; border:1px solid #333; line-height:1.7; font-family:Consolas,monospace; font-size:13px; }
.result { margin:14px 0; padding:18px; background:#1b1b1b; border:1px solid #3a3a3a; border-radius:6px; }
.result-head { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; }
.fps { color:#ff9a32; font-size:34px; font-weight:700; white-space:nowrap; }
.fps span { color:#63d471; font-size:16px; }
.metrics { margin-top:14px; color:#bbb; line-height:1.8; }
.details { margin-top:12px; color:#eee; line-height:1.8; }
.aborted { border-color:#ff8a00; background:#21180f; }
.abort { margin-top:10px; color:#ff8a00; font-size:24px; font-weight:700; }
.footer { margin-top:28px; padding-top:14px; border-top:2px solid #63d471; color:#777; font-size:12px; }
</style>
</head>
<body><main>
<h1>Fio Benchmark Report</h1>
<div class="meta">
Fio version: %s<br>
Generated: %s<br>
Execution: %s<br>
Average FPS definition: 1000 / mean(captured frame time)<br>
VSync: <strong>%s</strong> (swap interval %d)<br>
VSync source: %s<br>
<div style="margin:8px 0; padding:8px; color:#aaa; background:#151515; border-left:3px solid #63d471;">Live editor/window tests use this VSync setting. Isolated stress workers use independent GL test contexts, so their renderer FPS is not capped by the editor's presentation VSync.</div>
Platform: %s<br>
Python: %s<br>
CPU: %s<br>
Git commit: %s
</div>
%s
<div class="footer">Generated by Fio Tools &gt; Benchmark. Worker-process JSON is internal IPC; this file is the user-facing report.</div>
</main></body>
</html>""" % (
            version, self._html_escape(timestamp), environment, vsync_state,
            vsync_interval, vsync_source, platform_name, python_version, cpu,
            commit, "".join(cards)
        )

    def _export_results(self):
        if not self._results:
            return
        default_name = "fio_benchmark_%s.html" % datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Fio Benchmark Results",
            default_name,
            "HTML files (*.html);;All files (*)",
        )
        if not path:
            return
        version = "unknown"
        version_path = os.path.join(self.root_dir, "editor", "version.txt")
        try:
            with open(version_path, "r", encoding="utf-8") as f:
                version = f.read().strip()
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._benchmark_results_html(version))
            self.status_label.setText("Exported benchmark report: %s" % os.path.basename(path))
        except Exception:
            self._append("<span style='color:#ff6666;'>Export failed.</span><pre>%s</pre>" % traceback.format_exc())

    def _start_monitor_for_risky_test(self, label, timeout_s, process):
        """Supervise an isolated stress worker and terminate it on timeout."""
        self._stop_monitor()
        self._monitor_stop = threading.Event()
        self._monitor_lock = threading.Lock()
        self._monitor_heartbeat = time.perf_counter()
        self._monitor_phase = str(label)
        self._monitor_deadline = time.perf_counter() + float(timeout_s)
        self._monitor_timeout = False
        self._monitor_timeout_reason = ""
        self._worker_finished = False
        self._worker_exit_code = None
        stop = self._monitor_stop
        lock = self._monitor_lock
        deadline = self._monitor_deadline

        def terminate_worker():
            if process.poll() is not None:
                return
            try:
                process.terminate()
                process.wait(timeout=1.5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1.5)
                except Exception:
                    pass

        def monitor():
            while not stop.wait(0.25):
                now = time.perf_counter()
                exit_code = process.poll()
                if exit_code is not None:
                    with lock:
                        self._worker_finished = True
                        self._worker_exit_code = exit_code
                    return
                if now > deadline:
                    reason = (
                        "%s exceeded the %.1f s hard timeout. The isolated worker "
                        "was terminated before the benchmark could hang Fio."
                        % (label, float(timeout_s))
                    )
                    terminate_worker()
                    with lock:
                        self._monitor_timeout = True
                        self._monitor_timeout_reason = reason
                        self._worker_finished = True
                        self._worker_exit_code = process.poll()
                    return

        self._monitor_thread = threading.Thread(
            target=monitor,
            name="FioBenchmarkMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_beat(self, phase=None, deadline=None):
        if self._monitor_stop is None or self._monitor_lock is None:
            return
        with self._monitor_lock:
            self._monitor_heartbeat = time.perf_counter()
            if phase is not None:
                self._monitor_phase = str(phase)
            if deadline is not None:
                self._monitor_deadline = float(deadline)

    def _monitor_failed(self):
        if self._monitor_lock is None:
            return False, ""
        with self._monitor_lock:
            return bool(self._monitor_timeout), self._monitor_timeout_reason

    def _stop_monitor(self):
        stop = self._monitor_stop
        thread = self._monitor_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.75)
        self._monitor_stop = None
        self._monitor_thread = None
        self._monitor_lock = None

    def _worker_timeout_for(self, label):
        """Return the hard wall-clock timeout for an isolated stress test."""
        if label == "monster_apocalypse":
            return 60.0
        return 30.0

    def _terminate_worker_process(self):
        process = self._worker_process
        self._worker_process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1.5)
                except Exception:
                    pass

    def _start_worker_test(self, label, value):
        """Run a risky benchmark in a killable child process."""
        self._timer.stop()
        self._measurement_active = False
        self._worker_active = False
        self._worker_finished = False
        self._worker_exit_code = None
        self._worker_label = label
        self._worker_value = value

        fd, result_path = tempfile.mkstemp(
            prefix="fio_benchmark_worker_",
            suffix=".json",
        )
        os.close(fd)
        try:
            os.unlink(result_path)
        except OSError:
            pass
        self._worker_result_path = result_path

        env = os.environ.copy()
        env["FIO_FULLSCREEN_BENCH_WORKER_TEST"] = str(label)
        env["FIO_FULLSCREEN_BENCH_WORKER_OUT"] = result_path
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            self.root_dir + os.pathsep + existing_pythonpath
            if existing_pythonpath else self.root_dir
        )
        env["PYTHONUNBUFFERED"] = "1"

        script = os.path.join(
            self.root_dir, "tests", "performance", "fio_benchmark.py"
        )
        popen_kwargs = {
            "cwd": self.root_dir,
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        if creationflags:
            popen_kwargs["creationflags"] = creationflags
        elif os.name != "nt":
            popen_kwargs["start_new_session"] = True

        timeout_s = self._worker_timeout_for(label)
        self._worker_deadline = time.perf_counter() + timeout_s
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", script],
                **popen_kwargs,
            )
        except Exception:
            self._worker_result_path = None
            raise

        self._worker_process = process
        self._worker_active = True
        self._start_monitor_for_risky_test(label, timeout_s, process)
        self.status_label.setText(
            "Running isolated worker: %s (%.0f s hard timeout)"
            % (label, timeout_s)
        )
        self._append(
            "Isolated worker started: PID %d — hard timeout %.0f s."
            % (int(process.pid), timeout_s)
        )
        self._timer.start()

    def _cleanup_worker_result_path(self):
        path = self._worker_result_path
        self._worker_result_path = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _report_worker_result(self, label, payload):
        """Append results returned by an isolated benchmark worker."""
        results = payload.get("results", [])
        if not results:
            raise RuntimeError(
                "isolated worker %s returned no benchmark results" % label
            )

        for result in results:
            result = dict(result)
            result["worker_test"] = label
            self._results.append(result)
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)

            average_fps = result.get("average_fps")
            resolution = result.get("resolution", "")
            entities = result.get("entity_count", result.get("entities"))
            brushes = result.get("brush_count", result.get("brushes"))
            description = result.get("description", label)

            self.output.append(
                '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
                '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
                '<div style="color:#aaa;">%s%s%s%s</div>'
                '</div>'
                % (
                    result.get("test", label),
                    description,
                    (" &nbsp; • &nbsp; " + resolution) if resolution else "",
                    (" &nbsp; • &nbsp; " + str(brushes) + " brushes") if brushes is not None else "",
                    (" &nbsp; • &nbsp; " + str(entities) + " entities") if entities is not None else "",
                )
            )

            metrics = result.get("sysmon") or {}
            if average_fps is not None:
                self.output.append(
                    '<div style="margin-top:10px; padding:8px 0 2px 0; border-top:2px solid #555; white-space:nowrap;">'
                    '<span style="font-size:25px; font-weight:bold; color:#63d471;">average FPS:</span>'
                    '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f</span>'
                    '</div>' % float(average_fps)
                )
                self.output.append(
                    '<div style="color:#aaa; padding:4px 0;">frame time %.2f ms &nbsp; • &nbsp; p95 %.2f ms%s</div>'
                    % (
                        float(metrics.get("average_frame_time_ms", result.get("mean_ms", 0.0))),
                        float(metrics.get("p95_frame_time_ms", result.get("p95_ms", 0.0))),
                        (" &nbsp; • &nbsp; VRAM " + self._format_vram(metrics)) if metrics else "",
                    )
                )
            if "hops_per_second" in result:
                self.output.append(
                    '<div style="color:#aaa; padding:4px 0;">I/O throughput: <b style="color:#ff9a32;">%.0f hops/s</b></div>'
                    % float(result["hops_per_second"])
                )
            if "clip_operations_per_second" in result:
                self.output.append(
                    '<div style="color:#aaa; padding:4px 0;">CSG throughput: <b style="color:#ff9a32;">%.0f clip operations/s</b></div>'
                    % float(result["clip_operations_per_second"])
                )
            if "final_camera_pos" in result:
                self.output.append(
                    '<div style="color:#aaa; padding:4px 0;">Final camera position: %s</div>'
                    % (result["final_camera_pos"],)
                )

        self.output.append('<div style="border-top:2px solid #63d471; margin:14px 0 8px 0;"></div>')
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _abort_worker_test(self, reason, timed_out=False):
        """Abort one isolated test, record it, then continue the queue."""
        label = self._worker_label or (self._current[0] if self._current else "unknown")
        self._timer.stop()
        self._measurement_active = False
        self._worker_active = False
        self._terminate_worker_process()
        self._stop_monitor()

        result = {
            "test": label,
            "worker_test": label,
            "status": "aborted",
            "aborted": True,
            "abort_reason": str(reason),
        }
        self._results.append(result)
        self.export_button.setEnabled(True)
        self.output.append(
            '<div style="background:#2a1c10; border:1px solid #ff8a00; padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#ffb15a;">%s</div>'
            '<div style="font-size:25px; font-weight:bold; color:#ff8a00; margin-top:6px;">%s</div>'
            '<div style="color:#ddd; margin-top:4px;">%s</div>'
            '</div>' % (label, "ABORTED — timeout" if timed_out else "ABORTED — worker failure", str(reason))
        )
        self.output.append('<div style="border-top:2px solid #63d471; margin:14px 0 8px 0;"></div>')
        self._cleanup_worker_result_path()
        self._worker_label = None
        self._worker_value = None
        self._worker_deadline = 0.0
        self.status_label.setText("Aborted: %s — original Fio world remains intact." % label)
        QApplication.processEvents()
        self._begin_next()

    def _poll_worker_test(self):
        """Poll worker completion from Qt without doing the risky work here."""
        if not self._worker_active:
            return

        monitor_failed, monitor_reason = self._monitor_failed()
        if monitor_failed:
            self._abort_worker_test(monitor_reason, timed_out=True)
            return

        process = self._worker_process
        if process is None:
            self._abort_worker_test("isolated benchmark worker disappeared")
            return

        exit_code = process.poll()
        if exit_code is None:
            elapsed = time.perf_counter() - self._phase_started
            remaining = max(0.0, self._worker_deadline - time.perf_counter())
            self.status_label.setText(
                "Running isolated worker: %s — %.1f s elapsed, %.1f s remaining"
                % (self._worker_label, elapsed, remaining)
            )
            return

        self._worker_finished = True
        self._worker_exit_code = exit_code
        result_path = self._worker_result_path
        label = self._worker_label
        self._worker_active = False
        self._timer.stop()
        self._stop_monitor()

        payload = None
        if result_path and os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception:
                payload = None

        self._cleanup_worker_result_path()
        self._worker_process = None

        if not payload or not payload.get("ok"):
            error = (payload or {}).get("error")
            if not error:
                error = "worker exited with code %s without producing a valid result" % exit_code
            self._worker_label = label
            self._abort_worker_test(
                "isolated worker failed: %s" % error
            )
            return

        try:
            self._report_worker_result(label, payload)
        except Exception:
            self._worker_label = label
            self._finish_with_error(traceback.format_exc())
            return

        self._worker_label = None
        self._worker_value = None
        self._worker_deadline = 0.0
        self._begin_next()

    def _has_current_loaded_map(self):
        """Return True when the live editor has a current map/scene to benchmark."""
        if getattr(self.main_window, "file_path", None):
            return True

        state = self.main_window.state
        if getattr(state, "brushes", None):
            return True
        if getattr(state, "things", None):
            return True
        if getattr(state, "terrain_data", None) is not None:
            return True
        return False

    def _has_selected_stress_test(self):
        """Return True when at least one optional benchmark workload is selected."""
        return any(check.isChecked() for check in (
            self.additional_tests,
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
            self.monster_apocalypse,
            self.borderless_window,
            self.fullscreen_window,
            self.editor_windowed_1280,
            self.editor_windowed_1920,
        ))

    def _start(self):
        if self._running:
            return

        has_current_map = self._has_current_loaded_map()
        has_selected_tests = self._has_selected_stress_test()
        if not has_current_map and not has_selected_tests:
            self.output.clear()
            self.output.append(
                '<div style="color:#ff6666; font-size:16px; font-weight:bold; padding:10px;">'
                'No current loaded map and no tests selected'
                '</div>'
            )
            self.status_label.setText("No current loaded map and no tests selected")
            self.status_label.setStyleSheet("color:#ff6666; font-weight:bold;")
            self.throbber.setVisible(False)
            QApplication.processEvents()
            return

        self.status_label.setStyleSheet("")
        self.output.clear()
        self._results = []
        self.export_button.setEnabled(False)
        self.export_button.setVisible(False)
        self.status_label.setText("Preparing live Fio benchmark...")
        self._set_controls_enabled(False)
        # Keep the test selector collapsed while a benchmark is running so
        # the result pane retains the vertical space needed for live output.
        stress_toggle = self.findChild(QToolButton)
        if stress_toggle is not None:
            stress_toggle.setChecked(False)
        self._running = True
        self.throbber.setVisible(True)
        self._stop_monitor()

        try:
            from tests.performance import fio_benchmark as bench
            self._bench = bench

            # This is the actual editor state. We restore it when the benchmark
            # finishes, rather than creating a second Fio instance.
            self._original_level_data = copy.deepcopy(
                self.main_window.state.get_level_data()
            )
            self._original_play_mode = bool(self.main_window.view_3d.play_mode)
            self._original_unsaved_changes = bool(
                getattr(self.main_window, "unsaved_changes", False)
            )
            camera = self.main_window.view_3d.camera
            self._original_window_flags = self.main_window.windowFlags()
            self._original_window_geometry = self.main_window.geometry()
            self._original_window_state = self.main_window.windowState()
            self._original_window_fullscreen = self.main_window.isFullScreen()

            self._original_camera = (
                (float(camera.pos.x), float(camera.pos.y), float(camera.pos.z)),
                float(camera.yaw),
                float(camera.pitch),
                float(camera.fov),
            )

            self._queue = [("current_world", None)]
            if self.additional_tests.isChecked():
                self._queue.extend([
                    ("procedural_100_monsters", 100),
                    ("live_io_1000", 1000),
                ])
            if self.io_chain_1000.isChecked():
                self._queue.append(("live_io_1000", 1000))
            if self.monsters_100.isChecked():
                self._queue.append(("procedural_100_monsters", 100))
            if self.monsters_500.isChecked():
                self._queue.append(("procedural_500_monsters", 500))
            if self.monsters_1000.isChecked():
                self._queue.append(("procedural_1000_monsters", 1000))
            if self.monster_apocalypse.isChecked():
                self._queue.append(("monster_apocalypse", 1000))
            if self.borderless_window.isChecked():
                self._queue.append(("borderless_window", None))
            if self.fullscreen_window.isChecked():
                self._queue.append(("fullscreen_window", None))
            if self.editor_windowed_1280.isChecked():
                self._queue.append(("editor_windowed_1280", None))
            if self.editor_windowed_1920.isChecked():
                self._queue.append(("editor_windowed_1920", None))

            if self.brush_1000.isChecked():
                self._queue.append(("live_1000_brushes", 1000))
            if self.brush_10000.isChecked():
                self._queue.append(("live_10000_brushes", 10000))
            if self.brush_100000.isChecked():
                self._queue.append(("live_100000_brushes", 100000))

            seen = set()
            self._queue = [item for item in self._queue if not (item[0] in seen or seen.add(item[0]))]

            self._append("LIVE BENCHMARK: using the existing Fio MainWindow, QtGameView and renderer.")
            self._timer.start()
            self._stop_monitor()
            self._begin_next()
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _reset_between_tests(self):
        """Reset the live Fio instance to the original world before each test."""
        self._timer.stop()
        self._measurement_active = False
        self._restore_benchmark_window_mode()
        if self.main_window.view_3d.play_mode:
            self.main_window._exit_play_mode()
            QApplication.processEvents()
        if self._original_level_data is not None:
            self.main_window.state.load_from_data(copy.deepcopy(self._original_level_data))
            self.main_window.update_all_ui()
            self.main_window.update_views()
            self.main_window.view_3d.sysmon.reset_metrics()
            self.main_window.view_3d.update()
            QApplication.processEvents()
        if self._original_camera is not None:
            position, yaw, pitch, fov = self._original_camera
            camera = self.main_window.view_3d.camera
            import glm
            camera.pos = glm.vec3(*position)
            camera.yaw = yaw
            camera.pitch = pitch
            camera.fov = fov
        self.main_window.unsaved_changes = self._original_unsaved_changes
        self.main_window.view_3d.update()
        QApplication.processEvents()

    def _enter_benchmark_editor_window_mode(self, width, height):
        """Run the real editor UI in a normal decorated window and measure its 3D pane."""
        self._benchmark_window_mode = "editor_windowed"
        window = self.main_window
        window.showNormal()
        if self._original_window_flags is not None:
            window.setWindowFlags(self._original_window_flags)
        window.resize(int(width), int(height))
        window.showNormal()
        window.raise_()
        window.activateWindow()
        QApplication.processEvents()
        QApplication.processEvents()
        self._append(
            "Editor presentation: normal window %dx%d; measuring the live 3D view pane (%dx%d)."
            % (width, height, window.view_3d.width(), window.view_3d.height())
        )

    def _enter_benchmark_window_mode(self, mode):
        """Put the real MainWindow into the requested presentation mode.

        Borderless uses a frameless maximized window; fullscreen uses Qt's
        actual showFullScreen() state. The benchmark dialog remains visible
        above the live MainWindow and does not change the measured 3D viewport.
        """
        if mode not in ("borderless", "fullscreen"):
            return

        self._benchmark_window_mode = mode
        window = self.main_window

        if mode == "borderless":
            window.setWindowFlags(window.windowFlags() | Qt.FramelessWindowHint)
            window.showMaximized()
        else:
            window.showFullScreen()

        window.raise_()
        window.activateWindow()
        QApplication.processEvents()
        QApplication.processEvents()

        self._append(
            "Presentation mode: %s (%dx%d viewport)."
            % (
                "borderless maximized window"
                if mode == "borderless"
                else "true fullscreen",
                window.view_3d.width(),
                window.view_3d.height(),
            )
        )

    def _restore_benchmark_window_mode(self):
        """Restore the MainWindow presentation state captured at benchmark start."""
        if self._benchmark_window_mode is None and self._original_window_flags is None:
            return

        window = self.main_window
        window.showNormal()
        if self._original_window_flags is not None:
            window.setWindowFlags(self._original_window_flags)
        if self._original_window_geometry is not None:
            window.setGeometry(self._original_window_geometry)

        if self._original_window_fullscreen:
            window.showFullScreen()
        elif self._original_window_state is not None and self._original_window_state & Qt.WindowMaximized:
            window.showMaximized()
        else:
            window.showNormal()

        window.raise_()
        window.activateWindow()
        QApplication.processEvents()
        self._benchmark_window_mode = None
        if not self.isVisible():
            self.show()


    def _prepare_player_area_sweep(self):
        """Prepare a PlayerStart-centered outward spiral through playable space."""
        player_start, start_error = self._find_player_start()
        fallback = False
        fallback_reason = None
        playable = None
        flood_error = None

        if player_start is not None:
            playable, flood_error = self._flood_player_area(
                player_start[0], player_start[1]
            )

        if playable is None:
            fallback = True
            fallback_reason = start_error or flood_error or "playable flood failed"

            min_x, max_x, min_z, max_z = self._current_world_bounds()
            center_x = (min_x + max_x) * 0.5
            center_z = (min_z + max_z) * 0.5
            half_x = max(0.0, (max_x - min_x) * 0.42)
            half_z = max(0.0, (max_z - min_z) * 0.42)
            sweep_bounds = (min_x, max_x, min_z, max_z)
            cell_count = 0
            anchor_x = center_x
            anchor_z = center_z
            self._player_area_reachable_cells = None
            self._player_area_reachable_points = None
            self._player_area_cell_size = float(self.PLAYER_AREA_CELL_SIZE)
            radius_end = math.sqrt(half_x * half_x + half_z * half_z)
        else:
            min_x, max_x, min_z, max_z = playable["bounds"]
            cell_count = playable["cell_count"]
            anchor_x = float(player_start[0])
            anchor_z = float(player_start[1])
            sweep_bounds = (min_x, max_x, min_z, max_z)
            self._player_area_reachable_cells = playable["cells"]
            self._player_area_reachable_points = playable["points"]
            self._player_area_cell_size = float(playable["cell_size"])

            radius_end = 0.0
            for point_x, _point_y, point_z in playable["points"].values():
                radius_end = max(
                    radius_end,
                    math.hypot(
                        float(point_x) - anchor_x,
                        float(point_z) - anchor_z,
                    ),
                )

            radius_end = max(
                float(self.PLAYER_AREA_CELL_SIZE),
                radius_end * 0.90,
            )

        camera = self.main_window.view_3d.camera
        y = float(camera.pos.y)
        pitch = float(camera.pitch)

        turns = float(self.PLAYER_AREA_SPIRAL_TURNS) if not fallback else 1.0
        if fallback:
            # Preserve the previous bounds-based fallback path.
            effective_radius = math.sqrt(
                (half_x * half_x + half_z * half_z) * 0.5
            )
            travel_distance = 2.0 * math.pi * effective_radius
        else:
            # Approximate the Archimedean spiral path length while preserving
            # the existing distance / 250 speed model.
            radius_start = 0.0
            average_radius = (radius_start + radius_end) * 0.5
            travel_distance = math.hypot(
                radius_end - radius_start,
                2.0 * math.pi * turns * average_radius,
            )

        duration = max(6.0, min(12.0, travel_distance / 250.0))

        if fallback:
            self._player_area_camera_path = {
                "fallback": True,
                "center_x": anchor_x,
                "center_z": anchor_z,
                "half_x": half_x,
                "half_z": half_z,
                "y": y,
                "pitch": pitch,
                "duration": duration,
            }
        else:
            self._player_area_camera_path = {
                "fallback": False,
                "center_x": anchor_x,
                "center_z": anchor_z,
                "radius_end": radius_end,
                "turns": turns,
                "y": y,
                "pitch": pitch,
                "duration": duration,
            }

        self._player_area_sweep_metadata = {
            "mode": "player-start spiral" if not fallback else "bounds fallback",
            "anchor_source": "bounds fallback" if fallback else "PlayerStart",
            "fallback": fallback,
            "fallback_reason": fallback_reason,
            "bounds": tuple(float(value) for value in sweep_bounds),
            "reachable_cells": int(cell_count),
            "duration_s": float(duration),
            "travel_distance": float(travel_distance),
        }

        if fallback:
            self._append(
                "Camera sweep: BOUNDS FALLBACK for %.1f s — %s; "
                "using existing bounds-based sweep."
                % (duration, fallback_reason)
            )
        else:
            self._append(
                "Camera sweep: PLAYERSTART SPIRAL for %.1f s — PlayerStart at "
                "(%.1f, %.1f); flood reached %d cells; spiral is clamped to "
                "reachable collision space with a %.0f-unit safety margin."
                % (
                    duration,
                    anchor_x,
                    anchor_z,
                    int(cell_count),
                    float(self.PLAYER_AREA_CAMERA_MARGIN),
                )
            )

    def _clamp_player_area_camera(self, desired_x, desired_z):
        """Clamp a spiral position to a reachable, collision-safe camera point.

        The playable flood was generated from the real Player collision system.
        The flood point in each cell is therefore the safe anchor for that cell;
        the camera is kept biased toward that safe point, especially at the
        boundary of the reachable region.
        """
        path = getattr(self, "_player_area_camera_path", None)
        if path is None:
            return float(desired_x), float(desired_z)

        if path.get("fallback"):
            min_x, max_x, min_z, max_z = self._player_area_sweep_metadata["bounds"]
            return (
                min(max(float(desired_x), min_x), max_x),
                min(max(float(desired_z), min_z), max_z),
            )

        anchor_x = float(path["center_x"])
        anchor_z = float(path["center_z"])
        cell_size = float(
            getattr(self, "_player_area_cell_size", self.PLAYER_AREA_CELL_SIZE)
        )
        cells = getattr(self, "_player_area_reachable_cells", None)
        points = getattr(self, "_player_area_reachable_points", None)
        if not cells or not points:
            return anchor_x, anchor_z

        dx = float(desired_x) - anchor_x
        dz = float(desired_z) - anchor_z
        distance = math.hypot(dx, dz)
        key = (
            int(round(dx / cell_size)),
            int(round(dz / cell_size)),
        )

        selected_key = key
        if selected_key not in cells:
            # Walk back toward PlayerStart along the requested spiral ray until
            # it re-enters the reachable flood.
            steps = max(1, int(math.ceil(distance / cell_size)))
            selected_key = (0, 0)
            for step in range(1, steps + 1):
                scale = max(0.0, 1.0 - (float(step) / float(steps)))
                probe_x = anchor_x + dx * scale
                probe_z = anchor_z + dz * scale
                probe_key = (
                    int(round((probe_x - anchor_x) / cell_size)),
                    int(round((probe_z - anchor_z) / cell_size)),
                )
                if probe_key in cells:
                    selected_key = probe_key
                    break

        safe_point = points.get(selected_key)
        if safe_point is None:
            return anchor_x, anchor_z

        safe_x = float(safe_point[0])
        safe_z = float(safe_point[2])

        # Boundary cells are deliberately kept closer to their known-safe
        # flood point. This preserves a small wall/void margin rather than
        # running the camera all the way to the coarse cell edge.
        is_boundary = any(
            (selected_key[0] + dx, selected_key[1] + dz) not in cells
            for dx, dz in (
                (-1, -1), (0, -1), (1, -1),
                (-1, 0),            (1, 0),
                (-1, 1),  (0, 1),  (1, 1),
            )
        )
        blend = 0.40 if is_boundary else 0.70

        candidate_x = safe_x + (float(desired_x) - safe_x) * blend
        candidate_z = safe_z + (float(desired_z) - safe_z) * blend

        # A small explicit inward bias provides the requested safety margin
        # when the spiral is being clamped to the playable boundary.
        if is_boundary and distance > 0.0:
            margin = min(float(self.PLAYER_AREA_CAMERA_MARGIN), distance * 0.25)
            inward_x = -dx / distance
            inward_z = -dz / distance
            candidate_x += inward_x * margin
            candidate_z += inward_z * margin

        return candidate_x, candidate_z

    def _advance_player_area_sweep(self):
        path = getattr(self, "_player_area_camera_path", None)
        if path is None:
            return

        elapsed = time.perf_counter() - self._phase_started
        duration = float(path["duration"])
        progress = min(1.0, max(0.0, elapsed / max(duration, 0.001)))

        if path.get("fallback"):
            angle = progress * (2.0 * math.pi)
            x = path["center_x"] + path["half_x"] * math.sin(angle)
            z = path["center_z"] + path["half_z"] * math.cos(angle)
        else:
            angle = progress * (2.0 * math.pi * float(path["turns"]))
            radius = float(path["radius_end"]) * progress
            x = path["center_x"] + radius * math.sin(angle)
            z = path["center_z"] + radius * math.cos(angle)
            x, z = self._clamp_player_area_camera(x, z)

        if path.get("fallback"):
            bounds = self._player_area_sweep_metadata.get("bounds")
            if bounds is not None:
                min_x, max_x, min_z, max_z = bounds
                x = min(max(x, min_x), max_x)
                z = min(max(z, min_z), max_z)

        next_progress = min(1.0, progress + 0.01 / max(duration, 0.001))
        if path.get("fallback"):
            next_angle = next_progress * (2.0 * math.pi)
            next_x = path["center_x"] + path["half_x"] * math.sin(next_angle)
            next_z = path["center_z"] + path["half_z"] * math.cos(next_angle)
        else:
            next_angle = next_progress * (2.0 * math.pi * float(path["turns"]))
            next_radius = float(path["radius_end"]) * next_progress
            next_x = path["center_x"] + next_radius * math.sin(next_angle)
            next_z = path["center_z"] + next_radius * math.cos(next_angle)
            next_x, next_z = self._clamp_player_area_camera(next_x, next_z)

        yaw = math.degrees(math.atan2(next_z - z, next_x - x))

        camera = self.main_window.view_3d.camera
        import glm
        position = glm.vec3(float(x), path["y"], float(z))
        logic_thread = getattr(self.main_window.view_3d, "logic_thread", None)
        if logic_thread is not None and getattr(self.main_window.view_3d, "use_threading", False):
            logic_thread.set_editor_camera(position, yaw, path["pitch"], camera.fov)
        else:
            camera.pos = position
            camera.yaw = yaw
            camera.pitch = path["pitch"]

    def _begin_next(self):
        if not self._queue:
            self._restore_original()
            return

        self._reset_between_tests()

        label, value = self._queue.pop(0)
        self._current = (label, value)
        self._phase_started = time.perf_counter()
        self.status_label.setText("Preparing: %s" % label)
        self._append_test_separator(label)
        self._append("<span style='color:#ffb15a; font-weight:bold;'>START TEST</span> — %s" % label)
        self._append("Reset to baseline; loading isolated workload...")
        self._preparation_deadline = time.perf_counter() + self._preparation_timeout_s
        self.status_label.setText("Preparing: %s (30 s preparation limit)" % label)

        try:
            if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                if label == "current_world":
                    self.main_window.raise_()
                    self.main_window.activateWindow()
                    QApplication.processEvents()
                    QApplication.processEvents()
                elif label == "borderless_window":
                    self._enter_benchmark_window_mode("borderless")
                elif label == "fullscreen_window":
                    self._enter_benchmark_window_mode("fullscreen")
                elif label == "editor_windowed_1280":
                    self._enter_benchmark_editor_window_mode(1280, 720)
                elif label == "editor_windowed_1920":
                    self._enter_benchmark_editor_window_mode(1920, 1080)
                self._prepare_player_area_sweep()
                self._check_preparation_budget(label)
                self._start_measurement(label, duration=self._test_duration(label))
            else:
                # Every deliberately risky workload is process-isolated. The
                # parent Qt thread only starts the child process and polls it;
                # it never loads the pathological workload itself.
                self._start_worker_test(label, value)
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _check_preparation_budget(self, label):
        elapsed = time.perf_counter() - self._phase_started
        self._monitor_beat(label, deadline=self._preparation_deadline)
        monitor_failed, monitor_reason = self._monitor_failed()
        if monitor_failed:
            raise TimeoutError(monitor_reason)
        if time.perf_counter() > self._preparation_deadline:
            raise TimeoutError(
                "%s exceeded the %.0f s preparation limit after %.1f s. The workload was not measured; restoring the original world."
                % (label, self._preparation_timeout_s, elapsed)
            )
        self.status_label.setText("Preparing: %s — %.1f s elapsed (%.0f s limit)" % (label, elapsed, self._preparation_timeout_s))
        QApplication.processEvents()

    def _start_measurement(self, label, duration=1.0):
        # A measurement must be completely initialised before Qt is allowed to
        # re-enter the event loop.  _tick is timer-driven and processEvents()
        # below can dispatch it immediately.
        self._timer.stop()
        self._current = (label, duration)
        self._phase_started = time.perf_counter()
        self._measurement_deadline = time.perf_counter() + float(duration)
        self._measurement_watchdog_deadline = self._measurement_deadline + self._measurement_watchdog_extra_s
        self._measurement_active = True
        self._monitor_beat(label, deadline=self._measurement_watchdog_deadline)
        view = self.main_window.view_3d
        view.sysmon.reset_metrics()
        view.sysmon.begin_benchmark_capture()
        view.update()
        QApplication.processEvents()
        self._timer.start()

    def _tick(self):
        if not self._running:
            return
        if self._worker_active:
            try:
                self._poll_worker_test()
            except Exception:
                self._finish_with_error(traceback.format_exc())
            return
        if not self._measurement_active:
            return

        try:
            app = QApplication.instance()
            self._monitor_beat(self._current[0], deadline=self._measurement_watchdog_deadline)
            monitor_failed, monitor_reason = self._monitor_failed()
            if monitor_failed:
                raise TimeoutError(monitor_reason)
            if time.perf_counter() > self._measurement_watchdog_deadline:
                raise TimeoutError("%s exceeded its measurement watchdog; the test did not complete reliably." % self._current[0])
            view = self.main_window.view_3d
            if self._current and self._current[0] in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                self._advance_player_area_sweep()
            view.update()
            app.processEvents()

            if time.perf_counter() < self._measurement_deadline:
                return

            # Disarm the measurement before any reporting/teardown can pump
            # Qt events and re-enter _tick.
            self._measurement_active = False
            self._timer.stop()
            self.status_label.setText("Completed: %s — collecting results..." % self._current[0])
            capture = view.sysmon.end_benchmark_capture()
            metrics = self._benchmark_metrics(capture, time.perf_counter() - self._phase_started)
            live_metrics = view.sysmon.get_metrics()
            metrics.update({"viewport_width": int(view.width()), "viewport_height": int(view.height()), "vram_used_mb": live_metrics.get("vram_used_mb"), "vram_total_mb": live_metrics.get("vram_total_mb"), "visible_brushes": live_metrics.get("visible_brushes", 0), "culled_brushes": live_metrics.get("culled_brushes", 0), "total_brushes": live_metrics.get("total_brushes", 0), "visible_tris": live_metrics.get("visible_tris", 0), "culled_tris": live_metrics.get("culled_tris", 0), "visible_surfaces": live_metrics.get("visible_surfaces", 0), "culled_surfaces": live_metrics.get("culled_surfaces", 0)})
            label = self._current[0]
            if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                sweep = getattr(self, "_player_area_sweep_metadata", {})
                metrics.update({
                    "camera_sweep_mode": sweep.get("mode", "player-area"),
                    "camera_sweep_anchor": sweep.get("anchor_source", "unknown"),
                    "camera_sweep_fallback": bool(sweep.get("fallback", False)),
                    "camera_sweep_fallback_reason": sweep.get("fallback_reason"),
                    "camera_sweep_bounds": sweep.get("bounds"),
                    "camera_sweep_reachable_cells": sweep.get("reachable_cells"),
                })

            if label == "live_io_1000":
                self._run_live_io_stress()
            self._report_live_result(label, metrics)

            if label.startswith("procedural_") or label == "monster_apocalypse":
                # Monster tests already ran inside real Play Mode during the
                # measurement.  God mode kept the player alive while AI,
                # combat, infighting and monster I/O were active.
                pos = view.camera.pos
                self._append(
                    "  Play Mode: god_mode=True, AI active, infighting active, final camera=(%.1f, %.1f, %.1f)"
                    % (float(pos.x), float(pos.y), float(pos.z))
                )
                self._bench.finish_live_monster_test(self.main_window)

            self._begin_next()
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _run_live_io_stress(self):
        """Fire the generated relay chain through the live LogicThread I/O manager."""
        import sys as _sys
        view = self.main_window.view_3d
        if not view.play_mode:
            self.main_window.enter_play_mode()
            QApplication.processEvents()
        io_manager = getattr(view.logic_thread, "io_manager", None)
        if io_manager is None:
            raise RuntimeError("live Fio LogicThread has no IOManager")

        relays = [
            t for t in self.main_window.state.things
            if str(t.properties.get("name", "")).startswith("BenchmarkRelay_")
        ]
        if not relays:
            raise RuntimeError("live I/O benchmark found no generated LogicRelay entities")

        first = min(relays, key=lambda t: int(
            str(t.properties.get("name", "BenchmarkRelay_0")).rsplit("_", 1)[1]
        ))
        old_limit = _sys.getrecursionlimit()
        _sys.setrecursionlimit(max(old_limit, 10000))
        try:
            start = time.perf_counter()
            io_manager.reset()
            io_manager.fire_output(first, "OnTrigger")
            elapsed = time.perf_counter() - start
        finally:
            _sys.setrecursionlimit(old_limit)

        self._append(
            "  Live I/O: fired OnTrigger through %d LogicRelay entities in %.3f ms"
            % (len(relays), elapsed * 1000.0)
        )
        if view.play_mode:
            self.main_window._exit_play_mode()
            QApplication.processEvents()

    @staticmethod
    def _benchmark_metrics(capture, duration):
        """Calculate live benchmark metrics from the captured frame timings.

        Average FPS is derived from the same captured frame-time sample as
        Average frame time, matching the standalone renderer benchmarks.
        ``wall_clock_fps`` is retained separately for presentation throughput.
        """
        import numpy as np
        values = np.asarray(capture.get("frame_times", []), dtype=np.float64)
        visible = np.asarray(capture.get("visible_tris", []), dtype=np.float64)
        total = np.asarray(capture.get("total_tris", []), dtype=np.float64)
        culled = np.asarray(capture.get("culled_tris", []), dtype=np.float64)
        frame_count = int(values.size)
        duration = float(duration)
        if values.size == 0:
            return {
                "frame_count": frame_count,
                "measurement_duration_s": duration,
                "average_frame_time_ms": 0.0,
                "median_frame_time_ms": 0.0,
                "p95_frame_time_ms": 0.0,
                "p99_frame_time_ms": 0.0,
                "p999_frame_time_ms": 0.0,
                "min_frame_time_ms": 0.0,
                "max_frame_time_ms": 0.0,
                "average_fps": 0.0,
                "wall_clock_fps": 0.0,
                "average_visible_tris": 0.0,
                "average_total_tris": 0.0,
                "average_culled_tris": 0.0,
                "culling_efficiency": 0.0,
            }
        avg_ms = float(np.mean(values))
        avg_visible = float(np.mean(visible)) if visible.size else 0.0
        avg_total = float(np.mean(total)) if total.size else 0.0
        avg_culled = float(np.mean(culled)) if culled.size else 0.0
        efficiency = (avg_culled / avg_total * 100.0) if avg_total > 0.0 else 0.0
        return {
            "frame_count": frame_count,
            "measurement_duration_s": duration,
            "average_frame_time_ms": avg_ms,
            "median_frame_time_ms": float(np.percentile(values, 50)),
            "p95_frame_time_ms": float(np.percentile(values, 95)),
            "p99_frame_time_ms": float(np.percentile(values, 99)),
            "p999_frame_time_ms": float(np.percentile(values, 99.9)),
            "min_frame_time_ms": float(np.min(values)),
            "max_frame_time_ms": float(np.max(values)),
            "average_fps": (1000.0 / avg_ms) if avg_ms > 0.0 else 0.0,
            "wall_clock_fps": (frame_count / duration) if duration > 0.0 else 0.0,
            "average_visible_tris": avg_visible,
            "average_total_tris": avg_total,
            "average_culled_tris": avg_culled,
            "culling_efficiency": efficiency,
        }
    def _report_live_result(self, label, metrics):
        width = metrics.get("viewport_width", self.main_window.view_3d.width())
        height = metrics.get("viewport_height", self.main_window.view_3d.height())
        avg_fps = float(metrics.get("average_fps", 0.0))
        avg_ms = float(metrics.get("average_frame_time_ms", 0.0))
        p95_ms = float(metrics.get("p95_frame_time_ms", 0.0))
        p99_ms = float(metrics.get("p99_frame_time_ms", 0.0))
        p999_ms = float(metrics.get("p999_frame_time_ms", 0.0))
        frames = int(metrics.get("frame_count", 0))
        duration = float(metrics.get("measurement_duration_s", 0.0))
        low_1 = 1000.0 / p99_ms if p99_ms > 0.0 else 0.0
        low_01 = 1000.0 / p999_ms if p999_ms > 0.0 else 0.0
        result = dict(metrics)
        result.update({"test": label, "entities": len(self.main_window.state.things), "one_percent_low_fps": low_1, "zero_point_one_percent_low_fps": low_01})
        self._results.append(result)
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)
        self.output.append('<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
                           '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
                           '<div style="color:#aaa;">%dx%d &nbsp; • &nbsp; %.2f ms average frame &nbsp; • &nbsp; %.2f ms median &nbsp; • &nbsp; %.2f ms p95</div>'
                           '<div style="color:#aaa;">%d frames &nbsp; • &nbsp; %.2f s measured &nbsp; • &nbsp; wall-clock %.2f FPS &nbsp; • &nbsp; 1%% low %.2f FPS &nbsp; • &nbsp; 0.1%% low %.2f FPS</div>'
                           '<div style="color:#aaa;">VRAM %s &nbsp; • &nbsp; brushes %d visible / %d culled / %d total &nbsp; • &nbsp; entities %d</div>'
                           '</div>' % (label, width, height, avg_ms, float(metrics.get("median_frame_time_ms", 0.0)), p95_ms, frames, duration, float(metrics.get("wall_clock_fps", 0.0)), low_1, low_01, self._format_vram(metrics), metrics.get("visible_brushes", 0), metrics.get("culled_brushes", 0), metrics.get("total_brushes", 0), result["entities"]))
        if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            sweep_line = (
                '<div style="padding:4px 0; color:#aaa;">'
                '<b style="color:#eeeeee;">Camera sweep:</b> %s'
                ' &nbsp; • &nbsp; <b style="color:#eeeeee;">anchor:</b> %s%s'
                '</div>'
                % (
                    metrics.get("camera_sweep_mode", "player-area"),
                    metrics.get("camera_sweep_anchor", "unknown"),
                    " &nbsp; • &nbsp; <b style=\"color:#ff8a00;\">FALLBACK: %s</b>"
                    % str(metrics.get("camera_sweep_fallback_reason", "unknown"))
                    if metrics.get("camera_sweep_fallback") else "",
                )
            )
            self.output.append(sweep_line)
            self.output.append('<div style="padding:4px 0;">'
                               '<span style="color:#eeeeee; font-weight:bold;">Average visible triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average total triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average culled triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Culling efficiency: </span><span style="color:#ff9a32; font-weight:bold;">%.1f%%</span></div>'
                               % (metrics.get("average_visible_tris", 0.0), metrics.get("average_total_tris", 0.0), metrics.get("average_culled_tris", 0.0), metrics.get("culling_efficiency", 0.0)))
        self.output.append('<div style="margin-top:10px; padding:8px 0 2px 0; border-top:2px solid #555; white-space:nowrap;">'
                           '<span style="font-size:25px; font-weight:bold; color:#63d471;">average FPS:</span>'
                           '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f</span>'
                           '</div>' % avg_fps)
        self.output.append('<div style="color:#aaa; padding:2px 0 4px 0;">Average FPS = 1000 / mean(captured frame time). Wall-clock FPS is reported separately.</div>')
        self.output.append('<div style="border-top:2px solid #63d471; margin:14px 0 8px 0;"></div>')
        self.output.ensureCursorVisible()
        QApplication.processEvents()
    @staticmethod
    def _format_vram(metrics):
        used = metrics.get("vram_used_mb")
        total = metrics.get("vram_total_mb")
        if used is None and total is None:
            return "N/A"
        if used is None or total is None:
            return "%s / %s MB" % (used, total)
        return "%.1f / %.1f MB" % (used, total)

    def _restore_original(self, failed=False):
        if self._restoring:
            return
        self._restoring = True
        try:
            if self.main_window.view_3d.play_mode:
                self.main_window._exit_play_mode()
                QApplication.processEvents()

            if self._original_level_data is not None:
                self.main_window.state.load_from_data(
                    copy.deepcopy(self._original_level_data)
                )
                self.main_window.update_all_ui()
                self.main_window.update_views()
                self.main_window.view_3d.update()
                QApplication.processEvents()

            if self._original_camera is not None:
                position, yaw, pitch, fov = self._original_camera
                camera = self.main_window.view_3d.camera
                import glm
                camera.pos = glm.vec3(*position)
                camera.yaw = yaw
                camera.pitch = pitch
                camera.fov = fov
                self.main_window.view_3d.update()

            self.main_window.unsaved_changes = self._original_unsaved_changes
            self.main_window.update_title()
            self._restore_benchmark_window_mode()

            if self._original_play_mode:
                self.main_window.enter_play_mode()
                QApplication.processEvents()

            self._timer.stop()
            self._measurement_active = False
            self._worker_active = False
            self._terminate_worker_process()
            self._stop_monitor()
            self._cleanup_worker_result_path()
            self._running = False
            self.throbber.setVisible(False)
            self._set_controls_enabled(True)
            if failed:
                self.status_label.setText("Live benchmark failed; original Fio world restored.")
            else:
                self.status_label.setText("Live benchmark complete. Original Fio world restored.")
        except Exception:
            self._timer.stop()
            self._running = False
            self.throbber.setVisible(False)
            self._set_controls_enabled(True)
            self.status_label.setText("Live benchmark failed while restoring the original world.")
            self._append(traceback.format_exc())
        finally:
            self._restoring = False

    def _finish_with_error(self, details):
        self._timer.stop()
        self._measurement_active = False
        self._worker_active = False
        self._terminate_worker_process()
        self._stop_monitor()
        self._cleanup_worker_result_path()
        self._running = False
        self.throbber.setVisible(False)
        self._append(details)
        self._restore_original(failed=True)

    def reject(self):
        if self._running:
            self._finish_with_error("Benchmark cancelled; restoring original world...")
            self._restore_original(failed=True)
            return
        super().reject()