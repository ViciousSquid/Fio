"""Live benchmark support for Fio.

Imported only when Tools > Benchmark is first used. No benchmark objects,
timers or IPC are created during normal editor startup.
"""

from __future__ import annotations

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
        self._original_notarget = False
        self._original_unsaved_changes = False
        self._original_camera = None
        self._running = False
        self._restoring = False
        self._results = []
        self._measurement_active = False
        self._measurement_deadline = 0.0
        self._measurement_watchdog_deadline = 0.0
        self._preparation_deadline = 0.0
        # The external benchmark manager is the hard timeout boundary.
        # These large in-process values are only defensive guards for a
        # responsive Fio process; they must not decide whether Fio is killed.
        self._preparation_timeout_s = 300.0
        self._measurement_watchdog_extra_s = 3600.0
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
        self._live_watchdog_thread = None
        self._live_watchdog_stop = None
        self._live_io_elapsed = None
        self._sysmon_samples = []
        self._last_sysmon_sample = 0.0
        self._requested_duration = None
        self._requested_repetitions = 1
        self._current_phase_results = []
        self._monster_chaos_overlay = None
        self._monster_chaos_aggro_injected = False
        self._monster_chaos_fighters = []
        self._monster_chaos_aggro_count = 0
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

    def _append_test_end_separator(self):
        """Append the full-width orange separator between benchmark tests."""
        self.output.append(
            '<div style="width:100%; border-top:2px solid #ff9a32; margin:14px 0 8px 0;"></div>'
        )
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _append_test_separator(self, label):
        """Append the Fio-styled separator shown at the start of each test."""
        self.output.append(
            '<div style="border-top:2px solid #ff9a32; margin:14px 0 8px 0; '
            'padding-top:8px;">'
            '<span style="color:#63d471; font-size:15px; font-weight:bold;">TEST:</span> '
            '<span style="color:#ff9a32; font-weight:bold;">%s</span>'
            '</div>'
            % self._html_escape(label)
        )
        self.output.ensureCursorVisible()
        QApplication.processEvents()

    def _live_stress_timeout_for(self, label):
        """Return the manager-side timeout used for descriptive messages.

        Fio itself does not terminate or fail the process on this deadline.
        The external benchmark manager owns the hard supervision boundary.
        """
        return 60.0

    def _start_live_stress_monitor(self, label):
        """Compatibility no-op: process supervision is external now."""
        self._live_stress_timeout = False
        self._live_stress_timeout_reason = ""
        self._live_stress_deadline = 0.0

    def _stop_live_stress_monitor(self):
        """Compatibility no-op: the external manager supervises Fio."""
        self._live_stress_deadline = 0.0

    def _live_watchdog_beat(self):
        """Compatibility no-op; the manager observes the real process."""
        return

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
        """Legacy worker supervision hook; no longer used for live tests."""
        return

    def _monitor_beat(self, phase=None, deadline=None):
        """Compatibility no-op; timeout supervision belongs to the manager."""
        return

    def _monitor_failed(self):
        return False, ""

    def _stop_monitor(self):
        return

    def _worker_timeout_for(self, label):
        """Return the hard wall-clock timeout for an isolated stress test."""
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
        self._append_test_end_separator()
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
            self.monster_chaos_witness,
            self.borderless_window,
            self.fullscreen_window,
            self.editor_windowed_1280,
            self.editor_windowed_1920,
        ))
    

    def _reset_between_tests(self):
        """Restore the real Fio instance between tests."""
        self._remove_monster_chaos_overlay()
        self._monster_chaos_aggro_injected = False
        self._monster_chaos_fighters = []
        self._monster_chaos_aggro_count = 0
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = False
        self._restore_benchmark_window_mode()
        if self.main_window.view_3d.play_mode:
            self.main_window._exit_play_mode()
            QApplication.processEvents()
        logic = getattr(self.main_window.view_3d, "logic_thread", None)
        if logic is not None:
            logic.notarget = self._original_notarget
        if self._original_level_data is not None:
            self.main_window.state.load_from_data(copy.deepcopy(self._original_level_data))
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
        logic = getattr(self.main_window.view_3d, "logic_thread", None)
        if logic is not None:
            logic.notarget = self._original_notarget
        self.main_window.unsaved_changes = self._original_unsaved_changes
        self.main_window.view_3d.update()
        QApplication.processEvents()

    BENCHMARK_SCENE_SETTLE_SECONDS = 1.0

    def _settle_live_scene(self, seconds=None):
        """Pump Qt while allowing the live renderer/LogicThread to settle."""
        duration = (
            self.BENCHMARK_SCENE_SETTLE_SECONDS
            if seconds is None
            else float(seconds)
        )
        deadline = time.perf_counter() + max(0.0, duration)
        app = QApplication.instance()
        while time.perf_counter() < deadline:
            if app is not None:
                app.processEvents()
            time.sleep(0.01)

    def _clear_live_benchmark_scene(self, label="", yield_hook=None):
        """Clear authored scene data and derived live runtime state before a stress test.

        The live stress path remains the real Fio editor path: clearing a
        scene also rebuilds the scene hierarchy and all orthographic views so
        the next workload starts from the same editor state a user would see.
        """
        window = self.main_window
        view = window.view_3d

        if view.play_mode:
            window._exit_play_mode()
            QApplication.processEvents()

        # Clear the live terrain object as well as EditorState.  clear_scene()
        # only clears terrain_data; leaving MainWindow.terrain alive would let
        # the previous map remain visible/renderable during the next test.
        clear_terrain = getattr(window, "_clear_terrain", None)
        if callable(clear_terrain):
            clear_terrain()

        window.state.clear_scene()

        # Drop benchmark-visible caches that are keyed to the previous scene.
        view._io_conn_cache = None
        view._io_conn_scene_ver = None
        view._instance_tex_hash = None
        if getattr(view, "renderer", None) is not None:
            try:
                view.renderer.set_instance_textures({})
            except Exception:
                pass

        logic = getattr(view, "logic_thread", None)
        if logic is not None:
            try:
                logic.notify_visibility_changed()
            except Exception:
                pass
            try:
                logic._model_collision_brushes = []
                logic._refresh_collision_brushes_cache()
            except Exception:
                pass

        # Clear the currently displayed render snapshot.  The LogicThread's
        # write buffer is left alone and will publish a fresh snapshot from the
        # now-empty EditorState on its next tick.
        game_state = getattr(view, "game_state", None)
        if game_state is not None:
            lock = getattr(game_state, "_render_state_lock", None)
            if lock is not None:
                with lock:
                    try:
                        game_state._read_state.reset()
                        game_state._has_new_frame = False
                    except Exception:
                        pass

        # Rebuild the real editor UI and all orthographic views. The brush
        # stress benchmark deliberately exercises the same editor machinery
        # used by normal Fio editing, not a reduced 3D-only path.
        if yield_hook is not None:
            yield_hook()
        window.update_all_ui()
        if yield_hook is not None:
            yield_hook()
        window.update_views()
        if yield_hook is not None:
            yield_hook()
        view.update()
        QApplication.processEvents()
        if yield_hook is not None:
            yield_hook()

        self._append(
            "  Cleared the live scene and renderer/runtime scene caches"
            + (" before %s." % label if label else ".")
        )
        self._settle_live_scene()

    def _prepare_editor_window_benchmark_map(self):
        """Load the shared medium scene used by the editor-window benchmarks."""
        self._prepare_editor_windowed_map()


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
        self.status_label.setText(
            "Preparing: %s (external manager supervises this process)" % label
        )
    
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
                    self._prepare_editor_window_benchmark_map()
                elif label == "editor_windowed_1920":
                    self._enter_benchmark_editor_window_mode(1920, 1080)
                    self._prepare_editor_window_benchmark_map()
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
        """Start a SysMon-backed measurement on the real viewport."""
        self._timer.stop()
        repetition = getattr(self, "_current_repetition", None)
        self._current = (label, float(duration), repetition)
        self._phase_started = time.perf_counter()
        self._measurement_deadline = self._phase_started + float(duration)
        self._measurement_watchdog_deadline = (
            self._measurement_deadline + self._measurement_watchdog_extra_s
        )
        self._measurement_active = True
        self._sysmon_samples = []
        self._last_sysmon_sample = 0.0
        view = self.main_window.view_3d
        view.update()
        QApplication.processEvents()
        self._timer.start()

    def _restore_original(self):
        """Restore the real MainWindow to the state captured before benchmarking."""
        self._remove_monster_chaos_overlay()
        self._monster_chaos_aggro_injected = False
        self._monster_chaos_fighters = []
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
        """Fail the live benchmark and restore the original Fio world."""
        self._remove_monster_chaos_overlay()
        self._monster_chaos_aggro_injected = False
        self._monster_chaos_fighters = []
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
    

    def _skip_live_stress(self, label, reason):
        """Skip a live workload before it can make the existing Fio instance unresponsive."""
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = False
        self._stop_live_stress_monitor()
        self._results.append({
            "test": label,
            "status": "skipped",
            "skipped": True,
            "skip_reason": str(reason),
            "benchmark_live": True,
        })
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)
        self.output.append(
            '<div style="background:#211b10; border:1px solid #d9a441; '
            'padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#ffd27a;">%s</div>'
            '<div style="font-size:25px; font-weight:bold; color:#d9a441; margin-top:6px;">'
            'SKIPPED — safety limit</div>'
            '<div style="color:#ddd; margin-top:4px;">%s</div>'
            '</div>' % (self._html_escape(label), self._html_escape(reason))
        )
        self.status_label.setText("Skipped: %s — Fio remains responsive." % label)
        QApplication.processEvents()
        self._begin_next()

    def _abort_live_stress(self, reason):
        """Abort a live test without terminating the Fio process."""
        self._remove_monster_chaos_overlay()
        self._monster_chaos_aggro_injected = False
        self._monster_chaos_fighters = []
        label = self._live_stress_label or (
            self._current[0] if self._current else "unknown"
        )
        self._timer.stop()
        self._measurement_active = False
        self._live_stress_active = False
        self._stop_live_stress_monitor()
    
        try:
            if label == "monster_chaos_witness":
                logic = getattr(self.main_window.view_3d, "logic_thread", None)
                if logic is not None:
                    logic.notarget = self._original_notarget
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
        self._live_stress_active = False
        self._timer.stop()
        self._stop_live_stress_monitor()
        metrics = dict(metrics)
        metrics["benchmark_live"] = True

        if label == "monster_chaos_witness":
            self._remove_monster_chaos_overlay()
            chaos_info = dict(getattr(self, "_monster_chaos_info", {}) or {})
            monsters = [
                thing for thing in self.main_window.state.things
                if str(thing.properties.get("type", "")).lower() == "monster"
            ]
            alive = sum(
                1 for monster in monsters
                if not monster.properties.get("dead", False)
            )
            aggro_count = int(getattr(self, "_monster_chaos_aggro_count", 0))
            metrics.update({
                "test": label,
                "status": "passed",
                "description": (
                    "15-second live monster chaos witness: seed 43, "
                    "50 mixed human/flying monsters in two hostile teams starting on opposite sides and converging on a central PathNode, "
                    "followed by seeded random infighting."
                ),
                "seed": "43",
                "monster_count": len(monsters),
                "human_count": int(chaos_info.get("human_count", 0)),
                "flying_count": int(chaos_info.get("flying_count", 0)),
                "team_counts": dict(chaos_info.get("team_counts", {})),
                "pathnode_name": chaos_info.get(
                    "pathnode_name", "ChaosPathNode"
                ),
                "aggro_count": aggro_count,
                "aggro_delay_s": float(
                    getattr(self, "_monster_chaos_aggro_delay", 2.0)
                ),
                "witness_duration_s": 15.0,
                "alive_monsters": alive,
                "dead_monsters": max(0, len(monsters) - alive),
            })
            logic = getattr(view, "logic_thread", None)
            if logic is not None:
                logic.notarget = self._original_notarget
            if view.play_mode:
                self._bench.finish_live_monster_test(self.main_window)
            self._results.append(metrics)
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)
            self._append(
                '<div style="background:#222; border:1px solid #555; padding:12px; '
                'margin:4px 0 10px 0;">'
                '<div style="font-size:15px; font-weight:bold; color:#eeeeee;">'
                'Monster chaos witness</div>'
                '<div style="color:#aaa; margin-top:4px;">'
                'Seed 43 &nbsp; • &nbsp; 50 monsters &nbsp; • &nbsp; 30 human / 20 flying &nbsp; • &nbsp; PathNode %s'
                '</div>'
                '<table cellspacing="0" cellpadding="0" style="margin-top:10px;">'
                '<tr><td width="24" rowspan="2" bgcolor="#63d471"></td>'
                '<td height="2" bgcolor="#63d471" style="font-size:2px;"></td></tr>'
                '<tr><td style="padding:6px 16px 2px 12px;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">'
                'Infighting:</span>'
                '<span style="font-size:36px; font-weight:bold; color:#ff9a32; '
                'margin-left:10px;">%d fighters</span>'
                '</td></tr></table>'
                '<div style="color:#aaa; padding:4px 0;">'
                '%d alive &nbsp; • &nbsp; %d dead &nbsp; • &nbsp; '
                '15.0 second witness'
                '</div></div>'
                % (
                    self._html_escape(
                        chaos_info.get("pathnode_name", "ChaosPathNode")
                    ),
                    aggro_count,
                    alive,
                    max(0, len(monsters) - alive),
                )
            )
            QApplication.processEvents()
            self._monster_chaos_aggro_injected = False
            self._monster_chaos_fighters = []
            self._append_test_end_separator()
            self._begin_next()
            return

        if label == "live_io_1000":
            hops = len([
                thing for thing in self.main_window.state.things
                if str(thing.properties.get("name", "")).startswith("BenchmarkRelay_")
            ])
            elapsed = float(self._live_io_elapsed or 0.0)
            metrics.update({
                "io_elapsed_s": elapsed,
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
            elapsed_s = float(metrics.get("io_elapsed_s", 0.0))
            elapsed_ms = float(metrics.get("io_elapsed_ms", 0.0))
            hops = int(metrics.get("io_hops", 0))
            hops_per_second = float(metrics.get("hops_per_second", 0.0))
            metrics["test"] = label
            metrics["description"] = "Live LogicRelay I/O"
            self._results.append(metrics)
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)
            self.output.append(
                '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
                '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
                '<div style="color:#aaa;">%s &nbsp; • &nbsp; %s</div>'
                '<table cellspacing="0" cellpadding="0" style="margin-top:10px; margin-bottom:2px;">'
                '<tr><td width="24" rowspan="2" bgcolor="#63d471"></td>'
                '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td></tr>'
                '<tr><td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">Seconds:</span>'
                '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.3f</span>'
                '</td></tr></table>'
                '<table cellspacing="0" cellpadding="0" style="width:100%%; margin-top:4px; margin-bottom:2px;">'
                '<tr><td width="24" rowspan="2" bgcolor="#ff9a32"></td>'
                '<td height="2" bgcolor="#ff9a32" style="font-size:2px; line-height:2px;"></td>'
                '<td width="24" rowspan="2" bgcolor="#ff9a32"></td></tr>'
                '<tr><td style="padding:6px 16px 2px 12px; white-space:nowrap; text-align:right;">'
                '<span style="font-size:25px; font-weight:bold; color:#ff9a32;">Hops per second:</span>'
                '<span style="font-size:42px; line-height:1; font-weight:bold; color:#63d471; margin-left:12px;">%.0f</span>'
                '</td></tr></table>'
                '<div style="color:#aaa; padding:4px 0; text-align:right;">%d hops</div>'
                '</div>'
                % (
                    self._html_escape(label),
                    self._html_escape(metrics.get("description", label)),
                    "live logic throughput",
                    elapsed_s,
                    hops_per_second,
                    hops,
                )
            )
            self.output.ensureCursorVisible()
            QApplication.processEvents()
            self._append_test_end_separator()
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
                'No current project loaded and no tests selected'
                '</div>'
            )
            self.status_label.setText("No current project loaded and no tests selected")
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
            # Keep the benchmark module loaded only on explicit Tools > Benchmark use.
            # The repository root is first so a
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
            logic = getattr(self.main_window.view_3d, "logic_thread", None)
            self._original_notarget = bool(
                getattr(logic, "notarget", False)
            )
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
                    ("monster_chaos_witness", 50),
                ))
    
            if self.io_chain_1000.isChecked():
                self._queue.append(("live_io_1000", 1000))
            if self.brush_1000.isChecked():
                self._queue.append(("live_1000_brushes", 1000))
            if self.brush_10000.isChecked():
                self._queue.append(("live_10000_brushes", 10000))
            if self.brush_100000.isChecked():
                self._queue.append(("live_100000_brushes", 100000))
            if self.monster_chaos_witness.isChecked():
                self._queue.append(("monster_chaos_witness", 50))
    
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
    

    def _show_monster_chaos_overlay(self, seconds):
        """Display the temporary bottom-right witness countdown."""
        from PyQt5.QtWidgets import QLabel

        view = self.main_window.view_3d
        self._remove_monster_chaos_overlay()

        overlay = QLabel(view)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.setAlignment(Qt.AlignCenter)
        overlay.setStyleSheet(
            "QLabel {"
            "background: rgba(23,23,23,225);"
            "border: 2px solid #63d471;"
            "border-radius: 4px;"
            "color: #ff9a32;"
            "padding: 5px 10px;"
            "}"
        )
        overlay.setFixedSize(154, 92)
        overlay.show()
        overlay.raise_()
        self._monster_chaos_overlay = overlay
        self._update_monster_chaos_overlay(seconds)

    def _update_monster_chaos_overlay(self, seconds):
        overlay = self._monster_chaos_overlay
        view = self.main_window.view_3d
        if overlay is None:
            return

        remaining = max(0.0, float(seconds))
        title = (
            "INFIGHTING"
            if self._monster_chaos_aggro_injected
            else "MONSTER CHAOS"
        )
        overlay.setText(
            "<div style='font-size:12px; font-weight:bold; color:#63d471;'>%s</div>"
            "<div style='font-size:40px; line-height:42px; font-weight:bold; color:#ff9a32;'>%.1f</div>"
            "<div style='font-size:11px; color:#aaaaaa;'>seconds</div>"
            % (title, remaining)
        )
        overlay.move(
            max(0, view.width() - overlay.width() - 16),
            max(0, view.height() - overlay.height() - 16),
        )
        overlay.raise_()

    def _remove_monster_chaos_overlay(self):
        overlay = self._monster_chaos_overlay
        self._monster_chaos_overlay = None
        if overlay is None:
            return
        try:
            overlay.hide()
            overlay.deleteLater()
        except Exception:
            pass

    def _inject_monster_chaos_aggro(self):
        """Give a random subset direct monster targets once the mob has converged."""
        import random

        monsters = [
            thing for thing in self.main_window.state.things
            if str(thing.properties.get("type", "")).lower() == "monster"
            and not thing.properties.get("dead", False)
        ]
        if len(monsters) < 4:
            return 0

        rng = random.Random("43")
        fighter_count = min(8, max(4, len(monsters) // 3))
        fighters = rng.sample(monsters, fighter_count)
        rng.shuffle(fighters)

        # Release every monster from the PathNode target override. The
        # production AI can then use team-based enemy targeting for the full
        # mob, while the seeded subset gets direct deterministic aggro.
        for monster in monsters:
            monster.properties.pop("target_name", None)
            monster.properties["awake"] = True

        for source in fighters:
            source.properties["_aggro_target"] = None

        for index, source in enumerate(fighters):
            target = fighters[(index + 1) % len(fighters)]
            source.properties["_aggro_target"] = id(target)

        self._monster_chaos_fighters = fighters
        self._monster_chaos_aggro_count = len(fighters)
        self._monster_chaos_aggro_injected = True
        self._append(
            "  INFIGHTING! Injected %d seeded random monster-vs-monster "
            "aggro targets."
            % len(fighters)
        )
        return len(fighters)

    def _maintain_monster_chaos_aggro(self):
        """Retarget a surviving chaos fighter when its previous opponent dies."""
        import random

        fighters = [
            thing for thing in self._monster_chaos_fighters
            if thing in self.main_window.state.things
            and not thing.properties.get("dead", False)
        ]
        self._monster_chaos_fighters = fighters
        if len(fighters) < 2:
            return

        rng = random.Random("43-retarget")
        for source in fighters:
            target = None
            aggro_id = source.properties.get("_aggro_target")
            if aggro_id is not None:
                for candidate in fighters:
                    if id(candidate) == aggro_id:
                        target = candidate
                        break
            if target is None or target is source:
                candidates = [
                    candidate for candidate in fighters if candidate is not source
                ]
                if candidates:
                    target = rng.choice(candidates)
                    source.properties["_aggro_target"] = id(target)

    def _tick_monster_chaos_witness(self, now, app, view):
        elapsed = max(0.0, now - self._phase_started)

        if (
            not self._monster_chaos_aggro_injected
            and elapsed >= getattr(self, "_monster_chaos_aggro_delay", 2.0)
        ):
            self._inject_monster_chaos_aggro()

        if self._monster_chaos_aggro_injected:
            self._maintain_monster_chaos_aggro()

        remaining = max(0.0, self._measurement_deadline - now)
        self._update_monster_chaos_overlay(remaining)

        if now - self._last_sysmon_sample >= 1.0:
            self._last_sysmon_sample = now
            self._sysmon_samples.append(self._read_sysmon_metrics(view))

        view.update()
        app.processEvents()

        if now < self._measurement_deadline:
            return

        self._measurement_active = False
        self._timer.stop()
        elapsed = max(0.0, now - self._phase_started)
        live_metrics = self._read_sysmon_metrics(view)
        metrics = self._benchmark_metrics(
            live_metrics, elapsed, self._sysmon_samples
        )
        metrics.update({
            "viewport_width": int(view.width()),
            "viewport_height": int(view.height()),
        })
        self._finish_live_stress_result("monster_chaos_witness", metrics)

    def _tick(self):
        if not self._running or self._worker_active or not self._measurement_active:
            return

        try:
            app = QApplication.instance()
            view = self.main_window.view_3d
            now = time.perf_counter()

            if (
                self._live_stress_active
                and self._live_stress_timeout
                and now >= self._measurement_watchdog_deadline
            ):
                self._abort_live_stress(
                    self._live_stress_timeout_reason or
                    "live benchmark exceeded its cooperative time budget"
                )
                return

            if self._current and self._current[0] == "monster_chaos_witness":
                self._tick_monster_chaos_witness(now, app, view)
                return

            if self._current and self._current[0] in (
                "current_world", "current_world_phase1", "current_world_phase2",
                "borderless_window", "fullscreen_window",
                "editor_windowed_1280", "editor_windowed_1920",
            ):
                if self._current[0] == "current_world_phase2":
                    self._advance_player_area_rotation()
                else:
                    self._advance_player_area_sweep()

            view.update()
            app.processEvents()

            if now - self._last_sysmon_sample >= 1.0:
                self._last_sysmon_sample = now
                self._sysmon_samples.append(self._read_sysmon_metrics(view))

            if now < self._measurement_deadline:
                return

            self._measurement_active = False
            self._timer.stop()
            self.status_label.setText(
                "Completed: %s — collecting SysMon results..." % self._current[0]
            )
            elapsed = time.perf_counter() - self._phase_started
            live_metrics = self._read_sysmon_metrics(view)
            metrics = self._benchmark_metrics(
                live_metrics, elapsed, self._sysmon_samples
            )
            metrics.update({
                "viewport_width": int(view.width()),
                "viewport_height": int(view.height()),
            })

            label = self._current[0]
            if label in (
                "current_world", "current_world_phase1", "current_world_phase2",
                "borderless_window", "fullscreen_window",
                "editor_windowed_1280", "editor_windowed_1920",
            ):
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
                    "camera_sweep_collision_disabled": bool(
                        sweep.get("collision_disabled", True)
                    ),
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

            self._begin_next()
        except Exception:
            self._finish_with_error(traceback.format_exc())

    def _read_sysmon_metrics(self, view):
        """Read SysMon without changing renderer or logic-thread code."""
        make_current = getattr(view, "makeCurrent", None)
        done_current = getattr(view, "doneCurrent", None)
        if callable(make_current):
            make_current()
        try:
            return dict(view.sysmon.get_metrics())
        finally:
            if callable(done_current):
                done_current()
import math
import os
import sys
import time



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


class BenchmarkResults:
    def __init__(self, runner):
        object.__setattr__(self, "runner", runner)

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def __setattr__(self, name, value):
        if name == "runner":
            object.__setattr__(self, name, value)
        else:
            setattr(self.runner, name, value)

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
            if "clip_operations_per_second" in result:                metrics.append("CSG: %.0f clip operations/s" % float(result["clip_operations_per_second"]))
    
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
    <div class="footer">Generated by Fio Tools &gt; Benchmark. Benchmark workloads run against the existing Fio MainWindow; timeout supervision never terminates Fio.</div>
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
                    '<table cellspacing="0" cellpadding="0" style="margin-top:10px; margin-bottom:2px;">'
                    '<tr>'
                    '<td width="24" rowspan="2" bgcolor="#63d471"></td>'
                    '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td>'
                    '</tr>'
                    '<tr>'
                    '<td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
                    '<span style="font-size:25px; font-weight:bold; color:#63d471;">Average FPS:</span>'
                    '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
                    '</td>'
                    '</tr>'
                    '</table>' % float(average_fps)
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
                self.output.append(                    '<div style="color:#aaa; padding:4px 0;">CSG throughput: <b style="color:#ff9a32;">%.0f clip operations/s</b></div>'
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
    

    def _benchmark_metrics(self, metrics, duration_s, samples=None):
        """Build results directly from the authoritative SysMon snapshot."""
        metrics = dict(metrics or {})
        samples = list(samples or [])
        fps_values = []
        for sample in samples:
            try:
                fps = float(sample.get("fps", 0.0))
            except (TypeError, ValueError):
                continue
            if math.isfinite(fps) and fps > 0.0:
                fps_values.append(fps)
        try:
            current_fps = float(metrics.get("fps", 0.0))
        except (TypeError, ValueError):
            current_fps = 0.0
        if current_fps > 0.0 and math.isfinite(current_fps):
            fps_values.append(current_fps)
        average_fps = sum(fps_values) / len(fps_values) if fps_values else 0.0
        avg_frame_ms = float(metrics.get("average_frame_time_ms", 0.0) or 0.0)
        p95_ms = float(metrics.get("p95_frame_time_ms", 0.0) or 0.0)
        visible_tris = float(metrics.get("visible_tris", 0) or 0)
        culled_tris = float(metrics.get("culled_tris", 0) or 0)
        total_tris = visible_tris + culled_tris
        return {
            "average_fps": average_fps,
            "wall_clock_fps": average_fps,
            "average_frame_time_ms": avg_frame_ms,
            "p95_frame_time_ms": p95_ms,
            "mean_ms": avg_frame_ms,
            "p95_ms": p95_ms,
            "sample_count": len(samples),
            "measurement_duration_s": max(0.0, float(duration_s)),
            "average_visible_tris": visible_tris,
            "average_total_tris": total_tris,
            "average_culled_tris": culled_tris,
            "culling_efficiency": culled_tris / total_tris * 100.0 if total_tris > 0 else 0.0,
            "sysmon": metrics,
            "fps_source": "SysMon",
            "frame_time_source": "SysMon",
        }

    def _format_vram(self, metrics):
        used = metrics.get("vram_used_mb")
        total = metrics.get("vram_total_mb")
        if used is None and total is None:
            return "N/A"
        if used is None:
            return "%.0f MB total" % float(total)
        if total is None:
            return "%.0f MB used" % float(used)
        return "%.0f / %.0f MB" % (float(used), float(total))
    

    def _report_current_world_combined_if_complete(self):
        expected = int(self._requested_repetitions) * 2
        if len(self._current_phase_results) != expected:
            return

        total_duration = sum(
            float(r.get("measurement_duration_s", 0.0))
            for r in self._current_phase_results
        )
        total_frame_time = sum(
            float(r.get("average_frame_time_ms", 0.0))
            * float(r.get("measurement_duration_s", 0.0))
            for r in self._current_phase_results
        )
        if total_duration <= 0.0 or total_frame_time <= 0.0:
            return

        combined_frame_ms = total_frame_time / total_duration
        combined_fps = 1000.0 / combined_frame_ms
        self._append(
            '<table align="right" cellspacing="0" cellpadding="0" style="margin-top:14px; margin-bottom:10px;">'
            '<tr>'
            '<td height="2" bgcolor="#ff9a32" style="font-size:2px; line-height:2px;"></td>'
            '<td width="24" rowspan="2" bgcolor="#ff9a32"></td>'
            '</tr>'
            '<tr>'
            '<td style="padding:6px 12px 2px 16px; white-space:nowrap; text-align:right;">'
            '<span style="font-size:25px; font-weight:bold; color:#63d471;">Combined Average FPS:</span>'
            '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
            '</td>'
            '</tr>'
            '</table>'
            '<br style="clear:both;">' % combined_fps
        )
        self._results.append({
            "test": "current_world_combined",
            "description": "Combined Phase 1 + Phase 2 Current World result",
            "average_fps": combined_fps,
            "average_frame_time_ms": combined_frame_ms,
            "measurement_duration_s": total_duration,
            "sample_count": sum(
                int(r.get("sample_count", 0)) for r in self._current_phase_results
            ),
            "benchmark_combined": True,
            "benchmark_repetitions": int(self._requested_repetitions),
        })

    def _report_live_result(self, label, metrics):
        width = int(metrics.get("viewport_width", self.main_window.view_3d.width()))
        height = int(metrics.get("viewport_height", self.main_window.view_3d.height()))
        avg_fps = float(metrics.get("average_fps", 0.0))
        avg_ms = float(metrics.get("average_frame_time_ms", 0.0))
        p95_ms = float(metrics.get("p95_frame_time_ms", 0.0))
        samples = int(metrics.get("sample_count", 0))
        duration = float(metrics.get("measurement_duration_s", 0.0))
        result = dict(metrics)
        display_label = metrics.get("benchmark_phase_label", label)
        result.update({
            "test": label,
            "description": display_label,
            "entities": len(self.main_window.state.things),
        })
        self._results.append(result)
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)

        self.output.append(
            '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
            '<div style="color:#aaa;">%dx%d &nbsp; • &nbsp; %.2f ms average frame &nbsp; • &nbsp; %.2f ms p95</div>'
            '<div style="color:#aaa;">%d SysMon samples &nbsp; • &nbsp; %.2f s measured &nbsp; • &nbsp; wall-clock %.2f FPS</div>'
            '<div style="color:#aaa;">VRAM %s &nbsp; • &nbsp; brushes %d visible / %d culled / %d total &nbsp; • &nbsp; entities %d</div>'
            '</div>'
            % (
                self._html_escape(display_label),
                width,
                height,
                avg_ms,
                p95_ms,
                samples,
                duration,
                float(metrics.get("wall_clock_fps", 0.0)),
                self._format_vram(metrics),
                int(metrics.get("visible_brushes", 0)),
                int(metrics.get("culled_brushes", 0)),
                int(metrics.get("total_brushes", 0)),
                result["entities"],
            )
        )

        if label in (
            "current_world",
            "borderless_window",
            "fullscreen_window",
            "editor_windowed_1280",
            "editor_windowed_1920",
        ):
            sweep_line = (
                '<div style="padding:4px 0; color:#aaa;">'
                '<b style="color:#eeeeee;">Camera sweep:</b> %s'
                ' &nbsp; • &nbsp; <b style="color:#eeeeee;">anchor:</b> %s%s'
                '</div>'
                % (
                    metrics.get("camera_sweep_mode", "player-area"),
                    metrics.get("camera_sweep_anchor", "unknown"),
                    (
                        ' &nbsp; • &nbsp; <b style="color:#ff8a00;">FALLBACK: %s</b>'
                        % str(metrics.get("camera_sweep_fallback_reason", "unknown"))
                    ) if metrics.get("camera_sweep_fallback") else "",
                )
            )
            self.output.append(sweep_line)
            self.output.append(
                '<div style="padding:4px 0;">'
                '<span style="color:#eeeeee; font-weight:bold;">Average visible triangles: </span>'
                '<span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average total triangles: </span>'
                '<span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average culled triangles: </span>'
                '<span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Culling efficiency: </span>'
                '<span style="color:#ff9a32; font-weight:bold;">%.1f%%</span>'
                '</div>'
                % (
                    metrics.get("average_visible_tris", 0.0),
                    metrics.get("average_total_tris", 0.0),
                    metrics.get("average_culled_tris", 0.0),
                    metrics.get("culling_efficiency", 0.0),
                )
            )

        if label in ("current_world_phase1", "current_world_phase2"):
            phase_label = "Phase 1:" if label.endswith("phase1") else "Phase 2:"
            self.output.append(
                '<table cellspacing="0" cellpadding="0" style="margin-top:10px; margin-bottom:2px;">'
                '<tr>'
                '<td width="24" rowspan="2" bgcolor="#63d471"></td>'
                '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td>'
                '</tr>'
                '<tr>'
                '<td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">%s</span>'
                '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
                '</td>'
                '</tr>'
                '</table>' % (phase_label, avg_fps)
            )
        else:
            self.output.append(
                '<div style="margin-top:10px; padding:8px 0 2px 0; border-top:2px solid #555; white-space:nowrap;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">average FPS:</span>'
                '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f</span>'
                '</div>' % avg_fps
            )

        self.output.append(
            '<div style="color:#aaa; padding:2px 0 4px 0;">'
            'Average FPS = 1000 / mean SysMon frame time. Wall-clock FPS is reported separately.'
            '</div>'
        )
        self._append_test_end_separator()
        self.output.ensureCursorVisible()
        QApplication.processEvents()



import json
import os
import queue
import socket
import threading
import time
import traceback
import uuid

from PyQt5.QtCore import QTimer


class _HeadlessValue:
    def __init__(self, host):
        self.host = host

    def setText(self, value):
        self.host.send({"event": "status", "text": str(value)})

    def setStyleSheet(self, _style):
        pass


class _HeadlessCheckBox:
    def __init__(self):
        self._checked = False
        self._enabled = True

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = bool(checked)

    def setEnabled(self, enabled):
        self._enabled = bool(enabled)


class _HeadlessButton:
    def __init__(self):
        self.enabled = True
        self.visible = False

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def setVisible(self, visible):
        self.visible = bool(visible)


class _HeadlessOutput:
    def __init__(self, host):
        self.host = host

    def append(self, text):
        self.host.send({"event": "log", "html": str(text)})

    def clear(self):
        self.host.send({"event": "clear"})

    def ensureCursorVisible(self):
        pass


class _HeadlessToggle:
    def __init__(self):
        self.checked = False

    def setChecked(self, checked):
        self.checked = bool(checked)


def _json_default(value):
    """Convert NumPy scalar values emitted by SysMon into JSON-native values."""
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(
        "Object of type %s is not JSON serializable"
        % type(value).__name__
    )


class BenchmarkHost:
    """Expose the live benchmark runner to an external supervisor."""

    def __init__(self, main_window):
        self.main_window = main_window
        self.root_dir = main_window.root_dir
        self._token = uuid.uuid4().hex

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(0.5)
        self.host = "127.0.0.1"
        self.port = int(self._server.getsockname()[1])

        self._client = None
        self._client_lock = threading.Lock()
        self._commands = queue.Queue()
        self._shutdown = threading.Event()

        self._dialog_proxy = _BenchmarkDialogProxy(self)
        self.runner = BenchmarkRunner(self._dialog_proxy)
        self.runner.main_window = main_window
        self.runner.root_dir = self.root_dir
        self._dialog_proxy.attach_runner(self.runner)

        self._last_current = None
        self._last_result_count = 0
        self._last_running = False
        self._last_heartbeat = 0.0

        self._timer = QTimer(main_window)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._pump)
        self._timer.start()

        self._server_thread = threading.Thread(
            target=self._accept_loop,
            name="FioBenchmarkIPC",
            daemon=True,
        )
        self._server_thread.start()

    @property
    def token(self):
        return self._token

    def send(self, message):
        payload = (
            json.dumps(
                message,
                separators=(",", ":"),
                default=_json_default,
            )
            + "\n"
        ).encode("utf-8")
        with self._client_lock:
            client = self._client
            if client is None:
                return
            try:
                client.sendall(payload)
            except OSError:
                try:
                    client.close()
                except OSError:
                    pass
                if self._client is client:
                    self._client = None

    def _accept_loop(self):
        while not self._shutdown.is_set():
            try:
                client, _address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            client.settimeout(0.5)
            with self._client_lock:
                old = self._client
                self._client = client
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass

            try:
                while not self._shutdown.is_set():
                    try:
                        raw = client.recv(65536)
                    except socket.timeout:
                        continue
                    if not raw:
                        break
                    for line in raw.decode("utf-8", errors="replace").splitlines():
                        if not line.strip():
                            continue
                        try:
                            command = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._commands.put(command)
            except OSError:
                pass
            finally:
                with self._client_lock:
                    if self._client is client:
                        self._client = None
                try:
                    client.close()
                except OSError:
                    pass

    def _pump(self):
        for _ in range(16):
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            self._handle_command(command)

        running = bool(self.runner._running)
        current = self.runner._current[0] if self.runner._current else None

        now = time.monotonic()
        if now - self._last_heartbeat >= 1.0:
            self._last_heartbeat = now
            self.send({
                "event": "heartbeat",
                "running": running,
                "current": current,
            })

        if running and not self._last_running:
            self.send({"event": "run_started"})

        if current != self._last_current:
            self._last_current = current
            if current is not None:
                self.send({"event": "test_started", "label": current})

        result_count = len(self.runner._results)
        if result_count > self._last_result_count:
            for result in self.runner._results[self._last_result_count:result_count]:
                self.send({"event": "result", "result": result})
            self._last_result_count = result_count

        if not running and self._last_running:
            self.send({
                "event": "completed",
                "results": list(self.runner._results),
            })

        self._last_running = running

    def _handle_command(self, command):
        if command.get("token") != self._token:
            self.send({"event": "error", "error": "invalid benchmark host token"})
            return

        action = command.get("action")
        if action == "ping":
            self.send({
                "event": "hello",
                "pid": os.getpid(),
                "has_current_map": bool(self.runner._has_current_loaded_map()),
            })
            return

        if action == "start":
            if self.runner._running:
                self.send({"event": "error", "error": "benchmark already running"})
                return
            # Tell the external manager that supervision has begun before
            # entering any potentially long live preparation step.
            self._last_running = True
            self.send({"event": "run_started"})
            self._start(command.get("config") or {})
            return

        if action == "cancel":
            if not self.runner._running:
                return
            try:
                self.runner._restore_original()
            except Exception:
                self.send({
                    "event": "error",
                    "error": traceback.format_exc(),
                })

    def _start(self, config):
        self._last_result_count = 0
        self._last_current = None
        self._last_running = False

        self.runner._requested_duration = config.get("duration")
        self.runner._requested_repetitions = max(
            1, int(config.get("repetitions", 1))
        )
        self._dialog_proxy.apply_config(config)

        try:
            self.runner._start()
        except Exception:
            self.send({
                "event": "error",
                "error": traceback.format_exc(),
            })

    def shutdown(self):
        self._shutdown.set()
        self._timer.stop()
        try:
            self._server.close()
        except OSError:
            pass
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass


class _BenchmarkDialogProxy:
    """UI-compatible adapter for the existing BenchmarkRunner."""

    def __init__(self, host):
        self._host = host
        self.main_window = host.main_window
        self.root_dir = host.root_dir

        self._timer = QTimer(host.main_window)
        self._timer.setInterval(20)

        self.status_label = _HeadlessValue(host)
        self.output = _HeadlessOutput(host)
        self.throbber = _HeadlessButton()
        self.export_button = _HeadlessButton()
        self.run_button = _HeadlessButton()

        self.additional_tests = _HeadlessCheckBox()
        self.brush_1000 = _HeadlessCheckBox()
        self.brush_10000 = _HeadlessCheckBox()
        self.brush_100000 = _HeadlessCheckBox()
        self.io_chain_1000 = _HeadlessCheckBox()
        self.monster_chaos_witness = _HeadlessCheckBox()
        self.borderless_window = _HeadlessCheckBox()
        self.fullscreen_window = _HeadlessCheckBox()
        self.editor_windowed_1280 = _HeadlessCheckBox()
        self.editor_windowed_1920 = _HeadlessCheckBox()
        self._stress_toggle = _HeadlessToggle()

    def attach_runner(self, runner):
        self._timer.timeout.connect(runner._tick)

    def findChild(self, _widget_type):
        return self._stress_toggle

    def _append(self, text):
        self.output.append(text)

    def _set_controls_enabled(self, enabled):
        self.run_button.setEnabled(enabled)
        for name in (
            "additional_tests",
            "brush_1000",
            "brush_10000",
            "brush_100000",
            "io_chain_1000",
            "monster_chaos_witness",
            "borderless_window",
            "fullscreen_window",
            "editor_windowed_1280",
            "editor_windowed_1920",
        ):
            getattr(self, name).setEnabled(enabled)

    def apply_config(self, config):
        selected = set(config.get("tests") or ())
        self.additional_tests.setChecked("additional_tests" in selected)
        self.brush_1000.setChecked("live_1000_brushes" in selected)
        self.brush_10000.setChecked("live_10000_brushes" in selected)
        self.brush_100000.setChecked("live_100000_brushes" in selected)
        self.io_chain_1000.setChecked("live_io_1000" in selected)
        self.monster_chaos_witness.setChecked("monster_chaos_witness" in selected)
        self.borderless_window.setChecked("borderless_window" in selected)
        self.fullscreen_window.setChecked("fullscreen_window" in selected)
        self.editor_windowed_1280.setChecked("editor_windowed_1280" in selected)
        self.editor_windowed_1920.setChecked("editor_windowed_1920" in selected)

