"""Export a map for Fio builds that read an older map version.

Fio saves maps as version 5: every brush face has one material record (a
box face in the brush's per-tag dicts, an angled face on its plane: version
4) and an angled brush's planes are stored in its local frame, placed by its
``pos`` (version 5). Older builds read neither, so this writes an explicit
copy for them -- never the normal save path:

* ``--version 4`` (for a build from Stage 1 to before Stage 2d, and the
  Android player): every angled brush's planes carried into world space and
  its ``size`` written back. Lossless.
* ``--version 3`` (the default; for a build from before Stage 1): the
  version-4 copy, then the per-tag dicts of every angled brush filled from
  its tagged planes. Faces whose appearance that layout cannot express are
  listed (Natural on a cut face, brush-wide tiling on an angled brush): the
  old build draws those as it always did.

    python tools/export_legacy_map.py maps/MyMap.json out/MyMap_legacy.json [--version 4]

A map already at or below the requested version is copied unchanged.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('source', help='a map saved by this Fio')
    parser.add_argument('target', help='where to write the legacy copy')
    parser.add_argument('--version', type=int, choices=(3, 4), default=3,
                        help='the map version to write (default 3)')
    args = parser.parse_args(argv)
    sys.path.insert(0, ROOT)
    from engine import brush_faces, brush_placement
    with open(args.source) as handle:
        level = json.load(handle)
    version = int(level.get('version') or 0)
    if version <= args.version:
        exported, lossy = level, {}
        print('%s is version %d already: copied unchanged' % (args.source, version))
    elif args.version == 4:
        exported, lossy = brush_placement.export_world_level(level), {}
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
