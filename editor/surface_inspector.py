"""
surface_inspector.py
A Radiant-style Surface Inspector: the panel you texture geometry from.

It edits one face's mapping, or every face of the selected brushes at once,
and it works the same whether that face is one of a box brush's six tagged
sides or a cut face on an angled plane set — :mod:`editor.face_texture` hides
which of the two places a given face keeps its transform in.

Layout follows Radiant's: a value/step grid for shift, scale and rotation, then
the projection buttons a mapper actually reaches for — Fit (span the face a
given number of times), Natural (one texel per world unit, whatever the face's
size) and Axial (back to a plain world-axis projection).

A run of spin-box clicks is one undo step, not one per click: the panel opens
an undo checkpoint when editing starts and closes it after a pause, the same
coalescing the 2D views use for arrow-key nudges.
"""

import os

from PyQt5.QtWidgets import (
    QDialog, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDoubleSpinBox, QFrame, QSizePolicy, QRadioButton, QButtonGroup, QWidget,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImageReader

from editor import face_texture as ft


# The Face Mode toggle's look, carried over verbatim from the Asset Browser so
# the button a user already knows keeps its colour after the move.
FACE_BUTTON_STYLE = """
    QPushButton {
        background-color: #7B1FA2;
        color: white;
        font: 9pt;
        font-weight: bold;
        padding: 6px 12px;
        border: 1px solid #4A148C;
        border-radius: 5px;
    }
    QPushButton:hover { background-color: #8E24AA; }
    QPushButton:pressed { background-color: #4A148C; }
    QPushButton:checked { background-color: #D500F9; border: 1px solid white; }
    QPushButton:disabled { background-color: #444; color: #888; border: 1px solid #555; }
"""


class SurfaceInspector(QDialog):
    """Floating panel that edits the texture mapping of a face or a brush."""

    #: ms of inactivity that closes an undo burst
    UNDO_IDLE_MS = 600

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.target = None          # (brush, face_key) currently being edited
        self._loading = False       # suppress apply while populating fields
        self._undo_open = False

        # A burst of spin-box clicks is one undo step; this closes the burst
        # after a pause, exactly like the 2D views' nudge coalescing.
        self._undo_idle = QTimer(self)
        self._undo_idle.setSingleShot(True)
        self._undo_idle.setInterval(self.UNDO_IDLE_MS)
        self._undo_idle.timeout.connect(self._close_undo_burst)

        self._texture_sizes = {}    # filename -> (w, h), read once each

        self.setWindowTitle("Surface Inspector")
        # Floating tool window that stays above the editor without stealing it.
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowCloseButtonHint)
        self.setMinimumWidth(360)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI construction                                                     #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # --- Face Mode toggle (moved here from the Asset Browser: picking a
        #     face to texture belongs with the panel that textures it) ---
        self.face_btn = QPushButton("FACE")
        self.face_btn.setCheckable(True)
        self.face_btn.setStyleSheet(FACE_BUTTON_STYLE)
        self.face_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.face_btn.setToolTip(
            "Toggle Face Selection Mode\n"
            "Click a face in the 3D view to texture it; drag it to move its "
            "plane\n"
            "Page Up / Page Down rotates the highlighted face's texture")
        self.face_btn.clicked.connect(self._on_face_mode_clicked)
        outer.addWidget(self.face_btn)

        # --- What is being edited ---
        head = QGridLayout()
        head.setColumnStretch(1, 1)
        head.addWidget(QLabel("Texture"), 0, 0)
        self.tex_label = QLabel("(none)")
        self.tex_label.setStyleSheet("font-weight: bold;")
        self.tex_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        head.addWidget(self.tex_label, 0, 1)
        self.apply_tex_btn = QPushButton("Apply selected")
        self.apply_tex_btn.setToolTip(
            "Put the texture selected in the Asset Browser on the target")
        self.apply_tex_btn.clicked.connect(self._apply_selected_texture)
        head.addWidget(self.apply_tex_btn, 0, 2)

        head.addWidget(QLabel("Face"), 1, 0)
        self.face_label = QLabel("(none)")
        head.addWidget(self.face_label, 1, 1)
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("color: #888;")
        head.addWidget(self.size_label, 1, 2)
        outer.addLayout(head)

        # --- Scope: this face, or every face of the selection ---
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Apply to"))
        self.scope_face = QRadioButton("This face")
        self.scope_brush = QRadioButton("Whole brush")
        self.scope_face.setChecked(True)
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.scope_face)
        self.scope_group.addButton(self.scope_brush)
        scope_row.addWidget(self.scope_face)
        scope_row.addWidget(self.scope_brush)
        scope_row.addStretch(1)
        outer.addLayout(scope_row)

        outer.addWidget(self._separator())

        # --- Value / Step grid ---
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.addWidget(self._dim(QLabel("Value")), 0, 1)
        grid.addWidget(self._dim(QLabel("Step")), 0, 3)
        outer.addLayout(grid)

        self.hshift, self.hshift_step = self._add_row(
            grid, 1, "Horizontal shift", -8192.0, 8192.0, 3, 0.0, 0.125, 'shift')
        self.vshift, self.vshift_step = self._add_row(
            grid, 2, "Vertical shift", -8192.0, 8192.0, 3, 0.0, 0.125, 'shift')
        self.hstretch, self.hstretch_step = self._add_row(
            grid, 3, "Horizontal scale", -64.0, 64.0, 3, 1.0, 0.5, 'scale')
        self.vstretch, self.vstretch_step = self._add_row(
            grid, 4, "Vertical scale", -64.0, 64.0, 3, 1.0, 0.5, 'scale')
        self.rotate, self.rotate_step = self._add_row(
            grid, 5, "Rotate", -360.0, 360.0, 2, 0.0, 45.0, 'angle')

        self._rows = (
            (self.hshift, self.hshift_step),
            (self.vshift, self.vshift_step),
            (self.hstretch, self.hstretch_step),
            (self.vstretch, self.vstretch_step),
            (self.rotate, self.rotate_step),
        )

        outer.addWidget(self._separator())

        # --- Fit: span the face a given number of times ---
        fit_row = QHBoxLayout()
        fit_row.addWidget(QLabel("Fit"))
        self.fit_w = QDoubleSpinBox()
        self.fit_w.setRange(0.01, 256.0)
        self.fit_w.setDecimals(2)
        self.fit_w.setValue(1.0)
        self.fit_w.setToolTip("Times the texture repeats across the face")
        self.fit_h = QDoubleSpinBox()
        self.fit_h.setRange(0.01, 256.0)
        self.fit_h.setDecimals(2)
        self.fit_h.setValue(1.0)
        self.fit_h.setToolTip("Times the texture repeats up the face")
        fit_row.addWidget(QLabel("W"))
        fit_row.addWidget(self.fit_w)
        fit_row.addWidget(QLabel("H"))
        fit_row.addWidget(self.fit_h)
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setToolTip("Stretch the texture to span the face exactly")
        self.fit_btn.clicked.connect(self._apply_fit)
        fit_row.addWidget(self.fit_btn)
        fit_row.addStretch(1)
        outer.addLayout(fit_row)

        # --- Projection + tidy-up buttons ---
        proj_row = QHBoxLayout()
        self.natural_btn = QPushButton("Natural")
        self.natural_btn.setCheckable(True)
        self.natural_btn.setToolTip(
            "One texel per world unit — the texture at its own size, neither\n"
            "stretched nor squashed, whatever the face's dimensions.\n"
            "Stays on: resizing the brush reveals more texture instead of\n"
            "stretching it.  Setting a scale by hand switches it off.")
        self.natural_btn.clicked.connect(self._toggle_natural)
        proj_row.addWidget(self.natural_btn)

        self.axial_btn = QPushButton("Axial")
        self.axial_btn.setToolTip(
            "Back to a plain world-axis projection at natural size, dropping\n"
            "any texture rotation the face picked up from turning its brush")
        self.axial_btn.clicked.connect(self._apply_axial)
        proj_row.addWidget(self.axial_btn)

        self.match_grid_btn = QPushButton("Match Grid")
        self.match_grid_btn.setToolTip("Snap every value to its Step increment")
        self.match_grid_btn.clicked.connect(self._match_grid)
        proj_row.addWidget(self.match_grid_btn)
        proj_row.addStretch(1)
        outer.addLayout(proj_row)

        flip_row = QHBoxLayout()
        self.flip_h_btn = QPushButton("Flip H")
        self.flip_h_btn.setToolTip("Mirror the texture horizontally")
        self.flip_h_btn.clicked.connect(lambda: self._flip(horizontal=True))
        flip_row.addWidget(self.flip_h_btn)
        self.flip_v_btn = QPushButton("Flip V")
        self.flip_v_btn.setToolTip("Mirror the texture vertically")
        self.flip_v_btn.clicked.connect(lambda: self._flip(vertical=True))
        flip_row.addWidget(self.flip_v_btn)
        flip_row.addStretch(1)
        outer.addLayout(flip_row)

        hint = QLabel("T reopens this panel · Page Up/Down rotates the "
                      "hovered face")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        outer.addWidget(hint)

    @staticmethod
    def _dim(widget):
        widget.setStyleSheet("color: #888;")
        return widget

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _add_row(self, grid, row, label, vmin, vmax, decimals, default,
                 default_step, field):
        grid.addWidget(QLabel(label), row, 0)

        value = QDoubleSpinBox()
        value.setRange(vmin, vmax)
        value.setDecimals(decimals)
        value.setValue(default)
        value.setSingleStep(default_step)
        if label == "Rotate":
            value.setWrapping(True)
        value.valueChanged.connect(lambda _v, f=field: self._on_value_changed(f))
        grid.addWidget(value, row, 1)

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
    def set_target(self, brush, face_key):
        """Bind the inspector to a face and show it."""
        self.target = (brush, face_key)
        self.refresh_from_face()
        self.show()
        self.raise_()
        self.activateWindow()

    def target_faces(self):
        """The (brush, face key) pairs the current controls apply to.

        One face in "This face" scope; every face of every selected brush in
        "Whole brush" scope, so a wall can be textured in a single gesture.
        """
        if not self.target:
            return []
        brush, face_key = self.target
        if self.scope_face.isChecked():
            return [(brush, face_key)]
        brushes = [brush]
        selected = getattr(self.editor, '_selected_brushes', None)
        if selected is not None:
            chosen = [b for b in selected() if isinstance(b, dict)]
            if brush in chosen:
                brushes = chosen
        return [(b, key) for b in brushes for key in ft.face_keys(b)]

    # ------------------------------------------------------------------ #
    # Reading                                                             #
    # ------------------------------------------------------------------ #
    def refresh_from_face(self):
        """Reload every field from the bound face's stored transform."""
        if not self.target:
            return
        brush, face_key = self.target
        transform = ft.get_transform(
            brush, face_key,
            texture_size=self._texture_size(
                ft.get_transform(brush, face_key)['texture']))
        self._loading = True
        try:
            self.tex_label.setText(transform['texture'] or '(none)')
            self.face_label.setText(
                face_key if not face_key.startswith('#')
                else 'cut face %s' % face_key[1:])
            width, height = ft.face_extent(brush, face_key)
            self.size_label.setText("%.0f x %.0f" % (width, height))
            self.hshift.setValue(transform['shift'][0])
            self.vshift.setValue(transform['shift'][1])
            self.hstretch.setValue(transform['scale'][0])
            self.vstretch.setValue(transform['scale'][1])
            self.rotate.setValue(transform['angle'])
            self.natural_btn.setChecked(transform['natural'])
        finally:
            self._loading = False

    def _on_face_mode_clicked(self):
        """Hand the toggle straight to the editor's Face Mode."""
        toggle = getattr(self.editor, 'toggle_face_mode', None)
        if toggle is not None:
            toggle(self.face_btn.isChecked())

    def sync_face_button(self, active):
        """Reflect Face Mode being switched on or off from somewhere else."""
        if self.face_btn.isChecked() != active:
            self.face_btn.blockSignals(True)
            self.face_btn.setChecked(active)
            self.face_btn.blockSignals(False)

    def _sync_natural_button(self):
        """Keep the Natural toggle showing the bound face's actual mode."""
        if not self.target:
            return
        wanted = ft.is_natural(*self.target)
        if self.natural_btn.isChecked() != wanted:
            self.natural_btn.blockSignals(True)
            self.natural_btn.setChecked(wanted)
            self.natural_btn.blockSignals(False)

    def _texture_size(self, name):
        """Pixel size of a texture, read from its header and cached."""
        if not name:
            return ft.DEFAULT_TEXTURE_SIZE
        cached = self._texture_sizes.get(name)
        if cached is not None:
            return cached
        size = ft.DEFAULT_TEXTURE_SIZE
        root = getattr(self.editor, 'root_dir', '')
        path = os.path.join(root, 'assets', 'textures', name)
        reader = QImageReader(path)
        read = reader.size()
        if read.isValid() and read.width() > 0 and read.height() > 0:
            size = (read.width(), read.height())
        self._texture_sizes[name] = size
        return size

    # ------------------------------------------------------------------ #
    # Undo coalescing                                                     #
    # ------------------------------------------------------------------ #
    def _begin_undo_burst(self):
        """Open one checkpoint for a run of edits, and keep it open."""
        if not self._undo_open:
            self.editor.save_state()
            self._undo_open = True
        self._undo_idle.start()

    def _close_undo_burst(self):
        self._undo_open = False

    def _commit(self):
        """Push the change to the views and mark the map dirty."""
        self.editor.unsaved_changes = True
        self.editor.state.mark_lighting_dirty()
        self.editor.update_views()

    # ------------------------------------------------------------------ #
    # Editing                                                             #
    # ------------------------------------------------------------------ #
    def _on_value_changed(self, field=None):
        if self._loading or not self.target:
            return
        self._begin_undo_burst()
        shift = (self.hshift.value(), self.vshift.value())
        scale = (self.hstretch.value(), self.vstretch.value())
        angle = self.rotate.value()
        for brush, face_key in self.target_faces():
            # Writing a scale switches Natural off, so only send one when the
            # scale is what actually changed (or the face was not Natural
            # anyway).  Nudging the shift must not silently drop the mode.
            send_scale = scale if (field != 'shift' and field != 'angle') \
                or not ft.is_natural(brush, face_key) else None
            ft.set_transform(brush, face_key, shift=shift, scale=send_scale,
                             angle=angle)
        self._commit()
        self._sync_natural_button()

    def _apply_selected_texture(self):
        """Put the Asset Browser's current texture on the target faces."""
        if not self.target:
            return
        browser = getattr(self.editor, 'asset_browser', None)
        path = browser.get_selected_filepath() if browser is not None else None
        if not path:
            self.editor.show_toast("Select a texture first", is_error=True)
            return
        name = os.path.basename(path)
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            ft.set_transform(brush, face_key, texture=name)
        self._commit()
        self.refresh_from_face()

    def _apply_fit(self):
        """Stretch the texture to span each target face the given times."""
        if not self.target:
            return
        repeat = (self.fit_w.value(), self.fit_h.value())
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            ft.apply_fit(brush, face_key, repeat)
        self._commit()
        self.refresh_from_face()

    def _toggle_natural(self):
        """Turn Natural mode on, or bake its current scale and turn it off.

        Switching off freezes whatever the face is drawing right now, so the
        texture does not jump — it simply stops tracking the face's size.
        """
        if not self.target:
            return
        wanted = self.natural_btn.isChecked()
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            size = self._texture_size(ft.get_transform(brush, face_key)['texture'])
            if wanted:
                ft.apply_natural(brush, face_key, size)
            else:
                # An explicit scale write clears the mode for us.
                ft.set_transform(brush, face_key,
                                 scale=ft.natural_scale(brush, face_key, size))
        self._commit()
        self.refresh_from_face()

    def _apply_axial(self):
        """Drop back to a world-axis projection at natural size."""
        if not self.target:
            return
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            size = self._texture_size(ft.get_transform(brush, face_key)['texture'])
            ft.apply_axial(brush, face_key, size)
        self._commit()
        self.refresh_from_face()

    def _flip(self, horizontal=False, vertical=False):
        if not self.target:
            return
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            ft.flip(brush, face_key, horizontal=horizontal, vertical=vertical)
        self._commit()
        self.refresh_from_face()

    def _match_grid(self):
        """Snap each value to the nearest multiple of its own Step."""
        if not self.target:
            return
        steps = {
            'shift': (self.hshift_step.value(), self.vshift_step.value()),
            'scale': (self.hstretch_step.value(), self.vstretch_step.value()),
            'angle': self.rotate_step.value(),
        }
        self._begin_undo_burst()
        for brush, face_key in self.target_faces():
            ft.match_grid(brush, face_key, steps)
        self._commit()
        self.refresh_from_face()
