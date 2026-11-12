"""The Decal tool: place, select, drag and nudge decals in the 3D view.

It works the way terrain stamps do:

* The Asset Browser's **DECAL** button (Textures tab) turns it on and shows
  ``textures/decals``; clicking it again goes back to the folder you were in.
* **Left-click** places the image selected in the Asset Browser (any image in
  ``textures/decals``) on the surface under the cursor -- a brush, the
  terrain, a model -- facing it.
* **Shift-click** a decal to select it (again to deselect; on nothing, to
  clear). Selected decals are drawn tinted.
* **Press a selected decal and drag** to move the selection across surfaces;
  the one under the cursor turns to face each surface it crosses. One drag
  is one undo step.
* **Arrow keys** nudge the selection one grid step (five with Shift), on a
  floor along X/Z as stamps do (Up is -Z), on a wall along it and up/down.
  A run of key repeats is one undo step.
* **Delete** or **Backspace** removes the selection; **Escape** clears it,
  then leaves the tool.

An image with "wet" or "puddle" in its name places a Wet decal -- a mask of
where the surface is wet (darkened, reflecting the scene); any other image
is painted. There is no separate setting.

A decal is a ``Decal`` entity -- saved with the map, undone with the rest of
it -- and nothing else draws it: the logic thread makes it a row of the decal
table blood uses (``DecalTable.place``), and the decal pass paints it. The
tool panel's settings are what new decals get; changing them also changes
the selected decals.
"""

import math
import os

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QDoubleSpinBox, QFormLayout, QLabel,
                             QVBoxLayout)

from editor.things import Decal
from engine.change_journal import touch


def orientation_from_normal(normal):
    """``(yaw, pitch)`` in degrees throwing a decal into a surface that faces
    *normal* (the inverse of ``engine.decal_table.placed_row``)."""
    n = np.asarray(normal, dtype=np.float64)
    n = n / max(float(np.linalg.norm(n)), 1e-9)
    f = -n
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(f[1])))))
    if abs(f[1]) > 0.999:
        return 0.0, pitch
    return math.degrees(math.atan2(-float(f[2]), float(f[0]))), pitch


def decal_box_inverse(decal):
    """The world-to-box matrix of *decal* (None when it has no size)."""
    from engine.decal_table import placed_row
    row = placed_row(decal.pos, decal.properties, 0)
    if row is None:
        return None
    model = np.asarray(row[0], dtype=np.float64).reshape(4, 4).T
    return np.linalg.inv(model)


class DecalTool:
    """Owned by the 3D view; see the module docstring."""

    NUDGE_IDLE_MS = 350

    def __init__(self, view):
        self.view = view
        self.active = False
        #: What new decals get (the panel edits these).
        self.settings = {'width': 128.0, 'height': 128.0,
                         'depth': 32.0, 'roll': 0.0, 'opacity': 1.0}
        self.selected_ids = set()
        self._drag = None
        self._panel = None
        self._nudge_timer = None

    # -- the scene -----------------------------------------------------------

    @property
    def editor(self):
        return self.view.editor

    def decals(self):
        return [t for t in self.editor.state.things if isinstance(t, Decal)]

    def selected(self):
        return [d for d in self.decals() if d.properties.get('id') in self.selected_ids]

    def _sync_highlight(self):
        """Selected decals draw tinted (DecalTable.place reads this)."""
        for decal in self.decals():
            selected = decal.properties.get('id') in self.selected_ids
            if decal.editor_selected != selected:
                decal.editor_selected = selected
                touch(decal)                     # its row is remade, tinted or not
        self.view.update()

    def select(self, ids):
        self.selected_ids = set(ids)
        self._sync_highlight()
        decals = self.selected()
        if self._panel is not None and len(decals) == 1:
            self._panel.show_values(decals[0].properties)

    def clear_selection(self):
        if not self.selected_ids:
            return False
        self.select(())
        return True

    # -- on/off ----------------------------------------------------------------

    def set_active(self, active):
        active = bool(active)
        if active == self.active:
            return
        if active and getattr(self.view, 'terrain_sculpt_active', False):
            self.view.terrain_edit.set_terrain_sculpt_active(False)
        self.active = active
        self._drag = None
        if active:
            self.view.setCursor(Qt.CrossCursor)
            self.panel().show()
        else:
            self.clear_selection()
            self.view.setCursor(Qt.ArrowCursor)
            if self._panel is not None:
                self._panel.hide()
        # The Tools menu item shows the tool's state; the Asset Browser's
        # DECAL button too, and its Textures tab shows textures/decals while
        # the tool is on (and goes back to its folder after).
        action = getattr(self.editor, 'decal_tool_action', None)
        if action is not None and action.isChecked() != active:
            action.blockSignals(True)
            action.setChecked(active)
            action.blockSignals(False)
        browser = getattr(self.editor, 'asset_browser', None)
        textures = getattr(browser, 'tab_textures', None)
        if textures is not None and hasattr(textures, 'show_decal_folder'):
            textures.show_decal_folder(active)
        self.view.update()

    # -- where the cursor is -------------------------------------------------

    def surface_at(self, mx, my):
        """``(point, normal)`` of what the 3D view drew under (mx, my), read
        back from its depth buffer as the decal pass reads it; None over the
        sky or with no GL."""
        view = self.view
        try:
            import OpenGL.GL as gl
            import glm
            scale = view.devicePixelRatioF()
            width, height = int(view.width() * scale), int(view.height() * scale)
            px, py = int(mx * scale), int(height - 1 - my * scale)
            if not (1 <= px < width - 1 and 1 <= py < height - 1):
                return None
            view.makeCurrent()
            try:
                gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, view.defaultFramebufferObject())
                data = gl.glReadPixels(px - 1, py - 1, 3, 3, gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT)
            finally:
                view.doneCurrent()
            depth = np.frombuffer(data, dtype=np.float32).reshape(3, 3) if not isinstance(
                data, np.ndarray) else np.asarray(data, dtype=np.float32).reshape(3, 3)
            if depth[1, 1] >= 0.99999:
                return None
            inverse = glm.inverse(view.projection_matrix * view.view_matrix)

            def world(ix, iy):
                d = float(depth[iy, ix])
                if d >= 0.99999:
                    d = float(depth[1, 1])
                ndc = glm.vec4((px - 1 + ix + 0.5) / width * 2.0 - 1.0,
                               (py - 1 + iy + 0.5) / height * 2.0 - 1.0,
                               d * 2.0 - 1.0, 1.0)
                p = inverse * ndc
                return np.array((p.x / p.w, p.y / p.w, p.z / p.w))

            centre = world(1, 1)
            normal = np.cross(world(2, 1) - world(0, 1), world(1, 2) - world(1, 0))
            if float(np.linalg.norm(normal)) < 1e-9:
                normal = np.array((0.0, 1.0, 0.0))
            normal /= np.linalg.norm(normal)
            camera = view.logic_thread.camera.get_editor_camera().pos
            if float(np.dot(normal, np.array((camera.x, camera.y, camera.z)) - centre)) < 0.0:
                normal = -normal
            return tuple(centre), tuple(normal)
        except Exception:
            return None

    def decal_at(self, point):
        """The topmost decal whose box holds *point*, or None."""
        p = np.array((*point, 1.0))
        for decal in reversed(self.decals()):
            inverse = decal_box_inverse(decal)
            if inverse is None:
                continue
            local = inverse @ p
            if np.all(np.abs(local[:3]) <= 0.5):
                return decal
        return None

    # -- placing -----------------------------------------------------------------

    def chosen_image(self):
        """The decal image selected in the Asset Browser, or None (said)."""
        from engine import decal_textures
        browser = getattr(self.editor, 'asset_browser', None)
        path = browser.get_selected_filepath() if browser is not None else None
        if path:
            names = decal_textures.images()[1]
            if decal_textures.layer_of(path, names) < 0:
                names = decal_textures.refresh()[1]
            if decal_textures.layer_of(path, names) >= 0:
                return names[decal_textures.layer_of(path, names)]
        self.editor.show_toast(
            "Select an image from textures/decals in the Asset Browser first",
            is_error=True)
        return None

    def place_at(self, point, normal, image):
        """Place a new decal of *image* on the surface at *point* facing
        *normal*; it becomes the selection. One undo step."""
        yaw, pitch = orientation_from_normal(normal)
        s = self.settings
        self.editor.save_state()
        decal = Decal(pos=[float(v) for v in point], properties={
            'texture': image,
            'width': float(s['width']), 'height': float(s['height']),
            'depth': float(s['depth']), 'yaw': yaw, 'pitch': pitch,
            'roll': float(s['roll']), 'opacity': float(s['opacity'])})
        self.editor.state.things.append(decal)
        self.select({decal.properties['id']})
        self.editor.show_toast(f"Placed {image}")
        return decal

    # -- mouse ---------------------------------------------------------------

    def press(self, event):
        """A left press in the 3D view while the tool is on. Always handled."""
        hit = self.surface_at(event.x(), event.y())
        if hit is None:
            if event.modifiers() & Qt.ShiftModifier:
                self.clear_selection()
            return True
        point, normal = hit
        decal = self.decal_at(point)
        decal_id = decal.properties.get('id') if decal is not None else None
        if event.modifiers() & Qt.ShiftModifier:
            if decal_id is None:
                self.clear_selection()
                return True
            if decal_id in self.selected_ids:
                self._drag = {'last': point, 'moved': False, 'toggle_off': decal_id,
                              'primary': decal_id}
            else:
                self.select(self.selected_ids | {decal_id})
                self._drag = {'last': point, 'moved': False, 'toggle_off': None,
                              'primary': decal_id}
            return True
        if decal_id is not None and decal_id in self.selected_ids:
            self._drag = {'last': point, 'moved': False, 'toggle_off': None,
                          'primary': decal_id}
            return True
        image = self.chosen_image()
        if image is not None:
            self.place_at(point, normal, image)
        return True

    def dragging(self):
        return self._drag is not None

    def drag(self, mx, my):
        drag = self._drag
        hit = self.surface_at(mx, my)
        if drag is None or hit is None:
            return
        point, normal = hit
        delta = np.subtract(point, drag['last'])
        if not drag['moved']:
            if float(np.linalg.norm(delta)) < 0.5:
                return
            self.editor.save_state()                 # the whole drag: one undo step
            drag['moved'] = True
        yaw, pitch = orientation_from_normal(normal)
        for decal in self.selected():
            if decal.properties.get('id') == drag['primary']:
                # The decal under the cursor follows the surface it is on.
                decal.properties['yaw'], decal.properties['pitch'] = yaw, pitch
                decal.pos = [float(v) for v in point]     # journals it, turn and all
            else:
                decal.pos = [float(a + b) for a, b in zip(decal.pos, delta)]
        drag['last'] = point
        self.view.update()

    def release(self):
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if not drag['moved'] and drag['toggle_off'] is not None:
            self.select(self.selected_ids - {drag['toggle_off']})
        elif drag['moved']:
            self.editor.mark_as_modified()
        self.view.update()

    # -- keys ------------------------------------------------------------------

    def nudge(self, key, shift=False, grid=16.0, grid_visible=True):
        """Arrow-key nudge of the selection; True when it moved something."""
        decals = self.selected()
        if not self.active or not decals:
            return False
        step = (grid if grid_visible and grid else 16.0) * (5 if shift else 1)
        primary = decals[0]
        throw = np.array(primary.throw_direction())
        if abs(throw[1]) > 0.7:                       # a floor/ceiling: X/Z, as stamps
            axes = {Qt.Key_Left: (-1, 0, 0), Qt.Key_Right: (1, 0, 0),
                    Qt.Key_Up: (0, 0, -1), Qt.Key_Down: (0, 0, 1)}
        else:                                         # a wall: along it, and up/down
            right = np.cross((0.0, 1.0, 0.0), throw)
            right /= max(float(np.linalg.norm(right)), 1e-9)
            axes = {Qt.Key_Left: tuple(-right), Qt.Key_Right: tuple(right),
                    Qt.Key_Up: (0, 1, 0), Qt.Key_Down: (0, -1, 0)}
        if key not in axes:
            return False
        move = np.array(axes[key], dtype=np.float64) * step
        if grid_visible and grid:
            # The first decal lands on the grid along a world axis it moves on.
            for i in range(3):
                if abs(move[i]) > 1e-6 and abs(abs(move[i]) - step) < 1e-6:
                    target = round((primary.pos[i] + move[i]) / grid) * grid
                    move[i] = target - primary.pos[i]
        if self._nudge_timer is None:
            self._nudge_timer = QTimer()
            self._nudge_timer.setSingleShot(True)
        if not self._nudge_timer.isActive():
            self.editor.save_state()                  # a run of repeats: one undo step
        for decal in decals:
            decal.pos = [float(a + b) for a, b in zip(decal.pos, move)]
        self._nudge_timer.start(self.NUDGE_IDLE_MS)
        self.view.update()
        return True

    def delete_selected(self):
        decals = self.selected()
        if not decals:
            return False
        self.editor.save_state()
        for decal in decals:
            self.editor.state.things.remove(decal)
        self.select(())
        self.editor.show_toast(f"Deleted {len(decals)} decal{'s' if len(decals) > 1 else ''}")
        return True

    def escape(self):
        """Escape: clear the selection, else leave the tool."""
        if not self.clear_selection():
            self.set_active(False)

    # -- panel -------------------------------------------------------------------

    def panel(self):
        if self._panel is None:
            self._panel = DecalToolPanel(self)
        return self._panel

    def apply_setting(self, key, value):
        """A panel change: new decals get it, and so do the selected ones."""
        self.settings[key] = value
        decals = self.selected()
        if not decals:
            return
        self.editor.save_state()
        for decal in decals:
            decal.properties[key] = value
            touch(decal)
        self.view.update()


class DecalToolPanel(QDialog):
    """The Decal tool's settings, beside the 3D view."""

    def __init__(self, tool):
        super().__init__(tool.view.editor,
                         Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.tool = tool
        self.setWindowTitle('Decal')
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Left-click a surface to place the image selected in the Asset "
            "Browser (textures/decals). Shift-click a decal to select it, then "
            "drag it or nudge it with the arrow keys. Delete removes it. "
            "Images with \"wet\" or \"puddle\" in the name make the surface wet.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(hint)
        form = QFormLayout()
        layout.addLayout(form)
        s = tool.settings

        def spin(key, label, lo, hi, step, decimals=0, suffix=''):
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setDecimals(decimals)
            box.setSingleStep(step)
            box.setSuffix(suffix)
            box.setValue(float(s[key]))
            box.valueChanged.connect(lambda v: tool.apply_setting(key, float(v)))
            form.addRow(label, box)
            return box

        # Scale one up to cover a whole wall or floor: a solid mask (such as
        # wet_full.png) then makes all of it wet and glossy.
        self.width_spin = spin('width', "Width:", 8, 16384, 16, suffix=' u')
        self.height_spin = spin('height', "Height:", 8, 16384, 16, suffix=' u')
        self.depth_spin = spin('depth', "Depth:", 4, 1024, 8, suffix=' u')
        self.roll_spin = spin('roll', "Rotation:", -360, 360, 15, suffix='°')
        self.opacity_spin = spin('opacity', "Opacity / wetness:", 0, 1, 0.1, decimals=2)

    def show_values(self, props):
        """Show a selected decal's settings (without applying them back)."""
        widgets = ((self.width_spin, 'width'), (self.height_spin, 'height'),
                   (self.depth_spin, 'depth'), (self.roll_spin, 'roll'),
                   (self.opacity_spin, 'opacity'))
        for widget, _key in widgets:
            widget.blockSignals(True)
        try:
            for widget, key in widgets:
                try:
                    widget.setValue(float(props.get(key, widget.value())))
                except (TypeError, ValueError):
                    pass
        finally:
            for widget, _key in widgets:
                widget.blockSignals(False)

    def closeEvent(self, event):
        self.tool.set_active(False)
        super().closeEvent(event)
