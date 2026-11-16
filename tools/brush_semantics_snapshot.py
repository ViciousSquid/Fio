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

    # The helper now exposes the per-map oracle and shipped-map inventory
    # separately. Aggregate them here rather than relying on the removed
    # snapshot(root) convenience wrapper.
    recorded = _load(SNAPSHOT) or {}
    snapshot = {
        name: brush_semantics.map_snapshot(brushes)
        for name, brushes in brush_semantics.shipped_maps(ROOT).items()
    }

    # A null digest in the fixture is an intentional opt-out from whole-map
    # digest pinning (currently _SHOWCASE: its canonical v5 layout is pinned
    # by explicit structural assertions). Preserve that marker in both
    # drift-check and --write modes; all other maps remain fully digested.
    for name, entry in snapshot.items():
        baseline = recorded.get(name)
        if baseline is not None and baseline.get('digest') is None:
            entry['digest'] = None

    return snapshot, write_sites.scan(ROOT)


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


def _display_path(path):
    """Prefer a repo-relative path; temp files on another Windows drive are absolute."""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return os.path.abspath(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--write', action='store_true',
                        help='regenerate both fixtures')
    args = parser.parse_args(argv)
    snapshot, sites = _current()
    if args.write:
        _dump(SNAPSHOT, snapshot)
        _dump(WRITE_SITES, sites)
        print('wrote %s and %s' % (_display_path(SNAPSHOT),
                                   _display_path(WRITE_SITES)))
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
