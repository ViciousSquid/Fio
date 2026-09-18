"""Tools > Benchmark dialog.

Runs the same opt-in pytest benchmark used by the test suite in a child
process, keeping the editor responsive and ensuring the GUI reports the exact
same measurements as command-line benchmark runs.
"""

import os
import sys

from PyQt5.QtCore import QProcess, Qt
from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout


class BenchmarkDialog(QDialog):
    def __init__(self, root_dir, parent=None):
        super().__init__(parent)
        self.root_dir = os.path.abspath(root_dir)
        self.process = None

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        self.status_label = QLabel("Running renderer benchmark...")
        layout.addWidget(self.status_label)

        self.additional_tests = QCheckBox(
            "Additional stress tests (I/O, renderer, CSG)"
        )
        self.additional_tests.setToolTip(
            "Run deliberately heavy workloads for Fio's I/O dispatcher, renderer and CSG geometry."
        )
        layout.addWidget(self.additional_tests)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, monospace; }"
        )
        layout.addWidget(self.output)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.setEnabled(False)
        layout.addWidget(self.buttons)

        self._start()

    def _start(self):
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.root_dir)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)

        environment = self.process.processEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        if self.additional_tests.isChecked():
            environment.insert("FIO_FULLSCREEN_BENCH_ADDITIONAL", "1")
        self.process.setProcessEnvironment(environment)

        self.process.start(
            sys.executable,
            [
                "-m",
                "pytest",
                "tests/performance/test_fullscreen_resolution_benchmark.py",
                "--run-benchmarks",
                "-s",
                "-q",
            ],
        )

    def _read_output(self):
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if data:
            self.output.moveCursor(self.output.textCursor().End)
            self.output.insertPlainText(data)
            self.output.ensureCursorVisible()

    def _finished(self, exit_code, exit_status):
        self._read_output()
        if exit_code == 0:
            self.status_label.setText("Benchmark complete.")
        else:
            self.status_label.setText(
                "Benchmark finished with exit code %d." % exit_code
            )
        self.buttons.setEnabled(True)

    def _error(self, error):
        self._read_output()
        self.status_label.setText(
            "Could not start benchmark: %s" % error
        )
        self.buttons.setEnabled(True)

    def reject(self):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().reject()
