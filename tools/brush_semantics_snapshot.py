"""Check or regenerate the brush-model Stage 0 fixtures.

Two fixtures pin how brushes behave today (``docs/brush-model-proposal.md``):

* ``tests/brush_model/semantics_snapshot.json`` -- for every shipped map,
  each brush's encoding, the digest of what it draws (faces, materials,
  bounds) and the exact inventory of inconsistencies it carries;
* ``tests/brush_model/write_sites.json`` -- the ratchet of code that writes
  the fields the unified model will change.

Without ``--write`` this prints what differs and exits non-zero if
anything does. Regenerate only for a deliberate, reviewed change, in the
same commit as that change.

    python tools/brush_semantics_snapshot.py
    python tools/brush_semantics_snapshot.py --write
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, 'tests', 'brush_model')
SNAPSHOT = os.path.join(FIXTURES, 'semantics_snapshot.json')
WRITE_SITES = os.path.join(FIXTURES, 'write_sites.json')


def _current():
    sys.path.insert(0, ROOT)
    from tests.helpers import brush_semantics, write_sites
    return brush_semantics.snapshot(ROOT), write_sites.scan(ROOT)


def _load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def _dump(path, data):
    with open(path, 'w') as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--write', action='store_true',
                        help='regenerate both fixtures')
    args = parser.parse_args(argv)
    snapshot, sites = _current()
    if args.write:
        _dump(SNAPSHOT, snapshot)
        _dump(WRITE_SITES, sites)
        print('wrote %s and %s' % (os.path.relpath(SNAPSHOT, ROOT),
                                   os.path.relpath(WRITE_SITES, ROOT)))
        return 0
    from tests.helpers.write_sites import ratchet
    status = 0
    stored = _load(SNAPSHOT) or {}
    for name in sorted(set(stored) | set(snapshot)):
        if stored.get(name) != snapshot.get(name):
            print('semantics changed: %s' % name)
            status = 1
    grown, shrunk = ratchet(_load(WRITE_SITES) or {}, sites)
    for line in grown:
        print('new write: ' + line)
    for line in shrunk:
        print('fewer writes (lower the baseline): ' + line)
    if grown or shrunk:
        status = 1
    print('fixtures up to date' if not status else 'fixtures differ')
    return status


if __name__ == '__main__':
    sys.exit(main())
