import os
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QDoubleSpinBox,
    QComboBox, QPushButton, QGroupBox, QFormLayout, QSlider, QCheckBox,
    QTabWidget, QWidget, QFrame, QGridLayout, QScrollArea, QSizePolicy,
    QColorDialog, QMessageBox, QProgressDialog, QApplication
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QPainter, QLinearGradient, QPen

from engine.terrain import (Terrain, BIOMES, DEFAULT_BIOME, DEFAULT_USE_TEXTURES,
                            DEFAULT_GRASS_ENABLED,
                            biome_key_for_name)
from engine import terrain_style


class GradientPreview(QWidget):
    """Widget to preview terrain color gradient."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(50)
        self.setMaximumHeight(50)
        self.colors = []
    
    def set_colors(self, colors):
        """Set colors list: [(height, (r,g,b)), ...]"""
        self.colors = colors
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        rect = self.rect()
        
        if not self.colors:
            painter.fillRect(rect, QColor(128, 128, 128))
            return
        
        # Draw gradient
        gradient = QLinearGradient(0, 0, rect.width(), 0)
        for height, (r, g, b) in self.colors:
            gradient.setColorAt(height, QColor(int(r*255), int(g*255), int(b*255)))
        
        painter.fillRect(rect, gradient)
        
        # Draw border
        painter.setPen(QPen(QColor(80, 80, 80), 2))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))


class TerrainEditorPanel(QWidget):
    """Embeddable terrain editing panel.

    Lives in the Properties dock (bottom-left pane) as an overlay, the same
    way the procedural map generator does, rather than in a floating window.
    """

    # Signals
    terrain_changed = pyqtSignal()
    terrain_generated = pyqtSignal()

    def __init__(self, terrain: Terrain, parent=None):
        super().__init__(parent)
        self.terrain = terrain
        self.editor = parent

        self.setObjectName("TerrainEditorPanel")
        self._building_ui = False

        # MainWindow applies Display.font_size to QApplication before opening
        # this panel. Use that configured point size rather than hard-coded
        # pixel font sizes so the terrain editor follows Fio's global font and
        # Qt's high-DPI text scaling.
        app_font = QApplication.font()
        self._base_font_size = app_font.pointSize()
        if self._base_font_size <= 0:
            self._base_font_size = 11
        self.setFont(app_font)

        # Apply global stylesheet
        terrain_style = """
            QWidget#TerrainEditorPanel {
                background-color: #2b2b2b;
                color: #f0f0f0;
            }
            QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {
                padding: 5px;
                min-height: 30px;
                background-color: #444;
                color: #f0f0f0;
                border: 1px solid #666;
            }
            QCheckBox {
                spacing: 10px;
            }
            QCheckBox::indicator:checked {
                background-color: #F08000;
                border: 2px solid #333;
                border-radius: 3px;
            }
            QCheckBox::indicator:unchecked {
                background-color: #555;
                border: 2px solid #333;
                border-radius: 3px;
            }
            QCheckBox::indicator {
                width: 24px;
                height: 24px;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #555;
                border-radius: 6px;
                margin-top: 18px;
                padding-top: 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                top: 2px;
                padding: 0 8px;
                background-color: #2d3d3b;
                color: #F08000;
                font-size: __GROUP_TITLE_FONT__pt;
            }
            QPushButton {
                padding: 8px 14px;
                min-height: 34px;
                background-color: #555;
                color: #f0f0f0;
                border: 1px solid #666;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #6a6a6a;
            }
            QPushButton:pressed {
                background-color: #F08000;
            }
            QTabBar::tab:selected { 
                background: #F08000; 
                color: white; 
                font-weight: bold;
            }
            QTabBar::tab { 
                background: #425f5d; 
                color: #ccc; 
                padding: 10px 18px; 
                min-width: 70px;
            }
            QTabBar::tab:hover { 
                background: #5a7a82; 
            }
            QTabWidget::pane {
                border: 2px solid #555;
                border-radius: 6px;
                padding: 8px;
            }
            QLabel {
                color: #f0f0f0;
            }
        """
        self.setStyleSheet(
            terrain_style
            .replace("__GROUP_TITLE_FONT__", str(self._base_font_size + 3))
        )
        
        self.paint_tool_mode = 'sculpt'
        self._sculpt_mode = 'raise'

        self.setup_ui()

        # Defer terrain-state population until the panel has been installed
        # in the Properties dock. During construction the widget hierarchy is
        # still being reparented by MainWindow._show_overlay(); touching child
        # widgets at that point can leave PyQt wrappers pointing at deleted
        # native controls.
        QTimer.singleShot(0, self.load_from_terrain)
    
    def setup_ui(self):
        """Build the UI."""
        self._building_ui = True
        
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(12, 12, 12, 12)
        
        # Top action row: Regenerate / Reset / Close. Mirrors the procedural
        # map generator's button row so terrain editing lives in the dock
        # instead of a floating window, with Regenerate at the top.
        top_button_layout = QHBoxLayout()
        top_button_layout.setSpacing(10)

        regenerate_btn = QPushButton("🔄 Regenerate")
        regenerate_btn.setStyleSheet("""
            QPushButton {
                background-color: #F08000;
                color: white;
                font-weight: bold;
                padding: 12px 20px;
            }
            QPushButton:hover {
                background-color: #FF9020;
            }
        """)
        regenerate_btn.clicked.connect(self.regenerate_terrain)
        top_button_layout.addWidget(regenerate_btn, 2)

        reset_btn = QPushButton("Reset Defaults")
        reset_btn.clicked.connect(self.reset_to_defaults)
        top_button_layout.addWidget(reset_btn, 1)

        close_btn = QPushButton("✕ Close")
        close_btn.clicked.connect(self.request_close)
        top_button_layout.addWidget(close_btn, 1)

        main_layout.addLayout(top_button_layout)

        # Everything below the fixed action row lives in a vertical scroll area
        # so the panel keeps its size in the dock and scrolls instead of forcing
        # the pane larger when the tabs need more room.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.verticalScrollBar().setStyleSheet("""
            QScrollBar:vertical {
                width: 26px;
                background: #222;
                border: none;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #555;
                min-height: 28px;
                border-radius: 5px;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # Top controls row (Textures, Wireframe, Solid, Flat)
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(20)
        
        self.textures_checkbox = QCheckBox("Use Textures")
        self.textures_checkbox.setChecked(DEFAULT_USE_TEXTURES)
        self.textures_checkbox.toggled.connect(self.on_textures_changed)
        controls_layout.addWidget(self.textures_checkbox)
        
        self.wireframe_checkbox = QCheckBox("Wireframe")
        self.wireframe_checkbox.setChecked(self.terrain.wireframe)
        self.wireframe_checkbox.toggled.connect(self.on_wireframe_changed)
        controls_layout.addWidget(self.wireframe_checkbox)
        
        # Flat Mode Checkbox
        self.flat_checkbox = QCheckBox("Flat Mode")
        self.flat_checkbox.setChecked(self.terrain.flat_mode)
        self.flat_checkbox.setToolTip("Disable height and colors (Greyscale Flat)")
        self.flat_checkbox.toggled.connect(self.on_flat_changed)
        controls_layout.addWidget(self.flat_checkbox)
        
        self.solid_checkbox = QCheckBox("Solid")
        self.solid_checkbox.setChecked(self.terrain.solid)
        self.solid_checkbox.setToolTip("Enable collision - player can walk on terrain")
        self.solid_checkbox.toggled.connect(self.on_solid_changed)
        controls_layout.addWidget(self.solid_checkbox)
        
        controls_layout.addStretch()
        content_layout.addLayout(controls_layout)

        # Tab widget
        # Keep the tab widget as an explicit Python-owned reference as well as
        # a child of the content layout. This prevents PyQt from dropping the
        # wrapper during panel construction, which can otherwise destroy the
        # native QComboBox children before load_from_terrain() runs.
        self._terrain_tabs = QTabWidget()
        tabs = self._terrain_tabs
        
        # === BIOME TAB ===
        biome_tab = QWidget()
        biome_layout = QVBoxLayout(biome_tab)
        biome_layout.setSpacing(12)
        biome_layout.setContentsMargins(8, 8, 8, 8)
        
        # Biome selection
        biome_group = QGroupBox("Biome Preset")
        biome_group_layout = QVBoxLayout(biome_group)
        biome_group_layout.setSpacing(10)
        biome_group_layout.setContentsMargins(12, 20, 12, 12)
        
        self.biome_combo = QComboBox()
        self.biome_combo.setMinimumHeight(36)
        for name, biome in BIOMES.items():
            self.biome_combo.addItem(biome.name, name)
        self.biome_combo.currentIndexChanged.connect(self.on_biome_changed)
        biome_group_layout.addWidget(self.biome_combo)
        
        self.gradient_preview = GradientPreview()
        biome_group_layout.addWidget(self.gradient_preview)
        
        biome_group.setLayout(biome_group_layout)
        biome_layout.addWidget(biome_group)
        
        # Height controls
        height_group = QGroupBox("Height Settings")
        height_layout = QFormLayout(height_group)
        height_layout.setSpacing(10)
        height_layout.setContentsMargins(12, 20, 12, 12)
        
        self.base_height_spin = QDoubleSpinBox()
        self.base_height_spin.setRange(-500, 500)
        self.base_height_spin.setSingleStep(5)
        self.base_height_spin.valueChanged.connect(self.on_height_changed)
        height_layout.addRow("Base Height:", self.base_height_spin)
        
        self.height_scale_spin = QDoubleSpinBox()
        self.height_scale_spin.setRange(10, 500)
        self.height_scale_spin.setSingleStep(10)
        self.height_scale_spin.valueChanged.connect(self.on_height_changed)
        height_layout.addRow("Height Scale:", self.height_scale_spin)
        
        height_group.setLayout(height_layout)
        biome_layout.addWidget(height_group)
        
        # Seed controls
        seed_group = QGroupBox("Random Seed")
        seed_layout = QHBoxLayout(seed_group)
        seed_layout.setSpacing(10)
        seed_layout.setContentsMargins(12, 20, 12, 12)
        
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 999999)
        self.seed_spin.valueChanged.connect(self.on_seed_changed)
        seed_layout.addWidget(self.seed_spin)
        
        randomize_btn = QPushButton("🎲 Randomize")
        randomize_btn.clicked.connect(self.randomize_seed)
        seed_layout.addWidget(randomize_btn)
        
        seed_group.setLayout(seed_layout)
        biome_layout.addWidget(seed_group)
        
        biome_layout.addStretch()
        tabs.addTab(biome_tab, "Biome")

        # === APPEARANCE TAB ===
        self._build_appearance_tab(tabs)
        
        # === FEATURES TAB ===
        # The terrain editor already has one vertical scroll area around all
        # tab content. Keep Features as a normal tab page so it does not create
        # a nested vertical scrollbar inside the Properties dock.
        features_tab = QWidget()
        features_layout = QVBoxLayout(features_tab)
        features_layout.setSpacing(12)
        features_layout.setContentsMargins(8, 8, 8, 8)
        
        # Rolling Hills
        hills_group = QGroupBox("Rolling Hills (Base Layer)")
        hills_layout = QFormLayout(hills_group)
        hills_layout.setSpacing(8)
        hills_layout.setContentsMargins(12, 20, 12, 12)
        
        hills_info = QLabel("Smooth, gentle undulations - the foundation of the terrain")
        hills_info.setStyleSheet("color: #aaa; font-style: italic;")
        hills_info.setWordWrap(True)
        hills_layout.addRow(hills_info)
        
        self.hills_scale_spin = QDoubleSpinBox()
        self.hills_scale_spin.setRange(0.001, 0.05)
        self.hills_scale_spin.setSingleStep(0.001)
        self.hills_scale_spin.setDecimals(4)
        self.hills_scale_spin.valueChanged.connect(self.on_feature_changed)
        hills_layout.addRow("Scale:", self.hills_scale_spin)
        
        self.hills_intensity_spin = QDoubleSpinBox()
        self.hills_intensity_spin.setRange(0.0, 1.5)
        self.hills_intensity_spin.setSingleStep(0.1)
        self.hills_intensity_spin.valueChanged.connect(self.on_feature_changed)
        hills_layout.addRow("Intensity:", self.hills_intensity_spin)
        
        hills_group.setLayout(hills_layout)
        features_layout.addWidget(hills_group)
        
        # Mountains
        mountains_group = QGroupBox("Mountains")
        mountains_layout = QFormLayout(mountains_group)
        mountains_layout.setSpacing(8)
        mountains_layout.setContentsMargins(12, 20, 12, 12)
        
        self.mountains_enabled_check = QCheckBox("Enable Mountains")
        self.mountains_enabled_check.toggled.connect(self.on_feature_changed)
        mountains_layout.addRow(self.mountains_enabled_check)
        
        self.mountains_scale_spin = QDoubleSpinBox()
        self.mountains_scale_spin.setRange(0.001, 0.05)
        self.mountains_scale_spin.setSingleStep(0.001)
        self.mountains_scale_spin.setDecimals(4)
        self.mountains_scale_spin.valueChanged.connect(self.on_feature_changed)
        mountains_layout.addRow("Scale:", self.mountains_scale_spin)
        
        self.mountains_intensity_spin = QDoubleSpinBox()
        self.mountains_intensity_spin.setRange(0.0, 2.0)
        self.mountains_intensity_spin.setSingleStep(0.1)
        self.mountains_intensity_spin.valueChanged.connect(self.on_feature_changed)
        mountains_layout.addRow("Intensity:", self.mountains_intensity_spin)
        
        self.mountains_sharpness_spin = QDoubleSpinBox()
        self.mountains_sharpness_spin.setRange(0.0, 1.0)
        self.mountains_sharpness_spin.setSingleStep(0.1)
        self.mountains_sharpness_spin.valueChanged.connect(self.on_feature_changed)
        mountains_layout.addRow("Sharpness:", self.mountains_sharpness_spin)
        
        mountains_group.setLayout(mountains_layout)
        features_layout.addWidget(mountains_group)
        
        # Valleys
        valleys_group = QGroupBox("Valleys")
        valleys_layout = QFormLayout(valleys_group)
        valleys_layout.setSpacing(8)
        valleys_layout.setContentsMargins(12, 20, 12, 12)
        
        self.valleys_enabled_check = QCheckBox("Enable Valleys")
        self.valleys_enabled_check.toggled.connect(self.on_feature_changed)
        valleys_layout.addRow(self.valleys_enabled_check)
        
        self.valleys_scale_spin = QDoubleSpinBox()
        self.valleys_scale_spin.setRange(0.001, 0.02)
        self.valleys_scale_spin.setSingleStep(0.0005)
        self.valleys_scale_spin.setDecimals(4)
        self.valleys_scale_spin.valueChanged.connect(self.on_feature_changed)
        valleys_layout.addRow("Scale:", self.valleys_scale_spin)
        
        self.valleys_depth_spin = QDoubleSpinBox()
        self.valleys_depth_spin.setRange(0.0, 1.0)
        self.valleys_depth_spin.setSingleStep(0.05)
        self.valleys_depth_spin.valueChanged.connect(self.on_feature_changed)
        valleys_layout.addRow("Depth:", self.valleys_depth_spin)
        
        valleys_group.setLayout(valleys_layout)
        features_layout.addWidget(valleys_group)
        
        # Plateaus
        plateaus_group = QGroupBox("Plateaus / Mesas")
        plateaus_layout = QFormLayout(plateaus_group)
        plateaus_layout.setSpacing(8)
        plateaus_layout.setContentsMargins(12, 20, 12, 12)
        
        self.plateaus_enabled_check = QCheckBox("Enable Plateaus")
        self.plateaus_enabled_check.toggled.connect(self.on_feature_changed)
        plateaus_layout.addRow(self.plateaus_enabled_check)
        
        self.plateaus_scale_spin = QDoubleSpinBox()
        self.plateaus_scale_spin.setRange(0.001, 0.02)
        self.plateaus_scale_spin.setSingleStep(0.0005)
        self.plateaus_scale_spin.setDecimals(4)
        self.plateaus_scale_spin.valueChanged.connect(self.on_feature_changed)
        plateaus_layout.addRow("Scale:", self.plateaus_scale_spin)
        
        self.plateaus_intensity_spin = QDoubleSpinBox()
        self.plateaus_intensity_spin.setRange(0.0, 1.5)
        self.plateaus_intensity_spin.setSingleStep(0.1)
        self.plateaus_intensity_spin.valueChanged.connect(self.on_feature_changed)
        plateaus_layout.addRow("Intensity:", self.plateaus_intensity_spin)
        
        self.plateaus_flatness_spin = QDoubleSpinBox()
        self.plateaus_flatness_spin.setRange(0.0, 1.0)
        self.plateaus_flatness_spin.setSingleStep(0.1)
        self.plateaus_flatness_spin.valueChanged.connect(self.on_feature_changed)
        plateaus_layout.addRow("Flatness:", self.plateaus_flatness_spin)
        
        plateaus_group.setLayout(plateaus_layout)
        features_layout.addWidget(plateaus_group)


        # Grass
        grass_group = QGroupBox("Grass")
        grass_layout = QFormLayout(grass_group)
        grass_layout.setSpacing(8)
        grass_layout.setContentsMargins(12, 20, 12, 12)

        self.grass_checkbox = QCheckBox("Enable Grass")
        self.grass_checkbox.toggled.connect(self.on_grass_changed)
        grass_layout.addRow(self.grass_checkbox)

        self.grass_density_slider = QSlider(Qt.Horizontal)
        self.grass_density_slider.setRange(0, 100)
        self.grass_density_slider.setSingleStep(1)
        self.grass_density_slider.valueChanged.connect(self.on_grass_density_changed)
        self.grass_density_value = QLabel("20%")
        density_row = QHBoxLayout()
        density_row.addWidget(self.grass_density_slider, 1)
        density_row.addWidget(self.grass_density_value)
        grass_layout.addRow("Density:", density_row)

        self.grass_color_btn = QPushButton("Blade Colour")
        self.grass_color_btn.clicked.connect(self.choose_grass_color)
        self.grass_color_preview = QFrame()
        self.grass_color_preview.setFixedSize(28, 28)
        color_row = QHBoxLayout()
        color_row.addWidget(self.grass_color_btn)
        color_row.addWidget(self.grass_color_preview)
        self.grass_ground_label = QLabel("Matches the ground")
        self.grass_ground_label.setStyleSheet("color: #aaa; font-style: italic;")
        color_row.addWidget(self.grass_ground_label)
        self.grass_ground_btn = QPushButton("Match Ground")
        self.grass_ground_btn.setToolTip(
            "Give each blade the colour of the terrain it grows on")
        self.grass_ground_btn.clicked.connect(self.match_grass_to_ground)
        color_row.addWidget(self.grass_ground_btn)
        color_row.addStretch()
        grass_layout.addRow("Colour:", color_row)

        # The blades fade from the blade colour to this at their tips.
        self.grass_tip_btn = QPushButton("Tip Colour")
        self.grass_tip_btn.clicked.connect(self.choose_grass_tip_color)
        self.grass_tip_preview = QFrame()
        self.grass_tip_preview.setFixedSize(28, 28)
        self.grass_tip_auto_btn = QPushButton("Auto")
        self.grass_tip_auto_btn.setToolTip(
            "Derive the tips from the blade colour (a sun-bleached shade)")
        self.grass_tip_auto_btn.clicked.connect(self.reset_grass_tip_color)
        tip_row = QHBoxLayout()
        tip_row.addWidget(self.grass_tip_btn)
        tip_row.addWidget(self.grass_tip_preview)
        tip_row.addWidget(self.grass_tip_auto_btn)
        tip_row.addStretch()
        grass_layout.addRow("Tips:", tip_row)

        # Where the grass grows, as % of the terrain's height: by default the
        # grass texture layer (so no grass on high rock/snow or low sand).
        self.grass_height_sliders = []
        for label in ("Lowest:", "Highest:"):
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            value_label = QLabel("0%")
            value_label.setMinimumWidth(36)
            slider.valueChanged.connect(self.on_grass_height_changed)
            row = QHBoxLayout()
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            grass_layout.addRow(label, row)
            self.grass_height_sliders.append((slider, value_label))
        self.grass_height_sliders[0][0].setToolTip(
            "Lowest height grass grows at (% of the terrain's height range)")
        self.grass_height_sliders[1][0].setToolTip(
            "Highest height grass grows at (% of the terrain's height range)")
        self.grass_follow_btn = QPushButton("Follow Texture Layers")
        self.grass_follow_btn.setToolTip(
            "Grow grass exactly where the grass texture layer is")
        self.grass_follow_btn.clicked.connect(self.reset_grass_height_range)
        grass_layout.addRow(self.grass_follow_btn)

        # Grass is the feature people reach for most, so it heads the tab.
        features_layout.insertWidget(0, grass_group)

        features_layout.addStretch()
        tabs.addTab(features_tab, "Features")
        
        # === SIZE TAB ===
        size_tab = QWidget()
        size_layout = QVBoxLayout(size_tab)
        size_layout.setSpacing(12)
        size_layout.setContentsMargins(8, 8, 8, 8)
        
        # Chunk size / resolution
        chunk_group = QGroupBox("Triangle Size")
        chunk_layout = QFormLayout(chunk_group)
        chunk_layout.setSpacing(10)
        chunk_layout.setContentsMargins(12, 20, 12, 12)
        
        lowpoly_info = QLabel("Lower values = bigger triangles = chunkier low-poly look")
        lowpoly_info.setStyleSheet("color: #aaa; font-style: italic;")
        lowpoly_info.setWordWrap(True)
        chunk_layout.addRow(lowpoly_info)
        
        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(4, 64)
        self.chunk_size_spin.setSingleStep(4)
        self.chunk_size_spin.valueChanged.connect(self.on_size_changed)
        chunk_layout.addRow("Vertices per Chunk:", self.chunk_size_spin)
        
        # Resolution presets
        res_preset_layout = QHBoxLayout()
        res_presets = [
            ("Very Chunky", 8),
            ("Chunky", 12),
            ("Medium", 20),
            ("Smooth", 32),
        ]
        
        for label, value in res_presets:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, v=value: self.chunk_size_spin.setValue(v))
            res_preset_layout.addWidget(btn)
        
        chunk_layout.addRow("Presets:", res_preset_layout)
        
        chunk_group.setLayout(chunk_layout)
        size_layout.addWidget(chunk_group)
        
        # World size (chunk count)
        bounds_group = QGroupBox("World Size (Chunks)")
        bounds_layout = QFormLayout(bounds_group)
        bounds_layout.setSpacing(10)
        bounds_layout.setContentsMargins(12, 20, 12, 12)
        
        self.min_x_spin = QSpinBox()
        self.min_x_spin.setRange(-20, 20)
        self.min_x_spin.valueChanged.connect(self.on_bounds_changed)
        bounds_layout.addRow("Min X:", self.min_x_spin)
        
        self.max_x_spin = QSpinBox()
        self.max_x_spin.setRange(-20, 20)
        self.max_x_spin.valueChanged.connect(self.on_bounds_changed)
        bounds_layout.addRow("Max X:", self.max_x_spin)
        
        self.min_z_spin = QSpinBox()
        self.min_z_spin.setRange(-20, 20)
        self.min_z_spin.valueChanged.connect(self.on_bounds_changed)
        bounds_layout.addRow("Min Z:", self.min_z_spin)
        
        self.max_z_spin = QSpinBox()
        self.max_z_spin.setRange(-20, 20)
        self.max_z_spin.valueChanged.connect(self.on_bounds_changed)
        bounds_layout.addRow("Max Z:", self.max_z_spin)
        
        bounds_group.setLayout(bounds_layout)
        size_layout.addWidget(bounds_group)
        
        # Size presets
        preset_group = QGroupBox("Size Presets")
        preset_layout = QGridLayout(preset_group)
        preset_layout.setSpacing(8)
        preset_layout.setContentsMargins(12, 20, 12, 12)
        
        size_presets = [
            ("Tiny (1×1)", (-0, 0)),
            ("Small (3×3)", (-1, 1)),
            ("Medium (5×5)", (-2, 2)),
            ("Large (7×7)", (-3, 3)),
            ("Huge (11×11)", (-5, 5)),
        ]
        
        self._size_preset_btns = []
        for i, (label, bounds) in enumerate(size_presets):
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, b=bounds: self.apply_size_preset(b))
            preset_layout.addWidget(btn, i // 3, i % 3)
            self._size_preset_btns.append(btn)

        preset_group.setLayout(preset_layout)
        size_layout.addWidget(preset_group)

        # Shown only while Big World "Fill world with terrain" owns the world
        # size; the manual bounds/presets above are disabled to avoid a conflict.
        self._bigworld_size_note = QLabel(
            "🌍 Size is managed by Big World “Fill world with terrain”.\n"
            "Turn that option off on the Big World Settings entity to set bounds "
            "manually. Biome, sculpting, seed and height stay fully editable.")
        self._bigworld_size_note.setWordWrap(True)
        self._bigworld_size_note.setStyleSheet(
            "QLabel { background-color: #2a2340; color: #cbb8f0; padding: 10px;"
            " border: 1px solid #6a5aa0; border-radius: 6px; }")
        self._bigworld_size_note.setVisible(False)
        size_layout.addWidget(self._bigworld_size_note)
        
        # Size info
        self.size_info_label = QLabel()
        self.size_info_label.setStyleSheet("""
            QLabel {
                background-color: #2a3a38;
                padding: 12px;
                border-radius: 6px;
            }
        """)
        size_layout.addWidget(self.size_info_label)
        
        size_layout.addStretch()
        tabs.addTab(size_tab, "Size")

        # === SCALE TAB (NEW) ===
        scale_tab = QWidget()
        scale_layout = QVBoxLayout(scale_tab)
        scale_layout.setSpacing(12)
        scale_layout.setContentsMargins(8, 8, 8, 8)

        # Physical Scale Group
        scale_group = QGroupBox("Physical Mesh Scale (Chunk Size)")
        scale_group_layout = QVBoxLayout(scale_group)
        scale_group_layout.setSpacing(10)
        scale_group_layout.setContentsMargins(12, 20, 12, 12)

        scale_info = QLabel("Uniformly scales the physical terrain. X, Y and Z stay proportional; 1x = 256 units.")
        scale_info.setStyleSheet("color: #aaa; font-style: italic;")
        scale_info.setWordWrap(True)
        scale_group_layout.addWidget(scale_info)

        scale_grid = QGridLayout()
        scale_options = [
            ("1x (Default)", 1.0),
            ("2x Larger", 2.0),
            ("4x Larger", 4.0),
            ("8x Larger", 8.0),
            ("16x Larger", 16.0),
        ]
        
        for i, (label, factor) in enumerate(scale_options):
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, f=factor: self.apply_mesh_scale(f))
            scale_grid.addWidget(btn, i // 2, i % 2)
        
        scale_group_layout.addLayout(scale_grid)
        scale_group.setLayout(scale_group_layout)
        scale_layout.addWidget(scale_group)

        # Tiling Scale Group
        tiling_group = QGroupBox("Tiling Scale (World Extent)")
        tiling_layout = QVBoxLayout(tiling_group)
        tiling_layout.setSpacing(10)
        tiling_layout.setContentsMargins(12, 20, 12, 12)

        tiling_info = QLabel("Multiplies the number of chunks to cover a larger area.")
        tiling_info.setStyleSheet("color: #aaa; font-style: italic;")
        tiling_info.setWordWrap(True)
        tiling_layout.addWidget(tiling_info)

        tiling_grid = QGridLayout()
        tiling_options = [
            ("2x Grid (Double)", 2),
            ("4x Grid (Quadruple)", 4),
            ("8x Grid (Massive)", 8),
            ("Reset Grid", 1),
        ]

        for i, (label, factor) in enumerate(tiling_options):
            btn = QPushButton(label)
            if factor == 1:
                btn.clicked.connect(lambda checked: self.apply_size_preset((-2, 2)))
            else:
                btn.clicked.connect(lambda checked, f=factor: self.apply_tiling_scale(f))
            tiling_grid.addWidget(btn, i // 2, i % 2)

        tiling_layout.addLayout(tiling_grid)
        tiling_group.setLayout(tiling_layout)
        scale_layout.addWidget(tiling_group)

        scale_layout.addStretch()
        tabs.addTab(scale_tab, "Scale")
        
        # === POSITION TAB ===
        pos_tab = QWidget()
        pos_layout = QVBoxLayout(pos_tab)
        pos_layout.setSpacing(12)
        pos_layout.setContentsMargins(8, 8, 8, 8)
        
        offset_group = QGroupBox("World Offset")
        offset_layout = QFormLayout(offset_group)
        offset_layout.setSpacing(10)
        offset_layout.setContentsMargins(12, 20, 12, 12)
        
        self.x_offset_spin = QDoubleSpinBox()
        self.x_offset_spin.setRange(-10000, 10000)
        self.x_offset_spin.setSingleStep(50)
        self.x_offset_spin.valueChanged.connect(self.on_offset_changed)
        offset_layout.addRow("X Offset:", self.x_offset_spin)
        
        self.z_offset_spin = QDoubleSpinBox()
        self.z_offset_spin.setRange(-10000, 10000)
        self.z_offset_spin.setSingleStep(50)
        self.z_offset_spin.valueChanged.connect(self.on_offset_changed)
        offset_layout.addRow("Z Offset:", self.z_offset_spin)
        
        self.y_offset_spin = QDoubleSpinBox()
        self.y_offset_spin.setRange(-500, 500)
        self.y_offset_spin.setSingleStep(5)
        self.y_offset_spin.valueChanged.connect(self.on_offset_changed)
        offset_layout.addRow("Y Offset:", self.y_offset_spin)
        
        offset_group.setLayout(offset_layout)
        pos_layout.addWidget(offset_group)
        
        pos_layout.addStretch()
        tabs.addTab(pos_tab, "Position")

        # === HEIGHTMAP TAB ===
        hm_tab = QWidget()
        hm_layout = QVBoxLayout(hm_tab)
        hm_layout.setSpacing(12)
        hm_layout.setContentsMargins(8, 8, 8, 8)

        hm_load_group = QGroupBox("Heightmap Image")
        hm_load_layout = QVBoxLayout(hm_load_group)
        hm_load_layout.setSpacing(10)
        hm_load_layout.setContentsMargins(12, 20, 12, 12)

        hm_info = QLabel("Load a greyscale image to drive terrain height.\n"
                         "White = high, Black = low.")
        hm_info.setStyleSheet("color: #aaa; font-style: italic;")
        hm_info.setWordWrap(True)
        hm_load_layout.addWidget(hm_info)

        hm_btn_row = QHBoxLayout()
        load_hm_btn = QPushButton("📂 Load Image…")
        load_hm_btn.clicked.connect(self.load_heightmap_image)
        hm_btn_row.addWidget(load_hm_btn)

        clear_hm_btn = QPushButton("✕ Clear")
        clear_hm_btn.clicked.connect(self.clear_heightmap)
        hm_btn_row.addWidget(clear_hm_btn)
        hm_load_layout.addLayout(hm_btn_row)

        self.hm_status_label = QLabel("No heightmap loaded")
        self.hm_status_label.setStyleSheet("color: #F08000;")
        hm_load_layout.addWidget(self.hm_status_label)

        hm_load_group.setLayout(hm_load_layout)
        hm_layout.addWidget(hm_load_group)

        # Heightmap settings
        hm_settings_group = QGroupBox("Heightmap Settings")
        hm_settings_layout = QFormLayout(hm_settings_group)
        hm_settings_layout.setSpacing(10)
        hm_settings_layout.setContentsMargins(12, 20, 12, 12)

        self.hm_strength_spin = QDoubleSpinBox()
        self.hm_strength_spin.setRange(1, 2000)
        self.hm_strength_spin.setSingleStep(10)
        self.grass_checkbox.setChecked(getattr(self.terrain, 'grass_enabled', False))
        self.grass_density_slider.setValue(int(round(getattr(self.terrain, 'grass_density', 0.02) / 0.06 * 100.0)))
        self.grass_density_value.setText(f"{self.grass_density_slider.value()}%")
        self._update_grass_color_preview()

        self.hm_strength_spin.setValue(self.terrain.heightmap_strength)
        self.hm_strength_spin.valueChanged.connect(self.on_heightmap_settings_changed)
        hm_settings_layout.addRow("Strength:", self.hm_strength_spin)

        self.hm_blend_combo = QComboBox()
        self.hm_blend_combo.addItem("Additive", "additive")
        self.hm_blend_combo.addItem("Replace", "replace")
        idx = 0 if self.terrain.heightmap_blend == 'additive' else 1
        self.hm_blend_combo.setCurrentIndex(idx)
        self.hm_blend_combo.currentIndexChanged.connect(self.on_heightmap_settings_changed)
        hm_settings_layout.addRow("Blend Mode:", self.hm_blend_combo)

        hm_settings_group.setLayout(hm_settings_layout)
        hm_layout.addWidget(hm_settings_group)

        hm_layout.addStretch()
        tabs.addTab(hm_tab, "Heightmap")

        # === SCULPT / STAMP TAB ===
        sculpt_tab = QWidget()
        sculpt_tab.setMinimumWidth(320)
        sculpt_layout = QVBoxLayout(sculpt_tab)
        sculpt_layout.setSpacing(10)
        sculpt_layout.setContentsMargins(10, 10, 10, 10)

        # Shared brush controls: these stay visible in both modes.
        shared_group = QGroupBox("Brush")
        shared_layout = QVBoxLayout(shared_group)
        shared_layout.setSpacing(8)
        shared_layout.setContentsMargins(12, 20, 12, 12)

        mode_label = QLabel("Mode")
        mode_label.setStyleSheet("font-weight: bold; color: #ddd;")
        shared_layout.addWidget(mode_label)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self.paint_tool_buttons = {}
        for label, mode in (("Sculpt", "sculpt"), ("Stamp", "stamp")):
            btn = QPushButton(label, shared_group)
            btn.setCheckable(True)
            btn.setMinimumHeight(40)
            btn.setProperty("paintTool", mode)
            btn.clicked.connect(lambda _checked, m=mode: self.set_paint_tool_mode(m))
            mode_row.addWidget(btn)
            self.paint_tool_buttons[mode] = btn
        shared_layout.addLayout(mode_row)

        brush_size_row = QHBoxLayout()
        brush_size_label = QLabel("Brush Size")
        brush_size_label.setStyleSheet("font-weight: bold; color: #ddd;")
        brush_size_row.addWidget(brush_size_label)
        brush_size_row.addStretch()
        self.sculpt_radius_value = QLabel("64")
        self.sculpt_radius_value.setMinimumWidth(50)
        self.sculpt_radius_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sculpt_radius_value.setStyleSheet(
            "color: #F08000; font-weight: bold;"
        )
        brush_size_row.addWidget(self.sculpt_radius_value)
        shared_layout.addLayout(brush_size_row)

        self.sculpt_radius_slider = QSlider(Qt.Horizontal)
        self.sculpt_radius_slider.setRange(8, 512)
        self.sculpt_radius_slider.setSingleStep(4)
        self.sculpt_radius_slider.setPageStep(32)
        self.sculpt_radius_slider.setValue(64)
        self.sculpt_radius_slider.setToolTip(
            "Shared world-space diameter of the terrain brush"
        )
        self.sculpt_radius_slider.valueChanged.connect(
            self.on_sculpt_radius_slider_changed
        )
        shared_layout.addWidget(self.sculpt_radius_slider)

        shared_hint = QLabel(
            "Move the mouse over the 3D view to see the brush circle. "
            "Left-click applies the active tool."
        )
        shared_hint.setWordWrap(True)
        shared_hint.setStyleSheet("color: #aaa; font-style: italic;")
        shared_layout.addWidget(shared_hint)

        sculpt_layout.addWidget(shared_group)

        # Sculpt-only settings.
        self.sculpt_controls_widget = QGroupBox("Sculpt")
        sculpt_controls_layout = QVBoxLayout(self.sculpt_controls_widget)
        sculpt_controls_layout.setSpacing(8)
        sculpt_controls_layout.setContentsMargins(12, 20, 12, 12)

        action_label = QLabel("Tool")
        action_label.setStyleSheet("font-weight: bold; color: #ddd;")
        sculpt_controls_layout.addWidget(action_label)

        sculpt_mode_row = QHBoxLayout()
        sculpt_mode_row.setSpacing(6)
        self.sculpt_mode_buttons = {}
        for label, mode in (("Raise", "raise"), ("Lower", "lower"), ("Flatten", "flatten")):
            btn = QPushButton(label, self.sculpt_controls_widget)
            btn.setCheckable(True)
            btn.setMinimumHeight(38)
            btn.setProperty("sculptMode", mode)
            btn.clicked.connect(lambda _checked, m=mode: self.set_sculpt_mode(m))
            sculpt_mode_row.addWidget(btn)
            self.sculpt_mode_buttons[mode] = btn
        sculpt_controls_layout.addLayout(sculpt_mode_row)

        strength_row = QHBoxLayout()
        strength_label = QLabel("Strength")
        strength_label.setStyleSheet("font-weight: bold; color: #ddd;")
        strength_row.addWidget(strength_label)
        strength_row.addStretch()
        self.sculpt_strength_value = QLabel("20")
        self.sculpt_strength_value.setMinimumWidth(50)
        self.sculpt_strength_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sculpt_strength_value.setStyleSheet(
            "color: #F08000; font-weight: bold;"
        )
        strength_row.addWidget(self.sculpt_strength_value)
        sculpt_controls_layout.addLayout(strength_row)

        self.sculpt_strength_slider = QSlider(Qt.Horizontal)
        self.sculpt_strength_slider.setRange(1, 200)
        self.sculpt_strength_slider.setValue(20)
        self.sculpt_strength_slider.setToolTip("Sculpt strength")
        self.sculpt_strength_slider.valueChanged.connect(
            self.on_sculpt_strength_slider_changed
        )
        sculpt_controls_layout.addWidget(self.sculpt_strength_slider)

        clear_sculpt_btn = QPushButton("Clear Sculpt")
        clear_sculpt_btn.setMinimumHeight(36)
        clear_sculpt_btn.setStyleSheet("""
            QPushButton {
                background-color: #4a3030;
                color: #f0d0d0;
                font-weight: bold;
                border: 1px solid #704040;
            }
            QPushButton:hover { background-color: #603838; }
            QPushButton:pressed { background-color: #8a4040; }
        """)
        clear_sculpt_btn.clicked.connect(self.clear_sculpt)
        sculpt_controls_layout.addWidget(clear_sculpt_btn)
        sculpt_layout.addWidget(self.sculpt_controls_widget)

        # Stamp-only settings.
        self.stamp_controls_widget = QGroupBox("Stamp")
        stamp_controls_layout = QVBoxLayout(self.stamp_controls_widget)
        stamp_controls_layout.setSpacing(8)
        stamp_controls_layout.setContentsMargins(12, 20, 12, 12)

        self.stamp_texture_label = QLabel("Texture: none selected")
        self.stamp_texture_label.setWordWrap(True)
        self.stamp_texture_label.setStyleSheet(
            "color: #F08000; font-weight: bold;"
        )
        stamp_controls_layout.addWidget(self.stamp_texture_label)

        stamp_hint = QLabel(
            "Left-click to bake the texture selected in the Asset Browser "
            "directly into the terrain."
        )
        stamp_hint.setWordWrap(True)
        stamp_hint.setStyleSheet("color: #aaa; font-style: italic;")
        stamp_controls_layout.addWidget(stamp_hint)

        feather_row = QHBoxLayout()
        feather_label = QLabel("Feather")
        feather_label.setStyleSheet("font-weight: bold; color: #ddd;")
        feather_row.addWidget(feather_label)
        feather_row.addStretch()
        self.stamp_feather_value = QLabel("25%")
        self.stamp_feather_value.setMinimumWidth(50)
        self.stamp_feather_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.stamp_feather_value.setStyleSheet(
            "color: #F08000; font-weight: bold;"
        )
        feather_row.addWidget(self.stamp_feather_value)
        stamp_controls_layout.addLayout(feather_row)

        self.stamp_feather_slider = QSlider(Qt.Horizontal)
        self.stamp_feather_slider.setRange(0, 100)
        self.stamp_feather_slider.setValue(25)
        self.stamp_feather_slider.setToolTip("Softness of the stamp edge")
        self.stamp_feather_slider.valueChanged.connect(self.on_stamp_feather_changed)
        stamp_controls_layout.addWidget(self.stamp_feather_slider)

        clear_stamps_btn = QPushButton("Clear Texture Paint")
        clear_stamps_btn.setMinimumHeight(36)
        clear_stamps_btn.clicked.connect(self.clear_texture_stamps)
        stamp_controls_layout.addWidget(clear_stamps_btn)

        sculpt_layout.addWidget(self.stamp_controls_widget)

        self.sculpt_info_label = QLabel("No sculpt deformations")
        self.sculpt_info_label.setAlignment(Qt.AlignCenter)
        self.sculpt_info_label.setStyleSheet("color: #888; padding: 4px;")
        sculpt_layout.addWidget(self.sculpt_info_label)
        self._update_sculpt_info()
        sculpt_layout.addStretch()

        tabs.addTab(sculpt_tab, "Sculpt/Stamp")

        # The tab widget must itself be inserted into the content layout.
        # Without this, all of the tab pages exist but QTabWidget is never
        # shown, leaving only the controls above and the stats label visible.
        content_layout.addWidget(tabs)
        
        # Stats
        self.stats_label = QLabel("Visible: 0 chunks  |  Culled: 0  |  Triangles: 0")
        self.stats_label.setStyleSheet("""
            QLabel {
                background-color: #1a2a28;
                padding: 10px;
                border-radius: 4px;
                color: #888;
            }
        """)
        self.stats_label.setAlignment(Qt.AlignCenter)
        content_layout.addWidget(self.stats_label)

        scroll_area.setWidget(content_widget)
        main_layout.addWidget(scroll_area)

        self._building_ui = False

    # ------------------------------------------------------------------
    # Appearance tab
    # ------------------------------------------------------------------
    def _build_appearance_tab(self, tabs):
        """Look options: preset, terracing, colours, texture layers, details.

        Every control drives exactly one ``TerrainAppearance`` option, so the
        presets are only starting points - any option can be changed after.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)
        layout.setContentsMargins(8, 8, 8, 8)
        # option name -> (widget, kind); kinds: combo, check, spin, pct, color
        self._appearance_widgets = {}

        def group(title):
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setSpacing(8)
            form.setContentsMargins(12, 20, 12, 12)
            layout.addWidget(box)
            return form

        def combo(option, items):
            w = QComboBox()
            for key, label in items:
                w.addItem(label, key)
            w.currentIndexChanged.connect(
                lambda _i, o=option, w=w: self._set_appearance(o, w.currentData()))
            self._appearance_widgets[option] = (w, 'combo')
            return w

        def check(option, text):
            w = QCheckBox(text)
            w.toggled.connect(lambda v, o=option: self._set_appearance(o, bool(v)))
            self._appearance_widgets[option] = (w, 'check')
            return w

        def spin(option, lo, hi, step, decimals=1, integer=False, tip=None):
            w = QSpinBox() if integer else QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setSingleStep(step)
            if not integer:
                w.setDecimals(decimals)
            if tip:
                w.setToolTip(tip)
            w.valueChanged.connect(lambda v, o=option: self._set_appearance(o, v))
            self._appearance_widgets[option] = (w, 'spin')
            return w

        def pct(option, tip=None):
            """0..1 option on a 0..100 slider with a live % readout."""
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            label = QLabel("0%")
            label.setMinimumWidth(36)
            if tip:
                slider.setToolTip(tip)

            def changed(v, o=option, label=label):
                label.setText(f"{v}%")
                self._set_appearance(o, v / 100.0)
            slider.valueChanged.connect(changed)
            row = QHBoxLayout()
            row.addWidget(slider, 1)
            row.addWidget(label)
            self._appearance_widgets[option] = ((slider, label), 'pct')
            return row

        def color(option, title):
            btn = QPushButton("Choose...")
            swatch = QFrame()
            swatch.setFixedSize(28, 28)
            btn.clicked.connect(lambda _c=False, o=option, t=title: self._choose_appearance_color(o, t))
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addWidget(swatch)
            row.addStretch()
            self._appearance_widgets[option] = (swatch, 'color')
            return row

        # -- Look preset -------------------------------------------------
        form = group("Look")
        self.appearance_preset_combo = QComboBox()
        self.appearance_preset_combo.setMinimumHeight(32)
        for key, label in terrain_style.PRESET_LABELS.items():
            self.appearance_preset_combo.addItem(label, key)
        self.appearance_preset_combo.addItem("Custom", 'custom')
        self.appearance_preset_combo.currentIndexChanged.connect(self.on_appearance_preset_changed)
        form.addRow("Preset:", self.appearance_preset_combo)
        hint = QLabel("A preset sets every option below; each can then be changed on its own.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa; font-style: italic;")
        form.addRow(hint)

        # -- Shape -------------------------------------------------------
        form = group("Shape")
        form.addRow("Terracing:", combo('terrace_mode', terrain_style.TERRACE_MODE_LABELS.items()))
        form.addRow("Step Height:", spin('terrace_step', 1.0, 200.0, 1.0,
                                         tip="Height of one terrace step or block level"))
        form.addRow("Riser Share:", pct('terrace_ramp',
                                        "How much of each step is slope rather than flat ground"))
        form.addRow("Block Size:", spin('block_size', 2.0, 128.0, 2.0,
                                        tip="Footprint of one column in Blocks mode"))
        form.addRow(check('skirt', "Solid sides at the terrain edge (Blocks)"))

        # -- Colour ------------------------------------------------------
        form = group("Colour")
        form.addRow("Colour Source:", combo('color_mode', terrain_style.COLOR_MODE_LABELS.items()))
        form.addRow("Palette:", combo('palette', terrain_style.PALETTE_LABELS.items()))
        # One swatch per palette colour: the strata bands in the order they
        # repeat, or the height steps from low to high. Editing one turns
        # the palette into a custom copy.
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(4)
        self.palette_swatches = []
        for i in range(terrain_style.MAX_PALETTE):
            btn = QPushButton()
            btn.setFixedSize(26, 26)
            btn.setToolTip(f"Colour {i + 1}: click to change")
            btn.clicked.connect(lambda _c=False, i=i: self.choose_palette_color(i))
            swatch_row.addWidget(btn)
            self.palette_swatches.append(btn)
        self.palette_remove_btn = QPushButton("−")
        self.palette_remove_btn.setFixedSize(26, 26)
        self.palette_remove_btn.setToolTip("One colour fewer")
        self.palette_remove_btn.clicked.connect(lambda: self.change_palette_size(-1))
        self.palette_add_btn = QPushButton("+")
        self.palette_add_btn.setFixedSize(26, 26)
        self.palette_add_btn.setToolTip("One colour more")
        self.palette_add_btn.clicked.connect(lambda: self.change_palette_size(1))
        swatch_row.addWidget(self.palette_remove_btn)
        swatch_row.addWidget(self.palette_add_btn)
        swatch_row.addStretch()
        form.addRow("Colours:", swatch_row)
        form.addRow("Band Height:", spin('band_height', 1.0, 200.0, 1.0,
                                         tip="Height of each colour band and contour interval"))

        # -- Texture layers ----------------------------------------------
        form = group("Texture Height Layers")
        note = QLabel("With Use Textures on, sand, grass, rock and snow are blended by height. "
                      "Grass blades also grow only in the grass layer.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #aaa; font-style: italic;")
        form.addRow(note)
        self.layer_sliders = []
        for label in ("Sand → Grass:", "Grass → Rock:", "Rock → Snow:"):
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            value_label = QLabel("0%")
            value_label.setMinimumWidth(36)
            slider.valueChanged.connect(self.on_layer_height_changed)
            row = QHBoxLayout()
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            form.addRow(label, row)
            self.layer_sliders.append((slider, value_label))
        form.addRow("Blend Width:", spin('layer_blend', 0.005, 0.3, 0.005, decimals=3))
        form.addRow("Rock on Slopes:", pct('slope_rock'))
        reset_layers = QPushButton("Biome Defaults")
        reset_layers.clicked.connect(lambda: self._set_appearance('layer_heights', None))
        form.addRow(reset_layers)

        # -- Surface detail ------------------------------------------------
        form = group("Surface Detail")
        form.addRow("Contour Lines:", pct('contour_lines'))
        form.addRow("Line Width (px):", spin('contour_width', 0.5, 6.0, 0.5))
        form.addRow("Tile Grid:", pct('grid_lines'))
        form.addRow("Tile Size:", spin('grid_size', 1.0, 256.0, 1.0))
        form.addRow("Tile Variation:", pct('cell_variation'))
        form.addRow("Cliff Colour:", color('wall_color', "Cliff Colour"))
        form.addRow("Cliff Tint:", pct('wall_amount'))
        form.addRow("Cliff Stripes:", pct('wall_stripes'))
        form.addRow("Shrub Colour:", color('speckle_color', "Shrub Colour"))
        form.addRow("Shrub Dots:", pct('speckle_amount'))
        form.addRow("Patch Colour:", color('patch_color', "Patch Colour"))
        form.addRow("Ground Patches:", pct('patch_amount'))

        # -- Shading -------------------------------------------------------
        form = group("Shading")
        form.addRow(check('smooth_shading', "Smooth shading"))
        form.addRow("Light Bands:", spin('light_steps', 0, 8, 1, integer=True,
                                         tip="0 = continuous lighting"))
        form.addRow("Colour Depth:", spin('dither_levels', 0, 32, 1, integer=True,
                                          tip="Dithered colour levels per channel, 0 = off"))

        layout.addStretch()
        tabs.addTab(tab, "Appearance")

    def _set_appearance(self, option, value):
        if self._building_ui:
            return
        self.terrain.set_appearance(**{option: value})
        self._load_appearance_ui()
        self.terrain_changed.emit()

    def on_appearance_preset_changed(self, index):
        if self._building_ui:
            return
        key = self.appearance_preset_combo.itemData(index)
        if key in terrain_style.PRESETS:
            self.terrain.apply_appearance_preset(key)
            self._load_appearance_ui()
            self.terrain_changed.emit()

    def choose_palette_color(self, index):
        colors = terrain_style.palette_colors(self.terrain.appearance)
        if index >= len(colors):
            return
        chosen = QColorDialog.getColor(QColor.fromRgbF(*colors[index]), self,
                                       f"Palette Colour {index + 1}")
        if not chosen.isValid():
            return
        self.terrain.set_palette_color(
            index, (chosen.redF(), chosen.greenF(), chosen.blueF()))
        self._load_appearance_ui()
        self.terrain_changed.emit()

    def change_palette_size(self, delta):
        count = len(terrain_style.palette_colors(self.terrain.appearance)) + delta
        self.terrain.resize_palette(count)
        self._load_appearance_ui()
        self.terrain_changed.emit()

    def on_layer_height_changed(self, _value=None):
        for slider, label in self.layer_sliders:
            label.setText(f"{slider.value()}%")
        if self._building_ui:
            return
        values = tuple(slider.value() / 100.0 for slider, _ in self.layer_sliders)
        self._set_appearance('layer_heights', values)

    def _choose_appearance_color(self, option, title):
        current = QColor.fromRgbF(*getattr(self.terrain.appearance, option))
        chosen = QColorDialog.getColor(current, self, title)
        if chosen.isValid():
            self._set_appearance(option, (chosen.redF(), chosen.greenF(), chosen.blueF()))

    def _load_appearance_ui(self):
        """Show the terrain's current appearance options in the tab."""
        if not hasattr(self, '_appearance_widgets'):
            return
        from PyQt5.QtGui import QPalette
        a = self.terrain.appearance
        was_building = self._building_ui
        self._building_ui = True
        try:
            idx = self.appearance_preset_combo.findData(a.preset)
            if idx < 0:
                idx = self.appearance_preset_combo.findData('custom')
            self.appearance_preset_combo.setCurrentIndex(idx)
            for option, (widget, kind) in self._appearance_widgets.items():
                value = getattr(a, option)
                if kind == 'combo':
                    i = widget.findData(value)
                    if i >= 0:
                        widget.setCurrentIndex(i)
                elif kind == 'check':
                    widget.setChecked(bool(value))
                elif kind == 'spin':
                    widget.setValue(value)
                elif kind == 'pct':
                    slider, label = widget
                    slider.setValue(int(round(value * 100)))
                    label.setText(f"{slider.value()}%")
                elif kind == 'color':
                    palette = widget.palette()
                    palette.setColor(QPalette.Window, QColor.fromRgbF(*value))
                    widget.setAutoFillBackground(True)
                    widget.setPalette(palette)
            colors = terrain_style.palette_colors(a)
            for i, btn in enumerate(self.palette_swatches):
                if i < len(colors):
                    r, g, b = (int(round(c * 255)) for c in colors[i])
                    btn.setStyleSheet(
                        f"background-color: rgb({r}, {g}, {b}); border: 1px solid #222;")
                    btn.show()
                else:
                    btn.hide()
            self.palette_remove_btn.setEnabled(len(colors) > terrain_style.MIN_PALETTE)
            self.palette_add_btn.setEnabled(len(colors) < terrain_style.MAX_PALETTE)
            self._update_grass_height_ui()
            for (slider, label), v in zip(self.layer_sliders, self.terrain._layer_heights()):
                slider.setValue(int(round(v * 100)))
                label.setText(f"{slider.value()}%")
        finally:
            self._building_ui = was_building

    def on_textures_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.set_use_textures(enabled)
        self.terrain_changed.emit()
    
    def load_from_terrain(self):
        """Load current terrain values into UI."""
        self._building_ui = True
        self.textures_checkbox.setChecked(
            getattr(self.terrain, 'use_textures', DEFAULT_USE_TEXTURES))
        
        # Find biome index
        biome_index = 0
        for i in range(self.biome_combo.count()):
            if self.biome_combo.itemData(i) == biome_key_for_name(self.terrain.biome.name):
                biome_index = i
                break
        self.biome_combo.setCurrentIndex(biome_index)
        
        # Checkboxes
        self.solid_checkbox.setChecked(self.terrain.solid)
        self.grass_checkbox.setChecked(getattr(self.terrain, 'grass_enabled', False))
        grass_density = getattr(self.terrain, 'grass_density', 0.02)
        self.grass_density_slider.setValue(
            int(round(max(0.0, min(0.06, grass_density)) / 0.06 * 100.0))
        )
        self.grass_density_value.setText(f"{self.grass_density_slider.value()}%")
        self._update_grass_color_preview()
        self.flat_checkbox.setChecked(self.terrain.flat_mode)
        
        # Height
        self.base_height_spin.setValue(self.terrain.biome.base_height)
        self.height_scale_spin.setValue(self.terrain.biome.height_scale)
        
        # Seed
        self.seed_spin.setValue(self.terrain.seed)
        
        # Features - Hills
        self.hills_scale_spin.setValue(self.terrain.biome.hills_scale)
        self.hills_intensity_spin.setValue(self.terrain.biome.hills_intensity)
        
        # Features - Mountains
        self.mountains_enabled_check.setChecked(self.terrain.biome.mountains_enabled)
        self.mountains_scale_spin.setValue(self.terrain.biome.mountains_scale)
        self.mountains_intensity_spin.setValue(self.terrain.biome.mountains_intensity)
        self.mountains_sharpness_spin.setValue(self.terrain.biome.mountains_sharpness)
        
        # Features - Valleys
        self.valleys_enabled_check.setChecked(self.terrain.biome.valleys_enabled)
        self.valleys_scale_spin.setValue(self.terrain.biome.valleys_scale)
        self.valleys_depth_spin.setValue(self.terrain.biome.valleys_depth)
        
        # Features - Plateaus
        self.plateaus_enabled_check.setChecked(self.terrain.biome.plateaus_enabled)
        self.plateaus_scale_spin.setValue(self.terrain.biome.plateaus_scale)
        self.plateaus_intensity_spin.setValue(self.terrain.biome.plateaus_intensity)
        self.plateaus_flatness_spin.setValue(self.terrain.biome.plateaus_flatness)
        
        # Size
        self.chunk_size_spin.setValue(16)
        
        self.min_x_spin.setValue(self.terrain.min_chunk_x)
        self.max_x_spin.setValue(self.terrain.max_chunk_x)
        self.min_z_spin.setValue(self.terrain.min_chunk_z)
        self.max_z_spin.setValue(self.terrain.max_chunk_z)

        # If Big World is filling the world, lock the manual size controls.
        self.set_bigworld_managed(
            getattr(self.terrain, '_authored_bounds', None) is not None)
        
        # Position
        self.x_offset_spin.setValue(self.terrain.offset_x)
        self.z_offset_spin.setValue(self.terrain.offset_z)
        self.y_offset_spin.setValue(self.terrain.offset_y)
        
        self.update_gradient_preview()
        self.update_size_info()
        
        # Heightmap status
        if self.terrain.heightmap_data is not None:
            h, w = self.terrain.heightmap_data.shape
            self.hm_status_label.setText(f"Loaded: {w}×{h} px")
        else:
            self.hm_status_label.setText("No heightmap loaded")
        self.hm_strength_spin.setValue(self.terrain.heightmap_strength)
        idx = 0 if self.terrain.heightmap_blend == 'additive' else 1
        self.hm_blend_combo.setCurrentIndex(idx)

        # Sculpt / stamp tool state
        self._update_sculpt_info()
        self.set_sculpt_mode("raise")
        self.set_paint_tool_mode("sculpt")
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is not None and not getattr(view_3d, 'play_mode', False):
            view_3d.set_terrain_sculpt_active(True)

        self._load_appearance_ui()

        self._building_ui = False
    
    def update_gradient_preview(self):
        """Update the gradient preview widget."""
        if self.terrain.biome.color_gradient:
            self.gradient_preview.set_colors(self.terrain.biome.color_gradient)
    
    def update_size_info(self):
        """Update the size information label."""
        chunks_x = self.max_x_spin.value() - self.min_x_spin.value() + 1
        chunks_z = self.max_z_spin.value() - self.min_z_spin.value() + 1
        total_chunks = chunks_x * chunks_z
        chunk_size = self.terrain.chunk_size
        total_size = chunk_size * max(chunks_x, chunks_z)
        
        self.size_info_label.setText(
            f"<b>Total:</b> {chunks_x}×{chunks_z} = {total_chunks} chunks<br>"
            f"<b>World Size:</b> ~{total_size:.0f}×{total_size:.0f} units"
        )
    
    def update_stats(self):
        """Update statistics display."""
        self.stats_label.setText(
            f"Visible: {self.terrain.visible_chunks} chunks  |  "
            f"Culled: {self.terrain.culled_chunks}  |  "
            f"Triangles: {self.terrain.total_triangles:,}"
        )
    
    def show_progress(self, message="Generating terrain..."):
        """Show a progress dialog."""
        self.progress = QProgressDialog(message, None, 0, 0, self)
        self.progress.setWindowTitle("Please Wait")
        self.progress.setWindowModality(Qt.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.setMinimumWidth(400)
        self.progress.setStyleSheet(
            f"""
            QProgressDialog {{ font-size: {self._base_font_size + 3}pt; }}
            QLabel {{ font-size: {self._base_font_size}pt; padding: 20px; font-weight: bold; }}
            """
        )
        self.progress.show()
        QApplication.processEvents()
    
    def hide_progress(self):
        """Hide the progress dialog."""
        if hasattr(self, 'progress') and self.progress:
            self.progress.close()
            self.progress = None
    

    def on_grass_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.set_grass(enabled=enabled)
        self.terrain_changed.emit()

    def on_grass_density_changed(self, value):
        self.grass_density_value.setText(f"{value}%")
        if self._building_ui:
            return
        self.terrain.set_grass(enabled=self.grass_checkbox.isChecked(),
                               density=(value / 100.0) * 0.06)
        self.terrain_changed.emit()

    def choose_grass_color(self):
        current = QColor.fromRgbF(*self.terrain.grass_color)
        color = QColorDialog.getColor(current, self, "Grass Colour")
        if not color.isValid():
            return
        rgb = (color.redF(), color.greenF(), color.blueF())
        self.terrain.set_grass(
            enabled=self.grass_checkbox.isChecked(),
            color=rgb,
        )
        self._update_grass_color_preview()
        self.terrain_changed.emit()

    def match_grass_to_ground(self):
        self.terrain.set_grass(enabled=self.grass_checkbox.isChecked(), color='ground')
        self._update_grass_color_preview()
        self.terrain_changed.emit()

    def choose_grass_tip_color(self):
        current = QColor.fromRgbF(*self.terrain.grass_tip_colour())
        color = QColorDialog.getColor(current, self, "Grass Tip Colour")
        if not color.isValid():
            return
        self.terrain.set_grass(
            enabled=self.grass_checkbox.isChecked(),
            tip_color=(color.redF(), color.greenF(), color.blueF()),
        )
        self._update_grass_color_preview()
        self.terrain_changed.emit()

    def reset_grass_tip_color(self):
        self.terrain.set_grass(enabled=self.grass_checkbox.isChecked(), tip_color='auto')
        self._update_grass_color_preview()
        self.terrain_changed.emit()

    def on_grass_height_changed(self, _value=None):
        for slider, label in self.grass_height_sliders:
            label.setText(f"{slider.value()}%")
        if self._building_ui:
            return
        low = self.grass_height_sliders[0][0].value() / 100.0
        high = self.grass_height_sliders[1][0].value() / 100.0
        self.terrain.set_grass(enabled=self.grass_checkbox.isChecked(),
                               height_range=(low, high))
        self._update_grass_height_ui()
        self.terrain_changed.emit()

    def reset_grass_height_range(self):
        self.terrain.set_grass(enabled=self.grass_checkbox.isChecked(),
                               height_range='auto')
        self._update_grass_height_ui()
        self.terrain_changed.emit()

    def _update_grass_height_ui(self):
        """Show the grass height range (following the layers or its own)."""
        if not hasattr(self, 'grass_height_sliders'):
            return
        was_building = self._building_ui
        self._building_ui = True
        try:
            bounds = self.terrain.grass_height_bounds()
            (lo_s, lo_l), (hi_s, hi_l) = self.grass_height_sliders
            # Sliders never cross: the lowest stays at or below the highest.
            lo_v = int(round(min(bounds) * 100))
            hi_v = int(round(max(bounds) * 100))
            lo_s.setValue(lo_v)
            hi_s.setValue(hi_v)
            lo_l.setText(f"{lo_v}%")
            hi_l.setText(f"{hi_v}%")
            self.grass_follow_btn.setEnabled(
                getattr(self.terrain, 'grass_height_range', None) is not None)
        finally:
            self._building_ui = was_building

    def _update_grass_color_preview(self):
        r, g, b = self.terrain.grass_color
        # Use the palette for the colour swatch rather than injecting a
        # per-widget stylesheet. This avoids QSS parser warnings on QFrame
        # while the application-wide stylesheet supplies the border.
        from PyQt5.QtGui import QPalette
        palette = self.grass_color_preview.palette()
        palette.setColor(QPalette.Window, QColor.fromRgbF(r, g, b))
        self.grass_color_preview.setAutoFillBackground(True)
        self.grass_color_preview.setPalette(palette)
        matching = not getattr(self.terrain, 'grass_color_custom', False)
        if hasattr(self, 'grass_ground_label'):
            self.grass_color_preview.setVisible(not matching)
            self.grass_ground_label.setVisible(matching)
            self.grass_ground_btn.setEnabled(not matching)
        if hasattr(self, 'grass_tip_preview'):
            palette = self.grass_tip_preview.palette()
            palette.setColor(QPalette.Window,
                             QColor.fromRgbF(*self.terrain.grass_tip_colour()))
            self.grass_tip_preview.setAutoFillBackground(True)
            self.grass_tip_preview.setPalette(palette)
            auto = getattr(self.terrain, 'grass_tip_color', None) is None
            self.grass_tip_auto_btn.setEnabled(not auto)
            # An automatic tip derives from each blade's own colour; with
            # ground-coloured blades there is no single colour to show.
            self.grass_tip_preview.setVisible(not (auto and matching))
        self._update_grass_height_ui()

    def on_wireframe_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.wireframe = enabled
        self.terrain_changed.emit()
    
    def on_solid_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.solid = enabled
        self.terrain_changed.emit()
    
    def on_flat_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.flat_mode = enabled
        self.terrain.mark_all_dirty()
        self.terrain_changed.emit()

    def on_biome_changed(self, index):
        if self._building_ui:
            return
        biome_key = self.biome_combo.itemData(index)
        if biome_key and biome_key in BIOMES:
            self.show_progress("Applying biome preset...")
            self.terrain.set_biome(biome_key)
            
            # Update UI to match biome
            self._building_ui = True
            self.base_height_spin.setValue(self.terrain.biome.base_height)
            self.height_scale_spin.setValue(self.terrain.biome.height_scale)
            
            self.hills_scale_spin.setValue(self.terrain.biome.hills_scale)
            self.hills_intensity_spin.setValue(self.terrain.biome.hills_intensity)
            
            self.mountains_enabled_check.setChecked(self.terrain.biome.mountains_enabled)
            self.mountains_scale_spin.setValue(self.terrain.biome.mountains_scale)
            self.mountains_intensity_spin.setValue(self.terrain.biome.mountains_intensity)
            self.mountains_sharpness_spin.setValue(self.terrain.biome.mountains_sharpness)
            
            self.valleys_enabled_check.setChecked(self.terrain.biome.valleys_enabled)
            self.valleys_scale_spin.setValue(self.terrain.biome.valleys_scale)
            self.valleys_depth_spin.setValue(self.terrain.biome.valleys_depth)
            
            self.plateaus_enabled_check.setChecked(self.terrain.biome.plateaus_enabled)
            self.plateaus_scale_spin.setValue(self.terrain.biome.plateaus_scale)
            self.plateaus_intensity_spin.setValue(self.terrain.biome.plateaus_intensity)
            self.plateaus_flatness_spin.setValue(self.terrain.biome.plateaus_flatness)
            self._building_ui = False
            # The biome brings its own texture layer heights.
            self._load_appearance_ui()
            
            self.update_gradient_preview()
            self.terrain_changed.emit()
            self.hide_progress()
    
    def on_height_changed(self, value):
        if self._building_ui:
            return
        self.terrain.biome.base_height = self.base_height_spin.value()
        self.terrain.biome.height_scale = self.height_scale_spin.value()
        self.terrain.mark_all_dirty()
        self.terrain_changed.emit()
    
    def on_seed_changed(self, value):
        if self._building_ui:
            return
        self.show_progress("Regenerating with new seed...")
        self.terrain.set_seed(value)
        self.terrain_changed.emit()
        self.hide_progress()
    
    def on_feature_changed(self, value=None):
        if self._building_ui:
            return
        self.show_progress("Updating terrain features...")
        
        self.terrain.biome.hills_scale = self.hills_scale_spin.value()
        self.terrain.biome.hills_intensity = self.hills_intensity_spin.value()
        
        self.terrain.biome.mountains_enabled = self.mountains_enabled_check.isChecked()
        self.terrain.biome.mountains_scale = self.mountains_scale_spin.value()
        self.terrain.biome.mountains_intensity = self.mountains_intensity_spin.value()
        self.terrain.biome.mountains_sharpness = self.mountains_sharpness_spin.value()
        
        self.terrain.biome.valleys_enabled = self.valleys_enabled_check.isChecked()
        self.terrain.biome.valleys_scale = self.valleys_scale_spin.value()
        self.terrain.biome.valleys_depth = self.valleys_depth_spin.value()
        
        self.terrain.biome.plateaus_enabled = self.plateaus_enabled_check.isChecked()
        self.terrain.biome.plateaus_scale = self.plateaus_scale_spin.value()
        self.terrain.biome.plateaus_intensity = self.plateaus_intensity_spin.value()
        self.terrain.biome.plateaus_flatness = self.plateaus_flatness_spin.value()
        
        self.terrain.mark_all_dirty()
        self.terrain_changed.emit()
        self.hide_progress()
    
    def on_size_changed(self, value):
        if self._building_ui:
            return
        self.show_progress("Resizing terrain...")
        self.terrain.base_resolution = self.chunk_size_spin.value()
        self.terrain.mark_all_dirty()
        self.update_size_info()
        self.terrain_changed.emit()
        self.hide_progress()
    
    def on_bounds_changed(self, value):
        if self._building_ui:
            return
        if getattr(self.terrain, '_authored_bounds', None) is not None:
            # The world size is owned by Big World "Fill world with terrain";
            # ignore manual bounds edits so they can't fight / desync the fill.
            return
        self.show_progress("Updating terrain bounds...")
        self.terrain.set_bounds(
            self.min_x_spin.value(),
            self.max_x_spin.value(),
            self.min_z_spin.value(),
            self.max_z_spin.value()
        )
        self.update_size_info()
        self.terrain_changed.emit()
        self.hide_progress()
    
    def on_offset_changed(self, value):
        if self._building_ui:
            return
        self.terrain.offset_x = self.x_offset_spin.value()
        self.terrain.offset_z = self.z_offset_spin.value()
        self.terrain.offset_y = self.y_offset_spin.value()
        self.terrain.mark_all_dirty()
        self.terrain_changed.emit()

    def apply_mesh_scale(self, factor):
        """Apply a uniform physical terrain scale without flattening relief."""
        self.show_progress(f"Scaling terrain by {factor}x...")
        # Terrain owns the representation boundary: X, Y and Z scale together
        # while the procedural generator continues to operate in terrain-space.
        self.terrain.set_mesh_scale(factor)
        self.update_size_info()
        self.terrain_changed.emit()
        self.hide_progress()

    def apply_tiling_scale(self, factor):
        """Apply a tiling factor to world bounds."""
        self.show_progress(f"Expanding grid by {factor}x...")
        current_min_x = self.min_x_spin.value()
        current_max_x = self.max_x_spin.value()
        current_min_z = self.min_z_spin.value()
        current_max_z = self.max_z_spin.value()

        # Update spinners which triggers on_bounds_changed
        self._building_ui = True
        self.min_x_spin.setValue(current_min_x * factor)
        self.max_x_spin.setValue(current_max_x * factor)
        self.min_z_spin.setValue(current_min_z * factor)
        self.max_z_spin.setValue(current_max_z * factor)
        self._building_ui = False
        
        # Trigger manually
        self.on_bounds_changed(0)
        self.hide_progress()
    
    def randomize_seed(self):
        import random
        self.seed_spin.setValue(random.randint(0, 999999))
    
    def set_bigworld_managed(self, managed: bool):
        """Reflect Big World fill ownership of the world size in the Size tab.

        When *managed*, the manual bounds spin-boxes and size presets are
        disabled and an explanatory note is shown — everything else (biome,
        sculpt, seed, height, offsets) stays fully editable so the generated
        terrain can still be customised.
        """
        for w in (getattr(self, 'min_x_spin', None), getattr(self, 'max_x_spin', None),
                  getattr(self, 'min_z_spin', None), getattr(self, 'max_z_spin', None)):
            if w is not None:
                w.setEnabled(not managed)
        for b in getattr(self, '_size_preset_btns', None) or []:
            b.setEnabled(not managed)
        note = getattr(self, '_bigworld_size_note', None)
        if note is not None:
            note.setVisible(bool(managed))

    def apply_size_preset(self, bounds):
        if getattr(self.terrain, '_authored_bounds', None) is not None:
            return  # size owned by Big World fill (see on_bounds_changed)
        self._building_ui = True
        self.min_x_spin.setValue(bounds[0])
        self.max_x_spin.setValue(bounds[1])
        self.min_z_spin.setValue(bounds[0])
        self.max_z_spin.setValue(bounds[1])
        self._building_ui = False
        self.on_bounds_changed(0)

    # =========================================================================
    # HEIGHTMAP
    # =========================================================================

    def load_heightmap_image(self):
        """Open a file dialog and load a greyscale image as a heightmap."""
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Heightmap Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;All Files (*)"
        )
        if not path:
            return
        self.show_progress("Loading heightmap…")
        try:
            self.terrain.load_heightmap(path)
            h, w = self.terrain.heightmap_data.shape
            self.hm_status_label.setText(f"Loaded: {w}×{h} px  —  {path.split('/')[-1].split(chr(92))[-1]}")
            self.terrain_changed.emit()
        except Exception as e:
            QMessageBox.warning(self, "Heightmap Error", str(e))
        finally:
            self.hide_progress()

    def clear_heightmap(self):
        """Remove the heightmap overlay."""
        self.terrain.clear_heightmap()
        self.hm_status_label.setText("No heightmap loaded")
        self.terrain_changed.emit()

    def on_heightmap_settings_changed(self, _=None):
        if self._building_ui:
            return
        self.terrain.heightmap_strength = self.hm_strength_spin.value()
        self.terrain.heightmap_blend = self.hm_blend_combo.currentData()
        if self.terrain.heightmap_data is not None:
            self.terrain.mark_all_dirty()
            self.terrain_changed.emit()

    # =========================================================================
    # SCULPT / STAMP
    # =========================================================================

    def set_paint_tool_mode(self, mode):
        """Switch between the sculpt and stamp tools."""
        mode = "stamp" if mode == "stamp" else "sculpt"
        self.paint_tool_mode = mode

        for name, button in self.paint_tool_buttons.items():
            button.setChecked(name == mode)

        self.sculpt_controls_widget.setVisible(mode == "sculpt")
        self.stamp_controls_widget.setVisible(mode == "stamp")
        self._update_stamp_texture_label()
        self._sync_sculpt_to_viewport()

        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is not None and not getattr(view_3d, 'play_mode', False):
            view_3d.set_terrain_sculpt_active(True)

    def _update_stamp_texture_label(self):
        browser = getattr(self.editor, 'asset_browser', None) if self.editor else None
        path = browser.get_selected_filepath() if browser is not None else None
        if path:
            self.stamp_texture_label.setText(
                f"Texture: {os.path.basename(path)}"
            )
        else:
            self.stamp_texture_label.setText("Texture: none selected")

    def set_sculpt_mode(self, mode):
        """Select Raise, Lower or Flatten for the active sculpt tool."""
        mode = mode if mode in self.sculpt_mode_buttons else "raise"
        self._sculpt_mode = mode
        for name, button in self.sculpt_mode_buttons.items():
            button.setChecked(name == mode)
        self._sync_sculpt_to_viewport()

    def on_sculpt_radius_slider_changed(self, value):
        self.sculpt_radius_value.setText(str(int(value)))
        self._sync_sculpt_to_viewport()

    def on_sculpt_strength_slider_changed(self, value):
        self.sculpt_strength_value.setText(str(int(value)))
        self._sync_sculpt_to_viewport()

    def clear_sculpt(self):
        """Remove all sculpt deformations."""
        self.terrain.clear_sculpt()
        self._update_sculpt_info()
        self.terrain_changed.emit()
        if self.editor and hasattr(self.editor, 'show_toast'):
            self.editor.show_toast("Sculpt data cleared")

    def _update_sculpt_info(self):
        count = len(self.terrain.sculpt_offsets)
        if count == 0:
            self.sculpt_info_label.setText("No sculpt deformations")
        else:
            self.sculpt_info_label.setText(
                f"{count:,} deformation points stored"
            )

    def _sync_sculpt_to_viewport(self):
        """Push current shared brush settings to the 3D view."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is None:
            return
        view_3d.terrain_sculpt_mode = (
            'stamp' if getattr(self, 'paint_tool_mode', 'sculpt') == 'stamp'
            else self._sculpt_mode
        )
        # The UI exposes brush size as the visible diameter. The terrain
        # backend APIs consume radius, so convert once at the boundary.
        view_3d.terrain_sculpt_radius = max(
            4.0, float(self.sculpt_radius_slider.value()) * 0.5
        )
        view_3d.terrain_sculpt_strength = float(
            self.sculpt_strength_slider.value()
        )
        view_3d.terrain_sculpt_feather = (
            self.stamp_feather_slider.value() / 100.0
        )

    def on_stamp_feather_changed(self, value):
        self.stamp_feather_value.setText(f"{value}%")
        self._sync_sculpt_to_viewport()

    def clear_texture_stamps(self):
        """Remove every baked terrain texture paint."""
        if not getattr(self.terrain, 'texture_paint_maps', None):
            return
        if self.editor and hasattr(self.editor, 'save_state'):
            self.editor.state.terrain_data = self.terrain.to_dict()
            self.editor.save_state()
        self.terrain.clear_texture_paint()
        if self.editor and hasattr(self.editor, 'state'):
            self.editor.state.terrain_data = self.terrain.to_dict()
        self.terrain_changed.emit()
        if self.editor and hasattr(self.editor, 'show_toast'):
            self.editor.show_toast("Terrain texture paint cleared")

    def request_close(self):
        """Close the panel and disable its 3D brush."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is not None:
            view_3d.set_terrain_sculpt_active(False)
        if self.editor and hasattr(self.editor, '_close_current_overlay'):
            self.editor._close_current_overlay()

    def closeEvent(self, event):
        """Disable the terrain brush when the panel is closed."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is not None:
            view_3d.set_terrain_sculpt_active(False)
        super().closeEvent(event)

    def hideEvent(self, event):
        """Disable the terrain brush when the panel is hidden."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is not None:
            view_3d.set_terrain_sculpt_active(False)
        if hasattr(self, '_stats_timer'):
            self._stats_timer.stop()
        super().hideEvent(event)

    def regenerate_terrain(self):
        self.show_progress("Regenerating terrain...")
        self.terrain.mark_all_dirty()
        self.terrain_generated.emit()
        self.terrain_changed.emit()
        self.hide_progress()
        if self.editor and hasattr(self.editor, 'show_toast'):
            self.editor.show_toast("Terrain regenerated!")
    
    def reset_to_defaults(self):
        self._building_ui = True
        default_index = max(0, self.biome_combo.findData(DEFAULT_BIOME))
        self.biome_combo.setCurrentIndex(default_index)
        self.textures_checkbox.setChecked(DEFAULT_USE_TEXTURES)
        self.terrain.set_use_textures(DEFAULT_USE_TEXTURES)
        self.grass_checkbox.setChecked(DEFAULT_GRASS_ENABLED)
        self.terrain.set_grass(DEFAULT_GRASS_ENABLED)
        self.seed_spin.setValue(42)
        self.chunk_size_spin.setValue(16)
        self.min_x_spin.setValue(-2)
        self.max_x_spin.setValue(2)
        self.min_z_spin.setValue(-2)
        self.max_z_spin.setValue(2)
        self.x_offset_spin.setValue(0)
        self.z_offset_spin.setValue(0)
        self.y_offset_spin.setValue(0)
        # Reset chunk scale to default 256.0
        self.terrain.chunk_size = 256.0
        self.terrain.cleanup()
        self._building_ui = False
        self.on_biome_changed(default_index)
        self.on_bounds_changed(0)
    
    def showEvent(self, event):
        super().showEvent(event)
        self.update_stats()
        if not hasattr(self, '_stats_timer'):
            self._stats_timer = QTimer(self)
            self._stats_timer.timeout.connect(self.update_stats)
        self._stats_timer.start(500)
