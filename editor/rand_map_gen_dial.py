import random
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QDialogButtonBox,
    QSpinBox,
    QComboBox,
    QLineEdit
)

class RandomMapGeneratorDialog(QDialog):
    """
    A dialog window for setting parameters for the random map generator,
    built with PyQt5 to integrate with the main application.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Random Map Generator")

        # --- Create Widgets ---
        self.style_combo = QComboBox()
        self.style_combo.addItems(["genA", "genB"])

        self.width_spinbox = QSpinBox()
        self.width_spinbox.setRange(20, 500)
        self.width_spinbox.setValue(100)

        self.length_spinbox = QSpinBox()
        self.length_spinbox.setRange(20, 500)
        self.length_spinbox.setValue(60)
        
        self.seed_input = QLineEdit()
        self.seed_input.setPlaceholderText("Leave empty for random seed")

        # --- Layout ---
        form_layout = QFormLayout()
        form_layout.addRow("Generation Style:", self.style_combo)
        form_layout.addRow("Map Width:", self.width_spinbox)
        form_layout.addRow("Map Length:", self.length_spinbox)
        form_layout.addRow("Seed:", self.seed_input)

        # --- Buttons ---
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        # --- Main Layout ---
        main_layout = QVBoxLayout()
        main_layout.addLayout(form_layout)
        main_layout.addWidget(button_box)
        self.setLayout(main_layout)

    def get_parameters(self):
        """Returns the selected parameters from the dialog widgets."""
        seed_text = self.seed_input.text()
        return {
            "style": self.style_combo.currentText(),
            "width": self.width_spinbox.value(),
            "length": self.length_spinbox.value(),
            "seed": int(seed_text) if seed_text.isdigit() else random.randint(0, 999999)
        }