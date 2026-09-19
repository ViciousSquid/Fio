"""Tools > Benchmark dialog.

Runs the benchmark inside the already-running Fio MainWindow. The benchmark
uses the live QtGameView, renderer, world state, LogicThread and SysMon; it
does not launch a second Fio process and does not depend on pytest.
"""

import os
import sys
import json
import platform
import subprocess
import math
from datetime import datetime, timezone

from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QApplication, QFileDialog, QTextBrowser, QToolButton, QWidget
from PyQt5.QtCore import QTimer, Qt
import copy
import time
import traceback


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
        self._original_window_flags = None
        self._original_window_geometry = None
        self._original_window_state = None
        self._original_window_fullscreen = False
        self._benchmark_window_mode = None

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        self.environment_label = QLabel(_execution_environment())
        self.environment_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.environment_label)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        intro_label = QLabel(
            "<b>Run Benchmark</b> analyses the currently loaded project"
        )
        layout.addWidget(intro_label)

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
        stress_toggle.toggled.connect(self.stress_options.setVisible)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(self._select_all_stress_tests)
        self.select_all_button.setVisible(False)
        stress_toggle.toggled.connect(self.select_all_button.setVisible)
        layout.addWidget(self.select_all_button)
        layout.addWidget(self.stress_options)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setStyleSheet(
            "QTextBrowser { font-family: Consolas, monospace; background: #171717; border: 1px solid #444; }"
        )
        layout.addWidget(self.output)

        self.export_button = QPushButton("Export Results…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_results)
        layout.addWidget(self.export_button)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)

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
        ):
            checkbox.setEnabled(enabled)

    def _append(self, text):
        self.output.append(text)
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _append_test_separator(self, label):
        self.output.append('<div style="border-top:2px solid #ff8a00; margin:14px 0 8px 0; padding-top:8px;"><span style="color:#ffb15a; font-weight:bold;">TEST: %s</span></div>' % label)

    def _test_duration(self, label):
        # The current-world test is map-scale dependent. A fixed 3-second
        # window is too short to sample culling across a meaningful portion of
        # a large map, while tiny maps do not need a long measurement.
        if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            return self._current_world_sweep_duration()
        return {"procedural_100_monsters": 4.0, "procedural_500_monsters": 4.0, "procedural_1000_monsters": 5.0, "live_io_1000": 2.0, "live_1000_brushes": 3.0, "live_10000_brushes": 3.0, "live_100000_brushes": 2.0, "monster_apocalypse": 4.0}.get(label, 3.0)

    def _current_world_bounds(self):
        """Return the X/Z bounds of the loaded map from actual brush positions."""
        brushes = getattr(self.main_window.state, "brushes", [])
        points = []
        for brush in brushes:
            pos = getattr(brush, "pos", None)
            if pos is None and isinstance(brush, dict):
                pos = brush.get("pos")
            if pos is not None and len(pos) >= 3:
                points.append((float(pos[0]), float(pos[2])))

        if not points:
            camera = self.main_window.view_3d.camera
            x = float(camera.pos.x)
            z = float(camera.pos.z)
            return x - 128.0, x + 128.0, z - 128.0, z + 128.0

        xs = [p[0] for p in points]
        zs = [p[1] for p in points]
        return min(xs), max(xs), min(zs), max(zs)

    def _current_world_sweep_duration(self):
        """Choose a deterministic measurement duration from map scale.

        Approximate traversal speed is 250 world units/sec along the map
        diagonal, with a 5-second floor and 20-second ceiling. This gives
        roughly 15 seconds for a ~3,750-unit diagonal map and avoids making
        the camera artificially fast just to fit a fixed benchmark window.
        """
        min_x, max_x, min_z, max_z = self._current_world_bounds()
        diagonal = math.hypot(max_x - min_x, max_z - min_z)
        return max(5.0, min(20.0, diagonal / 250.0))

    def _git_commit(self):
        try:
            return subprocess.check_output(["git", "-C", self.root_dir, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
        except Exception:
            return "unknown"

    def _export_results(self):
        if not self._results:
            return
        default_name = "fio_benchmark_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "Export Fio Benchmark Results", default_name, "JSON files (*.json);;All files (*)")
        if not path:
            return
        payload = {"format": "fio-benchmark-v2", "timestamp_utc": datetime.now(timezone.utc).isoformat(), "environment": _execution_environment(), "platform": platform.platform(), "python": platform.python_version(), "cpu": platform.processor(), "commit": self._git_commit(), "viewport": {"width": self.main_window.view_3d.width(), "height": self.main_window.view_3d.height()}, "results": self._results}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self.status_label.setText("Exported benchmark results: %s" % os.path.basename(path))
        except Exception:
            self._append("<span style='color:#ff6666;'>Export failed.</span><pre>%s</pre>" % traceback.format_exc())
    def _start(self):
        if self._running:
            return

        self.output.clear()
        self._results = []
        self.export_button.setEnabled(False)
        self.status_label.setText("Preparing live Fio benchmark...")
        self._set_controls_enabled(False)
        self._running = True

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
            self._append("NaN")
            self._timer.start()
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
        self.hide()
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
        actual showFullScreen() state. The benchmark dialog is hidden so it
        cannot affect the presentation being measured.
        """
        if mode not in ("borderless", "fullscreen"):
            return

        self._benchmark_window_mode = mode
        self.hide()
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

    def _prepare_current_world_sweep(self):
        """Set up a deterministic camera path through the loaded map."""
        brushes = getattr(self.main_window.state, "brushes", [])
        points = []
        for brush in brushes:
            pos = getattr(brush, "pos", None)
            if pos is None and isinstance(brush, dict):
                pos = brush.get("pos")
            if pos is not None and len(pos) >= 3:
                points.append((float(pos[0]), float(pos[2])))

        camera = self.main_window.view_3d.camera
        if points:
            min_x = min(p[0] for p in points)
            max_x = max(p[0] for p in points)
            min_z = min(p[1] for p in points)
            max_z = max(p[1] for p in points)
            center_x = (min_x + max_x) * 0.5
            center_z = (min_z + max_z) * 0.5
            half_x = max((max_x - min_x) * 0.42, 32.0)
            half_z = max((max_z - min_z) * 0.42, 32.0)
        else:
            center_x = float(camera.pos.x)
            center_z = float(camera.pos.z)
            half_x = half_z = 128.0

        duration = self._current_world_sweep_duration()
        self._current_world_camera_path = (
            center_x, center_z, half_x, half_z,
            float(camera.pos.y), float(camera.pitch), duration
        )
        self._append(
            "Camera sweep: traversing the loaded map for %.1f s "
            "(map-scale dependent, 5–20 s)." % duration
        )

    def _advance_current_world_sweep(self):
        path = getattr(self, "_current_world_camera_path", None)
        if path is None:
            return
        center_x, center_z, half_x, half_z, y, pitch, duration = path
        elapsed = time.perf_counter() - self._phase_started
        angle = min(1.0, elapsed / max(duration, 0.001)) * (2.0 * math.pi)
        x = center_x + half_x * math.sin(angle)
        z = center_z + half_z * math.sin(angle + math.pi * 0.5)
        next_x = center_x + half_x * math.sin(angle + 0.01)
        next_z = center_z + half_z * math.sin(angle + 0.01 + math.pi * 0.5)
        yaw = math.degrees(math.atan2(next_z - z, next_x - x))

        camera = self.main_window.view_3d.camera
        import glm
        camera.pos = glm.vec3(x, y, z)
        camera.yaw = yaw
        camera.pitch = pitch

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

        try:
            if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                if label == "current_world":
                    self.hide()
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
                self._prepare_current_world_sweep()
                self._start_measurement(label, duration=self._test_duration(label))
            elif label.startswith("procedural_"):
                data = self._bench._generate_procedural_map(
                    monsters=int(value), relay_count=32, seed=self._bench.BENCHMARK_MAP_SEED
                )
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._bench.prepare_live_monster_test(self.main_window)
                self._start_measurement(label, duration=self._test_duration(label))
            elif label == "live_io_1000":
                data = self._bench._generate_procedural_map(
                    monsters=0, relay_count=1000, seed=self._bench.BENCHMARK_MAP_SEED
                )
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._start_measurement(label, duration=self._test_duration(label))
            elif label == "monster_apocalypse":
                data = self._bench._generate_monster_apocalypse()
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._bench.prepare_live_monster_test(self.main_window, aggro_fraction=0.10)
                self._start_measurement(label, duration=self._test_duration(label))
            elif label.startswith("live_") and label.endswith("_brushes"):
                count = int(label.split("_")[1])
                data = self._bench._generate_procedural_map(monsters=0, relay_count=32)
                # Duplicate actual generated Fio brush records to the requested
                # size, then load them through the real EditorState.
                source = list(data["brushes"])
                brushes = list(source)
                index = 0
                while len(brushes) < count:
                    original = dict(source[index % len(source)])
                    original["pos"] = list(original.get("pos", [0, 0, 0]))
                    original["pos"][0] += (index // len(source) + 1) * 5000.0
                    original["id"] = "live_benchmark_%d" % len(brushes)
                    brushes.append(original)
                    index += 1
                data["brushes"] = brushes
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._start_measurement(label, duration=self._test_duration(label))
            else:
                raise RuntimeError("unknown live benchmark: %s" % label)
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _start_measurement(self, label, duration=1.0):
        # A measurement must be completely initialised before Qt is allowed to
        # re-enter the event loop.  _tick is timer-driven and processEvents()
        # below can dispatch it immediately.
        self._timer.stop()
        self._current = (label, duration)
        self._phase_started = time.perf_counter()
        self._measurement_deadline = time.perf_counter() + float(duration)
        self._measurement_active = True
        view = self.main_window.view_3d
        view.sysmon.reset_metrics()
        view.sysmon.begin_benchmark_capture()
        view.update()
        QApplication.processEvents()
        self._timer.start()

    def _tick(self):
        if not self._running or not self._measurement_active:
            return

        try:
            app = QApplication.instance()
            view = self.main_window.view_3d
            if self._current and self._current[0] in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                self._advance_current_world_sweep()
            view.update()
            app.processEvents()

            if time.perf_counter() < self._measurement_deadline:
                return

            # Disarm the measurement before any reporting/teardown can pump
            # Qt events and re-enter _tick.
            self._measurement_active = False
            self._timer.stop()
            capture = view.sysmon.end_benchmark_capture()
            metrics = self._benchmark_metrics(capture, time.perf_counter() - self._phase_started)
            live_metrics = view.sysmon.get_metrics()
            metrics.update({"viewport_width": int(view.width()), "viewport_height": int(view.height()), "vram_used_mb": live_metrics.get("vram_used_mb"), "vram_total_mb": live_metrics.get("vram_total_mb"), "visible_brushes": live_metrics.get("visible_brushes", 0), "culled_brushes": live_metrics.get("culled_brushes", 0), "total_brushes": live_metrics.get("total_brushes", 0), "visible_tris": live_metrics.get("visible_tris", 0), "culled_tris": live_metrics.get("culled_tris", 0), "visible_surfaces": live_metrics.get("visible_surfaces", 0), "culled_surfaces": live_metrics.get("culled_surfaces", 0)})
            label = self._current[0]

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
        import numpy as np
        values = np.asarray(capture.get("frame_times", []), dtype=np.float64)
        visible = np.asarray(capture.get("visible_tris", []), dtype=np.float64)
        total = np.asarray(capture.get("total_tris", []), dtype=np.float64)
        culled = np.asarray(capture.get("culled_tris", []), dtype=np.float64)
        if values.size == 0:
            return {"frame_count": 0, "measurement_duration_s": float(duration), "average_frame_time_ms": 0.0, "median_frame_time_ms": 0.0, "p95_frame_time_ms": 0.0, "p99_frame_time_ms": 0.0, "p999_frame_time_ms": 0.0, "min_frame_time_ms": 0.0, "max_frame_time_ms": 0.0, "average_fps": 0.0, "average_visible_tris": 0.0, "average_total_tris": 0.0, "average_culled_tris": 0.0, "culling_efficiency": 0.0}
        avg_ms = float(np.mean(values))
        avg_visible = float(np.mean(visible)) if visible.size else 0.0
        avg_total = float(np.mean(total)) if total.size else 0.0
        avg_culled = float(np.mean(culled)) if culled.size else 0.0
        efficiency = (avg_culled / avg_total * 100.0) if avg_total > 0.0 else 0.0
        return {"frame_count": int(values.size), "measurement_duration_s": float(duration), "average_frame_time_ms": avg_ms, "median_frame_time_ms": float(np.percentile(values, 50)), "p95_frame_time_ms": float(np.percentile(values, 95)), "p99_frame_time_ms": float(np.percentile(values, 99)), "p999_frame_time_ms": float(np.percentile(values, 99.9)), "min_frame_time_ms": float(np.min(values)), "max_frame_time_ms": float(np.max(values)), "average_fps": float(values.size / duration) if duration > 0 else 0.0, "average_visible_tris": avg_visible, "average_total_tris": avg_total, "average_culled_tris": avg_culled, "culling_efficiency": efficiency}

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
        self.output.append('<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;"><div style="white-space:nowrap; margin-bottom:4px;"><span style="font-size:25px; font-weight:bold; color:#eeeeee;">average FPS:</span><span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f</span><span style="font-size:30px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:7px;">FPS</span></div><div style="font-size:15px; font-weight:bold; color:#eeeeee;">%s</div><div style="color:#aaa;">%dx%d &nbsp; • &nbsp; %.2f ms average frame &nbsp; • &nbsp; %.2f ms median &nbsp; • &nbsp; %.2f ms p95</div><div style="color:#aaa;">%d frames &nbsp; • &nbsp; %.2f s measured &nbsp; • &nbsp; 1%% low %.2f FPS &nbsp; • &nbsp; 0.1%% low %.2f FPS</div><div style="color:#aaa;">VRAM %s &nbsp; • &nbsp; brushes %d visible / %d culled / %d total &nbsp; • &nbsp; entities %d</div></div>' % (avg_fps, label, width, height, avg_ms, float(metrics.get("median_frame_time_ms", 0.0)), p95_ms, frames, duration, low_1, low_01, self._format_vram(metrics), metrics.get("visible_brushes", 0), metrics.get("culled_brushes", 0), metrics.get("total_brushes", 0), result["entities"]))
        if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            self.output.append('<div style="color:#ffb15a; font-weight:bold; padding:4px 0;">Average visible triangles: %.0f &nbsp; • &nbsp; Average total triangles: %.0f &nbsp; • &nbsp; Average culled triangles: %.0f &nbsp; • &nbsp; Culling efficiency: %.1f%%</div>' % (metrics.get("average_visible_tris", 0.0), metrics.get("average_total_tris", 0.0), metrics.get("average_culled_tris", 0.0), metrics.get("culling_efficiency", 0.0)))
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
            self._running = False
            self._set_controls_enabled(True)
            if failed:
                self.status_label.setText("Live benchmark failed; original Fio world restored.")
            else:
                self.status_label.setText("Live benchmark complete. Original Fio world restored.")
        except Exception:
            self._timer.stop()
            self._running = False
            self._set_controls_enabled(True)
            self.status_label.setText("Live benchmark failed while restoring the original world.")
            self._append(traceback.format_exc())
        finally:
            self._restoring = False

    def _finish_with_error(self, details):
        self._timer.stop()
        self._measurement_active = False
        self._running = False
        self._append(details)
        self._restore_original(failed=True)

    def reject(self):
        if self._running:
            self._finish_with_error("Benchmark cancelled; restoring original world...")
            self._restore_original(failed=True)
            return
        super().reject()