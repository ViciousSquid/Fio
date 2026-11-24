"""Bake the HD renderer's companion material maps for the stock textures.

    python tools/make_material_maps.py [--root assets] [--only GLOB]
                                       [--force] [--check] [--report]

For every surface texture under ``<root>/textures`` (the root folder,
``terrain/``, ``decals/`` and any other folder of surfaces) and every image
beside the models in ``<root>/models``, writes next to it::

    <name>_n.webp     normal map (OpenGL, +Y up) with the height in alpha
    <name>_m.webp     R occlusion, G roughness, B metalness, A 1 - emission
    <name>.mat.json   the parameters used, plus a "generated" block

The maps come from :func:`engine.material_maps.generate`; see that module for
the formats and the algorithm.  Both are lossless WebP, so what the renderer
reads is exactly what the generator made.  The diffuse images are only read.

Parameters come from the texture's ``.mat.json`` when it has one, so a baked
result is reproducible and hand-tuning survives: edit the numbers, run the
tool again and only that texture is rebuilt.  A texture without a sidecar
starts from a small table of guesses keyed by its name (:data:`HEURISTICS`).

What it leaves alone:

* maps beside a sidecar with no ``"generated"`` block, or with no sidecar at
  all -- those are hand-made, and are never overwritten, not even by
  ``--force``;
* generated maps that are still current (same image, same parameters, same
  generator version), unless ``--force``;
* tool textures (nodraw, trigger, ``Dev/`` ...), which get flat values from
  the engine and neither maps nor a sidecar; light cookies, effect sprites
  and ``water_normal.png``, which are not surfaces.  Textures embedded in
  ``.glb`` models are out of reach.

``--only GLOB`` (repeatable) restricts the run to matching file names or
paths, e.g. ``--only 'terrain/*'``.  ``--report`` lists every texture with
its status and writes nothing.  ``--check`` writes nothing either, and exits
non-zero naming every surface texture that lacks a map or whose generated
maps are stale: the test suite's view of the stock assets.

Deterministic: the same files every run.
"""

import argparse
import io
import os
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import material_maps as mm  # noqa: E402

#: Starting parameters by name, for a texture with no sidecar yet.  Each row
#: is ``(substrings, overrides)``; the name is the lower-case path relative to
#: the assets root (``textures/decals/vent.jpg``), and the FIRST row with a
#: matching substring wins, so the specific rows come before the general.
HEURISTICS = (
    # -- decals: projected onto a surface, so never any parallax -------------
    (('decals/wet_',),
     {'roughness_base': 0.08, 'roughness_range': 0.04, 'height_strength': 0.15,
      'ao_strength': 0.0, 'parallax': 0.0}),
    (('decals/blood_',),
     {'roughness_base': 0.25, 'roughness_range': 0.1, 'height_strength': 0.4,
      'ao_strength': 0.3, 'parallax': 0.0}),
    (('screenframe',),   # a switched-off screen: dark glass, nothing emits
     {'roughness_base': 0.2, 'roughness_range': 0.05, 'height_strength': 0.15,
      'ao_strength': 0.0, 'parallax': 0.0}),
    (('decals/compscreen',),
     {'roughness_base': 0.25, 'roughness_range': 0.1, 'height_strength': 0.15,
      'ao_strength': 0.2, 'emission_threshold': 0.35, 'parallax': 0.0}),
    (('decals/vent',),
     {'metal': 0.9, 'roughness_base': 0.4, 'roughness_range': 0.2,
      'height_strength': 1.2, 'parallax': 0.0}),
    (('decals/signage', 'decals/banner', 'decals/decal'),
     {'roughness_base': 0.5, 'roughness_range': 0.0, 'height_strength': 0.05,
      'ao_strength': 0.0, 'parallax': 0.0}),
    (('decals/',),
     {'roughness_base': 0.85, 'roughness_range': 0.1, 'height_strength': 0.4,
      'ao_strength': 0.3, 'parallax': 0.0}),
    # -- models: UV-mapped meshes, no parallax -------------------------------
    (('models/oil_drum',),
     {'metal': 0.9, 'roughness_base': 0.5, 'roughness_range': 0.2,
      'height_strength': 0.5, 'parallax': 0.0}),
    (('models/ball',),
     {'roughness_base': 0.3, 'roughness_range': 0.1, 'height_strength': 0.8,
      'parallax': 0.0}),
    (('models/',),
     {'roughness_base': 0.75, 'height_strength': 0.5, 'parallax': 0.0}),
    # -- terrain layers: blended, so no parallax ------------------------------
    (('terrain/crackedsand',),
     {'roughness_base': 0.9, 'roughness_range': 0.1, 'height_strength': 1.5,
      'detail': 0.5, 'parallax': 0.0}),
    (('terrain/sandgrass',),
     {'roughness_base': 0.85, 'roughness_range': 0.15, 'height_strength': 1.0,
      'parallax': 0.0}),
    (('terrain/grass',),
     {'roughness_base': 0.9, 'roughness_range': 0.1, 'height_strength': 0.8,
      'parallax': 0.0}),
    (('terrain/rock',),
     {'roughness_base': 0.85, 'roughness_range': 0.15, 'height_strength': 1.5,
      'parallax': 0.0}),
    (('terrain/snow',),
     {'roughness_base': 0.6, 'roughness_range': 0.1, 'height_strength': 0.4,
      'detail': 0.3, 'ao_strength': 0.5, 'parallax': 0.0}),
    (('terrain/',),      # sand and anything new
     {'roughness_base': 0.9, 'roughness_range': 0.1, 'height_strength': 0.6,
      'parallax': 0.0}),
    # -- lights and screens: bright texels emit -------------------------------
    (('scifi_light', 'striplight'),
     {'metal': 0.8, 'roughness_base': 0.4, 'roughness_range': 0.2,
      'height_strength': 0.8, 'emission_threshold': 0.8, 'parallax': 1.0}),
    (('screen', 'monitor'),
     {'roughness_base': 0.2, 'roughness_range': 0.1, 'height_strength': 0.2,
      'ao_strength': 0.2, 'emission_threshold': 0.35, 'parallax': 0.0}),
    # -- printed patterns: the picture is paint, not relief ---------------------
    (('blacklodge',),
     {'roughness_base': 0.3, 'roughness_range': 0.0, 'height_strength': 0.05,
      'ao_strength': 0.0, 'parallax': 0.0}),
    # -- cut-outs and cloth ----------------------------------------------------
    (('cable', 'wire'),
     {'roughness_base': 0.6, 'roughness_range': 0.1, 'height_strength': 0.5,
      'ao_strength': 0.3, 'parallax': 0.0}),
    (('cloth', 'curtain', 'fabric', 'carpet'),
     {'roughness_base': 0.95, 'roughness_range': 0.05, 'height_strength': 0.4,
      'detail': 0.3, 'parallax': 0.5}),
    # -- metal -----------------------------------------------------------------
    (('grate',),
     {'metal': 0.9, 'roughness_base': 0.45, 'roughness_range': 0.25,
      'height_strength': 1.5, 'parallax': 2.0}),
    (('metal', 'mtl', 'panel', 'pnl', 'sci_fi', 'scifi', 'track', 'steel', 'iron',
      'foxhex'),
     {'metal': 0.85, 'roughness_base': 0.45, 'roughness_range': 0.25,
      'height_strength': 1.2, 'parallax': 1.5}),
    # -- everything else: not metal --------------------------------------------
    (('wood', 'crate', 'plank'),
     {'roughness_base': 0.7, 'roughness_range': 0.2, 'height_strength': 1.2,
      'parallax': 1.5}),
    (('cobble', 'brick'),
     {'roughness_base': 0.85, 'roughness_range': 0.15, 'height_strength': 1.6,
      'detail': 0.5, 'parallax': 2.5}),
    (('stone', 'rock'),
     {'roughness_base': 0.85, 'roughness_range': 0.15, 'height_strength': 1.2,
      'parallax': 1.5}),
    (('concrete', 'plaster'),
     {'roughness_base': 0.9, 'roughness_range': 0.1, 'height_strength': 0.8,
      'parallax': 0.5}),
    (('tile', 'floor', 'marble'),
     {'roughness_base': 0.35, 'roughness_range': 0.2, 'height_strength': 0.8,
      'parallax': 0.5}),
)


def starting_params(label):
    """The parameters a texture with no sidecar starts from.

    *label* is its path relative to the assets root, e.g.
    ``textures/terrain/rock.jpg``.
    """
    params = dict(mm.DEFAULT_PARAMS)
    lower = label.lower()
    for needles, overrides in HEURISTICS:
        if any(needle in lower for needle in needles):
            params.update(overrides)
            break
    return params


def find_textures(root):
    """Every image the tool looks at: ``(label, path, rel)`` sorted by label.

    *label* is relative to *root* with forward slashes; *rel* is what
    engine.material_maps' name tests take -- relative to ``textures`` for a
    texture, the bare file name for a model's image.
    """
    found = []
    textures = os.path.join(root, 'textures')
    for folder, dirs, files in os.walk(textures):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, textures).replace(os.sep, '/')
            found.append(('textures/' + rel, path, rel))
    models = os.path.join(root, 'models')
    for folder, dirs, files in os.walk(models):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            label = 'models/' + os.path.relpath(path, models).replace(os.sep, '/')
            found.append((label, path, name))
    return sorted(found)


def classify(path, rel):
    """One of ``'skip'`` (never gets maps), ``'tool'``, ``'hand-made'``,
    ``'generated'``, ``'stale'`` (generated, out of date) or ``'missing'``."""
    if mm.is_tool_texture(rel):
        return 'tool' if mm.is_surface_texture(rel) else 'skip'
    if not mm.is_surface_texture(rel):
        return 'skip'
    found = mm.companion_paths(path)
    if not (found['normal'] or found['material']):
        return 'missing'
    if not mm.generated_info(found['sidecar']):
        return 'hand-made'
    return 'generated' if mm.is_current(path) else 'stale'


def encode_webp(array):
    """*array* (HxWx4 uint8) as lossless WebP, checked to decode to itself.

    ``exact`` keeps the colour of fully transparent texels: in these files
    alpha is the height or the emission, not coverage, and RGB matters
    everywhere.  (An alpha channel that is 255 throughout is not stored: such
    a file opens as RGB, and the missing alpha means 255.)
    """
    buffer = io.BytesIO()
    Image.fromarray(array, 'RGBA').save(buffer, format='WEBP', lossless=True,
                                         exact=True, quality=100, method=4)
    data = buffer.getvalue()
    with Image.open(io.BytesIO(data)) as back:
        if not np.array_equal(np.asarray(back.convert('RGBA')), array):
            raise RuntimeError('lossless WebP did not round-trip')
    return data


def bake(path, label):
    """Generate and write one texture's maps.  Returns the bytes written, or
    None when the image cannot be read."""
    with open(path, 'rb') as handle:
        source = handle.read()
    try:
        with Image.open(io.BytesIO(source)) as image:
            rgba = np.asarray(image.convert('RGBA'))
    except Exception as error:
        print('  cannot read %s: %s' % (label, error), file=sys.stderr)
        return None

    found = mm.companion_paths(path)
    params = mm.load_params(found['sidecar']) if found['sidecar'] else starting_params(label)

    mask = None
    if params.get('emission_mask'):
        mask_path = os.path.join(os.path.dirname(path), str(params['emission_mask']))
        try:
            with Image.open(mask_path) as image:
                mask = np.asarray(image.convert('L'))
        except Exception as error:
            print('  %s: emission mask %s unreadable (%s); using the threshold'
                  % (label, params['emission_mask'], error), file=sys.stderr)

    normal, material = mm.generate(rgba, params, emission_mask=mask)
    base = os.path.splitext(path)[0]
    written = 0
    for suffix, array in (('_n', normal), ('_m', material)):
        data = encode_webp(array)
        mm._atomic_write(base + suffix + '.webp', data)
        written += len(data)
        # A generated map left over in another format would be found first.
        for ext in mm.COMPANION_EXTENSIONS:
            other = base + suffix + ext
            if ext != '.webp' and os.path.isfile(other):
                os.remove(other)
    sidecar = mm.sidecar_path(path)
    mm.save_params(sidecar, params, {
        'version': mm.GENERATOR_VERSION,
        'source_sha256': mm.sha256_bytes(source),
        'params_sha256': mm.params_digest(params),
    })
    return written + os.path.getsize(sidecar)


def companion_bytes(path):
    """The size on disk of *path*'s companions and sidecar."""
    return sum(os.path.getsize(p) for p in mm.companion_paths(path).values() if p)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bake normal and material maps for Fio's stock textures.")
    parser.add_argument('--root', default='assets',
                        help='the assets folder (default: assets)')
    parser.add_argument('--only', action='append', default=[], metavar='GLOB',
                        help='only textures whose name or path matches; repeatable')
    parser.add_argument('--force', action='store_true',
                        help='rebuild generated maps even when they are current')
    parser.add_argument('--check', action='store_true',
                        help='write nothing; fail if any map is missing or stale')
    parser.add_argument('--report', action='store_true',
                        help='write nothing; list every texture with its status')
    args = parser.parse_args(argv)

    if not os.path.isdir(os.path.join(args.root, 'textures')):
        parser.error('no textures folder under %r' % args.root)

    entries = []
    for label, path, rel in find_textures(args.root):
        if args.only and not (mm.matches_any(label, args.only)
                              or mm.matches_any(rel, args.only)):
            continue
        state = classify(path, rel)
        if state != 'skip':
            entries.append((label, path, state))

    if args.report or args.check:
        problems = [(label, state) for label, _path, state in entries
                    if state in ('missing', 'stale')]
        if args.report:
            for label, path, state in entries:
                shown = 'runtime' if state == 'missing' else state
                print('%-10s %s' % (shown, label))
            total = sum(companion_bytes(path) for _l, path, s in entries if s != 'tool')
            print('%d textures, %.1f MB of companion files'
                  % (len(entries), total / 1e6))
        if args.check:
            for label, state in problems:
                print('%s: %s' % (state.upper(), label), file=sys.stderr)
            if problems:
                print('%d texture(s) need tools/make_material_maps.py'
                      % len(problems), file=sys.stderr)
                return 1
            if not args.report:
                print('%d textures: all maps present and current' % len(entries))
        return 0

    started = time.perf_counter()
    baked = written = 0
    for label, path, state in entries:
        if state in ('tool', 'hand-made'):
            continue
        if state == 'generated' and not args.force:
            continue
        size = bake(path, label)
        if size is None:
            continue
        baked += 1
        written += size
        print('%-9s %s (%.0f KB)' % ('rebuilt' if state != 'missing' else 'new',
                                     label, size / 1024.0))
    total = sum(companion_bytes(path) for _l, path, s in entries if s != 'tool')
    print('%d baked (%.1f MB) in %.1f s; companion files now total %.1f MB'
          % (baked, written / 1e6, time.perf_counter() - started, total / 1e6))
    return 0


if __name__ == '__main__':
    sys.exit(main())
