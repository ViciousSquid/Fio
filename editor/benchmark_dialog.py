"""Compatibility shim for the pre-external benchmark dialog.

Tools > Benchmark now launches `benchmark_manager.py` in a separate
process. This class is retained only for third-party code that still imports
the old module name.
"""

from PyQt5.QtWidgets import QDialog


class BenchmarkDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("Fio Benchmark")
        self.setModal(False)

    def _start(self):
        return self.main_window.run_benchmark()

    def _tick(self):
        return None

    def _export_results(self):
        return None
