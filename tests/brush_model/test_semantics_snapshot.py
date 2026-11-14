"""Stage 0: every shipped map's brushes behave exactly as recorded.

``tests/helpers/brush_semantics`` states what a brush dict means today --
its encoding, the faces the renderer draws (world-space polygon and
resolved material), the bound the bound-readers use -- and what its two
readings disagree on. ``semantics_snapshot.json`` records that for every
map in ``maps/``: a digest of the drawn faces and bounds, and the exact
inventory of inconsistencies per brush.

The unified brush model (``docs/brush-model-proposal.md``) changes how
brushes are stored, not what they draw. A stage that changes the meaning of
a shipped map must say so: it regenerates the snapshot in the same commit
(``python tools/brush_semantics_snapshot.py --write``) and names the change.
Nothing here is GL: it runs in the headless tier.
"""

import copy
import json

import pytest

from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT, repo_path

with open(repo_path('tests', 'brush_model', 'semantics_snapshot.json')) as _handle:
    RECORDED = json.load(_handle)

MAPS = bs.shipped_maps(REPO_ROOT)

#: The two _SHOWCASE brushes whose authored per-tag scale Stage 1 draws.
SHOWCASE_QUARTER = '5b10b49b-9b6c-45d9-91ee-0b6f06970590'
SHOWCASE_TEN_BY_EIGHT = '8c04e483-4d44-4217-a03d-32844a727043'

REGENERATE = ("if this change is deliberate, regenerate with "
              "`python tools/brush_semantics_snapshot.py --write` and name it "
              "in docs/brush-model-proposal.md")


def test_every_shipped_map_is_recorded():
    assert sorted(MAPS) == sorted(RECORDED), REGENERATE


@pytest.mark.parametrize("name", sorted(RECORDED))
def test_a_shipped_map_draws_what_was_recorded(name):
    recorded = RECORDED[name]
    current = bs.map_snapshot(MAPS[name])
    assert current['encodings'] == recorded['encodings'], REGENERATE
    assert current['inconsistencies'] == recorded['inconsistencies'], REGENERATE
    if recorded['digest'] is not None:
        assert current['digest'] == recorded['digest'], (
            "%s: the drawn faces, materials or bounds changed; %s"
            % (name, REGENERATE))


def test_the_recorded_inventory_is_the_known_one():
    """The inventory in words, so a reader need not decode the fixture.

    * Bounds: three convex brushes whose ``pos``/``size`` are not their
      planes' AABB by more than float noise -- 4.85e-5 in
      ``Angled_Brush_Test`` and 128 units in ``Terrain_Test_small`` and
      ``Topdown_example`` (four more are within 1e-6 and not pinned).
    * Material (Stage 1): two convex brushes in ``_SHOWCASE`` whose authored
      per-tag ``uv_scale`` -- what the Surface Inspector showed -- is not on
      their planes. By the precedence rule it is their one record and is now
      drawn: 0.25 on four faces of one brush, 10 x 8 on six faces of the
      other. (Two more carry a per-tag scale of 1.0, which draws as their
      planes' missing scale did: no change.)
    * Runtime keys: seven maps saved a mover's ``original_pos``, one also
      its ``rotation_yaw``.
    """
    kinds = {}
    for name, entry in RECORDED.items():
        for found in entry['inconsistencies'].values():
            for kind in found:
                kinds.setdefault(kind, set()).add(name)
    assert kinds == {
        'bound_drift': {'Angled_Brush_Test.json', 'Terrain_Test_small.json',
                        'Topdown_example.json'},
        'runtime_keys': {'Key_Test.json', 'MonsterTest.json',
                         'Simple_Map_Test_Extended.json', 'button_test.json',
                         'dLight2_Test.json', 'dLight_Test.json'},
    }
    assert RECORDED['_SHOWCASE.json']['inconsistencies'] == {}
    drifts = sorted(f['bound_drift'] for e in RECORDED.values()
                    for f in e['inconsistencies'].values() if 'bound_drift' in f)
    assert drifts == [4.85e-05, 128.0, 128.0]



def test_version5_showcase_uses_local_canonical_convex_records():
    """Version 5 stores convex planes locally and keeps one record per face."""
    brushes = MAPS['_SHOWCASE.json']
    assert len(brushes) == 88
    encodings = {}
    for brush in brushes:
        kind = bs.encoding(brush)
        encodings[kind] = encodings.get(kind, 0) + 1
        if kind != 'convex':
            continue
        assert not brush.get(bs.WORLD_PLANES)
        tags = {plane.get('face') for plane in brush['geometry']['planes']
                if plane.get('face')}
        for key, _field in bs.TAG_FIELDS:
            assert not tags & set(brush.get(key) or {}), (brush['id'], key)
        assert not any(brush.get(key) is not None for key in bs.RUNTIME_KEYS)
        assert 'bound_drift' not in bs.inconsistencies(brush)
        local_centre, local_size = bs.local_rest(brush)
        assert np.allclose(local_centre, (0.0, 0.0, 0.0), atol=1e-8), brush['id']
        assert np.allclose(local_size, brush['size'], atol=1e-9), brush['id']
    assert encodings == {'box': 70, 'convex': 18}

def test_the_digest_sees_a_visible_change_and_not_float_noise():
    """The oracle's own resolution: 1e-3 of a unit is a change, 1e-9 is not."""
    brushes = copy.deepcopy(MAPS['_SHOWCASE.json'])
    base = bs.digest(brushes)
    box = next(b for b in brushes if bs.encoding(b) == 'box')
    box['pos'] = [box['pos'][0] + 1e-9] + list(box['pos'][1:])
    assert bs.digest(brushes) == base
    box['pos'] = [box['pos'][0] + 1e-3] + list(box['pos'][1:])
    assert bs.digest(brushes) != base
    box['pos'] = [box['pos'][0] - 1e-3] + list(box['pos'][1:])
    textures = box.setdefault('textures', {})
    textures['top'] = 'Dev/elsewhere.jpg'
    assert bs.digest(brushes) != base
