"""Feed the frozen M1/M2 reference the brush layout it was written for.

``tests/helpers/geo_reference.py`` is a frozen copy of the angled-brush path
as it was before Stage 1 of the unified brush model, and is not modified:
it is what proves M1/M2 batching drew exactly what the old loop drew. That
old loop read a tagged angled face's Natural flag from the brush's per-tag
dicts. Since Stage 1 a face's one material record lives on its plane, so
the reference is handed each brush through the explicit legacy export
(``engine.brush_faces.legacy_layout``) -- the layout a pre-Stage-1 build
reads, in which the old loop draws exactly what the new path draws for the
canonical brush.

Only faces the old layout can express may be compared this way: a scene
with a Natural cut face or a tiled angled brush (Stage 1 behaviours the old
loop never had) is refused rather than silently compared. Those behaviours
are proved against the GPU on their own, in
``tests/visual/test_brush_semantics_gl.py``.
"""

from engine import brush_faces


def for_frozen_reference(brushes, geometry_of=None):
    """``(brushes, geometry_of)`` laid out as the frozen reference reads
    them: each brush (and geometry snapshot, re-keyed to its copy) through
    :func:`engine.brush_faces.legacy_layout`."""
    copies, remapped = [], {}
    for brush in brushes:
        copy, lossy = brush_faces.legacy_layout(brush)
        assert not lossy, (
            "%s: %s cannot be expressed in the layout the frozen reference "
            "reads; compare it against the GPU instead" % (brush.get('name'), lossy))
        copies.append(copy)
        if geometry_of and id(brush) in geometry_of:
            remapped[id(copy)] = brush_faces.legacy_layout(geometry_of[id(brush)])[0]
    return copies, remapped
