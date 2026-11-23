"""The Animation tab's Preview: a model's spin, played in the editor.

As the mover and door Preview does for a brush: the selected model spins
in the 3D view -- spinning up over its Ramp at its Speed, about its Axis --
until the button is pressed again, the selection changes, the map is saved
or Play starts. Only the Prop's runtime angle (``_spin_angle``) is driven,
never its properties, so stopping puts the model back exactly as placed.

The same maths as Play (engine.model_spin), so what the Preview shows is
what the game does.
"""

from __future__ import annotations

import time

from PyQt5.QtCore import QObject, QTimer

from engine import model_spin
from engine.change_journal import touch


def _editor_model_bounds(view_3d, model_path):
    """``(lo, hi)`` of a model the 3D view has loaded, or None."""
    renderer = getattr(view_3d, 'renderer', None)
    if renderer is None or not model_path:
        return None
    try:
        obj = renderer.get_loaded_model(model_path)
        if not obj or not obj.is_loaded:
            return None
        from editor import model_wireframe
        found = model_wireframe.model_edges(obj)
        if not found:
            return None
        positions = found[0]
        if not len(positions):
            return None
        return positions.min(axis=0), positions.max(axis=0)
    except Exception:
        return None


class SpinPreview(QObject):
    """Plays one model's spin in the editor."""

    #: Repaint interval, milliseconds.
    INTERVAL_MS = 16

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.prop = None
        self._speed = 0.0
        self._last = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    @property
    def active(self):
        return self.prop is not None

    def start(self, prop):
        """Spin *prop* (a model Prop) until :meth:`stop`."""
        self.stop()
        props = getattr(prop, 'properties', None)
        if props is None or not props.get('model_path'):
            return False
        bounds = None
        if str(props.get('spin_axis', 'auto')).lower() not in ('x', 'y', 'z'):
            bounds = _editor_model_bounds(getattr(self.main_window, 'view_3d', None),
                                          props.get('model_path'))
        prop._spin_axis = model_spin.axis_index(props.get('spin_axis'), bounds)
        prop._spin_angle = 0.0
        self.prop = prop
        self._speed = 0.0
        self._last = time.perf_counter()
        self.timer.start(self.INTERVAL_MS)
        return True

    def stop(self):
        """Stop, and put the model back as it was placed."""
        self.timer.stop()
        prop, self.prop = self.prop, None
        self._speed = 0.0
        if prop is not None:
            prop._spin_angle = 0.0
            touch(prop)
            self._repaint()
        return prop is not None

    def _tick(self):
        prop = self.prop
        if prop is None:
            self.timer.stop()
            return
        now = time.perf_counter()
        delta = min(0.1, max(0.0, now - (self._last or now)))
        self._last = now
        props = prop.properties
        full = model_spin.spin_speed(props)
        rate = model_spin.ramp_rate(full, model_spin.spin_ramp(props))
        self._speed = model_spin.approach(self._speed, full, rate, delta)
        if self._speed:
            prop._spin_angle = (prop._spin_angle + self._speed * delta) % 360.0
        self._repaint()

    def _repaint(self):
        view = getattr(self.main_window, 'view_3d', None)
        if view is not None:
            view.update()
