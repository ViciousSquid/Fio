"""Tools > Benchmark dialog.

Runs Fio's standalone performance benchmark in a child process. The benchmark
uses Fio runtime APIs directly and deliberately does not depend on pytest.
"""

import os
import sys

from PyQt5.QtCore import QProcess
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout


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
    def __init__(self, root_dir, parent=None):
        super().__init__(parent)
        self.root_dir = os.path.abspath(root_dir)
        self.process = None

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        self.environment_label = QLabel(_execution_environment())
        self.environment_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.environment_label)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.additional_tests = QCheckBox(
            "Additional stress tests (I/O, renderer, CSG)"
        )
        self.additional_tests.setToolTip(
            "Run deliberately heavy workloads for Fio's I/O dispatcher, renderer and CSG geometry."
        )
        layout.addWidget(self.additional_tests)

        self.brush_1000 = QCheckBox("Renderer scene: 1,000 brushes")
        self.brush_10000 = QCheckBox("Renderer scene: 10,000 brushes")
        self.brush_100000 = QCheckBox("Renderer scene: 100,000 brushes")
        self.io_chain_1000 = QCheckBox("I/O chain: 1,000 entities")
        self.monsters_100 = QCheckBox("Procedural room: 100 monsters")
        self.monsters_500 = QCheckBox("Procedural room: 500 monsters")
        self.monsters_1000 = QCheckBox("Procedural room: 1,000 monsters")
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
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

    def _start(self):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            return
        self.output.clear()
        self.status_label.setText("Running renderer benchmark...")
        self.run_button.setEnabled(False)
        self.additional_tests.setEnabled(False)
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
        ):
            checkbox.setEnabled(False)
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.root_dir)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)

        environment = self.process.processEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")

        brush_counts = []
        if self.brush_1000.isChecked():
            brush_counts.append("1000")
        if self.brush_10000.isChecked():
            brush_counts.append("10000")
        if self.brush_100000.isChecked():
            brush_counts.append("100000")
        if brush_counts:
            environment.insert(
                "FIO_FULLSCREEN_BENCH_BRUSH_STRESS", ",".join(brush_counts)
            )

        if self.io_chain_1000.isChecked():
            environment.insert("FIO_FULLSCREEN_BENCH_IO_CHAIN", "1000")

        monster_counts = []
        if self.monsters_100.isChecked():
            monster_counts.append("100")
        if self.monsters_500.isChecked():
            monster_counts.append("500")
        if self.monsters_1000.isChecked():
            monster_counts.append("1000")
        if monster_counts:
            environment.insert(
                "FIO_FULLSCREEN_BENCH_MONSTERS", ",".join(monster_counts)
            )

        if (self.additional_tests.isChecked() or brush_counts
                or self.io_chain_1000.isChecked() or monster_counts):
            environment.insert("FIO_FULLSCREEN_BENCH_ADDITIONAL", "1")

        self.process.setProcessEnvironment(environment)

        self.process.start(
            sys.executable,
            ["tests/performance/fio_benchmark.py"],
        )

    def _read_output(self):
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if data:
            self.output.moveCursor(QTextCursor.End)
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
        self.run_button.setEnabled(True)
        self.additional_tests.setEnabled(True)
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
        ):
            checkbox.setEnabled(True)

    def _error(self, error):
        self._read_output()
        self.status_label.setText(
            "Could not start benchmark: %s" % error
        )
        self.run_button.setEnabled(True)
        self.additional_tests.setEnabled(True)
        for checkbox in (
            self.brush_1000,
            self.brush_10000,
            self.brush_100000,
            self.io_chain_1000,
            self.monsters_100,
            self.monsters_500,
            self.monsters_1000,
        ):
            checkbox.setEnabled(True)

    def reject(self):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().reject()
