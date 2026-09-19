"""Live benchmark sequencing, lifecycle, timeout supervision and restoration.

All tests continue to run against the already-running Fio MainWindow.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QToolButton

from .benchmark_results import BenchmarkResults
from .benchmark_tests import BenchmarkTests


class BenchmarkRunner:
    """Own benchmark sequencing/lifecycle while the dialog stays UI-only."""

    def __init__(self, dialog):
        self.dialog = dialog
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
        self._measurement_watchdog_extra_s = 60.0
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
        self._worker_stdout_path = None
        self._worker_stderr_path = None
        self._worker_stdout_handle = None
        self._worker_stderr_handle = None
        self._worker_label = None
        self._worker_value = None
        self._worker_deadline = 0.0
        self._worker_active = False
        self._worker_finished = False
        self._worker_exit_code = None
        self._live_stress_active = False
        self._live_stress_phase = ""
        self._live_stress_label = None
        self._live_stress_value = None
        self._live_stress_deadline = 0.0
        self._live_stress_timeout = False
        self._live_stress_timeout_reason = ""
        self._live_io_elapsed = None
        self._requested_duration = None
        self._requested_repetitions = 1
        self._current_phase_results = []
        self.tests = BenchmarkTests(self)
        self.results = BenchmarkResults(self)

    def __getattr__(self, name):
        dialog = self.__dict__.get("dialog")
        if dialog is not None:
            try:
                return getattr(dialog, name)
            except AttributeError:
                pass
        for component_name in ("tests", "results"):
            component = self.__dict__.get(component_name)
            if component is not None:
                try:
                    return object.__getattribute__(component, name)
                except AttributeError:
                    pass
        raise AttributeError(name)

    def _live_stress_timeout_for(self, label):
        """Return the same-process safety timeout for a live stress workload."""
        # Live I/O can legitimately take around 44 seconds on low-power
        # hardware.  Keep the safety ceiling at 60 seconds for every live
        # stress workload rather than aborting a valid benchmark early.
        return 60.0
    

    def _start_live_stress_monitor(self, label):
        """Arm the live workload timeout on the existing Qt tick."""
        timeout_s = self._live_stress_timeout_for(label)
        self._live_stress_timeout = False
        self._live_stress_timeout_reason = ""
        self._live_stress_deadline = time.perf_counter() + timeout_s


    def _stop_live_stress_monitor(self):
        """Disarm the live workload timeout."""
        self._live_stress_deadline = 0.0
    

    def _on_live_stress_timeout(self, reason):
        if not self._live_stress_active:
            return
        self._live_stress_timeout = True
        self._live_stress_timeout_reason = str(reason)
        self._append(
            "<span style='color:#ff8a00; font-weight:bold;'>"
            "LIVE TEST TIMEOUT</span> — %s" % self._html_escape(reason)
        )
    

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
        try:            os.unlink(result_path)
        except OSError:
            pass
        self._worker_result_path = result_path
    
        stdout_fd, stdout_path = tempfile.mkstemp(
            prefix="fio_benchmark_worker_",
            suffix=".stdout.log",
        )
        stderr_fd, stderr_path = tempfile.mkstemp(
            prefix="fio_benchmark_worker_",
            suffix=".stderr.log",
        )
        os.close(stdout_fd)
        os.close(stderr_fd)
        self._worker_stdout_path = stdout_path
        self._worker_stderr_path = stderr_path
    
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
        try:
            self._worker_stdout_handle = open(
                self._worker_stdout_path, "w", encoding="utf-8", buffering=1
            )
            self._worker_stderr_handle = open(
                self._worker_stderr_path, "w", encoding="utf-8", buffering=1
            )
        except Exception:
            for path in (self._worker_stdout_path, self._worker_stderr_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            self._worker_stdout_path = None
            self._worker_stderr_path = None
            raise
    
        popen_kwargs = {
            "cwd": self.root_dir,
            "env": env,
            "stdout": self._worker_stdout_handle,
            "stderr": self._worker_stderr_handle,
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
    

    def _read_worker_diagnostics(self):
        """Return captured worker stdout/stderr without hiding native crashes."""
        chunks = []
        for label, path in (
            ("stdout", self._worker_stdout_path),
            ("stderr", self._worker_stderr_path),
        ):
            if not path:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    data = handle.read().strip()
            except OSError:
                data = ""
            if data:
                # Keep the report bounded if a native component floods stderr.
                if len(data) > 12000:
                    data = data[-12000:]
                    data = "[...truncated...]\n" + data
                chunks.append("%s:\n%s" % (label, data))
        return "\n\n".join(chunks)
    

    def _cleanup_worker_result_path(self):
        path = self._worker_result_path
        self._worker_result_path = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    
        for attr in ("_worker_stdout_handle", "_worker_stderr_handle"):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                setattr(self, attr, None)
    
        for attr in ("_worker_stdout_path", "_worker_stderr_path"):
            log_path = getattr(self, attr, None)
            setattr(self, attr, None)
            if log_path:
                try:
                    os.unlink(log_path)
                except OSError:
                    pass
    

    def _abort_worker_test(self, reason, timed_out=False):
        """Abort one isolated test, record it, then continue the queue."""
        label = self._worker_label or (self._current[0] if self._current else "unknown")
        diagnostics = self._read_worker_diagnostics()
        if diagnostics:
            reason = "%s\n\nWorker diagnostics:\n%s" % (reason, diagnostics)
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
    
        diagnostics = self._read_worker_diagnostics()
        self._cleanup_worker_result_path()
        self._worker_process = None
    
        if not payload or not payload.get("ok"):
            error = (payload or {}).get("error")
            if not error:
                error = "worker exited with code %s without producing a valid result" % exit_code
            if diagnostics:
                error = "%s\n\nWorker diagnostics:\n%s" % (error, diagnostics)
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
    
    

    def _begin_next(self):
        if not self._queue:
            self._restore_original()
            return
    
        self._reset_between_tests()
    
        label, value = self._queue.pop(0)
        self._current = (label, value)
        self._current_repetition = value if label in ("current_world_phase1", "current_world_phase2") else None
        self._phase_started = time.perf_counter()
        self.status_label.setText("Preparing: %s" % label)
        self._append_test_separator(label)
        self._append("<span style='color:#ffb15a; font-weight:bold;'>START TEST</span> — %s" % label)
        self._append("Reset to baseline; loading live workload...")
        self._preparation_deadline = time.perf_counter() + self._preparation_timeout_s
        self.status_label.setText("Preparing: %s (30 s preparation limit)" % label)
    
        try:
            if label in ("current_world", "current_world_phase1", "current_world_phase2", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                if label in ("current_world", "current_world_phase1", "current_world_phase2"):
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
                # Stress workloads are live too: load them into the existing
                # MainWindow and measure the real Qt/OpenGL viewport.
                self._run_live_stress_test(label, value)
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
        repetition = getattr(self, "_current_repetition", None)
        self._current = (label, float(duration), repetition)
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
    
    
    def _restore_original(self):
        """Restore the real MainWindow to the state captured before benchmarking."""
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = False
        self._stop_live_stress_monitor()
        self._restore_benchmark_window_mode()
    
        try:
            if self.main_window.view_3d.play_mode and not self._original_play_mode:
                self.main_window._exit_play_mode()
                QApplication.processEvents()
        except Exception:
            pass
    
        try:
            if self._original_level_data is not None:
                self.main_window.state.load_from_data(copy.deepcopy(self._original_level_data))
                self.main_window.update_all_ui()
                self.main_window.update_views()
        except Exception:
            pass
    
        try:
            if self._original_camera is not None:
                position, yaw, pitch, fov = self._original_camera
                camera = self.main_window.view_3d.camera
                import glm
                camera.pos = glm.vec3(*position)
                camera.yaw = yaw
                camera.pitch = pitch
                camera.fov = fov
        except Exception:
            pass
    
        self.main_window.unsaved_changes = self._original_unsaved_changes
        self.main_window.view_3d.update()
        QApplication.processEvents()
    
        self._running = False
        self.throbber.setVisible(False)
        self._set_controls_enabled(True)
        self.status_label.setText("Benchmark complete.")
        self.export_button.setEnabled(bool(self._results))
        self.export_button.setVisible(bool(self._results))
        QApplication.processEvents()
    

    def _finish_with_error(self, error_text):
        """Fail the benchmark dialog without killing the live Fio process."""
        self._timer.stop()
        self._measurement_active = False
        self._worker_active = False
        self._live_stress_active = False
        self._stop_live_stress_monitor()
        self._stop_monitor()
    
        try:
            self._terminate_worker_process()
        except Exception:
            pass
    
        try:
            self.main_window.view_3d.sysmon.end_benchmark_capture()
        except Exception:
            pass
    
        label = self._current[0] if self._current else "benchmark"
        self._results.append({
            "test": label,
            "status": "error",
            "aborted": True,
            "abort_reason": str(error_text),
            "benchmark_live": True,
        })
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)
        self.output.append(
            '<div style="background:#2a1010; border:1px solid #ff5555; padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#ff7777; font-weight:bold;">Benchmark error</div>'
            '<pre style="white-space:pre-wrap; color:#ddd; margin-top:8px;">%s</pre>'
            '</div>' % self._html_escape(error_text)
        )
        self.status_label.setText(
            "Benchmark failed — restoring the original Fio world."
        )
        QApplication.processEvents()
    
        self._restore_original()
        self.status_label.setText("Benchmark failed; original world restored.")
        QApplication.processEvents()
    

    def _abort_live_stress(self, reason):
        """Abort a live test without terminating the Fio process."""
        label = self._live_stress_label or (
            self._current[0] if self._current else "unknown"
        )
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = False
        self._stop_live_stress_monitor()
    
        try:
            self.main_window.view_3d.sysmon.end_benchmark_capture()
        except Exception:
            pass
    
        try:
            if self.main_window.view_3d.play_mode:
                if label.startswith(("procedural_", "monster_")):
                    self._bench.finish_live_monster_test(self.main_window)
                else:
                    self.main_window._exit_play_mode()
                QApplication.processEvents()
        except Exception:
            pass
    
        self._results.append({
            "test": label,
            "status": "aborted",
            "aborted": True,
            "abort_reason": str(reason),
            "benchmark_live": True,
        })
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)
        self.output.append(
            '<div style="background:#2a1c10; border:1px solid #ff8a00; '
            'padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#ffb15a;">%s</div>'
            '<div style="font-size:25px; font-weight:bold; color:#ff8a00; margin-top:6px;">'
            'ABORTED — timeout</div>'
            '<div style="color:#ddd; margin-top:4px;">%s</div>'
            '</div>' % (self._html_escape(label), self._html_escape(reason))
        )
        self.status_label.setText(
            "Timed out: %s — Fio was not terminated; restoring the original world."
            % label
        )
        QApplication.processEvents()
        self._begin_next()
    

    def _finish_live_stress_result(self, label, metrics):
        """Finish a live stress measurement and cleanly leave Play Mode."""
        view = self.main_window.view_3d
        self._measurement_active = False
        self._timer.stop()
        self._stop_live_stress_monitor()
        metrics = dict(metrics)
        metrics["benchmark_live"] = True
    
        if label == "live_io_1000":
            hops = len([
                thing for thing in self.main_window.state.things
                if str(thing.properties.get("name", "")).startswith("BenchmarkRelay_")
            ])
            elapsed = float(self._live_io_elapsed or 0.0)
            metrics.update({
                "io_elapsed_ms": elapsed * 1000.0,
                "io_hops": hops,
                "hops_per_second": hops / elapsed if elapsed > 0.0 else 0.0,
            })
            self._append(                "  Live I/O throughput: %.0f hops/s."
                % metrics["hops_per_second"]
            )
    
        if label.startswith(("procedural_", "monster_")):
            pos = view.camera.pos
            self._append(
                "  Play Mode: god_mode=True, AI active, infighting active, "
                "final camera=(%.1f, %.1f, %.1f)"
                % (float(pos.x), float(pos.y), float(pos.z))
            )
            self._bench.finish_live_monster_test(self.main_window)
        elif label == "live_io_1000" and view.play_mode:
            self.main_window._exit_play_mode()
            QApplication.processEvents()
    
        if label == "live_io_1000":
            # I/O is a throughput benchmark, not a rendering benchmark.
            # Report hops, elapsed time and throughput instead of an FPS card.
            elapsed_ms = float(metrics.get("io_elapsed_ms", 0.0))
            hops = int(metrics.get("io_hops", 0))
            hops_per_second = float(metrics.get("hops_per_second", 0.0))
            metrics["test"] = label
            metrics["description"] = "Live LogicRelay I/O"
            self._results.append(metrics)
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)
            self.output.append(
                '<div style="padding:12px 0 22px 38px;">'
                '<div style="font-size:27px; font-weight:bold; color:#2db34a; '
                'padding-bottom:13px; border-bottom:3px solid #2db34a; '
                'width:347px; white-space:nowrap;">'
                '%d hops took <span style="color:#ff7f20;">%.3f ms</span>'
                '</div>'
                '<table cellspacing="0" cellpadding="0" '
                'style="margin-top:0;">'
                '<tr><td width="347" height="84" bgcolor="#2db34a" '
                'style="padding:0 0 0 58px; white-space:nowrap;">'
                '<span style="font-size:56px; line-height:1; '
                'font-weight:bold; color:#ff7f20;">%.0f</span>'
                '<span style="font-size:28px; font-weight:bold; '
                'color:#2db34a; margin-left:13px;">hops/second</span>'
                '</td></tr></table>'
                '</div>'
                % (hops, elapsed_ms, hops_per_second)
            )
            self.output.ensureCursorVisible()
            QApplication.processEvents()
            self._begin_next()
            return
    
        self._report_live_result(label, metrics)
        self._begin_next()

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
        self._queue = []
        self.export_button.setEnabled(False)
        self.export_button.setVisible(False)
        self.status_label.setText("Preparing live Fio benchmark...")
        self._set_controls_enabled(False)
    
        stress_toggle = self.findChild(QToolButton)
        if stress_toggle is not None:
            stress_toggle.setChecked(False)
    
        self._running = True
        self.throbber.setVisible(True)
        self._stop_monitor()
    
        try:
            # benchmark_dialog.py lives in editor/, while fio_benchmark.py lives
            # under the repository root. Put this Fio checkout first so a
            # globally installed package named "tests" cannot shadow Fio's
            # own tests.performance package.
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            else:
                sys.path.remove(repo_root)
                sys.path.insert(0, repo_root)
    
            import importlib
            import inspect
            expected_tests_dir = os.path.normcase(
                os.path.abspath(os.path.join(repo_root, "tests"))
            )
            expected_benchmark = os.path.normcase(
                os.path.abspath(
                    os.path.join(repo_root, "tests", "performance", "fio_benchmark.py")
                )
            )
    
            # Fio must load its own tests package. A different package named
            # "tests" may already be cached in this long-lived Python process;
            # changing sys.path alone cannot replace that module object.
            for module_name in list(sys.modules):
                if module_name == "tests" or module_name.startswith("tests."):
                    del sys.modules[module_name]
    
            importlib.invalidate_caches()
            from tests.performance import fio_benchmark as bench
    
            # Always reload the exact module from this checkout so an older
            # in-memory copy cannot survive a source update.
            bench = importlib.reload(bench)
    
            actual_benchmark = os.path.normcase(
                os.path.abspath(getattr(bench, "__file__", ""))
            )
            signature = inspect.signature(bench.load_live_benchmark_world)
    
            if actual_benchmark != expected_benchmark:
                raise ImportError(
                    "Fio benchmark module was shadowed: expected %s, imported %s"
                    % (expected_benchmark, actual_benchmark)
                )
    
            if "yield_hook" not in signature.parameters:
                raise ImportError(
                    "Fio benchmark module has the wrong loader signature: %s (%s)"
                    % (actual_benchmark, signature)
                )
    
            self._bench = bench
    
            # Snapshot the real running editor. Every benchmark is restored to
            # this state between tests; no second Fio instance is created.
            self._original_level_data = copy.deepcopy(
                self.main_window.state.get_level_data()
            )
            self._original_play_mode = bool(self.main_window.view_3d.play_mode)
            self._original_unsaved_changes = bool(
                getattr(self.main_window, "unsaved_changes", False)
            )
            self._original_window_flags = self.main_window.windowFlags()
            self._original_window_geometry = self.main_window.geometry()
            self._original_window_state = self.main_window.windowState()
            self._original_window_fullscreen = self.main_window.isFullScreen()
    
            camera = self.main_window.view_3d.camera
            self._original_camera = (
                (float(camera.pos.x), float(camera.pos.y), float(camera.pos.z)),
                float(camera.yaw),
                float(camera.pitch),
                float(camera.fov),
            )
    
            repetitions = max(1, int(self._requested_repetitions or 1))
            if has_current_map:
                for repetition in range(1, repetitions + 1):
                    self._queue.append(("current_world_phase1", repetition))
                    self._queue.append(("current_world_phase2", repetition))
    
            # "Additional stress tests" is the bundle selector. Individual
            # checkboxes can also be selected independently.
            if self.additional_tests.isChecked():
                self._queue.extend((
                    ("live_io_1000", 1000),
                    ("live_1000_brushes", 1000),
                    ("live_10000_brushes", 10000),
                    ("procedural_100_monsters", 100),
                    ("procedural_500_monsters", 500),
                    ("procedural_1000_monsters", 1000),
                    ("monster_apocalypse", 1000),
                ))
    
            if self.io_chain_1000.isChecked():
                self._queue.append(("live_io_1000", 1000))
            if self.brush_1000.isChecked():
                self._queue.append(("live_1000_brushes", 1000))
            if self.brush_10000.isChecked():
                self._queue.append(("live_10000_brushes", 10000))
            if self.brush_100000.isChecked():
                self._queue.append(("live_100000_brushes", 100000))
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
    
            seen = set()
            self._queue = [
                item for item in self._queue
                if not (item[0] in seen or seen.add(item[0]))
            ]
    
            self._append(
                "LIVE BENCHMARK: using the existing Fio MainWindow, QtGameView "
                "and renderer. Stress timeout supervision never terminates Fio."
            )
            self._timer.start()
            self._begin_next()
    
        except Exception:
            self._finish_with_error(traceback.format_exc())
    

    def _tick(self):
        if not self._running:
            return
        if self._worker_active:
            try:
                self._poll_worker_test()
            except Exception:
                self._finish_with_error(traceback.format_exc())
            return

        # Live stress tests use the existing Qt benchmark tick for timeout
        # supervision.  No additional QThread is needed for a simple wall-clock
        # deadline, avoiding native thread lifetime/signal races.
        if (
            self._live_stress_active
            and self._live_stress_deadline > 0.0
            and time.perf_counter() >= self._live_stress_deadline
            and not self._live_stress_timeout
        ):
            label = self._live_stress_label or (
                self._current[0] if self._current else "live benchmark"
            )
            timeout_s = self._live_stress_timeout_for(label)
            self._live_stress_timeout = True
            self._live_stress_timeout_reason = (
                "%s exceeded its %.1f s live benchmark timeout. "
                "The benchmark was stopped without terminating Fio."
                % (label, timeout_s)
            )

        if not self._measurement_active:
            return
    
        try:
            app = QApplication.instance()
            if self._live_stress_active and self._live_stress_timeout:
                self._abort_live_stress(
                    self._live_stress_timeout_reason or
                    "live benchmark exceeded its timeout"
                )
                return
            self._monitor_beat(self._current[0], deadline=self._measurement_watchdog_deadline)
            monitor_failed, monitor_reason = self._monitor_failed()
            if monitor_failed:
                raise TimeoutError(monitor_reason)
            if time.perf_counter() > self._measurement_watchdog_deadline:
                raise TimeoutError("%s exceeded its measurement watchdog; the test did not complete reliably." % self._current[0])
            view = self.main_window.view_3d
            if self._current and self._current[0] in ("current_world", "current_world_phase1", "current_world_phase2", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                if self._current[0] == "current_world_phase2":
                    self._advance_player_area_rotation()
                else:
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
            if label in ("current_world", "current_world_phase1", "current_world_phase2", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
                if label in ("current_world_phase1", "current_world_phase2"):
                    phase_number = 1 if label.endswith("phase1") else 2
                    metrics["benchmark_phase"] = phase_number
                    metrics["benchmark_repetition"] = int(self._current[2] or 1)
                    metrics["benchmark_phase_label"] = (
                        "Phase 1 — PlayerStart orbit"
                        if phase_number == 1 else
                        "Phase 2 — PlayerStart 360° rotation"
                    )
                sweep = getattr(self, "_player_area_sweep_metadata", {})
                metrics.update({
                    "camera_sweep_mode": sweep.get("mode", "player-area"),
                    "camera_sweep_anchor": sweep.get("anchor_source", "unknown"),
                    "camera_sweep_fallback": bool(sweep.get("fallback", False)),
                    "camera_sweep_fallback_reason": sweep.get("fallback_reason"),
                    "camera_sweep_bounds": sweep.get("bounds"),
                    "camera_sweep_reachable_cells": sweep.get("reachable_cells"),
                    "camera_sweep_collision_disabled": bool(sweep.get("collision_disabled", True)),
                })
    
            if self._live_stress_active:
                self._finish_live_stress_result(label, metrics)
                return
            if label in ("current_world_phase1", "current_world_phase2"):
                self._current_phase_results.append(metrics)
                self._report_live_result(label, metrics)
                self._report_current_world_combined_if_complete()
            else:
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
    
