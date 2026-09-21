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
        # Whether Fio has a map worth benchmarking.  Answered by the host's
        # "hello"; until then only a ticked test can enable a run.
        self.has_current_map = False

        self.setWindowTitle("Fio Benchmark")
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
                height: 3px;
                min-height: 3px;
                max-height: 3px;
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
            'Click <span style="color:#ff9a32;">Run Benchmark</span> to analyse '
            'the currently loaded map or choose a stress-test from below to '
            'benchmark this Fio installation against another one.'
        )
        description.setTextFormat(Qt.RichText)
        description.setWordWrap(True)
        description.setStyleSheet("font-size: 15px; padding: 6px 2px 10px 2px;")
        root.addWidget(description)

        # Connection state and map availability are only worth screen space
        # when they have something to say: the opening screen is the sentence
        # above and the two controls, not a status readout.
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        root.addWidget(self.status)

        self.current_map_label = QLabel("")
        self.current_map_label.setVisible(False)
        root.addWidget(self.current_map_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        toggle = QToolButton()
        toggle.setText("See tests")
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
            # Ticking a test is the other way to have something to benchmark,
            # so it has to re-open the Run button.  Without this the button's
            # enabled state was decided once, on connect, and a session opened
            # with no map loaded could never start a run at all.
            box.toggled.connect(self._refresh_run_enabled)
            self.checkboxes[key] = box
            options_layout.addWidget(box)

        additional = QCheckBox(
            "Additional stress tests (I/O, renderer, gameplay)"
        )
        additional.setToolTip(
            "Run the standard live I/O, renderer and monster-capacity workloads."
        )
        additional.toggled.connect(self._refresh_run_enabled)
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
        scroll.setVisible(False)
        root.addWidget(scroll)

        def _show_tests(shown):
            # Hide the scroll area, not just its contents: leaving an empty
            # bordered box behind the collapsed list is what made the opening
            # screen look like two output panes.
            options.setVisible(shown)
            scroll.setVisible(shown)
            toggle.setArrowType(Qt.DownArrow if shown else Qt.RightArrow)

        toggle.setArrowType(Qt.RightArrow)
        toggle.toggled.connect(_show_tests)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setStyleSheet(
            "QTextBrowser { font-family: Consolas, monospace; "
            "background: #171717; border: 1px solid #444; }"
        )
        root.addWidget(self.output, 1)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.setEnabled(False)
        self.run_button.setMinimumHeight(44)
        self.run_button.setStyleSheet(
            """
            QPushButton {
                background: #3aa757;
                color: #ffffff;
                border: none;
                border-radius: 3px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover   { background: #45bd66; }
            QPushButton:pressed { background: #2f8b47; }
            QPushButton:disabled { background: #2f4636; color: #7d8b81; }
            """
        )
        self.run_button.clicked.connect(self.start)
        root.addWidget(self.run_button)

        actions = QHBoxLayout()

        # Nothing to export until a run has produced results, so it stays out
        # of the opening screen entirely rather than sitting there greyed out.
        self.export_button = QPushButton("Export HTML Report…")
        self.export_button.setEnabled(False)
        self.export_button.setVisible(False)
        self.export_button.clicked.connect(self.export_html)
        actions.addWidget(self.export_button)

        actions.addStretch(1)

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
            self.has_current_map = bool(message.get("has_current_map"))
            self.current_map_label.setText(
                "Current loaded map: "
                + ("available — included in the benchmark."
                   if self.has_current_map else
                   "none — tick a test under “See tests”.")
            )
            self.current_map_label.setVisible(not self.has_current_map)
            self.status.setText(
                "Connected to Fio (PID %s) — ready."
                % message.get("pid", self.args.pid)
            )
            self._refresh_run_enabled()

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
                '<div style="margin-top:12px; padding:14px 16px; background:#1f241f; '
                'border:1px solid #63d471; color:#eeeeee;">'
                '<div style="color:#63d471; font-size:22px; font-weight:bold; '
                'line-height:1.2; margin-bottom:6px;">Benchmark complete.</div>'
                '<div style="color:#eeeeee; font-size:14px; font-weight:bold;">'
                '%d result(s) recorded.</div>'
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

    def _selected_tests(self):
        """The stress tests ticked right now, additional_tests first."""
        tests = [key for key, box in self.checkboxes.items()
                 if key != "additional_tests" and box.isChecked()]
        if self.checkboxes["additional_tests"].isChecked():
            tests.insert(0, "additional_tests")
        return tests

    def _can_run(self):
        """There is something to benchmark: a loaded map, or a ticked test."""
        return bool(self.has_current_map or self._selected_tests())

    def _refresh_run_enabled(self, *_args):
        """Re-decide whether Run Benchmark is available.

        Called on connect and on every checkbox toggle, so the button always
        reflects what is actually selectable rather than what was true when the
        window opened.
        """
        if self.running or not self._connected:
            return
        self.run_button.setEnabled(self._can_run())

    def start(self):
        tests = self._selected_tests()

        if not self._can_run():
            QMessageBox.warning(
                self,
                "Nothing to benchmark",
                "There is no map loaded in Fio. Open “See tests” and tick at "
                "least one stress test, or load a map in Fio first."
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
                "flying_count", "team_counts", "aggro_count",
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
