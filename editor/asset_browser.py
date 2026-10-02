import os
import sys
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame,
                             QHBoxLayout, QGridLayout, QSplitter, QApplication,
                             QMainWindow, QPushButton, QFileDialog, QTreeView, QFileSystemModel)
from PyQt5.QtCore import Qt, QSize, QDir
from PyQt5.QtGui import QPixmap

class AssetBrowser(QWidget):
    """
    An asset browser widget with a directory selector, a grid view for assets,
    and a details panel.
    """
    def __init__(self, asset_folder, editor=None):
        super().__init__()
        self.base_asset_folder = asset_folder # Store the base folder
        self.current_asset_folder = asset_folder
        self.editor = editor
        self.selected_item = None

        # --- Main Layout ---
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(5)

        # --- Main Horizontal Splitter ---
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_layout.addWidget(self.main_splitter)

        # --- Left Pane (Directory Tree) ---
        left_pane_tree = QFrame()
        left_pane_tree_layout = QVBoxLayout(left_pane_tree)
        left_pane_tree_layout.setContentsMargins(0, 0, 0, 0)

        # --- Textures Button ---
        self.textures_button = QPushButton("Textures")
        self.textures_button.clicked.connect(self.reset_to_default_path)
        self.textures_button.setStyleSheet("""
            QPushButton {
                padding: 8px;
                text-align: center;
                background-color: #444;
                border: 1px solid #666;
                color: #f0f0f0;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #555;
            }
        """)
        left_pane_tree_layout.addWidget(self.textures_button)


        self.model = QFileSystemModel()
        self.model.setRootPath(self.base_asset_folder)
        self.model.setFilter(QDir.NoDotAndDotDot | QDir.AllDirs)

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(self.base_asset_folder))
        self.tree.clicked.connect(self.on_tree_selection_changed)

        # Hide columns for size, type, date modified
        self.tree.setHeaderHidden(True)
        for i in range(1, self.model.columnCount()):
            self.tree.hideColumn(i)


        left_pane_tree_layout.addWidget(self.tree)

        # --- Right Pane (Nested Splitter for Grid and Details) ---
        right_pane_nested = QSplitter(Qt.Horizontal)

        # --- Middle Pane (Asset Grid) ---
        middle_pane = QFrame()
        middle_pane_layout = QVBoxLayout(middle_pane)
        middle_pane_layout.setContentsMargins(0, 0, 0, 0)

        self.browse_button = QPushButton(os.path.abspath(self.current_asset_folder))
        self.browse_button.clicked.connect(self.browse_for_folder)
        self.browse_button.setStyleSheet("""
            QPushButton {
                padding: 8px;
                text-align: left;
                background-color: #444;
                border: 1px solid #666;
                color: #f0f0f0;
            }
            QPushButton:hover {
                background-color: #555;
            }
        """)
        middle_pane_layout.addWidget(self.browse_button)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("border: none;")

        self.scroll_content = QWidget()
        self.asset_layout = QGridLayout(self.scroll_content)
        self.asset_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.asset_layout.setSpacing(10)
        self.scroll_area.setWidget(self.scroll_content)

        middle_pane_layout.addWidget(self.scroll_area)

        # --- Rightmost Pane (Details Panel) ---
        rightmost_pane = QFrame()
        rightmost_pane.setFrameShape(QFrame.StyledPanel)
        rightmost_pane.setMinimumWidth(300)
        rightmost_pane.setStyleSheet("background-color: #3c3c3c;")

        self.details_layout = QVBoxLayout(rightmost_pane)
        self.details_layout.setContentsMargins(10, 10, 10, 10)

        self.preview_label = QLabel("Select an asset")
        self.preview_label.setFixedSize(256, 256)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #2b2b2b; border: 1px solid #555;")

        self.name_label = QLabel("Name: ")
        self.name_label.setStyleSheet("color: #f0f0f0; font-weight: bold; padding-top: 10px;")
        self.name_label.setWordWrap(True)

        self.type_label = QLabel("Type: ")
        self.type_label.setStyleSheet("color: #ccc;")

        self.details_layout.addWidget(self.preview_label)
        self.details_layout.addWidget(self.name_label)
        self.details_layout.addWidget(self.type_label)
        self.details_layout.addStretch()

        # Add panes to nested splitter
        right_pane_nested.addWidget(middle_pane)
        right_pane_nested.addWidget(rightmost_pane)

        # Add panes to main splitter
        self.main_splitter.addWidget(left_pane_tree)
        self.main_splitter.addWidget(right_pane_nested)

        self.setMinimumWidth(1280)
        self.main_splitter.setSizes([200, 1080]) # Adjust initial sizes
        right_pane_nested.setSizes([600,300])
        right_pane_nested.setStretchFactor(0, 2)
        right_pane_nested.setStretchFactor(1, 1)

        self.refresh_assets()

    def reset_to_default_path(self):
        """Resets the asset browser to the base asset folder."""
        self.current_asset_folder = self.base_asset_folder
        self.browse_button.setText(os.path.abspath(self.current_asset_folder))
        self.refresh_assets()
        self.tree.setCurrentIndex(self.model.index(self.base_asset_folder))


    def on_tree_selection_changed(self, index):
        path = self.model.filePath(index)
        self.current_asset_folder = path
        self.browse_button.setText(os.path.abspath(self.current_asset_folder))
        self.refresh_assets()


    def browse_for_folder(self):
        """Opens a dialog to select a new asset folder."""
        new_folder = QFileDialog.getExistingDirectory(self, "Select Image Directory", self.current_asset_folder)

        if new_folder and new_folder != self.current_asset_folder:
            self.current_asset_folder = new_folder
            self.browse_button.setText(os.path.abspath(self.current_asset_folder))
            self.refresh_assets()

    def refresh_assets(self):
        """Reloads assets from the current asset_folder into the grid view."""
        while self.asset_layout.count():
            child = self.asset_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        textures_path = self.current_asset_folder
        if not os.path.exists(textures_path) or not os.path.isdir(textures_path):
            print(f"Warning: Asset path not found or not a directory: '{textures_path}'")
            self.preview_label.setText("Directory not found.")
            self.name_label.setText("Name: ")
            self.type_label.setText("Type: ")
            self.selected_item = None
            return

        row, col = 0, 0
        num_columns = 4

        for filename in sorted(os.listdir(textures_path)):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                filepath = os.path.join(textures_path, filename)
                item_widget = AssetItem(filepath, self)
                self.asset_layout.addWidget(item_widget, row, col)

                col += 1
                if col >= num_columns:
                    col = 0
                    row += 1

        self.preview_label.setText("Select an asset")
        self.name_label.setText("Name: ")
        self.type_label.setText("Type: ")
        self.selected_item = None


    def select_item(self, item):
        if self.selected_item:
            self.selected_item.deselect()
        self.selected_item = item
        self.selected_item.select()

        filepath = item.filepath
        try:
            pixmap = QPixmap(filepath)
            if not pixmap.isNull():
                self.preview_label.setPixmap(pixmap.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.preview_label.setText("Preview\nNot Available")
        except Exception:
            self.preview_label.setText("Preview\nError")

        self.name_label.setText(f"Name: {os.path.basename(filepath)}")
        self.type_label.setText("Type: Texture")

    def get_selected_filepath(self):
        """Returns the path of the currently selected file, or None if invalid/not selected"""
        if not self.selected_item:
            return None
        
        try:
            # Verify the file exists and is accessible
            if os.path.exists(self.selected_item.filepath) and os.path.isfile(self.selected_item.filepath):
                return self.selected_item.filepath
            return None
        except Exception:
            return None


class AssetItem(QWidget):
    """
    A clickable widget representing a single asset in the grid.
    Displays a thumbnail and the asset's name underneath.
    """
    THUMBNAIL_SIZE = QSize(100, 100)
    ITEM_SIZE = QSize(120, 140)

    def __init__(self, filepath, browser):
        super().__init__()
        self.filepath = filepath
        self.browser = browser
        self.is_selected = False

        self.setFixedSize(self.ITEM_SIZE)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setSpacing(5)

        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(self.THUMBNAIL_SIZE)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setStyleSheet("background-color: #4a4a4a; border-radius: 4px;")

        pixmap = QPixmap(filepath)
        if not pixmap.isNull():
            self.thumbnail.setPixmap(pixmap.scaled(self.THUMBNAIL_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        self.name = QLabel(os.path.basename(filepath))
        self.name.setStyleSheet("color: #f0f0f0;")
        self.name.setAlignment(Qt.AlignCenter)
        self.name.setWordWrap(True)

        self.layout.addWidget(self.thumbnail)
        self.layout.addWidget(self.name)
        self.layout.addStretch()

        self.update_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.browser.select_item(self)

    def select(self):
        self.is_selected = True
        self.update_style()

    def deselect(self):
        self.is_selected = False
        self.update_style()

    def update_style(self):
        if self.is_selected:
            self.setStyleSheet("""
                AssetItem {
                    background-color: #0078d7;
                    border-radius: 4px;
                }
            """)
            self.name.setStyleSheet("color: white;")
        else:
            self.setStyleSheet("""
                AssetItem {
                    background-color: #3c3c3c;
                    border-radius: 4px;
                }
                AssetItem:hover {
                    background-color: #4a4a4a;
                }
            """)
            self.name.setStyleSheet("color: #f0f0f0;")


# --- Main Application Runner ---
if __name__ == '__main__':
    # Define the path for dummy assets
    dummy_asset_path = "./project_folder/assets" # Base asset folder
    dummy_texture_path = os.path.join(dummy_asset_path, "textures") # Specific texture path for initial view

    # Create dummy files for demonstration if they don't exist
    if not os.path.exists(dummy_texture_path):
        os.makedirs(dummy_texture_path)
        dummy_pixmap = QPixmap(100, 100)
        dummy_pixmap.fill(Qt.red)
        dummy_pixmap.save(os.path.join(dummy_texture_path, "dummy_red.png"), "PNG")
        dummy_pixmap.fill(Qt.blue)
        dummy_pixmap.save(os.path.join(dummy_texture_path, "dummy_blue.png"), "PNG")


    app = QApplication(sys.argv)
    app.setStyleSheet("""
        QWidget {
            background-color: #2b2b2b;
            color: #f0f0f0;
        }
    """)

    main_window = QMainWindow()
    main_window.setWindowTitle("Asset Browser Standalone Test")
    main_window.setGeometry(100, 100, 1280, 768)

    # Pass the base asset path to the browser
    asset_browser = AssetBrowser(dummy_asset_path)

    main_window.setCentralWidget(asset_browser)
    main_window.show()

    sys.exit(app.exec_())