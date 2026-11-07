"""Visual tier: angled brushes cost nothing but GL calls on a steady frame.

The textured pass's convex half resolves every face's draw state -- texture,
caulk/nodraw, UV scale/rotation/shift -- once, when the face's mesh is built
(a cache miss), and keeps the result in the renderer's own columns and
per-table handle arrays. These tests pin the consequences:

* **No authored state in the steady-state frame.** After warm-up, the
  geometry records, the brush dicts, the texture cache and the loaders are
  replaced by tripwires that raise if touched, and frames still draw --
  identically.
* **Work happens only on a genuine miss.** A still world builds no mesh,
  loads no texture and uploads no byte of geometry, transforms or indices; a change to an unrelated brush resolves no geometry
  handle at all; a structural edit that renumbers every handle moves what was
  resolved, by record serial, without resolving it again; a reshape builds
  exactly one mesh and releases the one it replaces at once.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

SIZE = 128


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture(params=('elements', 'multidraw', 'off'))
def renderer(context, request):
    made = glh.make_renderer()
    made.shadows_enabled = False
    made.geo_batching = request.param
    try:
        yield made
    finally:
        made.cleanup()


class Tripwire:
    """Raises on any use: stands in for state a steady frame must not read."""

    def __init__(self, what):
        object.__setattr__(self, '_what', what)

    def _trip(self, *args, **kwargs):
        raise AssertionError("steady-state frame touched %s" % self._what)

    __getattr__ = __getitem__ = __iter__ = __len__ = __contains__ = _trip
    __call__ = __bool__ = __eq__ = __hash__ = _trip


def _world():
    from tests.helpers.worlds import angled_brush, box_brush
    brushes = [box_brush("floor", (0, -16, 0), (900, 32, 900))]
    for i in range(6):
        brush = angled_brush("ramp%d" % i, (-300 + 120 * i, 40, 0),
                             (90, 80, 90), clip_normal=(0.2 * i - 0.5, 1.0, 0.3))
        brush['geometry']['planes'][0]['texture'] = 'caulk.jpg'
        brushes.append(brush)
    return brushes


def _frame(renderer, context, config):
    import OpenGL.GL as gl
    context.bind()
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    renderer.render_scene(*glh.camera_matrices(eye=(0, 420, 700), target=(0, 0, 0)),
                          None, config, clear=False,
                          brush_slots=config['all_brush_slots'])
    return context.read_pixels()


def test_a_steady_frame_reads_no_authored_state(renderer, context):
    brushes = _world()
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False)
    table = config['render_table']
    before = _frame(renderer, context, config)
    _frame(renderer, context, config)
    store = renderer._geo_store()
    uploaded = store.uploaded

    # Everything authored, or derived from authored state per brush, that the
    # draw could reach: the brushes, the geometry records' contents, the
    # texture cache and every way of resolving or loading a texture or mesh.
    for record in table.geometry_records:
        for name in ('convex', 'origin', 'scale', 'materials', 'signature'):
            setattr(record, name, Tripwire('GeometryRecord.' + name))
    table.brushes = Tripwire('RenderTable.brushes')
    table.refs = Tripwire('RenderTable.refs')
    config['all_brushes'] = Tripwire("config['all_brushes']")
    renderer.texture_manager = Tripwire('the texture cache')
    for name in ('load_texture_callback', 'load_texture', '_tex_cache_path',
                 '_texture_pixel_size', '_build_geo_mesh',
                 '_get_geo_mesh_record', '_resolve_face_texture'):
        setattr(renderer, name, Tripwire('renderer.' + name))
    try:
        after = [_frame(renderer, context, config) for _ in range(3)]
    finally:
        del renderer.texture_manager
        renderer.texture_manager = {}
    for image in after:
        assert np.array_equal(image, before), "the steady frame changed"
    assert store.uploaded == uploaded, (
        "a still world uploaded %d bytes of geometry, transforms or indices"
        % (store.uploaded - uploaded))


def _counting(renderer):
    counts = {'builds': 0, 'resolved': 0}
    build, resolve = renderer._build_geo_mesh, renderer._get_geo_mesh_record

    def counted_build(*args, **kwargs):
        counts['builds'] += 1
        return build(*args, **kwargs)

    def counted_resolve(*args, **kwargs):
        counts['resolved'] += 1
        return resolve(*args, **kwargs)

    renderer._build_geo_mesh = counted_build
    renderer._get_geo_mesh_record = counted_resolve
    return counts


def _sync(table, brushes, epoch, dirty):
    table.sync(brushes, epoch, dirty_objects={id(b) for b in dirty})
    config = glh.render_config(all_brushes=brushes, render_table=table,
                               all_brush_slots=np.arange(table.count, dtype=np.int32),
                               shadows_enabled=False)
    return config


def test_geometry_work_happens_only_on_a_genuine_miss(renderer, context):
    from engine import brush_geometry as bg
    from engine.render_table import RenderTable
    from tests.helpers.worlds import box_brush

    brushes = _world()
    table = RenderTable()
    config = _sync(table, brushes, 1, ())
    counts = _counting(renderer)
    _frame(renderer, context, config)
    assert counts == {'builds': 6, 'resolved': 6}
    cached = len(renderer._geo_mesh_cache)

    # A still world.
    for _ in range(3):
        _frame(renderer, context, config)
    assert counts == {'builds': 6, 'resolved': 6}

    # An unrelated change -- a box retextured, an angled brush recoloured --
    # moves no geometry record: nothing about any handle is looked at.
    version = table.geometry_version
    brushes[0]['textures']['top'] = 'Dev/128.jpg'
    brushes[2]['colour'] = [0.2, 0.9, 0.1]
    config = _sync(table, brushes, 2, (brushes[0], brushes[2]))
    assert table.geometry_version == version
    _frame(renderer, context, config)
    assert counts == {'builds': 6, 'resolved': 6}

    # A structural edit that renumbers every geometry handle: the first
    # angled brush deleted, a box added in front. Every surviving record
    # keeps what was resolved for it, at its new handle.
    removed = brushes.pop(1)
    brushes.insert(0, box_brush("new box", (0, 300, 0)))
    config = _sync(table, brushes, 3, (brushes[0],))
    assert table.geometry_version != version
    _frame(renderer, context, config)
    assert counts == {'builds': 6, 'resolved': 6}, (
        "a reconcile that only moved geometry handles re-resolved them")
    assert len(renderer._geo_mesh_cache) == cached - 1, (
        "the deleted brush's mesh was not released when its table let go")
    assert removed['geometry']

    # A reshape: exactly one build, and the replaced mesh released at once.
    assert bg.clip_brush(brushes[3], (1.0, 0.0, 0.0), brushes[3]['pos'][0] + 10.0)
    config = _sync(table, brushes, 4, (brushes[3],))
    _frame(renderer, context, config)
    assert counts == {'builds': 7, 'resolved': 7}
    assert len(renderer._geo_mesh_cache) == cached - 1


def test_two_tables_share_one_mesh_per_brush(renderer, context):
    """The render buffers' tables, resolved independently, upload once."""
    from engine.render_table import RenderTable

    brushes = _world()
    counts = _counting(renderer)
    for _ in range(2):
        table = RenderTable()
        _frame(renderer, context, _sync(table, brushes, 1, ()))
    assert counts['builds'] == 6
