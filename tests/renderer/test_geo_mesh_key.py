"""The convex mesh cache key is exactly the mesh's input -- both directions.

The renderer keys angled-brush GPU meshes by ``GeometryRecord.surface``. A
cache is correct under every state transition only if:

* **equal keys build identical meshes** -- vertex bytes, face layout, face
  state, edges -- whatever the brush's current position or which record (an
  undo's restored dict, a refresh) produced the key; and
* **a change to anything the mesh is built from changes the key.** Each
  authored property is mutated below and the tessellation compared.

Fio's angled brushes keep the AABB brushes' unit-cube transform, so a mesh is
expressed relative to an *anchor* AABB (``origin``/``scale``). Since Stage 2a
(``docs/brush-model-stage2.md``) the anchor is the shape's own bounds, derived
from the planes, so it is in the key but cannot change on its own: the
``pos``/``size`` a convex brush stores are a copy nothing reads for drawing,
and a mover's displacement is the row's transform.

Conservative by design: every authored plane field is in the key, including
one the tessellation does not use for a brush in canonical form (the face
tag, which only selects a legacy brush's per-tag entries). Changing it
rebuilds a mesh unnecessarily; it can never reuse a wrong one, and a plane
field added later is covered without anyone remembering to add it. Each
face's resolved material (its one record, ``engine.brush_faces``) is in the
key as well, so a legacy brush's per-tag entries are covered too.
"""

import copy

import pytest

from engine import brush_geometry as bg
from engine.render_table import RenderTable
from tests.helpers import runtime

pytestmark = pytest.mark.qt          # imports the renderer core (PyOpenGL)

#: Plane fields in the key that do not change the tessellation (see above).
CONSERVATIVE = {'face tag'}


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
    bytes, face layout, and each face's resolved material and extent -- the
    inputs of the renderer's per-face resolution, not the raw plane dicts."""
    from engine.renderer.core.geometry import tessellate_geo
    data = tessellate_geo(record)
    faces = []
    for face, _plane, first, count, extent in data.faces:
        faces.append((first, count, extent, record.materials[id(face)]))
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
    from engine import brush_faces
    brush_faces.set_face_material(brush, brush['geometry']['planes'][0]['face'],
                                  uv_natural=True)


def _stray_per_tag(brush):
    """A per-tag write on an angled brush (a plugin, a hand edit): not the
    face's record, so nothing at run time reads it."""
    tag = brush['geometry']['planes'][0]['face']
    brush['uv_natural'] = {tag: True}
    brush['uv_scale'] = {tag: [9.0, 9.0]}


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
    'natural scale': _natural,
    'face tag': _set_plane(0, 'face', 'renamed'),
    'plane uv_natural': _set_plane(2, 'uv_natural', True),
}

TRANSFORM_ONLY = {
    'rotation angle': lambda b: runtime.drive(b, spin=((0, 1, 0), 30.0)),
    'runtime displacement': lambda b: runtime.drive(b, offset=(32.0, 0.0, 0.0)),
    'colour': lambda b: b.update(colour=[0.9, 0.1, 0.2]),
    'name': lambda b: b.update(name='other'),
    'hidden': lambda b: b.update(hidden=True),
    'mover flags': lambda b: b.update(is_mover=True),
    'stray per-tag copy (ignored)': _stray_per_tag,
    'stored pos copy (ignored)': _move,
    'stored size copy (ignored)': _resize,
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


def test_moving_a_mover_is_a_transform_not_a_new_mesh():
    """Same dict, a mover carried elsewhere, surface not: the record --
    anchor and key -- is kept, and its mesh is identical although the brush
    is elsewhere. Then a real surface change re-anchors on the new shape's
    own bounds."""
    from engine import brush_placement
    brush = _brush()
    brush['is_mover'] = True
    table = RenderTable()
    table.sync([brush], 1)
    record = table.geometry_records[0]
    mesh = _mesh(record)
    runtime.drive(brush, offset=(32.0, 0.0, 0.0))  # its runtime transform
    brush['colour'] = [0.2, 0.3, 0.4]           # a STATE refresh as well
    table.sync([brush], 2, dirty_objects={id(brush)})
    assert table.geometry_records[0] is record
    assert _mesh(table.geometry_records[0]) == mesh
    assert tuple(table.center[0]) == brush_placement.placed_box(brush)[:3]   # moved
    _nudge_offset(brush)
    table.sync([brush], 3, dirty_objects={id(brush)})
    moved = table.geometry_records[0]
    assert moved is not record and moved.surface != record.surface
    # Re-anchored on the new shape's own bounds, in its local frame (Stage 2d).
    assert tuple(moved.origin) == bg.local_convex(brush).shape_box[:3]
