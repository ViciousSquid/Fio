import os
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QFileDialog, QMessageBox, QFormLayout, QWidget, QApplication
)
from PyQt5.QtCore import Qt


class PackageMetadataDialog(QDialog):
    """
    Export dialog that embeds inside the Properties dock area.
    High-DPI aware: font sizes scale with the application font.
    """
    def __init__(self, current_map, parent=None, close_callback=None):
        super().__init__(parent)
        self.close_callback = close_callback
        self.setWindowFlags(Qt.Widget)  # Embed as widget, not popup window
        self.setObjectName("PackageExportDialog")
        
        self._metadata = {}
        
        # High-DPI: get base font size from application
        base_font = QApplication.font()
        base_size = base_font.pointSize()
        large_size = base_size + 4
        small_size = max(base_size - 1, 8)
        
        self.setStyleSheet(f"""
            QWidget#PackageExportDialog {{
                background-color: #2b2b2b;
                color: #f0f0f0;
            }}
            QLabel {{
                color: #f0f0f0;
                font-size: {base_size}pt;
            }}
            QLineEdit, QTextEdit {{
                background-color: #3c3c3c;
                color: #f0f0f0;
                border: 1px solid #555;
                padding: 6px;
                font-size: {base_size}pt;
            }}
            QPushButton {{
                background-color: #555;
                color: #f0f0f0;
                border: 1px solid #666;
                padding: 8px 16px;
                font-size: {base_size}pt;
            }}
            QPushButton:hover {{
                background-color: #F08000;
            }}
            QPushButton:pressed {{
                background-color: #d06000;
            }}
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        # Header
        header = QLabel("Export Game Package")
        header.setStyleSheet(f"font-size: {large_size}pt; font-weight: bold; color: #F08000; padding-bottom: 8px;")
        layout.addWidget(header)
        
        info = QLabel(f"Base map: {os.path.basename(current_map)}")
        info.setStyleSheet(f"color: #888; font-size: {small_size}pt; padding-bottom: 8px;")
        layout.addWidget(info)
        
        # Buttons (at the top, below the header)
        btn_layout_top = QHBoxLayout()
        btn_layout_top.setSpacing(0)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_close)
        btn_layout_top.addWidget(self.cancel_btn, 1)
        
        self.export_btn = QPushButton("Export...")
        self.export_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2E7D32;
                font-weight: bold;
                font-size: {base_size}pt;
            }}
            QPushButton:hover {{
                background-color: #388E3C;
            }}
        """)
        self.export_btn.clicked.connect(self._on_export)
        btn_layout_top.addWidget(self.export_btn, 1)
        
        layout.addLayout(btn_layout_top)

        # Form
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft)
        
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Required")
        form.addRow("Package Title:", self.title_edit)
        
        self.author_edit = QLineEdit()
        self.author_edit.setPlaceholderText("Required")
        form.addRow("Author:", self.author_edit)
        
        self.version_edit = QLineEdit()
        self.version_edit.setText("1.0.0")
        form.addRow("Version:", self.version_edit)
        
        self.desc_edit = QTextEdit()
        #self.desc_edit.setPlaceholderText("Package Description...")
        self.desc_edit.setMaximumHeight(100)
        form.addRow("Description:", self.desc_edit)
        
        layout.addLayout(form)
        
        # Dependency info label
        self.dep_label = QLabel("")
        self.dep_label.setStyleSheet(f"color: #888; font-size: {small_size}pt; padding: 8px;")
        self.dep_label.setWordWrap(True)
        layout.addWidget(self.dep_label)
        
        layout.addStretch()
        
        self.setMinimumWidth(400)
    
    def set_dependency_info(self, map_count, asset_count, warnings=None):
        """Update the dependency info label from the exporter."""
        base_font = QApplication.font()
        small_size = max(base_font.pointSize() - 1, 8)
        
        text = f"Maps: {map_count} | Assets: {asset_count}"
        if warnings:
            text += f"\nWarnings: {len(warnings)}"
            for w in warnings[:3]:
                text += f"\n  • {w}"
            if len(warnings) > 3:
                text += f"\n  ... and {len(warnings) - 3} more"
        self.dep_label.setText(text)
    
    def _on_export(self):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Required", "Please enter a title.")
            return
        
        self._metadata = {
            'title': title,
            'author': self.author_edit.text().strip(),
            'version': self.version_edit.text().strip(),
            'description': self.desc_edit.toPlainText().strip(),
            'banner_source_path': ''
        }
        self.accept()
    
    def _on_close(self):
        """Close without exporting — restore the original tabs."""
        self.reject()
        if self.close_callback:
            self.close_callback()
    
    def get_metadata(self):
        return self._metadata

    def build_metadata(self):
        """Build metadata dict from form fields"""
        title = self.title_edit.text().strip()
        if not title:
            return None
        return {
            'title': title,
            'author': self.author_edit.text().strip(),
            'version': self.version_edit.text().strip(),
            'description': self.desc_edit.toPlainText().strip(),
            'banner_source_path': ''
        }
    
    def closeEvent(self, event):
        """Ensure close button (X) also restores tabs."""
        if self.close_callback:
            self.close_callback()
        super().closeEvent(event)