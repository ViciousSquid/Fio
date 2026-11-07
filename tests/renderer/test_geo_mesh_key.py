"""The convex mesh cache key is exactly the mesh's input -- both directions.

The renderer keys angled-brush GPU meshes by ``GeometryRecord.surface``. A
cache is correct under every state transition only if:

* **equal keys build identical meshes** -- vertex bytes, face layout, face
  state, edges -- whatever the brush's current position or which record (an
  undo's restored dict, a refresh) produced the key; and
* **a change to anything the mesh is built from changes the key.** Each
  authored property is mutated below and the tessellation compared.

Fio's angled brushes keep the AABB brushes' unit-cube transform, so a mesh is
expressed relative to an *anchor* AABB (``origin``/``scale``), taken from
``pos``/``size`` when the brush's geometry is resolved and kept while its
surface is unchanged. ``pos``/``size`` therefore change the mesh of a *new*
record (they are in its key) but are only a transform for an existing one.

Conservative by design: every authored plane field is in the key, including
two the tessellation ignores (the face tag, and a plane-level ``uv_natural``
that the renderer reads from the brush's per-tag flags instead). Changing
those rebuilds a mesh unnecessarily; it can never reuse a wrong one, and a
plane field added later is covered without anyone remembering to add it.
"""

import copy

import pytest

from engine import brush_geometry as bg
from engine.render_table import RenderTable

pytestmark = pytest.mark.qt          # imports the renderer core (PyOpenGL)

#: Plane fields in the key that do not change the tessellation (see above).
CONSERVATIVE = {'face tag', 'plane uv_natural'}


def _brush():
    from tests.helpers.worlds import angled_brush
    brush = angled_brush("keyed", (120.0, 40.0, -60.0), (96.0, 80.0, 112.0),
                         clip_normal=(0.4, 1.0, -0.3))
    planes = brush['geometry']['planes']
    planes[0]['uv_scale'] = [2.0, 1.0]
    planes[1]['uv_angle'] = 15.0
    planes[2]['uv_shift'] = [0.25, 0.5]
    bg.invalidate_geometry_cache(brush)
    return brush


def _fresh(brush):
    """A dict as an undo would restore it: same content, no runtime keys."""
    out = copy.deepcopy({k: v for k, v in brush.items()
                         if k not in bg.GEO_RUNTIME_KEYS})
    return out


def _record(brush):
    table = RenderTable()
    table.sync([brush], 1)
    return table.geometry_records[0]


def _mesh(record):
    """Everything the tessellation hands the GPU, comparable: vertex and edge
    bytes, face layout, and each face's resolved state -- the inputs of the
    renderer's per-face resolution (texture, scale, rotation, shift,
    natural-scale flag and extent), not the raw plane dicts."""
    from engine.renderer.core.geometry import tessellate_geo
    data = tessellate_geo(record)
    faces = []
    for face, plane, first, count, extent in data.faces:
        scale = face.get('uv_scale') or plane.get('uv_scale')
        faces.append((first, count, extent, face.get('texture') or 'default.png',
                       None if scale is None else tuple(scale),
                       plane.get('uv_angle', 0.0),
                       tuple(plane.get('uv_shift', (0.0, 0.0)) or (0.0, 0.0)),
                       bool(record.natural_scale.get(id(face), False))))
    return (data.vertices.tobytes(), data.edges.tobytes(), data.side_count,
            data.edge_count, data.has_flat_top, tuple(faces))


def _set_plane(index, field, value):
    def mutate(brush):
        brush['geometry']['planes'][index][field] = value
        bg.invalidate_geometry_cache(brush)
    return mutate


def _nudge_offset(brush):
    brush['geometry']['planes'][0]['d'] += 1e-3
    bg.invalidate_geometry_cache(brush)


def _natural(brush):
    tag = brush['geometry']['planes'][0]['face']
    brush['uv_natural'] = {tag: True}


def _move(brush):
    brush['pos'] = [brush['pos'][0] + 32.0, brush['pos'][1], brush['pos'][2]]


def _resize(brush):
    brush['size'] = [brush['size'][0] * 1.5, brush['size'][1], brush['size'][2]]


def _basis(brush):
    plane = brush['geometry']['planes'][3]
    plane['uv_u'], plane['uv_v'] = [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]
    bg.invalidate_geometry_cache(brush)


MESH_INPUTS = {
    'plane offset': _nudge_offset,
    'texture': _set_plane(1, 'texture', 'Dev/128.jpg'),
    'uv scale': _set_plane(0, 'uv_scale', [0.5, 3.0]),
    'uv angle': _set_plane(1, 'uv_angle', 40.0),
    'uv shift': _set_plane(2, 'uv_shift', [0.75, 0.1]),
    'texture basis': _basis,
    'natural scale (per-tag)': _natural,
    'anchor position (new record)': _move,
    'anchor size (new record)': _resize,
    'face tag': _set_plane(0, 'face', 'renamed'),
    'plane uv_natural': _set_plane(2, 'uv_natural', True),
}

TRANSFORM_ONLY = {
    'rotation angle': lambda b: b.update(_rot_angle=30.0, rot_axis=[0, 1, 0]),
    'colour': lambda b: b.update(colour=[0.9, 0.1, 0.2]),
    'name': lambda b: b.update(name='other'),
    'hidden': lambda b: b.update(hidden=True),
    'mover flags': lambda b: b.update(is_mover=True),
}


@pytest.mark.parametrize("name", sorted(MESH_INPUTS))
def test_a_mesh_input_changes_the_key_and_the_mesh(name):
    base = _brush()
    changed = _fresh(base)
    MESH_INPUTS[name](changed)
    a, b = _record(_fresh(base)), _record(changed)
    assert a.surface != b.surface, "%s did not change the key" % name
    if name in CONSERVATIVE:
        assert _mesh(a) == _mesh(b), (
            "%s now changes the mesh: move it out of CONSERVATIVE" % name)
    else:
        assert _mesh(a) != _mesh(b), "%s did not change the mesh" % name


@pytest.mark.parametrize("name", sorted(TRANSFORM_ONLY))
def test_a_transform_only_change_keeps_the_key_and_the_mesh(name):
    base = _brush()
    changed = _fresh(base)
    TRANSFORM_ONLY[name](changed)
    a, b = _record(_fresh(base)), _record(changed)
    assert a.surface == b.surface
    assert _mesh(a) == _mesh(b)


def test_equal_keys_build_identical_meshes_from_different_records():
    """An undo restores every brush as a new dict: a new record, same key."""
    base = _brush()
    a, b = _record(base), _record(_fresh(base))
    assert a is not b and a.serial != b.serial
    assert a.surface == b.surface
    assert _mesh(a) == _mesh(b)


def test_moving_an_existing_brush_is_a_transform_not_a_new_mesh():
    """Same dict, pos and size changed, surface not: the record -- anchor
    and key -- is kept, and its mesh is identical although the brush is
    elsewhere. Then a real surface change re-anchors."""
    brush = _brush()
    table = RenderTable()
    table.sync([brush], 1)
    record = table.geometry_records[0]
    mesh = _mesh(record)
    _move(brush)
    _resize(brush)
    brush['colour'] = [0.2, 0.3, 0.4]           # a STATE refresh as well
    table.sync([brush], 2, dirty_objects={id(brush)})
    assert table.geometry_records[0] is record
    assert _mesh(table.geometry_records[0]) == mesh
    assert list(table.center[0]) == brush['pos']   # the transform moved
    _nudge_offset(brush)
    table.sync([brush], 3, dirty_objects={id(brush)})
    moved = table.geometry_records[0]
    assert moved is not record and moved.surface != record.surface
    assert list(moved.origin) == brush['pos']       # re-anchored
