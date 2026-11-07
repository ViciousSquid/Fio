"""Stage 0: no new writes to the fields the unified brush model will change.

``tests/helpers/write_sites`` counts, per module, the code that writes a
brush's duplicated or overloaded fields: ``pos``/``size`` (box geometry,
convex bound and mover transform at once), the per-tag material dicts (a
second copy of a convex face's material), ``geometry`` and play's keys in
the authored dict. ``write_sites.json`` is the count today. It may only go
down: a new write is a reviewed decision (regenerate the fixture with
``python tools/brush_semantics_snapshot.py --write`` in the same commit); a
removed one lowers the baseline the same way, so the ratchet stays tight.
"""

import json

from tests.helpers import write_sites
from tests.helpers.paths import REPO_ROOT, repo_path


def _baseline():
    with open(repo_path('tests', 'brush_model', 'write_sites.json')) as handle:
        return json.load(handle)


def test_no_new_writes_to_duplicated_brush_fields():
    grown, _shrunk = write_sites.ratchet(_baseline(), write_sites.scan(REPO_ROOT))
    assert not grown, (
        "new writes to a field the unified brush model moves to one owner "
        "(docs/brush-model-proposal.md):\n  " + "\n  ".join(grown))


def test_the_baseline_is_tight():
    _grown, shrunk = write_sites.ratchet(_baseline(), write_sites.scan(REPO_ROOT))
    assert not shrunk, (
        "writes were removed -- lower the baseline with "
        "`python tools/brush_semantics_snapshot.py --write`:\n  "
        + "\n  ".join(shrunk))


def test_the_scanner_counts_each_kind_of_write():
    import ast
    source = '''
b['pos'] = p
b['pos'][1] += 4
b['textures'][tag] = t
del b['uv_shift'][tag]
b.setdefault('uv_scale', {})[tag] = s
b.pop('original_pos', None)
b.update(size=s)
b.update({'_rot_angle': a, 'name': n})
x, b['geometry'] = 1, g
new = {'pos': p, 'size': s}
b.get('pos')
'''
    visitor = write_sites._Visitor()
    visitor.visit(ast.parse(source))
    assert dict(visitor.found) == {
        'pos': 2, 'textures': 1, 'uv_shift': 1, 'uv_scale': 1,
        'original_pos': 1, 'size': 1, '_rot_angle': 1, 'geometry': 1}
