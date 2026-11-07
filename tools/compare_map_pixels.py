"""Render every shipped map in two checkouts and compare the pixels.

The unified brush model's stages change how brushes are stored, not what
they draw, except where a stage names a deliberate change. This is the
check: each map in ``maps/`` is loaded as the editor loads it
(``EditorState.load_from_data``), its brushes drawn with the map's lights and
real textures from fixed cameras, once by the checkout under test and once
by a base checkout (a ``git worktree``), and the images compared exactly.

Brushes named with ``--expect MAP:ID[,ID...]`` are the deliberate changes:
for that map the comparison is repeated with them removed (must then be
identical), and every pixel that differs in the full render must lie where
removing them changes the image -- so the change is confined to them.

    xvfb-run -a python tools/compare_map_pixels.py --base ../fio-base \\
        --expect _SHOWCASE.json:5b10b49b-...,8c04e483-... --out /tmp/pixels

Each checkout renders in its own process (its own engine), via
``--render-one``. Exit status is non-zero when an unexpected pixel differs.
"""

import argparse
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = (320, 240)


# ---------------------------------------------------------------------------
# Rendering (runs inside one checkout)
# ---------------------------------------------------------------------------

def _cameras(brushes, aspect):
    import glm
    import numpy as np
    pos = np.array([b.get('pos', (0, 0, 0)) for b in brushes], dtype=float)
    size = np.array([b.get('size', (64, 64, 64)) for b in brushes], dtype=float)
    lo = (pos - size / 2).min(axis=0)
    hi = (pos + size / 2).max(axis=0)
    centre = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo)) / 2 or 256.0
    projection = glm.perspective(glm.radians(70.0), aspect, 1.0, 60000.0)
    out = []
    for dx, dy, dz in ((1, 0.6, 1), (-1, 0.6, 1), (1, 0.6, -1), (-1, 0.6, -1),
                       (0.05, 1.5, 0.05), (0.4, 0.15, 0.9)):
        eye = centre + np.array([dx, dy, dz]) * radius * 0.9
        view = glm.lookAt(glm.vec3(*eye), glm.vec3(*centre), glm.vec3(0, 1, 0))
        out.append((projection, view, glm.vec3(*eye)))
    return out


def _focus_cameras(brushes, ids, aspect):
    """Close views of each named brush, so a deliberate change is visible."""
    import glm
    import numpy as np
    projection = glm.perspective(glm.radians(60.0), aspect, 1.0, 60000.0)
    out = []
    for brush in brushes:
        if brush.get('id') not in ids:
            continue
        centre = np.asarray(brush['pos'], dtype=float)
        reach = float(np.linalg.norm(brush['size'])) * 0.9 + 32.0
        for direction in ((0.7, 0.5, 0.8), (-0.8, 0.4, -0.6)):
            eye = centre + np.array(direction) * reach
            view = glm.lookAt(glm.vec3(*eye), glm.vec3(*centre), glm.vec3(0, 1, 0))
            out.append((projection, view, glm.vec3(*eye)))
    return out


def render_tree(out_dir, expect):
    """Render every map with this checkout's engine into *out_dir*."""
    os.environ.setdefault("PYOPENGL_ERROR_CHECKING", "0")
    sys.path.insert(0, HERE)
    import numpy as np
    import bench_geo_passes as passes
    width, height = SIZE
    keep = passes._context(width, height)
    import OpenGL.GL as gl
    from editor.editor_state import EditorState
    from engine.constants import RENDER_MODE_LIT
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    from engine.renderer.forward import ForwardRenderer

    os.makedirs(out_dir, exist_ok=True)
    for path in sorted(glob.glob(os.path.join('maps', '*.json'))):
        name = os.path.basename(path)
        with open(path) as handle:
            data = json.load(handle)
        if not data.get('brushes'):
            continue
        state = EditorState()
        state.load_from_data(data, stream_editor=False, save_undo=False)
        lights = [t for t in state.things
                  if type(t).__name__ == 'Light']
        hidden_ids = set(expect.get(name, ()))
        variants = [('all', state.brushes)]
        if hidden_ids:
            variants.append(('without', [b for b in state.brushes
                                         if b.get('id') not in hidden_ids]))
        # Framed from the file's own records, which both checkouts read
        # alike: a loader that re-derives a stored bound (Stage 2a) must not
        # move the camera.
        cameras = _cameras(data['brushes'], width / height)
        cameras += _focus_cameras(data['brushes'], hidden_ids, width / height)
        images = {}
        for label, brushes in variants:
            renderer = ForwardRenderer(None)
            renderer.shadows_enabled = False
            renderer.set_grid(4096, 64)
            renderer.set_sprite_textures({})
            table = RenderTable()
            table.begin_frame(brushes, epoch=1)
            etable = EntityTable()
            thing_hidden = etable.begin_frame(lights, epoch=1)
            _mask, slots = table.shown()
            config = {"render_mode": RENDER_MODE_LIT, "brush_display_mode": "Textured",
                      "play_mode": False, "grid_visible": False,
                      "camera_distance_cull": False, "shadows_enabled": False,
                      "render_table": table, "all_brush_slots": slots.astype(np.int32),
                      "entity_table": etable,
                      "visible_thing_slots": np.arange(etable.count, dtype=np.int32),
                      "thing_hidden": thing_hidden, "all_brushes": brushes,
                      "all_things": lights, "time": 0.0}
            for index, camera in enumerate(cameras):
                for _ in range(2):          # the second frame is the one kept
                    gl.glViewport(0, 0, width, height)
                    renderer.render_scene(*camera, None, config,
                                          brush_slots=config["all_brush_slots"])
                gl.glFinish()
                pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                                         gl.GL_UNSIGNED_BYTE)
                images["%s_%d" % (label, index)] = np.frombuffer(
                    pixels, dtype=np.uint8).reshape(height, width, 4)
            renderer.cleanup()
        np.savez_compressed(os.path.join(out_dir, name + '.npz'), **images)
        print("rendered %s: %d images" % (name, len(images)), flush=True)
    del keep


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _run(tree, out_dir, expect):
    command = [sys.executable, os.path.abspath(__file__), '--render-one', tree,
               '--out', out_dir, '--expect-json', json.dumps(expect)]
    subprocess.run(command, check=True, cwd=tree)


def compare(base_dir, head_dir, expect):
    import numpy as np
    report = {}
    unexpected = False
    for path in sorted(glob.glob(os.path.join(head_dir, '*.npz'))):
        name = os.path.basename(path)[:-4]
        head = np.load(path)
        base = np.load(os.path.join(base_dir, name + '.npz'))
        rows = {}
        for key in head.files:
            a, b = base[key], head[key]
            differ = (a != b).any(axis=2)
            rows[key] = int(differ.sum())
        entry = {"images": len(head.files), "differing_pixels": rows}
        if name in expect:
            # Full renders may differ only where the named brushes are seen.
            confined = True
            for key in head.files:
                if not key.startswith('all_'):
                    continue
                index = key[len('all_'):]
                differ = (base[key] != head[key]).any(axis=2)
                without = 'without_' + index
                if without in head.files:
                    footprint = (head[key] != head[without]).any(axis=2) | \
                                (base[key] != base[without]).any(axis=2)
                    if (differ & ~footprint).any():
                        confined = False
            entry["changes_confined_to_named_brushes"] = confined
            entry["without_named_identical"] = all(
                rows[k] == 0 for k in head.files if k.startswith('without_'))
            if not (confined and entry["without_named_identical"]):
                unexpected = True
        elif any(rows.values()):
            unexpected = True
        report[name] = entry
    return report, unexpected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', help='the base checkout to compare against')
    parser.add_argument('--tree', default=os.path.dirname(HERE),
                        help='the checkout under test (default: this one)')
    parser.add_argument('--expect', nargs='*', default=[],
                        help='MAP:ID[,ID...] brushes changed on purpose')
    parser.add_argument('--out', default='map_pixels')
    parser.add_argument('--render-one')
    parser.add_argument('--expect-json', default='{}')
    args = parser.parse_args(argv)
    if args.render_one:
        os.chdir(args.render_one)
        sys.path.insert(0, args.render_one)
        render_tree(os.path.abspath(args.out), json.loads(args.expect_json))
        return 0
    expect = {}
    for item in args.expect:
        name, _, ids = item.partition(':')
        expect[name] = [i for i in ids.split(',') if i]
    out = os.path.abspath(args.out)
    base_dir, head_dir = os.path.join(out, 'base'), os.path.join(out, 'head')
    _run(os.path.abspath(args.base), base_dir, expect)
    _run(os.path.abspath(args.tree), head_dir, expect)
    report, unexpected = compare(base_dir, head_dir, expect)
    for name, entry in report.items():
        changed = {k: v for k, v in entry["differing_pixels"].items() if v}
        extra = ""
        if "changes_confined_to_named_brushes" in entry:
            extra = " | confined to named brushes: %s, identical without them: %s" % (
                entry["changes_confined_to_named_brushes"],
                entry["without_named_identical"])
        print("%-34s %2d images, %s%s" % (
            name, entry["images"],
            ("differing: %s" % changed) if changed else "identical", extra))
    with open(os.path.join(out, 'report.json'), 'w') as handle:
        json.dump(report, handle, indent=1)
    print("UNEXPECTED DIFFERENCES" if unexpected else "no unexpected differences")
    return 1 if unexpected else 0


if __name__ == '__main__':
    sys.exit(main())
