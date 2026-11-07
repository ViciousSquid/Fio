"""Drive a brush's runtime transform from a test (unified brush model, 2b).

Since Stage 2b a mover's displacement and spin are owned by a runtime source
(the logic thread's ``MoverTable``, the editor's mover preview) and are never
written into the brush, so a test that needs a moved or spinning brush asks
this source -- registered with ``engine.brush_placement`` like the real ones
-- instead of writing ``pos`` or ``_rot_angle``. The root ``conftest`` clears
it after every test.

Only a brush flagged ``is_mover`` or ``is_door`` has a runtime transform;
:func:`drive` flags a plain brush ``is_mover`` for the caller.
"""

from engine import brush_placement

SOURCE = brush_placement.RuntimeOverrides()
brush_placement.register_runtime(SOURCE)


def drive(brush, offset=(0.0, 0.0, 0.0), spin=None, centre=None):
    """Carry *brush* by *offset* from its authored centre (or to *centre*)
    and turn it by *spin* (``(axis, angle_degrees)``); returns the brush."""
    if not (brush.get('is_mover') or brush.get('is_door')):
        brush['is_mover'] = True
    SOURCE.set(brush, offset=offset, spin=spin, centre=centre)
    return brush


def release(brush=None):
    """Drop *brush*'s (or every) driven transform."""
    SOURCE.clear(brush)
