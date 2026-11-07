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
    assert current['digest'] == recorded['digest'], (
        "%s: the drawn faces, materials or bounds changed; %s" % (name, REGENERATE))


def test_the_recorded_inventory_is_the_known_one():
    """The inventory in words, so a reader need not decode the fixture.

    * Bounds: five convex brushes in five maps whose ``pos``/``size`` are not
      their planes' AABB by more than float noise -- 4.85e-5 in
      ``Angled_Brush_Test`` and 128 units in ``Terrain_Test_small`` and
      ``Topdown_example`` (the others are within 1e-6 and not pinned).
    * Material: four convex brushes in ``_SHOWCASE`` whose per-tag
      ``uv_scale`` (what the Surface Inspector shows, and what a box would
      draw) is not on their planes (what renders).
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
        'material_mismatch': {'_SHOWCASE.json'},
        'runtime_keys': {'Key_Test.json', 'MonsterTest.json',
                         'Simple_Map_Test_Extended.json', '_SHOWCASE.json',
                         'button_test.json', 'dLight2_Test.json',
                         'dLight_Test.json'},
    }
    showcase = RECORDED['_SHOWCASE.json']['inconsistencies']
    assert sum('material_mismatch' in f for f in showcase.values()) == 4
    drifts = sorted(f['bound_drift'] for e in RECORDED.values()
                    for f in e['inconsistencies'].values() if 'bound_drift' in f)
    assert drifts == [4.85e-05, 128.0, 128.0]


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
