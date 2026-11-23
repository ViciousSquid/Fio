"""The Animation tab's Preview: a model's Rotate, played in the editor.

As the mover and door Preview does for a brush: the selected model rotates
in the 3D view -- continuously, swinging, or turning to its angle and back
-- until the button is pressed again, the selection changes, the map is
saved or Play starts. Only the Prop's runtime angle (``_rotate_angle``) is
driven, never its properties, so stopping puts the model back exactly as
placed.

It runs the same :class:`engine.model_rotate.RotateMotion` as Play, so what
the Preview shows is what the game does. A Turn model has no end state to
show by itself, so the Preview turns it to its Angle, holds, turns it back,
holds, and goes again.
"""

from __future__ import annotations

import time

from PyQt5.QtCore import QObject, QTimer

from engine import model_rotate
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


class RotatePreview(QObject):
    """Plays one model's Rotate in the editor."""

    #: Repaint interval, milliseconds.
    INTERVAL_MS = 16
    #: A Turn preview's pause at each end, seconds.
    TURN_HOLD = 0.6

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.prop = None
        self.motion = None
        self._last = None
        self._hold = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    @property
    def active(self):
        return self.prop is not None

    def start(self, prop):
        """Rotate *prop* (a model Prop) until :meth:`stop`."""
        self.stop()
        props = getattr(prop, 'properties', None)
        if props is None or not props.get('model_path'):
            return False
        if model_rotate.physics_owns_turn(props):
            return False                # as in Play: physics turns it, not Rotate
        bounds = None
        if str(props.get('rotate_axis', 'auto')).lower() not in ('x', 'y', 'z'):
            bounds = _editor_model_bounds(getattr(self.main_window, 'view_3d', None),
                                          props.get('model_path'))
        prop._rotate_axis = model_rotate.axis_index(props.get('rotate_axis'), bounds)
        prop._rotate_angle = 0.0
        self.prop = prop
        # From rest, set going -- as a StartRotate input does in Play: it
        # eases in (and a Turn model turns, holds, and comes back).
        self.motion = model_rotate.RotateMotion(props, running=False)
        self.motion.start()
        self._hold = 0.0
        self._last = time.perf_counter()
        self.timer.start(self.INTERVAL_MS)
        return True

    def restart(self):
        """Start again from as placed (the axis or mode was changed)."""
        prop = self.prop
        return self.start(prop) if prop is not None else False

    def stop(self):
        """Stop, and put the model back as it was placed."""
        self.timer.stop()
        prop, self.prop, self.motion = self.prop, None, None
        if prop is not None:
            prop._rotate_angle = 0.0
            touch(prop)
            self._repaint()
        return prop is not None

    def _tick(self):
        prop, motion = self.prop, self.motion
        if prop is None or motion is None:
            self.timer.stop()
            return
        if not self._still_previewed(prop):
            # Deselected without set_selected_objects (terrain picked) or
            # replaced by an undo: stop, as a selection change does.
            stop = getattr(self.main_window, 'stop_rotate_preview', None)
            if callable(stop):
                stop()
            else:
                self.stop()
            return
        now = time.perf_counter()
        delta = min(0.1, max(0.0, now - (self._last or now)))
        self._last = now
        motion.speed = model_rotate.rotate_speed(prop.properties)   # edits apply live
        if motion.mode != model_rotate.rotate_mode(prop.properties):
            self.restart()
            return
        if self._hold > 0.0:
            self._hold -= delta
            if self._hold <= 0.0:
                motion.toggle()                     # Turn: back, then on again
        else:
            for event in motion.step(delta):
                if event == 'stopped' and motion.mode == model_rotate.TURN:
                    self._hold = self.TURN_HOLD
        prop._rotate_angle = motion.angle
        self._repaint()

    def _still_previewed(self, prop):
        """*prop* is still in the scene and still selected."""
        state = getattr(self.main_window, 'state', None)
        if state is None:
            return True
        if not any(t is prop for t in getattr(state, 'things', ())):
            return False
        return any(o is prop for o in getattr(state, 'selected_objects', ()))

    def _repaint(self):
        view = getattr(self.main_window, 'view_3d', None)
        if view is not None:
            view.update()
