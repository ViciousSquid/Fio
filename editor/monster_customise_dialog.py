"""
monster_customise_dialog.py
Popup for assigning custom PNG sprites (idle / shoot / dead) and 3D billboard
size to an individual Monster entity.

All sprite paths are stored relative to the project root and must reside
inside  assets/sprites/monsters/.  The engine will automatically fall back
to the default monster sprite whenever a custom file is missing.
"""

import os

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QGroupBox,
    QDialogButtonBox, QFileDialog, QMessageBox, QWidget,
)
from PyQt5.QtCore import Qt

# All monster sprites must live under this directory (relative to project root)
from engine.monster_constants import MONSTER_SPRITE_SIZES, MONSTER_SPRITE_SIZE_DEFAULT

SPRITE_BASE = os.path.join("assets", "sprites", "monsters")


def _project_root() -> str:
    """Return the absolute project root directory."""
    try:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    except Exception:
        return os.getcwd()


class MonsterCustomiseDialog(QDialog):
    """
    Lets the designer assign per-instance custom sprites and a billboard size
    to a single Monster entity.

    Properties written to ``thing.properties`` on OK:
        custom_idle   (str)  – relative path from project root, or absent
        custom_shoot  (str)  – relative path from project root, or absent
        custom_dead   (str)  – relative path from project root, or absent
        sprite_width  (int)  – billboard width  in the 3D view (default 128)
        sprite_height (int)  – billboard height in the 3D view (default 128)

    On Cancel nothing is changed.
    """

    def __init__(self, thing, parent=None):
        super().__init__(parent)
        self.thing = thing
        self.setWindowTitle("Customise Monster Sprites")
        self.setMinimumWidth(500)
        self.setModal(True)
        self._build_ui()
        self._load_from_thing()

    # ------------------------------------------------------------------ #
    # UI construction                                                       #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Sprite frames ───────────────────────────────────────────────
        sprite_group = QGroupBox("Sprite Frames")
        sprite_group.setStyleSheet(self._group_style())
        sprite_form = QFormLayout(sprite_group)
        sprite_form.setSpacing(8)
        sprite_form.setContentsMargins(8, 12, 8, 8)

        self._idle_edit  = self._make_edit("Default  (monster_type / idle.png)")
        self._shoot_edit = self._make_edit("Default  (monster_type / shoot.png)")
        self._dead_edit  = self._make_edit("Default  (monster_type / dead.png)")

        sprite_form.addRow("Idle PNG:",  self._make_row(self._idle_edit,  "idle"))
        sprite_form.addRow("Shoot PNG:", self._make_row(self._shoot_edit, "shoot"))
        sprite_form.addRow("Dead PNG:",  self._make_row(self._dead_edit,  "dead"))

        root.addWidget(sprite_group)

        # ── 2D View Sprite ──────────────────────────────────────────────
        sprite_2d_group = QGroupBox("2D View Sprite")
        sprite_2d_group.setStyleSheet(self._group_style())
        sprite_2d_form = QFormLayout(sprite_2d_group)
        sprite_2d_form.setSpacing(8)
        sprite_2d_form.setContentsMargins(8, 12, 8, 8)

        self._sprite_2d_edit = self._make_edit("Default  (assets/sprites/monser.png)")
        sprite_2d_form.addRow("Sprite PNG:", self._make_row(self._sprite_2d_edit, "sprite_2d"))

        sprite_2d_note = QLabel(
            "Overrides the icon shown in the 2D editor views only.\n"
            "Always displayed at 60×60 px. Does not affect the 3D billboard."
        )
        sprite_2d_note.setStyleSheet("QLabel { color: #999; font-size: 11px; margin-top: 2px; }")
        sprite_2d_note.setWordWrap(True)
        sprite_2d_form.addRow("", sprite_2d_note)

        root.addWidget(sprite_2d_group)

        # ── Billboard size ──────────────────────────────────────────────
        size_group = QGroupBox("3D Billboard Size")
        size_group.setStyleSheet(self._group_style())
        size_form = QFormLayout(size_group)
        size_form.setSpacing(8)
        size_form.setContentsMargins(8, 12, 8, 8)

        self._width_spin  = self._make_spin("Billboard width in the 3D game view")
        self._height_spin = self._make_spin("Billboard height in the 3D game view")

        size_form.addRow("Width:",  self._width_spin)
        size_form.addRow("Height:", self._height_spin)

        root.addWidget(size_group)

        # ── Info note ───────────────────────────────────────────────────
        info = QLabel(
            "Sprites must be PNG files inside  assets/sprites/monsters/\n"
            "Missing custom files fall back to the default monster sprites automatically."
        )
        # Increased font-size to 13px and adjusted color for better contrast
        info.setStyleSheet("""
            QLabel {
                color: #bbb; 
                font-size: 13px;
                margin-top: 4px;
            }
        """)
        info.setWordWrap(True)
        root.addWidget(info)

        # ── Buttons ─────────────────────────────────────────────────────
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._on_reset)
        btn_box.addButton(reset_btn, QDialogButtonBox.ResetRole)

        root.addWidget(btn_box)

    # ------------------------------------------------------------------ #
    # Helper widget factories                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_edit(placeholder: str) -> QLineEdit:
        e = QLineEdit()
        e.setReadOnly(True)
        e.setPlaceholderText(placeholder)
        return e

    @staticmethod
    def _make_spin(tooltip: str) -> QSpinBox:
        s = QSpinBox()
        s.setRange(16, 1024)
        s.setSingleStep(16)
        s.setValue(128)
        s.setSuffix(" px")
        s.setToolTip(tooltip)
        return s

    def _make_row(self, line_edit: QLineEdit, slot: str) -> QWidget:
        """Line-edit + Browse + Clear in one row widget."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(line_edit)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(lambda _=False, s=slot: self._browse(s))

        clear_btn = QPushButton("✕")
        clear_btn.setFixedWidth(28)
        clear_btn.setToolTip("Clear — use default sprite")
        clear_btn.clicked.connect(lambda _=False, s=slot: self._clear(s))

        h.addWidget(browse_btn)
        h.addWidget(clear_btn)
        return w

    # ------------------------------------------------------------------ #
    # Populate / persist                                                   #
    # ------------------------------------------------------------------ #

    def _load_from_thing(self):
        p = self.thing.properties
        self._idle_edit.setText(p.get("custom_idle", ""))
        self._shoot_edit.setText(p.get("custom_shoot", ""))
        self._dead_edit.setText(p.get("custom_dead", ""))
        self._sprite_2d_edit.setText(p.get("sprite_2d", ""))
        # Use the subtype default from monster_constants when no override is stored
        mtype = p.get("monster_type", "human")
        default_w, default_h = MONSTER_SPRITE_SIZES.get(mtype, MONSTER_SPRITE_SIZE_DEFAULT)
        self._width_spin.setValue(int(p.get("sprite_width",  default_w)))
        self._height_spin.setValue(int(p.get("sprite_height", default_h)))

    def _on_ok(self):
        props = self.thing.properties

        slots = [
            ("custom_idle",  self._idle_edit),
            ("custom_shoot", self._shoot_edit),
            ("custom_dead",  self._dead_edit),
            ("sprite_2d",    self._sprite_2d_edit),
        ]

        for key, edit in slots:
            path = edit.text().strip()
            if path:
                abs_path = os.path.join(_project_root(), path)
                if not os.path.isfile(abs_path):
                    ans = QMessageBox.warning(
                        self,
                        "File Not Found",
                        f"The file could not be found on disk:\n"
                        f"  {path}\n\n"
                        "The engine will fall back to the default sprite when this\n"
                        "file is missing.  Save anyway?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if ans == QMessageBox.No:
                        return
                props[key] = path
            else:
                props.pop(key, None)

        props["sprite_width"]  = self._width_spin.value()
        props["sprite_height"] = self._height_spin.value()

        # Flush sprite cache so the viewport reloads immediately
        try:
            from editor.things import Monster
            Monster.clear_sprite_cache()
            Monster.invalidate_icon_cache()   # <-- NEW: clear 2D icon cache
            # Force the 2D views to redraw with the new sprite
            main_window = self.window()
            while main_window and not hasattr(main_window, 'update_all_ui'):
                main_window = main_window.parent()
            if main_window and hasattr(main_window, 'update_all_ui'):
                main_window.update_all_ui()
        except Exception as e:
            print(f"Error refreshing UI after sprite change: {e}")

        self.accept()

    def _on_reset(self):
        """Clear all custom overrides and restore subtype default sizes."""
        self._idle_edit.clear()
        self._shoot_edit.clear()
        self._dead_edit.clear()
        self._sprite_2d_edit.clear()
        mtype = self.thing.properties.get("monster_type", "human")
        default_w, default_h = MONSTER_SPRITE_SIZES.get(mtype, MONSTER_SPRITE_SIZE_DEFAULT)
        self._width_spin.setValue(default_w)
        self._height_spin.setValue(default_h)

    # ------------------------------------------------------------------ #
    # Browse / Clear                                                       #
    # ------------------------------------------------------------------ #

    def _browse(self, slot: str):
        # The 2D sprite can come from anywhere under assets/sprites/
        if slot == "sprite_2d":
            start_dir = os.path.join(_project_root(), "assets", "sprites")
            os.makedirs(start_dir, exist_ok=True)
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select 2D view sprite PNG  (any PNG inside assets/sprites/)",
                start_dir,
                "PNG Images (*.png)",
            )
            if not path:
                return
            try:
                rel = os.path.relpath(path, _project_root())
            except ValueError:
                rel = path
            self._sprite_2d_edit.setText(rel.replace("\\", "/"))
            return

        start_dir = os.path.join(_project_root(), SPRITE_BASE)
        os.makedirs(start_dir, exist_ok=True)

        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select {slot}.png sprite  (must be inside assets/sprites/monsters/)",
            start_dir,
            "PNG Images (*.png)",
        )
        if not path:
            return

        # Convert to a path relative to the project root
        try:
            rel = os.path.relpath(path, _project_root())
        except ValueError:
            # Different drive on Windows — keep absolute but warn
            rel = path

        # Enforce that the file lives under SPRITE_BASE
        norm_base = os.path.normcase(
            os.path.normpath(os.path.join(_project_root(), SPRITE_BASE))
        )
        norm_path = os.path.normcase(
            os.path.normpath(os.path.join(_project_root(), rel))
        )

        if not norm_path.startswith(norm_base):
            QMessageBox.warning(
                self,
                "Wrong Directory",
                "Monster sprites must be inside:\n"
                f"  assets/sprites/monsters/\n\n"
                "Please select a file within that folder.",
            )
            return

        edit = {"idle": self._idle_edit,
                "shoot": self._shoot_edit,
                "dead": self._dead_edit}[slot]
        edit.setText(rel.replace("\\", "/"))

    def _clear(self, slot: str):
        edit = {"idle":     self._idle_edit,
                "shoot":    self._shoot_edit,
                "dead":     self._dead_edit,
                "sprite_2d": self._sprite_2d_edit}[slot]
        edit.clear()

    # ------------------------------------------------------------------ #
    # Shared stylesheet                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _group_style() -> str:
        return """
            QGroupBox {
                font-weight: bold;
                color: #F08000;
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """



