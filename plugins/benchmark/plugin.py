"""Integration for Fio's optional developer Benchmark plugin."""

from __future__ import annotations

import math
import os
import subprocess
import sys

from plugins.api import FioPlugin


class BenchmarkPlugin(FioPlugin):
    name = "Benchmark"
    version = "1.0.0"
    description = "Optional developer tooling for live runtime benchmarks and reports."
    category = "Developer"
    enabled = False
    api_version = "1.4.0"
    requires = []

    def __init__(self):
        self._host = None
        self._process = None
        self._main_window = None

    def register(self, api):
        api.register_console_command(
            "benchmark",
            self._console_command,
            "benchmark [seconds] [repetitions] — benchmark the current map",
        )

    def _console_command(self, args, main_window, logic, play_mode):
        from editor.debug_console import debug_log

        raw = (args or "").strip()
        parts = raw.split()
        if len(parts) > 2:
            debug_log("Error", "Usage: benchmark [seconds] [repetitions]")
            return None

        duration = None
        repetitions = 1
        if parts:
            try:
                duration = float(parts[0])
            except ValueError:
                debug_log("Error", "Usage: benchmark [seconds] [repetitions]")
                return None
            if not math.isfinite(duration) or duration <= 0.0:
                debug_log("Error", "benchmark: duration must be greater than 0 seconds")
                return None

        if len(parts) == 2:
            try:
                repetitions = int(parts[1])
            except ValueError:
                debug_log("Error", "benchmark: repetitions must be a positive integer")
                return None
            if repetitions <= 0:
                debug_log("Error", "benchmark: repetitions must be a positive integer")
                return None

        try:
            self._launch(
                main_window,
                auto_start=True,
                duration=duration,
                repetitions=repetitions,
            )
        except Exception as exc:
            debug_log("Error", f"benchmark: could not open benchmark window: {exc}")
        return None

    def _launch(self, main_window, auto_start=False, duration=None, repetitions=1):
        if main_window is None:
            raise RuntimeError("Benchmark requires an editor MainWindow")

        if self._process is not None and self._process.poll() is None:
            if self._host is not None:
                self._host.send({"event": "manager_already_running"})
            return

        if self._host is None or self._main_window is not main_window:
            self._stop()
            from .benchmark import BenchmarkHost
            self._host = BenchmarkHost(main_window)
            self._main_window = main_window
            try:
                main_window.destroyed.connect(self._stop)
            except Exception:
                pass

        manager_path = os.path.join(
            main_window.root_dir, "plugins", "benchmark", "manager.py"
        )
        command = [
            sys.executable,
            manager_path,
            "--host", self._host.host,
            "--port", str(self._host.port),
            "--token", self._host.token,
            "--pid", str(os.getpid()),
            "--root", main_window.root_dir,
            "--repetitions", str(max(1, int(repetitions or 1))),
        ]
        if duration is not None:
            command.extend(["--duration", str(float(duration))])
        if auto_start:
            command.append("--auto-start")

        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

        self._process = subprocess.Popen(
            command,
            cwd=main_window.root_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def on_enabled_changed(self, enabled):
        if not enabled:
            self._stop()
            return

        # The Plugins menu is the user-facing entry point for optional
        # developer plugins. When Benchmark is enabled there, immediately open
        # its manager using the existing editor MainWindow.
        try:
            from PyQt5.QtWidgets import QApplication

            main_window = QApplication.activeWindow()
            if main_window is None:
                for widget in QApplication.topLevelWidgets():
                    if hasattr(widget, "view_3d") and hasattr(widget, "root_dir"):
                        main_window = widget
                        break
            if main_window is None:
                return

            self._launch(main_window)
        except Exception as exc:
            try:
                from editor.debug_console import debug_log
                debug_log(
                    "Error",
                    f"benchmark: could not open benchmark window: {exc}",
                )
            except Exception:
                pass

    def _stop(self, *_args):
        process = self._process
        self._process = None

        host = self._host
        self._host = None
        self._main_window = None

        if host is not None:
            try:
                if getattr(host.runner, "_running", False):
                    host.runner._restore_original()
            except Exception:
                pass
            try:
                host.shutdown()
            except Exception:
                pass

        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass