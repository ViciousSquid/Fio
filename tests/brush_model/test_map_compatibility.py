"""Stage 1 map compatibility: versioned layout, one-way migration, explicit export.

* A map's ``version`` says which layout its brushes use. Version 4 (the
  canonical layout) keeps every face's one material record in its one home
  -- nothing stored twice; earlier versions are migrated once, at load, by
  the field-specific precedence rule.
* New saves are version 4 and carry no per-tag copy on any angled brush.
* Older Fio builds get an explicit export (``tools/export_legacy_map.py``,
  ``engine.brush_faces.export_legacy_level``), never a dual-written save.
* A streamed Big World map migrates every record, resident or not, so a
  later save is canonical throughout.
"""

import copy
import json
import subprocess
import sys

import pytest

from engine import brush_faces as bf
from engine.brush_placement import MAP_VERSION, world_layout
from engine import brush_geometry as bg
from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT, repo_path
from tests.helpers.worlds import angled_brush, box_brush

pytestmark = pytest.mark.qt          # EditorState pulls in the editor package


def _legacy_brush(name='legacy', pos=(0.0, 40.0, 0.0)):
    """An angled brush as a pre-Stage-1 build saved it: per-tag copies, one
    of them (scale) authored after conversion and not on the plane."""
    brush = angled_brush(name, pos, (96.0, 80.0, 96.0), clip_normal=(0.3, 1.0, 0.2),
                         textures={t: 'Dev/128.jpg' for t in bf.FACE_TAGS})
    for plane in brush['geometry']['planes']:
        plane.pop('uv_scale', None)
        if plane.get('face'):
            plane['texture'] = 'Dev/128.jpg'
    bg.invalidate_geometry_cache(brush)
    brush.update(textures={t: 'Dev/128.jpg' for t in bf.FACE_TAGS},
                 uv_scale={'top': [0.25, 0.25]}, uv_natural={'north': True})
    # Saved as such a build wrote it: planes in world space (before Stage 2d).
    return world_layout({k: v for k, v in brush.items() if k not in bg.GEO_RUNTIME_KEYS})


def _load(level, **kwargs):
    from editor.editor_state import EditorState
    state = EditorState()
    state.load_from_data(copy.deepcopy(level), save_undo=False, **kwargs)
    return state


def _one_record(brush):
    tags = {p.get('face') for p in brush['geometry']['planes'] if p.get('face')}
    return not any(tags & set(brush.get(key) or {}) for key in bf.TAG_DICTS.values())


def test_a_legacy_map_is_migrated_and_saved_canonical():
    raw = _legacy_brush()
    state = _load({'version': 3, 'brushes': [raw], 'things': []})
    brush = state.brushes[0]
    assert _one_record(brush)
    assert bf.face_material(brush, 'top').uv_scale == (0.25, 0.25)
    assert bf.face_material(brush, 'north').natural
    saved = state.get_level_data()
    assert saved['version'] == MAP_VERSION
    assert all(_one_record(b) for b in saved['brushes'] if b.get('geometry'))


def test_a_map_without_a_version_is_legacy():
    raw = _legacy_brush()
    state = _load({'brushes': [raw], 'things': []})
    assert bf.face_material(state.brushes[0], 'top').uv_scale == (0.25, 0.25)


def test_a_canonical_map_is_not_reinterpreted():
    """In a version 4 map the plane is the record by definition: a stray
    per-tag entry (a hand edit, a plugin) is dropped, not promoted."""
    raw = _legacy_brush()
    raw['uv_scale'] = {'top': [9.0, 9.0]}
    plane = next(p for p in raw['geometry']['planes'] if p.get('face') == 'top')
    plane['uv_scale'] = [2.0, 2.0]
    state = _load({'version': 4, 'brushes': [raw], 'things': []})
    brush = state.brushes[0]
    assert bf.face_material(brush, 'top').uv_scale == (2.0, 2.0)
    assert _one_record(brush)


def test_saving_and_loading_again_changes_nothing():
    state = _load({'version': 3, 'brushes': [_legacy_brush(),
                                              box_brush('box', (200, 0, 0), (64, 64, 64),
                                                        uv_shift={'top': [0.5, 0.0]})],
                   'things': []})
    first = json.loads(json.dumps(state.get_level_data()))
    second = json.loads(json.dumps(_load(first).get_level_data()))
    assert first['brushes'] == second['brushes']


def test_the_legacy_export_reads_the_same_in_the_old_layout():
    """The export fills the per-tag dicts from the planes and says version 3;
    loading it again (migration) gives back the same records, and every
    tagged face's per-tag copy equals its plane -- what an old build reads."""
    state = _load({'version': 3, 'brushes': [_legacy_brush()], 'things': []})
    canonical = json.loads(json.dumps(state.get_level_data()))
    exported, lossy = bf.export_legacy_level(canonical)
    assert exported['version'] == 3 and lossy == {}
    brush = exported['brushes'][0]
    for plane in brush['geometry']['planes']:
        tag = plane.get('face')
        if tag:
            for field in bf.FIELDS:
                assert bf.tag_value(brush, field, tag) == bf.plane_value(plane, field), (tag, field)
    again = _load(exported).brushes[0]
    original = state.brushes[0]
    assert {k: bf.face_material(again, k) for k in bf.face_keys(again)} == \
        {k: bf.face_material(original, k) for k in bf.face_keys(original)}


def test_the_legacy_export_names_what_the_old_layout_cannot_express():
    brush = angled_brush("lossy", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2),
                         texture_tiling=True)
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    bf.set_face_material(brush, '#%d' % cut, uv_natural=True)
    _copy, lossy = bf.legacy_layout(brush)
    assert lossy == ['#%d.uv_natural' % cut, 'tiling']


def test_the_export_tool_writes_a_version_3_copy(tmp_path):
    state = _load({'version': 3, 'brushes': [_legacy_brush()], 'things': []})
    source = tmp_path / 'map.json'
    target = tmp_path / 'map_legacy.json'
    source.write_text(json.dumps(state.get_level_data()))
    out = subprocess.run([sys.executable, repo_path('tools', 'export_legacy_map.py'),
                          str(source), str(target)], check=True, capture_output=True,
                         text=True, cwd=REPO_ROOT).stdout
    assert 'version 3' in out
    assert json.loads(target.read_text())['version'] == 3


def test_a_streamed_legacy_world_is_migrated_record_by_record():
    """Resident or not, every record of a streamed Big World map is migrated
    at load, so saving writes no legacy record into a version 4 file."""
    with open(repo_path('maps', 'BigWorld_streaming_test.json')) as handle:
        world = json.load(handle)
    settings = [t for t in world['things'] if 'bigworldsettings' in json.dumps(t).lower()]
    settings = copy.deepcopy(settings)
    settings[0]['properties']['editor_streaming'] = True
    near = _legacy_brush('near', (0.0, 40.0, 0.0))
    far = _legacy_brush('far', (60000.0, 40.0, 60000.0))
    state = _load({'version': 3, 'brushes': [near, far], 'things': settings},
                  stream_editor=True)
    assert state.editor_stream is not None, "the test needs a streamed world"
    resident = {b.get('name') for b in state.brushes}
    assert 'near' in resident and 'far' not in resident
    saved = state.get_level_data()
    angled = [b for b in saved['brushes'] if b.get('geometry')]
    assert {b['name'] for b in angled} == {'near', 'far'}
    for brush in angled:
        assert _one_record(brush), brush['name']
        assert bf.plane_value(next(p for p in brush['geometry']['planes']
                                   if p.get('face') == 'top'), 'uv_scale') == (0.25, 0.25)


def test_every_shipped_map_loads_and_saves_canonical_with_the_same_faces():
    for name, brushes in bs.shipped_maps(REPO_ROOT).items():
        with open(repo_path('maps', name)) as handle:
            level = json.load(handle)
        state = _load(level)
        saved = state.get_level_data()
        assert saved['version'] == MAP_VERSION
        for raw, loaded in zip(brushes, state.brushes):
            assert bs.render_faces(loaded) == bs.render_faces(bs.loaded(raw)), name
            if loaded.get('geometry'):
                assert _one_record(loaded), name
