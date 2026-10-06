"""Describe the editor's selection for the renderer.

The renderer draws the selection overlay (outline, trigger bounds, Effect
preview box, gizmo) from a :class:`engine.renderer.SelectionOverlay` and never
sees editor objects. This is the one place that turns an editor selection --
a brush dict or a ``Thing`` -- into that descriptor.

The values are the ones the renderer used to read off the selected object,
with the same defaults and parsing, so the overlay draws exactly as before.
Known differences from how ``EntityTable`` reads the same Effect properties
(truthy ``preview`` versus ``_effect_bool``; the 24.0 fallback height versus
46.0 / 32.0 for ORB; ``effect_type`` whitespace) are preserved deliberately
and tracked as a separate bug.
"""

from engine.constants import brush_aabb_bounds
from engine.effect_entity import Effect
from engine.renderer import EffectBillboard, SelectionOverlay
from editor.things import Thing


def _xyz(value):
    return (float(value[0]), float(value[1]), float(value[2]))


def _effect_billboard(effect):
    props = getattr(effect, 'properties', {}) or {}
    try:
        width = max(0.01, float(props.get('width', 32.0)))
    except (TypeError, ValueError):
        width = 32.0
    try:
        height = max(0.01, float(props.get('height', 24.0)))
    except (TypeError, ValueError):
        height = 24.0
    return EffectBillboard(
        pos=_xyz(getattr(effect, 'pos', [0.0, 0.0, 0.0])),
        width=width,
        height=height,
        explosion=(str(effect.properties.get('effect_type', 'FIRE')).upper()
                   == 'EXPLOSION'),
    )


def describe_selection(selection):
    """The :class:`SelectionOverlay` for *selection*, or None to draw nothing."""
    if not selection:
        return None
    if isinstance(selection, dict):
        dashed = None
        if selection.get('is_trigger', False) and selection.get('show_aabb_bounds', False):
            dashed = tuple(float(v) for v in brush_aabb_bounds(selection))
        pos = selection.get('pos')
        return SelectionOverlay(
            brush_id=selection.get('id'),
            outline=(_xyz(selection.get('pos', [0, 0, 0])),
                     _xyz(selection.get('size', [64, 64, 64]))),
            dashed_bounds=dashed,
            gizmo_pos=(_xyz(pos) if pos is not None
                       and not selection.get('lock', False) else None),
        )
    if isinstance(selection, Thing):
        effect = None
        if (isinstance(selection, Effect)
                and selection.properties.get('preview', False)):
            effect = _effect_billboard(selection)
        return SelectionOverlay(effect_billboard=effect,
                                gizmo_pos=_xyz(selection.pos))
    return None
