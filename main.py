import sys
import os
import shutil
import argparse
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QProgressBar
from PyQt5.QtGui import QPixmap, QSurfaceFormat
from PyQt5.QtCore import Qt
from editor.main_window import MainWindow

def clean_pycache():
    """
    Finds and deletes all '__pycache__' folders recursively
    from the root directory.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    print(f"Starting cleanup from root: {project_root}")

    for root, dirs, files in os.walk(project_root):
        if '__pycache__' in dirs:
            pycache_path = os.path.join(root, '__pycache__')
            print(f"🗑️ Found existing cached data and deleted them")
            try:
                shutil.rmtree(pycache_path)
            except OSError as e:
                print(f"Skipping cleanup")
    print("Cleanup complete.")

class ProgressSplashScreen(QWidget):
    """Custom splash screen with uniform progress bar."""
    def __init__(self, pixmap_path):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        
        # Load splash image
        pixmap = QPixmap(pixmap_path)
        
        # Main layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Splash image
        self.image_label = QLabel()
        self.image_label.setPixmap(pixmap)
        layout.addWidget(self.image_label)
        
        # Progress bar with uniform styling
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(25)
        
        # Simple uniform color
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #2b2b2b;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #425f5d;
            }
        """)
        
        # Status label overlay
        self.status_label = QLabel("Starting RStudio...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("""
            color: #f0f0f0; 
            font-size: 10pt; 
            background: transparent;
            padding: 0px;
        """)
        
        # Container for progress bar and overlay label
        progress_container = QWidget()
        progress_layout = QVBoxLayout(progress_container)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(0)
        progress_layout.addWidget(self.progress_bar)
        
        self.status_label.setParent(progress_container)
        self.status_label.setGeometry(self.progress_bar.geometry())
        layout.addWidget(progress_container)
        self.setLayout(layout)
        self.adjustSize()
        
        # Center on screen
        screen = QApplication.primaryScreen()
        screen_geo = screen.geometry()
        self.move(
            screen_geo.center().x() - self.width() // 2,
            screen_geo.center().y() - self.height() // 2
        )
    
    def set_progress(self, value, status_text=None):
        """Update progress value (0-100) and optional status text."""
        self.progress_bar.setValue(value)
        if status_text:
            self.status_label.setText(status_text)
            self.status_label.setGeometry(self.progress_bar.geometry())
        QApplication.processEvents()
    
    def resizeEvent(self, event):
        """Keep overlay label centered on progress bar."""
        super().resizeEvent(event)
        if hasattr(self, 'status_label') and hasattr(self, 'progress_bar'):
            self.status_label.setGeometry(self.progress_bar.geometry())

dark_stylesheet = """
    QWidget {
        background-color: #2b2b2b;
        color: #f0f0f0;
        border: none;
    }
    QMainWindow {
        background-color: #3c3c3c;
    }
    QDockWidget {
        background-color: #3c3c3c;
        titlebar-close-icon: url(close.png);
        titlebar-normal-icon: url(float.png);
    }
    QDockWidget::title {
        text-align: left;
        background: #555;
        padding-left: 5px;
        padding-top: 3px;
        padding-bottom: 3px;
    }
    /* NEW: Make control buttons always visible */
    QDockWidget::close-button, QDockWidget::float-button {
        background-color: #444;
        border: 1px solid #666;
        padding: 2px;
        margin: 1px;
    }
    QDockWidget::close-button:hover, QDockWidget::float-button:hover {
        background-color: #666;
    }
    QMenuBar {
        background-color: #4a4a4a;
        color: #f0f0f0;
    }
    QMenuBar::item {
        background-color: #4a4a4a;
        color: #f0f0f0;
    }
    QMenuBar::item:selected {
        background-color: #F08000;
    }
    QMenu {
        background-color: #4a4a4c;
        border: 1px solid #000;
    }
    QMenu::item:selected {
        background-color: #F08000;
    }
    QToolBar {
        background-color: #4a4a4a;
        border: none;
    }
    QPushButton {
        background-color: #555;
        color: #f0f0f0;
        border: 1px solid #666;
        padding: 5px;
        min-width: 50px;
    }
    QPushButton:hover {
        background-color: #6a6a6a;
    }
    QPushButton:pressed {
        background-color: #F08000;
    }
    QTabWidget::pane {
        border-top: 2px solid #555;
    }
    QTabBar::tab {
        background: #444;
        color: #ccc;
        border: 1px solid #222;
        padding: 5px;
    }
    QTabBar::tab:selected {
        background: #F08000;
        color: white;
    }
    QStatusBar {
        background-color: #4a4a4a;
    }
    QSpinBox, QComboBox, QLineEdit {
        background-color: #444;
        color: #f0f0f0;
        border: 1px solid #666;
        padding: 3px;
    }
    QSpinBox::up-button, QSpinBox::down-button {
        subcontrol-origin: border;
        width: 16px;
        border-left: 1px solid #666;
        background-color: #555;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {
        background-color: #6a6a6a;
    }
    QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {
        background-color: #F08000;
    }
    QSpinBox::up-arrow {
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 6px solid #f0f0f0;
        width: 0px;
        height: 0px;
    }
    QSpinBox::down-arrow {
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 6px solid #f0f0f0;
        width: 0px;
        height: 0px;
    }
    QCheckBox::indicator {
        width: 13px;
        height: 13px;
    }
    QFrame {
        border: 1px solid #555;
    }
"""

if __name__ == "__main__":
    # Print version first thing
    try:
        with open('editor/version.txt', 'r') as f:
            version = f.read().strip()
            print(f"       +++ RStudio {version}")
    except FileNotFoundError:
        print("Version file not found")
    
    # Create application first
    app = QApplication(sys.argv)
    app.setStyleSheet(dark_stylesheet)
    
    # Create and show splash screen IMMEDIATELY
    splash = ProgressSplashScreen('assets/splash.png')
    splash.show()
    splash.set_progress(5, "Initializing OpenGL...")
    
    # Set OpenGL format
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.CoreProfile)
    format.setDepthBufferSize(24)
    format.setStencilBufferSize(8)
    QSurfaceFormat.setDefaultFormat(format)
    splash.set_progress(15, "Cleaning cache...")
    
    # Clean pycache
    clean_pycache()
    splash.set_progress(25, "Loading editor core...")
    
    # Create main window (heavy loading)
    root_directory = os.path.dirname(os.path.abspath(__file__))
    splash.set_progress(40, "Building UI...")
    
    main_win = MainWindow(root_dir=root_directory)
    splash.set_progress(80, "Finalizing...")
    
    # Show main window
    main_win.show()
    splash.set_progress(100, "Ready!")
    
    # Close splash
    splash.close()
    
    sys.exit(app.exec_())