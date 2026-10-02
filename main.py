import sys
import os
import shutil
import argparse
from PyQt5.QtWidgets import QApplication, QSplashScreen
from PyQt5.QtGui import QPixmap, QSurfaceFormat
from editor.main_window import MainWindow

def clean_pycache():
    """
    Finds and deletes all '__pycache__' folders recursively
    from the root directory.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    print(f"🧹 Starting cleanup from root: {project_root}")

    for root, dirs, files in os.walk(project_root):
        if '__pycache__' in dirs:
            pycache_path = os.path.join(root, '__pycache__')
            print(f"🗑️ Found existing cached data and deleted them")
            try:
                shutil.rmtree(pycache_path)
            except OSError as e:
                print(f"Skipping cleanup")
    print("✨ Cleanup complete.")

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
    QMenuBar {
        background-color: #4a4a4a;
        color: #f0f0f0;
    }
    QMenuBar::item {
        background-color: #4a4a4a;
        color: #f0f0f0;
    }
    QMenuBar::item:selected {
        background-color: #0078d7;
    }
    QMenu {
        background-color: #4a4a4a;
        border: 1px solid #000;
    }
    QMenu::item:selected {
        background-color: #0078d7;
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
        background-color: #0078d7;
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
        background: #0078d7;
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
        background-color: #0078d7;
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
    # --- Set the default OpenGL format ---
    # This must be done BEFORE the QApplication is created.
    # It tells Qt to request a specific version of OpenGL.
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.CoreProfile)
    format.setDepthBufferSize(24)
    format.setStencilBufferSize(8)
    QSurfaceFormat.setDefaultFormat(format)
    # --- End of new block ---

    # Always clean pycache on startup
    clean_pycache()

    app = QApplication(sys.argv)
    
    app.setStyleSheet(dark_stylesheet)
    
    splash_pixmap = QPixmap('assets/splash.png')
    splash = QSplashScreen(splash_pixmap)
    splash.show()
    app.processEvents()
    
    # --- MODIFICATION: Determine the root directory of the project ---
    root_directory = os.path.dirname(os.path.abspath(__file__))
    
    # --- MODIFICATION: Pass the root directory to the MainWindow ---
    main_win = MainWindow(root_dir=root_directory)
    main_win.show()
    
    splash.finish(main_win)
    
    sys.exit(app.exec_())