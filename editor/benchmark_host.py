"""IPC host for the external Fio benchmark manager.

The host lives inside the real Fio editor process. It has no benchmark UI.
The external manager owns presentation and hard process supervision.
"""

from __future__ import annotations

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

        from .benchmark_runner import BenchmarkRunner

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
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
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
        self.monster_capacity = _HeadlessCheckBox()
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
            "monster_capacity",
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
        self.monster_capacity.setChecked("monster_capacity" in selected)
        self.borderless_window.setChecked("borderless_window" in selected)
        self.fullscreen_window.setChecked("fullscreen_window" in selected)
        self.editor_windowed_1280.setChecked("editor_windowed_1280" in selected)
        self.editor_windowed_1920.setChecked("editor_windowed_1920" in selected)
