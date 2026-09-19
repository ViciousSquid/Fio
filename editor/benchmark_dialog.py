"""Tools > Benchmark dialog UI."""

import os
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton,
    QVBoxLayout, QTextBrowser, QToolButton, QWidget, QScrollArea, QProgressBar,
)

class BenchmarkDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.root_dir = os.path.abspath(main_window.root_dir)
        from .benchmark_runner import BenchmarkRunner
        self.runner = BenchmarkRunner(self)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)

        self.setWindowTitle("Fio Benchmark")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        
        benchmark_description = QLabel(
            'Click <span style="color: #2e9d4d;">run benchmark</span> to analyse the currently loaded map<br>'
            'or choose a stress-test from below to benchmark this<br>'
            'Fio installation against another one.'
        )
        # Use a real QFont point size rather than CSS px. Qt point sizes are
        # logical/font metrics and therefore participate in the same High-DPI
        # scaling used by the rest of the editor (e.g. the Debug Console).
        # CSS pixel sizes here stay effectively fixed on high-DPI displays.
        description_font = benchmark_description.font()
        description_font.setPointSize(11)
        benchmark_description.setFont(description_font)
        layout.addWidget(benchmark_description)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.throbber = QProgressBar()
        self.throbber.setRange(0, 0)
        self.throbber.setTextVisible(False)
        self.throbber.setFixedHeight(6)
        self.throbber.setVisible(False)
        self.throbber.setStyleSheet(
            "QProgressBar { border: 0; background: #292929; border-radius: 3px; }"
            "QProgressBar::chunk { background: #63d471; border-radius: 3px; }"
        )
        layout.addWidget(self.throbber)

        stress_toggle = QToolButton()
        stress_toggle.setText("See tests")
        stress_toggle.setCheckable(True)
        stress_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        stress_toggle.setArrowType(Qt.DownArrow)
        stress_toggle.setAutoRaise(True)
        layout.addWidget(stress_toggle)

        def update_stress_arrow(expanded):
            stress_toggle.setArrowType(Qt.UpArrow if expanded else Qt.DownArrow)

        stress_toggle.toggled.connect(update_stress_arrow)

        self.stress_options = QWidget()
        stress_layout = QVBoxLayout(self.stress_options)
        stress_layout.setContentsMargins(12, 0, 0, 0)

        self.additional_tests = QCheckBox(
            "Additional stress tests (I/O, renderer, gameplay)"
        )
        self.additional_tests.setToolTip(
            "Run deliberately heavy workloads against the live Fio renderer, procedural world and I/O system."
        )
        stress_layout.addWidget(self.additional_tests)

        self.brush_1000 = QCheckBox("Renderer scene: 1,000 brushes")
        self.brush_10000 = QCheckBox("Renderer scene: 10,000 brushes")
        self.brush_100000 = QCheckBox("Renderer scene: 100,000 brushes")
        self.io_chain_1000 = QCheckBox("I/O chain: 1,000 entities")
        self.monsters_50 = QCheckBox("Procedural room: 50 monsters")
        self.monsters_100 = QCheckBox("Procedural room: 100 monsters")
        self.monsters_500 = QCheckBox("Procedural room: 500 monsters")
        self.monsters_1000 = QCheckBox("Procedural room: 1,000 monsters")
        self.monster_apocalypse = QCheckBox("FINAL TEST: maximum procedural monster apocalypse (1000 monsters + 1000 relays)")
        self.borderless_window = QCheckBox("Window mode: borderless maximized")
        self.fullscreen_window = QCheckBox("Window mode: true fullscreen")
        self.editor_windowed_1280 = QCheckBox("Editor mode: windowed 1280×720 (3D view pane)")
        self.editor_windowed_1920 = QCheckBox("Editor mode: windowed 1920×1080 (3D view pane)")
        for checkbox in (
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
        ):
            checkbox.setToolTip(
                "Run this deliberately large workload in addition to the standard stress tests."
            )
            stress_layout.addWidget(checkbox)

        self.stress_options.setVisible(False)
        stress_scroll = QScrollArea()
        stress_scroll.setWidgetResizable(True)
        stress_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        stress_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        stress_scroll.setMaximumHeight(220)
        stress_scroll.setWidget(self.stress_options)
        stress_toggle.toggled.connect(stress_scroll.setVisible)
        stress_scroll.setVisible(False)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(self._select_all_stress_tests)
        self.select_all_button.setVisible(False)
        stress_toggle.toggled.connect(self.select_all_button.setVisible)
        layout.addWidget(self.select_all_button)
        layout.addWidget(stress_scroll)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setStyleSheet(
            "QTextBrowser { font-family: Consolas, monospace; background: #171717; border: 1px solid #444; }"
        )
        layout.addWidget(self.output, 1)

        self.export_button = QPushButton("Export HTML Report…")
        self.export_button.setEnabled(False)
        self.export_button.setVisible(False)
        self.export_button.clicked.connect(self._export_results)
        layout.addWidget(self.export_button)

        self.run_button = QPushButton("Run Benchmark")
        self.run_button.setStyleSheet(
            "QPushButton { background: #2e9d4d; color: white; font-weight: bold; "
            "border: 1px solid #3fbd63; padding: 7px 16px; border-radius: 3px; }"
            "QPushButton:hover { background: #39b85b; }"
            "QPushButton:pressed { background: #257f3e; }"
            "QPushButton:disabled { background: #3b5a42; color: #b8c4ba; border-color: #49634f; }"
        )
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


    def _select_all_stress_tests(self):
        for checkbox in (self.additional_tests, self.brush_1000, self.brush_10000, self.brush_100000, self.io_chain_1000, self.monsters_50, self.monsters_100, self.monsters_500, self.monsters_1000, self.monster_apocalypse, self.borderless_window, self.fullscreen_window, self.editor_windowed_1280, self.editor_windowed_1920):
            checkbox.setChecked(True)
    

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
            self.borderless_window,
            self.fullscreen_window,
            self.editor_windowed_1280,
            self.editor_windowed_1920,
        ):
            checkbox.setEnabled(enabled)
    

    def _append(self, text):
        self.output.append(text)
        self.output.ensureCursorVisible()
        QApplication.processEvents()
    

    def _append_test_separator(self, label):
        self.output.append('<div style="border-top:2px solid #ff8a00; margin:14px 0 8px 0; padding-top:8px;"><span style="color:#ffb15a; font-weight:bold;">TEST: %s</span></div>' % label)
    

    def _start(self):
        return self.runner._start()

    def _tick(self):
        return self.runner._tick()

    def _export_results(self):
        return self.runner.results._export_results()
