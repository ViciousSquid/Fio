"""
surface_inspector.py
A simplified, Radiant-style Surface Inspector for tuning a single brush face's
texture mapping in Face mode.

The floating panel appears when a face is clicked in Face mode and edits that
face's per-face texture transform, stored on the brush dict:

    brush['uv_shift'][face]  = [horizontal, vertical]   # offset, in tiles
    brush['uv_scale'][face]  = [horizontal, vertical]   # stretch / tiling
    brush['uv_angle'][face]  = degrees                  # free rotation

Each control has its own editable "Step" which doubles as the spin increment
and the grid-snap quantum used by the "Match Grid" button.
"""

from PyQt5.QtWidgets import (
    QDialog, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDoubleSpinBox, QFrame, QSizePolicy,
)
from PyQt5.QtCore import Qt


class SurfaceInspector(QDialog):
    """Floating panel that edits the texture mapping of one selected face."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.target = None          # (brush, face_name) currently being edited
        self._loading = False       # suppress apply while populating fields

        self.setWindowTitle("Surface Inspector")
        # Floating tool window that stays above the editor without stealing it.
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowCloseButtonHint)
        self.setMinimumWidth(300)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI construction                                                     #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        outer = QVBoxLayout(self)

        # --- Texture name ---
        tex_row = QHBoxLayout()
        tex_row.addWidget(QLabel("Texture"))
        self.tex_label = QLabel("(none)")
        self.tex_label.setStyleSheet("font-weight: bold;")
        self.tex_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tex_row.addWidget(self.tex_label, stretch=1)
        outer.addLayout(tex_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        outer.addWidget(line)

        # --- Value / Step grid ---
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        # rows: (attr, label, min, max, decimals, default_value, default_step)
        self.hshift,   self.hshift_step   = self._add_row(grid, 0, "Horizontal shift",   -8192.0, 8192.0, 3, 0.0, 0.125)
        self.vshift,   self.vshift_step   = self._add_row(grid, 1, "Vertical shift",     -8192.0, 8192.0, 3, 0.0, 0.125)
        self.hstretch, self.hstretch_step = self._add_row(grid, 2, "Horizontal stretch",  -64.0,   64.0, 3, 1.0, 0.5)
        self.vstretch, self.vstretch_step = self._add_row(grid, 3, "Vertical stretch",    -64.0,   64.0, 3, 1.0, 0.5)
        self.rotate,   self.rotate_step   = self._add_row(grid, 4, "Rotate",             -360.0,  360.0, 2, 0.0, 45.0)

        self._value_spins = (self.hshift, self.vshift,
                             self.hstretch, self.vstretch, self.rotate)
        self._rows = (
            (self.hshift, self.hshift_step),
            (self.vshift, self.vshift_step),
            (self.hstretch, self.hstretch_step),
            (self.vstretch, self.vstretch_step),
            (self.rotate, self.rotate_step),
        )

        # --- Buttons ---
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.match_grid_btn = QPushButton("Match Grid")
        self.match_grid_btn.setToolTip("Snap every value to its Step increment")
        self.match_grid_btn.clicked.connect(self._match_grid)
        btn_row.addWidget(self.match_grid_btn)
        outer.addLayout(btn_row)

        hint = QLabel("Page Up / Page Down also rotates the highlighted face.")
        hint.setStyleSheet("color: #888;")
        outer.addWidget(hint)

    def _add_row(self, grid, row, label, vmin, vmax, decimals, default, default_step):
        grid.addWidget(QLabel(label), row, 0)

        value = QDoubleSpinBox()
        value.setRange(vmin, vmax)
        value.setDecimals(decimals)
        value.setValue(default)
        value.setSingleStep(default_step)
        if label == "Rotate":
            value.setWrapping(True)
        value.valueChanged.connect(self._on_value_changed)
        grid.addWidget(value, row, 1)

        grid.addWidget(QLabel("Step"), row, 2)
        step = QDoubleSpinBox()
        step.setRange(0.001, 1000.0)
        step.setDecimals(decimals)
        step.setValue(default_step)
        # Editing the Step live-updates the value spin's increment.
        step.valueChanged.connect(lambda v, val=value: val.setSingleStep(v))
        grid.addWidget(step, row, 3)

        return value, step

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #
    def set_target(self, brush, face_name):
        """Bind the inspector to a face and show it."""
        self.target = (brush, face_name)
        self.refresh_from_face()
        self.show()
        self.raise_()
        self.activateWindow()

    def _cut_plane(self, brush, face):
        """Plane dict for an *untagged* (angled cut) face, else ``None``.

        Cut faces have no box tag, so their texture / scale live on the plane —
        not in the brush's per-tag dicts — and must be read & written there.
        """
        from engine import brush_geometry
        if not brush_geometry.brush_has_geometry(brush):
            return None
        pidx = brush_geometry.face_plane_index(brush, face)
        if pidx is None:
            return None
        planes = brush.get('geometry', {}).get('planes', [])
        if not (0 <= pidx < len(planes)):
            return None
        plane = planes[pidx]
        return plane if not plane.get('face') else None

    def refresh_from_face(self):
        """Reload every field from the bound face's stored transform."""
        if not self.target:
            return
        brush, face = self.target
        self._loading = True
        try:
            plane = self._cut_plane(brush, face)
            if plane is not None:
                self.tex_label.setText(plane.get('texture') or '(none)')
                shift = brush.get('uv_shift', {}).get(face, [0.0, 0.0])
                scale = plane.get('uv_scale') or [1.0, 1.0]
                angle = brush.get('uv_angle', {}).get(face, 0.0)
                self.hshift.setValue(float(shift[0]))
                self.vshift.setValue(float(shift[1]))
                self.hstretch.setValue(float(scale[0]))
                self.vstretch.setValue(float(scale[1]))
                self.rotate.setValue(float(angle))
                return
            self.tex_label.setText(brush.get('textures', {}).get(face, '(none)'))
            shift = brush.get('uv_shift', {}).get(face, [0.0, 0.0])
            scale = brush.get('uv_scale', {}).get(face, [1.0, 1.0])
            angle = brush.get('uv_angle', {}).get(face, 0.0)
            self.hshift.setValue(float(shift[0]))
            self.vshift.setValue(float(shift[1]))
            self.hstretch.setValue(float(scale[0]))
            self.vstretch.setValue(float(scale[1]))
            self.rotate.setValue(float(angle))
        finally:
            self._loading = False

    # ------------------------------------------------------------------ #
    # Editing                                                             #
    # ------------------------------------------------------------------ #
    def _on_value_changed(self, *_):
        if self._loading or not self.target:
            return
        brush, face = self.target
        self.editor.save_state()
        plane = self._cut_plane(brush, face)
        if plane is not None:
            # Cut face: stretch lives on the plane so the angled surface picks
            # it up. Shift / rotate aren't applied to angled faces by the
            # renderer yet, but are still recorded for when they are.
            plane['uv_scale'] = [self.hstretch.value(), self.vstretch.value()]
            brush.setdefault('uv_shift', {})[face] = [self.hshift.value(), self.vshift.value()]
            brush.setdefault('uv_angle', {})[face] = self.rotate.value()
            self.editor.update_views()
            return
        brush.setdefault('uv_shift', {})[face] = [self.hshift.value(), self.vshift.value()]
        brush.setdefault('uv_scale', {})[face] = [self.hstretch.value(), self.vstretch.value()]
        brush.setdefault('uv_angle', {})[face] = self.rotate.value()
        self.editor.update_views()

    def _match_grid(self):
        """Snap each value to the nearest multiple of its own Step."""
        if not self.target:
            return
        self._loading = True
        try:
            for value, step in self._rows:
                s = step.value()
                if s:
                    value.setValue(round(value.value() / s) * s)
        finally:
            self._loading = False
        self._on_value_changed()
