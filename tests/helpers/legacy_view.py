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

The old loop also anchored an angled brush's mesh, and built its model
matrix, from the brush's ``pos``/``size``. Since Stage 2a both come from
the planes' own bounds (``engine.brush_placement``), which is what the
loader writes into ``pos``/``size``; so each copy's ``pos``/``size`` are
the brush's derived box (:func:`with_derived_bounds`), and a brush whose
stored copy has drifted (two shipped brushes, by 128 units) is handed to
the reference as the loader leaves it.

Only faces the old layout can express may be compared this way: a scene
with a Natural cut face or a tiled angled brush (Stage 1 behaviours the old
loop never had) is refused rather than silently compared. Those behaviours
are proved against the GPU on their own, in
``tests/visual/test_brush_semantics_gl.py``.
"""

from engine import brush_faces, brush_placement


def with_derived_bounds(brush):
    """A shallow copy of *brush* whose ``pos``/``size`` are its derived box
    (:func:`engine.brush_placement.placed_box`) -- what the renderer anchors
    and transforms an angled brush by. A box is returned as it is."""
    if brush_placement.shape_convex(brush) is None:
        return brush
    cx, cy, cz, hx, hy, hz = brush_placement.placed_box(brush)
    view = dict(brush)
    view['pos'] = [cx, cy, cz]
    view['size'] = [hx * 2.0, hy * 2.0, hz * 2.0]
    return view


def for_frozen_reference(brushes, geometry_of=None):
    """``(brushes, geometry_of)`` laid out as the frozen reference reads
    them: each brush (and geometry snapshot, re-keyed to its copy) through
    :func:`engine.brush_faces.legacy_layout`, with its derived bounds."""
    copies, remapped = [], {}
    for brush in brushes:
        copy, lossy = brush_faces.legacy_layout(brush)
        assert not lossy, (
            "%s: %s cannot be expressed in the layout the frozen reference "
            "reads; compare it against the GPU instead" % (brush.get('name'), lossy))
        copy = with_derived_bounds(copy)
        copies.append(copy)
        if geometry_of and id(brush) in geometry_of:
            remapped[id(copy)] = with_derived_bounds(
                brush_faces.legacy_layout(geometry_of[id(brush)])[0])
    return copies, remapped
