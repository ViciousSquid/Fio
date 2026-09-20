"""External Fio benchmark manager.

The manager is a separate Python process. It owns the benchmark UI and the
hard timeout boundary. The workloads themselves stay inside the already
running Fio process, using its actual MainWindow, QtGameView, renderer and I/O.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import socket
import subprocess
import sys
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)


TESTS = (
    ("live_io_1000", "I/O chain: 1,000 entities"),
    ("live_1000_brushes", "Renderer scene: 1,000 brushes"),
    ("live_10000_brushes", "Renderer scene: 10,000 brushes"),
    ("live_100000_brushes", "Renderer scene: 100,000 brushes"),
    ("monster_capacity", "Monster capacity: find maximum live monsters before timeout"),
    ("monster_chaos_witness", "Monster chaos: 50 monsters / 10-second live witness"),
    ("borderless_window", "Window mode: borderless maximized"),
    ("fullscreen_window", "Window mode: true fullscreen"),
    ("editor_windowed_1280", "Editor mode: windowed 1280×720 (3D view pane)"),
    ("editor_windowed_1920", "Editor mode: windowed 1920×1080 (3D view pane)"),
)


class BenchmarkManager(QDialog):
    STARTUP_TIMEOUT = 300.0
    INACTIVITY_TIMEOUT = 120.0
    ABSOLUTE_TIMEOUT = 900.0

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.results = []
        self.current_test = None
        self.running = False
        self.done = False
        self._last_activity = time.monotonic()
        self._test_started_at = None
        self._sock = None
        self._buffer = b""
        self._connected = False

        self.setWindowTitle("Fio Benchmark Manager")
        self.resize(900, 700)
        self.setStyleSheet(
            """
            QDialog {
                background: #171717;
                color: #eeeeee;
            }
            QLabel {
                color: #dddddd;
            }
            QToolButton {
                color: #63d471;
                background: transparent;
                border: none;
                font-weight: bold;
                padding: 4px;
            }
            QToolButton:hover {
                color: #ff9a32;
            }
            QCheckBox {
                color: #dddddd;
                spacing: 8px;
                padding: 3px;
            }
            QCheckBox:hover {
                color: #ff9a32;
            }
            QCheckBox::indicator {
                width: 15px;
                height: 15px;
            }
            QCheckBox::indicator:unchecked {
                background: #202020;
                border: 1px solid #666666;
            }
            QCheckBox::indicator:checked {
                background: #63d471;
                border: 1px solid #63d471;
            }
            QScrollArea {
                background: #171717;
                border: 1px solid #444444;
            }
            QScrollBar:vertical {
                background: #202020;
                width: 12px;
                margin: 0;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: #555555;
                min-height: 24px;
                border-radius: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #ff9a32;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                background: #202020;
                height: 0;
                border: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: #202020;
            }
            QScrollBar:horizontal {
                background: #202020;
                height: 12px;
                margin: 0;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background: #555555;
                min-width: 24px;
                border-radius: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #ff9a32;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                background: #202020;
                width: 0;
                border: none;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: #202020;
            }
            QProgressBar {
                background: #202020;
                border: 1px solid #444444;
                height: 10px;
                text-align: center;
            }
            QProgressBar::chunk {
                background: #ff9a32;
            }
            QPushButton {
                background: #202020;
                color: #eeeeee;
                border: 1px solid #555555;
                padding: 7px 14px;
                border-radius: 2px;
            }
            QPushButton:hover {
                border: 1px solid #ff9a32;
                color: #ff9a32;
            }
            QPushButton:pressed {
                background: #2a2a2a;
            }
            QPushButton:disabled {
                color: #666666;
                border-color: #333333;
            }
            QTextBrowser {
                background: #171717;
                color: #dddddd;
                border: 1px solid #444444;
                selection-background-color: #ff9a32;
                selection-color: #111111;
            }
            """
        )

        root = QVBoxLayout(self)

        description = QLabel(
            "The benchmark runs against the existing Fio process. "
            "This window is a separate process, so it can terminate Fio "
            "if a live test stops responding."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        self.status = QLabel("Connecting to Fio benchmark host…")
        root.addWidget(self.status)

        self.current_map_label = QLabel("Current loaded map: checking…")
        root.addWidget(self.current_map_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        toggle = QToolButton()
        toggle.setText("See stress tests")
        toggle.setCheckable(True)
        toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.DownArrow)
        toggle.setAutoRaise(True)
        root.addWidget(toggle)

        options = QWidget()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(12, 0, 0, 0)

        self.checkboxes = {}
        for key, label in TESTS:
            box = QCheckBox(label)
            box.setToolTip(
                "Run this workload in the already-running Fio renderer/editor."
            )
            self.checkboxes[key] = box
            options_layout.addWidget(box)

        additional = QCheckBox(
            "Additional stress tests (I/O, renderer, gameplay)"
        )
        additional.setToolTip(
            "Run the standard live I/O, renderer and monster-capacity workloads."
        )
        self.checkboxes["additional_tests"] = additional
        options_layout.insertWidget(0, additional)

        options.setVisible(False)
        options.setStyleSheet(
            "QWidget { background: #171717; color: #dddddd; }"
            "QCheckBox { background: #171717; color: #dddddd; }"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: #171717; border: 1px solid #444444; }"
            "QScrollArea > QWidget { background: #171717; }"
        )
        scroll.viewport().setStyleSheet("background: #171717;")
        scroll.setWidget(options)
        scroll.setMaximumHeight(260)
        root.addWidget(scroll)
        toggle.toggled.connect(options.setVisible)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setStyleSheet(
            "QTextBrowser { font-family: Consolas, monospace; "
            "background: #171717; border: 1px solid #444; }"
        )
        root.addWidget(self.output, 1)

        actions = QHBoxLayout()

        self.export_button = QPushButton("Export HTML Report…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_html)
        actions.addWidget(self.export_button)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start)
        actions.addWidget(self.run_button)

        self.close_button = QPushButton("Close")
        self.close_button.setToolTip(
            "Close the manager; a running benchmark is cancelled first."
        )
        self.close_button.clicked.connect(self.close_manager)
        actions.addWidget(self.close_button)

        root.addLayout(actions)

        self.socket_timer = QTimer(self)
        self.socket_timer.setInterval(50)
        self.socket_timer.timeout.connect(self.poll_socket)
        self.socket_timer.start()

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(250)
        self.watchdog_timer.timeout.connect(self.watchdog)
        self.watchdog_timer.start()

        QTimer.singleShot(0, self.connect_to_host)

    def _send(self, payload):
        if self._sock is None:
            return
        message = dict(payload)
        message["token"] = self.args.token
        try:
            self._sock.sendall(
                (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            )
        except OSError:
            self._disconnect("Lost connection to Fio.")

    def connect_to_host(self):
        if self._sock is not None:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect((self.args.host, self.args.port))
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            self.status.setText("Could not connect to Fio benchmark host; retrying…")
            self.output.append(html.escape(str(exc)))
            QTimer.singleShot(1000, self.connect_to_host)
            return

        self._sock = sock
        self._connected = True
        self._last_activity = time.monotonic()
        self._send({"action": "ping"})
        self.status.setText("Connected — select stress tests and run the benchmark.")
        self.run_button.setEnabled(True)
        self._append(
            "LIVE BENCHMARK MANAGER: supervising the existing Fio process "
            "from a separate Python process."
        )

    def _disconnect(self, reason):
        self._sock = None
        if self.running:
            self._kill_fio(reason)
            return
        self._connected = False
        self.run_button.setEnabled(False)
        self.status.setText(reason)
        self._append(
            '<div style="color:#ffb15a; padding:6px 0;">%s</div>'
            % html.escape(reason)
        )

    def poll_socket(self):
        if self._sock is None:
            return

        try:
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    self._disconnect("Fio closed the benchmark connection.")
                    return
                self._buffer += chunk
                if len(chunk) < 65536:
                    break
        except BlockingIOError:
            pass
        except OSError:
            self._disconnect("Fio closed the benchmark connection.")
            return

        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            self._handle_message(message)

    def _handle_message(self, message):
        self._last_activity = time.monotonic()
        event = message.get("event")

        if event == "hello":
            has_map = bool(message.get("has_current_map"))
            self.current_map_label.setText(
                "Current loaded map: "
                + ("available — included in the benchmark." if has_map else
                   "none — select at least one stress test.")
            )
            self.status.setText(
                "Connected to Fio (PID %s) — ready."
                % message.get("pid", self.args.pid)
            )
            self.run_button.setEnabled(
                has_map or any(
                    box.isChecked()
                    for key, box in self.checkboxes.items()
                    if key != "additional_tests"
                ) or self.checkboxes["additional_tests"].isChecked()
            )

        elif event == "run_started":
            self.running = True
            self.done = False
            self._test_started_at = None
            self._last_activity = time.monotonic()
            self.progress.setVisible(True)
            self.run_button.setEnabled(False)
            self._set_checks_enabled(False)

        elif event == "test_started":
            self.current_test = str(message.get("label") or "benchmark")
            self._test_started_at = time.monotonic()
            self.status.setText("Running: %s" % self.current_test)
            self._append(
                '<div style="border-top:2px solid #ff9a32; margin:14px 0 8px 0; '
                'padding-top:8px;"><b style="color:#ff9a32;">TEST: %s</b></div>'
                % html.escape(self.current_test)
            )

        elif event == "log":
            self._append(message.get("html", ""))

        elif event == "clear":
            self.output.clear()

        elif event == "status":
            self.status.setText(str(message.get("text", "")))

        elif event == "result":
            result = message.get("result")
            if isinstance(result, dict):
                self.results.append(result)

        elif event == "completed":
            self.results = list(message.get("results") or self.results)
            self.running = False
            self.done = True
            self.current_test = None
            self.progress.setVisible(False)
            self._set_checks_enabled(True)
            self.run_button.setEnabled(True)
            self.export_button.setEnabled(bool(self.results))
            self.status.setText("Benchmark complete.")
            self._append(
                '<div style="margin-top:12px; padding:10px; background:#1f241f; '
                'border:1px solid #63d471; color:#eeeeee;">'
                '<span style="color:#63d471; font-weight:bold;">Benchmark complete.</span> '
                '%d result(s) recorded.'
                '</div>' % len(self.results)
            )

        elif event == "error":
            self.running = False
            self.progress.setVisible(False)
            self._set_checks_enabled(True)
            self.run_button.setEnabled(True)
            self.status.setText("Benchmark failed in Fio.")
            self._append(
                '<pre style="color:#ff7777;">%s</pre>'
                % html.escape(str(message.get("error", "unknown error")))
            )

    def _append(self, text):
        self.output.append(text)
        self.output.ensureCursorVisible()

    def _set_checks_enabled(self, enabled):
        for box in self.checkboxes.values():
            box.setEnabled(enabled)

    def start(self):
        tests = [
            key for key, box in self.checkboxes.items()
            if key != "additional_tests" and box.isChecked()
        ]
        if self.checkboxes["additional_tests"].isChecked():
            tests.insert(0, "additional_tests")

        if not tests and not self.current_map_label.text().endswith(
            "available — included in the benchmark."
        ):
            QMessageBox.warning(
                self,
                "No benchmark selected",
                "There is no loaded map. Select at least one stress test."
            )
            return

        self.output.clear()
        self.results = []
        self.current_test = None
        self.done = False
        self.running = True
        self._test_started_at = None
        self._last_activity = time.monotonic()

        self._send({
            "action": "start",
            "config": {
                "tests": tests,
                "duration": self.args.duration,
                "repetitions": self.args.repetitions,
            },
        })

    def watchdog(self):
        if self.done or not self.running:
            return

        now = time.monotonic()
        inactivity = now - self._last_activity

        if self.current_test is None:
            if inactivity > self.STARTUP_TIMEOUT:
                self._kill_fio(
                    "Fio did not start the benchmark within %.0f seconds."
                    % self.STARTUP_TIMEOUT
                )
            return

        absolute = now - (self._test_started_at or now)
        if inactivity > self.INACTIVITY_TIMEOUT:
            self._kill_fio(
                "%s stopped responding for %.0f seconds."
                % (self.current_test, inactivity)
            )
            return

        if absolute > self.ABSOLUTE_TIMEOUT:
            self._kill_fio(
                "%s exceeded the %.0f second hard test limit."
                % (self.current_test, self.ABSOLUTE_TIMEOUT)
            )

    def _kill_fio(self, reason):
        self.running = False
        self.progress.setVisible(False)
        self._set_checks_enabled(True)
        self.run_button.setEnabled(False)
        self.status.setText("Fio was terminated: %s" % reason)
        self._append(
            '<div style="background:#2a1010; border:1px solid #ff5555; '
            'padding:12px; margin:8px 0;"><b>TEST HUNG</b><br>%s</div>'
            % html.escape(reason)
        )

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        self._terminate_pid(self.args.pid)
        self.close_button.setEnabled(True)
        self.export_button.setEnabled(bool(self.results))

    @staticmethod
    def _terminate_pid(pid):
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return

        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass

    def export_html(self):
        if not self.results:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Benchmark Report",
            "fio_benchmark_report.html",
            "HTML files (*.html)",
        )
        if not path:
            return

        sections = []
        for result in self.results:
            label = html.escape(str(result.get("test", "benchmark")))
            status = html.escape(str(result.get("status", "passed")))
            description = html.escape(str(result.get("description", "")))
            rows = []

            for key in (
                "average_fps", "min_fps", "max_fps",
                "io_elapsed_ms", "io_hops", "hops_per_second",
                "monster_capacity", "monster_count", "aggro_count",
                "alive_monsters", "dead_monsters", "witness_duration_s",
                "seed", "pathnode_name", "viewport_width", "viewport_height",
                "visible_brushes", "culled_brushes", "total_brushes",
            ):
                if key in result:
                    rows.append(
                        "<tr><th>%s</th><td>%s</td></tr>"
                        % (html.escape(key), html.escape(str(result[key])))
                    )

            sections.append(
                "<section><h2>%s</h2><p>Status: <b>%s</b></p>"
                "<p>%s</p><table>%s</table></section>"
                % (label, status, description, "".join(rows))
            )

        report = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Fio Benchmark Report</title><style>"
            "body{font-family:Segoe UI,Arial,sans-serif;background:#171717;"
            "color:#eee;margin:32px}"
            "section{border:1px solid #444;padding:18px;margin:0 0 20px}"
            "table{border-collapse:collapse}th,td{padding:5px 10px;text-align:left}"
            "th{color:#aaa}</style></head><body>"
            "<h1>Fio Benchmark Report</h1>"
            "<p>Fio PID: %s</p>%s</body></html>"
            % (html.escape(str(self.args.pid)), "".join(sections))
        )

        with open(path, "w", encoding="utf-8") as handle:
            handle.write(report)

    def close_manager(self):
        self.close()

    def closeEvent(self, event):
        if self.running:
            self._send({"action": "cancel"})
            self.running = False

        self.socket_timer.stop()
        self.watchdog_timer.stop()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        event.accept()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--auto-start", action="store_true")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Fio Benchmark Manager")

    dialog = BenchmarkManager(args)
    dialog.show()

    if args.auto_start:
        QTimer.singleShot(250, dialog.start)

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
