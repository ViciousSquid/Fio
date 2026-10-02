from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QDoubleSpinBox,
    QComboBox, QPushButton, QGroupBox, QFormLayout, QSlider, QCheckBox,
    QTabWidget, QWidget, QFrame, QGridLayout, QScrollArea, QSizePolicy,
    QColorDialog, QMessageBox, QProgressDialog, QApplication
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor, QIcon, QPainter, QLinearGradient, QPen, QBrush

from engine.terrain import Terrain, BIOMES, BiomeConfig


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

        # Apply global stylesheet
        self.setStyleSheet("""
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
                font-size: 14px;
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
        """)
        
        self.setup_ui()
        self.load_from_terrain()
    
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
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # Top controls row (Textures, Wireframe, Solid, Flat)
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(20)
        
        self.textures_checkbox = QCheckBox("Use Textures")
        self.textures_checkbox.setChecked(False)
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
        tabs = QTabWidget()
        
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
        
        # === FEATURES TAB ===
        features_tab = QWidget()
        features_scroll = QScrollArea()
        features_scroll.setWidgetResizable(True)
        features_scroll.setWidget(features_tab)
        features_scroll.setFrameShape(QFrame.NoFrame)
        
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
        
        features_layout.addStretch()
        tabs.addTab(features_scroll, "Features")
        
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

        scale_info = QLabel("Scales the physical dimensions of each chunk. 1x = 256 units.")
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

        # === SCULPT TAB ===
        sculpt_tab = QWidget()
        sculpt_layout = QVBoxLayout(sculpt_tab)
        sculpt_layout.setSpacing(12)
        sculpt_layout.setContentsMargins(8, 8, 8, 8)

        # Sculpt brush settings
        brush_group = QGroupBox("Sculpt Brush")
        brush_layout = QFormLayout(brush_group)
        brush_layout.setSpacing(10)
        brush_layout.setContentsMargins(12, 20, 12, 12)

        sculpt_info = QLabel("Paint directly in the 3D viewport, or enter coordinates manually below.")
        sculpt_info.setStyleSheet("color: #aaa; font-style: italic;")
        sculpt_info.setWordWrap(True)
        brush_layout.addRow(sculpt_info)

        self.sculpt_paint_btn = QPushButton("🎨 Enable 3D Viewport Painting")
        self.sculpt_paint_btn.setCheckable(True)
        self.sculpt_paint_btn.setChecked(False)
        self.sculpt_paint_btn.setStyleSheet("""
            QPushButton {
                background-color: #555;
                color: white;
                font-weight: bold;
                padding: 10px;
            }
            QPushButton:checked {
                background-color: #C62828;
                color: white;
            }
            QPushButton:hover {
                background-color: #6a6a6a;
            }
            QPushButton:checked:hover {
                background-color: #D32F2F;
            }
        """)
        self.sculpt_paint_btn.toggled.connect(self.toggle_3d_sculpt_painting)
        brush_layout.addRow(self.sculpt_paint_btn)

        self.sculpt_mode_combo = QComboBox()
        self.sculpt_mode_combo.addItem("Raise", "raise")
        self.sculpt_mode_combo.addItem("Lower", "lower")
        self.sculpt_mode_combo.addItem("Smooth", "smooth")
        self.sculpt_mode_combo.addItem("Flatten", "flatten")
        self.sculpt_mode_combo.currentIndexChanged.connect(self.on_sculpt_brush_setting_changed)
        brush_layout.addRow("Mode:", self.sculpt_mode_combo)

        self.sculpt_radius_spin = QDoubleSpinBox()
        self.sculpt_radius_spin.setRange(4, 500)
        self.sculpt_radius_spin.setSingleStep(10)
        self.sculpt_radius_spin.setValue(50)
        self.sculpt_radius_spin.valueChanged.connect(self.on_sculpt_brush_setting_changed)
        brush_layout.addRow("Radius:", self.sculpt_radius_spin)

        self.sculpt_strength_spin = QDoubleSpinBox()
        self.sculpt_strength_spin.setRange(0.1, 200)
        self.sculpt_strength_spin.setSingleStep(5)
        self.sculpt_strength_spin.setValue(20)
        self.sculpt_strength_spin.valueChanged.connect(self.on_sculpt_brush_setting_changed)
        brush_layout.addRow("Strength:", self.sculpt_strength_spin)

        brush_group.setLayout(brush_layout)
        sculpt_layout.addWidget(brush_group)

        # Manual coordinate entry
        coord_group = QGroupBox("Apply At Coordinates")
        coord_layout = QFormLayout(coord_group)
        coord_layout.setSpacing(10)
        coord_layout.setContentsMargins(12, 20, 12, 12)

        self.sculpt_x_spin = QDoubleSpinBox()
        self.sculpt_x_spin.setRange(-50000, 50000)
        self.sculpt_x_spin.setSingleStep(50)
        self.sculpt_x_spin.setValue(0)
        coord_layout.addRow("World X:", self.sculpt_x_spin)

        self.sculpt_z_spin = QDoubleSpinBox()
        self.sculpt_z_spin.setRange(-50000, 50000)
        self.sculpt_z_spin.setSingleStep(50)
        self.sculpt_z_spin.setValue(0)
        coord_layout.addRow("World Z:", self.sculpt_z_spin)

        apply_sculpt_btn = QPushButton("🖌️ Apply Sculpt")
        apply_sculpt_btn.setStyleSheet("""
            QPushButton {
                background-color: #F08000;
                color: white;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #FF9020;
            }
        """)
        apply_sculpt_btn.clicked.connect(self.apply_sculpt)
        coord_layout.addRow(apply_sculpt_btn)

        coord_group.setLayout(coord_layout)
        sculpt_layout.addWidget(coord_group)

        # Sculpt info / clear
        sculpt_actions_group = QGroupBox("Sculpt Data")
        sculpt_actions_layout = QVBoxLayout(sculpt_actions_group)
        sculpt_actions_layout.setSpacing(10)
        sculpt_actions_layout.setContentsMargins(12, 20, 12, 12)

        self.sculpt_info_label = QLabel("No sculpt deformations")
        self.sculpt_info_label.setStyleSheet("color: #aaa;")
        sculpt_actions_layout.addWidget(self.sculpt_info_label)
        self._update_sculpt_info()

        clear_sculpt_btn = QPushButton("🗑️ Clear All Sculpt Data")
        clear_sculpt_btn.clicked.connect(self.clear_sculpt)
        sculpt_actions_layout.addWidget(clear_sculpt_btn)

        sculpt_actions_group.setLayout(sculpt_actions_layout)
        sculpt_layout.addWidget(sculpt_actions_group)

        sculpt_layout.addStretch()
        tabs.addTab(sculpt_tab, "Sculpt")
        
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

    def on_textures_changed(self, enabled):
        if self._building_ui:
            return
        self.terrain.use_textures = enabled
        self.terrain_changed.emit()
    
    def load_from_terrain(self):
        """Load current terrain values into UI."""
        self._building_ui = True
        
        # Find biome index
        biome_index = 0
        for i in range(self.biome_combo.count()):
            if self.biome_combo.itemData(i) == self.terrain.biome.name.lower().replace(' ', '_'):
                biome_index = i
                break
        self.biome_combo.setCurrentIndex(biome_index)
        
        # Checkboxes
        self.solid_checkbox.setChecked(self.terrain.solid)
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

        # Sculpt info
        self._update_sculpt_info()

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
        self.progress.setStyleSheet("""
            QProgressDialog { font-size: 14px; }
            QLabel { padding: 20px; font-weight: bold; }
        """)
        self.progress.show()
        QApplication.processEvents()
    
    def hide_progress(self):
        """Hide the progress dialog."""
        if hasattr(self, 'progress') and self.progress:
            self.progress.close()
            self.progress = None
    
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
        """Apply a physical scaling factor to chunk size."""
        self.show_progress(f"Scaling mesh by {factor}x...")
        # Default chunk size is 256.0
        new_size = 256.0 * factor
        self.terrain.chunk_size = new_size
        
        # Important: clear existing chunks so they are recreated with new size
        self.terrain.cleanup()
        self.terrain.mark_all_dirty()
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
    # SCULPT
    # =========================================================================

    def apply_sculpt(self):
        """Apply a single sculpt stroke at the entered coordinates."""
        x = self.sculpt_x_spin.value()
        z = self.sculpt_z_spin.value()
        radius = self.sculpt_radius_spin.value()
        strength = self.sculpt_strength_spin.value()
        mode = self.sculpt_mode_combo.currentData()

        self.show_progress("Sculpting terrain…")
        try:
            if mode == 'raise':
                self.terrain.apply_sculpt_at(x, z, radius, strength)
            elif mode == 'lower':
                self.terrain.apply_sculpt_at(x, z, radius, -strength)
            elif mode == 'smooth':
                self.terrain.smooth_sculpt_at(x, z, radius, min(strength / 20.0, 1.0))
            elif mode == 'flatten':
                self.terrain.flatten_sculpt_at(x, z, radius, min(strength / 20.0, 1.0))
            self._update_sculpt_info()
            self.terrain_changed.emit()
        finally:
            self.hide_progress()

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
            self.sculpt_info_label.setText(f"{count:,} deformation points stored")

    def toggle_3d_sculpt_painting(self, active):
        """Enable or disable 3D viewport sculpt painting mode."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is None:
            self.sculpt_paint_btn.setChecked(False)
            return
        view_3d.set_terrain_sculpt_active(active)
        if active:
            self._sync_sculpt_to_viewport()
            self.sculpt_paint_btn.setText("🛑 Disable 3D Viewport Painting")
        else:
            self.sculpt_paint_btn.setText("🎨 Enable 3D Viewport Painting")

    def _sync_sculpt_to_viewport(self):
        """Push current sculpt brush settings to the 3D view."""
        view_3d = getattr(self.editor, 'view_3d', None) if self.editor else None
        if view_3d is None:
            return
        view_3d.terrain_sculpt_mode = self.sculpt_mode_combo.currentData()
        view_3d.terrain_sculpt_radius = self.sculpt_radius_spin.value()
        view_3d.terrain_sculpt_strength = self.sculpt_strength_spin.value()

    def on_sculpt_brush_setting_changed(self, _=None):
        """Called when any sculpt brush setting changes — sync to viewport."""
        self._sync_sculpt_to_viewport()

    def request_close(self):
        """Close the panel by restoring the Properties dock's original content."""
        if self.sculpt_paint_btn.isChecked():
            self.sculpt_paint_btn.setChecked(False)
        if self.editor and hasattr(self.editor, '_close_current_overlay'):
            self.editor._close_current_overlay()

    def closeEvent(self, event):
        """Disable sculpt painting when the terrain editor is closed."""
        if self.sculpt_paint_btn.isChecked():
            self.sculpt_paint_btn.setChecked(False)
        super().closeEvent(event)

    def hideEvent(self, event):
        """Cleanup on hide: disable sculpt painting and stop the stats timer.

        NOTE: This used to be two separate hideEvent methods on the class — the
        second silently overrode the first, so the sculpt-painting disable was
        never running. They're now merged.
        """
        if self.sculpt_paint_btn.isChecked():
            self.sculpt_paint_btn.setChecked(False)
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
        self.biome_combo.setCurrentIndex(0)
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
        self.on_biome_changed(0)
        self.on_bounds_changed(0)
    
    def showEvent(self, event):
        super().showEvent(event)
        self.update_stats()
        if not hasattr(self, '_stats_timer'):
            self._stats_timer = QTimer(self)
            self._stats_timer.timeout.connect(self.update_stats)
        self._stats_timer.start(500)
