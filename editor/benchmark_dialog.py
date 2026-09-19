"""Tools > Benchmark dialog.

Runs Fio's standalone performance benchmark in a child process. The benchmark
uses Fio runtime APIs directly and deliberately does not depend on pytest.
"""

import os
import sys

from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QApplication
from PyQt5.QtCore import QTimer
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
        self._running = False

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        self.environment_label = QLabel(_execution_environment())
        self.environment_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.environment_label)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.additional_tests = QCheckBox(
            "Additional stress tests (I/O, renderer, gameplay)"
        )
        self.additional_tests.setToolTip(
            "Run deliberately heavy workloads against the live Fio renderer, procedural world and I/O system."
        )
        layout.addWidget(self.additional_tests)

        self.brush_1000 = QCheckBox("Renderer scene: 1,000 brushes")
        self.brush_10000 = QCheckBox("Renderer scene: 10,000 brushes")
        self.brush_100000 = QCheckBox("Renderer scene: 100,000 brushes")
        self.io_chain_1000 = QCheckBox("I/O chain: 1,000 entities")
        self.monsters_100 = QCheckBox("Procedural room: 100 monsters")
        self.monsters_500 = QCheckBox("Procedural room: 500 monsters")
        self.monsters_1000 = QCheckBox("Procedural room: 1,000 monsters")
        self.monster_apocalypse = QCheckBox("FINAL TEST: maximum procedural monster apocalypse (1000 monsters + 1000 relays)")
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
            self.monster_apocalypse,
        ):
            checkbox.setToolTip(
                "Run this deliberately large workload in addition to the standard stress tests."
            )
            layout.addWidget(checkbox)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, monospace; }"
        )
        layout.addWidget(self.output)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

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
        ):
            checkbox.setEnabled(enabled)

    def _append(self, text):
        self.output.appendPlainText(text)
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _start(self):
        if self._running:
            return

        self.output.clear()
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

            if self.brush_1000.isChecked():
                self._queue.append(("live_1000_brushes", 1000))
            if self.brush_10000.isChecked():
                self._queue.append(("live_10000_brushes", 10000))
            if self.brush_100000.isChecked():
                self._queue.append(("live_100000_brushes", 100000))

            self._append("LIVE BENCHMARK: using the existing Fio MainWindow, QtGameView and renderer.")
            self._append("The editor/3D view remains running behind this dialog.")
            self._timer.start()
            self._begin_next()
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _begin_next(self):
        if not self._queue:
            self._restore_original()
            return

        label, value = self._queue.pop(0)
        self._current = (label, value)
        self._phase_started = time.perf_counter()
        self.status_label.setText("Running: %s" % label)

        try:
            if label == "current_world":
                self._start_measurement(label)
            elif label.startswith("procedural_"):
                data = self._bench._generate_procedural_map(
                    monsters=int(value), relay_count=32, seed=1337 + int(value)
                )
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._start_measurement(label)
            elif label == "live_io_1000":
                data = self._bench._generate_procedural_map(
                    monsters=0, relay_count=1000, seed=0x10
                )
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._start_measurement(label, duration=0.5)
            elif label == "monster_apocalypse":
                data = self._bench._generate_monster_apocalypse()
                self._bench.load_live_benchmark_world(self.main_window, data)
                self._start_measurement(label, duration=2.0)
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
                self._start_measurement(label, duration=0.5)
            else:
                raise RuntimeError("unknown live benchmark: %s" % label)
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _start_measurement(self, label, duration=1.0):
        self._current = (label, duration)
        self._phase_started = time.perf_counter()
        self.main_window.view_3d.sysmon.reset_metrics()
        self.main_window.view_3d.update()
        QApplication.processEvents()
        self._measurement_deadline = time.perf_counter() + float(duration)
        self._timer.start()

    def _tick(self):
        if not self._running:
            return

        try:
            app = QApplication.instance()
            view = self.main_window.view_3d
            view.update()
            app.processEvents()

            if time.perf_counter() < self._measurement_deadline:
                return

            metrics = view.sysmon.get_metrics()
            label = self._current[0]

            if label == "live_io_1000":
                self._run_live_io_stress()
            self._report_live_result(label, metrics)

            if label == "monster_apocalypse":
                # Exercise the real game lifecycle on the exact world currently
                # visible in the editor. This is not a synthetic renderer call.
                self.main_window.enter_play_mode()
                if not view.play_mode:
                    raise RuntimeError("Fio failed to enter Play Mode")
                from PyQt5.QtCore import Qt
                self.main_window.keys_pressed.add(Qt.Key_W)
                self.main_window.keys_pressed.add(Qt.Key_D)
                play_deadline = time.perf_counter() + 2.0
                while time.perf_counter() < play_deadline:
                    view.update()
                    app.processEvents()
                    time.sleep(0.001)
                self.main_window.keys_pressed.discard(Qt.Key_W)
                self.main_window.keys_pressed.discard(Qt.Key_D)
                pos = view.camera.pos
                self._append(
                    "  Play Mode: %.3f s, final camera=(%.1f, %.1f, %.1f)"
                    % (2.0, float(pos.x), float(pos.y), float(pos.z))
                )
                self.main_window._exit_play_mode()
                app.processEvents()

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

    def _report_live_result(self, label, metrics):
        width = metrics.get("viewport_width", self.main_window.view_3d.width())
        height = metrics.get("viewport_height", self.main_window.view_3d.height())
        self._append(
            "%-30s %4dx%-4d  FPS %7.2f  frame %7.2f ms  p95 %7.2f ms  VRAM %s"
            % (
                label,
                width,
                height,
                metrics.get("fps", 0.0),
                metrics.get("average_frame_time_ms", 0.0),
                metrics.get("p95_frame_time_ms", 0.0),
                self._format_vram(metrics),
            )
        )
        self._append(
            "  brushes: visible=%d culled=%d total=%d | entities=%d"
            % (
                metrics.get("visible_brushes", 0),
                metrics.get("culled_brushes", 0),
                metrics.get("total_brushes", 0),
                len(self.main_window.state.things),
            )
        )

    @staticmethod
    def _format_vram(metrics):
        used = metrics.get("vram_used_mb")
        total = metrics.get("vram_total_mb")
        if used is None and total is None:
            return "N/A"
        if used is None or total is None:
            return "%s / %s MB" % (used, total)
        return "%.1f / %.1f MB" % (used, total)

    def _restore_original(self):
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

            self._timer.stop()
            self._running = False
            self.status_label.setText("Live benchmark complete. Original Fio world restored.")
            self._set_controls_enabled(True)
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _finish_with_error(self, details):
        self._timer.stop()
        self._running = False
        self.status_label.setText("Live benchmark failed.")
        self._set_controls_enabled(True)
        self._append(details)

    def reject(self):
        if self._running:
            self._finish_with_error("Benchmark cancelled; restoring original world...")
            self._restore_original()
            return
        super().reject()
