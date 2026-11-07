"""Export a map for Fio builds from before the canonical face layout.

Fio saves maps in the canonical layout (map ``version`` 4): every brush face
has one material record -- a box face in the brush's per-tag dicts, an
angled face on its plane -- and nothing is stored twice. A build from before
that layout read a tagged angled face's Natural flag, its textured-pass
routing and its Surface Inspector values from the per-tag dicts, so it would
draw a canonical map's angled brushes differently.

This writes an explicit copy for such a build -- never the normal save path:
the per-tag dicts of every angled brush are filled from its tagged planes
and ``version`` is set to 3. Faces whose appearance the old layout cannot
express are listed (Natural on a cut face, brush-wide tiling on an angled
brush): the old build draws those as it always did.

    python tools/export_legacy_map.py maps/MyMap.json out/MyMap_legacy.json

A map already older than version 4 is copied unchanged.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('source', help='a map saved by this Fio (version 4)')
    parser.add_argument('target', help='where to write the legacy copy')
    args = parser.parse_args(argv)
    sys.path.insert(0, ROOT)
    from engine import brush_faces
    with open(args.source) as handle:
        level = json.load(handle)
    version = int(level.get('version') or 0)
    if version < brush_faces.CANONICAL_MAP_VERSION:
        exported, lossy = level, {}
        print('%s is version %d already: copied unchanged' % (args.source, version))
    else:
        exported, lossy = brush_faces.export_legacy_level(level)
    if os.path.abspath(args.source) == os.path.abspath(args.target):
        parser.error('refusing to overwrite the source map')
    with open(args.target, 'w') as handle:
        json.dump(exported, handle, indent=2)
    for brush_id, faces in sorted(lossy.items()):
        print('brush %s: not expressible in the old layout: %s'
              % (brush_id, ', '.join(faces)))
    print('wrote %s (version %s)' % (args.target, exported.get('version')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
