"""Water benchmark: what the water pass costs, deterministically.

    xvfb-run -a python tools/bench_water.py [--scenes devtest,monster,showcase,pool]
        [--size 1280x720] [--frames 40] [--sky cloudy|space|none]
        [--quality expensive|cheap] [--json OUT] [--images DIR] [--diff DIR]

Each scene is a shipped map's brushes and lights (or ``pool``: one water
brush filling the view) drawn by the real ``ForwardRenderer`` -- real
textures, the real water normal map -- into an offscreen framebuffer, from
fixed cameras at a fixed time, so a frame is the same picture every run and
two checkouts can be compared pixel for pixel (``--images`` on one,
``--diff`` on the other).

Per camera it reports medians over ``--frames`` frames:

* ``water_ms``: the water pass alone, with the GL work before it finished
  first and its own finished after (``glFinish`` both sides), so the
  rasteriser's time for the water is inside it;
* ``water_cpu_ms``: the same call without the final finish -- Python and
  submission, what a hardware GPU still leaves on the CPU;
* ``capture_ms``: the scene colour/depth copies the pass makes;
* ``frame_ms``: the whole ``render_scene`` and its finish;
* ``water_fragments``: fragments the water pass shaded (``GL_SAMPLES_PASSED``)
  and ``overdraw``, those per water-covered pixel.

Under software GL (``LIBGL_ALWAYS_SOFTWARE=1``, llvmpipe) the shader cost is
CPU time and exaggerated; compare runs on one host. ``LP_NUM_THREADS=1``
makes it single-threaded and steadier.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
# As main.py: no glGetError after every GL call. Decided before anything
# imports OpenGL.GL, so it holds when another script imports this one.
if os.environ.get('FIO_GL_DEBUG') != '1':
    os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')

SKIES = {'cloudy': 'assets/backgrounds/cloudy-sky-panorama.webp',
         'space': 'assets/backgrounds/void-panorama-q80.webp',
         'none': ''}


def _look(eye, yaw_deg, pitch_deg):
    """Eye, and a target one unit along yaw/pitch (the player's convention:
    yaw 0 looks along +z, 90 along +x)."""
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    d = (math.sin(yaw) * math.cos(pitch), math.sin(pitch), math.cos(yaw) * math.cos(pitch))
    return eye, tuple(e + v for e, v in zip(eye, d))


#: scene -> (map file or None, [(camera name, eye, target), ...])
SCENES = {
    'devtest': ('DevTest.json', [
        ('high', *_look((0.0, 350.0, -120.0), 180.0, -14.0)),
        ('low', *_look((-450.0, 296.0, -360.0), 90.0, -3.0)),
        ('down', *_look((0.0, 350.0, -360.0), 180.0, -85.0)),
    ]),
    'monster': ('MonsterTest.json', [
        ('pool', *_look((-256.0, 160.0, 784.0), 180.0, -18.0)),
    ]),
    'showcase': ('_SHOWCASE.json', [
        ('pool', *_look((960.0, 240.0, 420.0), 180.0, -12.0)),
    ]),
    'pool': (None, [
        ('wide', (-700.0, 160.0, 900.0), (100.0, -60.0, -300.0)),
    ]),
}


def _pool_world():
    """One big water brush over a floor, a pillar in it, a high light."""
    from editor.things import Light
    water = {'name': 'water', 'pos': [0.0, -130.0, 0.0], 'size': [5000.0, 260.0, 5000.0],
             'shader': 'Water', 'is_water': True, 'water_tint': [0.05, 0.35, 0.45],
             'water_opacity': 0.85, 'water_fresnel': 0.6, 'water_wave_height': 0.25,
             'water_wave_enabled': True}
    brushes = [
        {'name': 'floor', 'pos': [0.0, -300.0, 0.0], 'size': [6000.0, 64.0, 6000.0]},
        {'name': 'pillar', 'pos': [-250.0, 20.0, -500.0], 'size': [120.0, 700.0, 120.0]},
        water,
    ]
    for b in brushes:
        b.setdefault('textures', {t: 'Dev/512.jpg' for t in
                                  ('north', 'south', 'east', 'west', 'top', 'down')})
    light = Light(pos=[0.0, 3000.0, 1500.0],
                  properties={'name': 'sun', 'colour': [255, 255, 255], 'intensity': 1.0,
                              'radius': 20000.0, 'state': 'on', 'casts_shadows': False})
    return brushes, [light]


def _map_world(name):
    """A shipped map's brushes and its lights (the water's light loop)."""
    from editor.things import Thing
    from engine import brush_placement as bp
    with open(os.path.join(ROOT, 'maps', name)) as handle:
        doc = json.load(handle)
    brushes = doc['brushes']
    for b in brushes:
        if 'geometry' in b and hasattr(bp, 'localise_legacy'):
            bp.localise_legacy(b)
    lights = []
    for record in doc.get('things', []):
        if record.get('type') == 'light':
            thing = Thing.from_dict(record)
            if thing is not None:
                lights.append(thing)
    return brushes, lights


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scenes', default='devtest,monster,showcase,pool')
    ap.add_argument('--size', default='1280x720')
    ap.add_argument('--frames', type=int, default=40)
    ap.add_argument('--sky', choices=sorted(SKIES), default='none')
    ap.add_argument('--quality', choices=('expensive', 'cheap'), default='expensive')
    ap.add_argument('--time', type=float, default=3.0, help='the water clock, fixed')
    ap.add_argument('--json')
    ap.add_argument('--images', help='save one frame per camera here (PNG)')
    ap.add_argument('--diff', help='compare each frame with the PNG of the same name here')
    args = ap.parse_args(argv)
    width, height = (int(v) for v in args.size.split('x'))

    os.chdir(ROOT)
    sys.path[:0] = [ROOT, HERE]
    import glm
    import numpy as np
    import OpenGL.GL as gl
    import bench_geo_passes as passes
    from engine.constants import RENDER_MODE_LIT
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    from engine.renderer.forward import ForwardRenderer
    from engine.view_distance import ViewDistance

    keep = passes._context(width, height)  # noqa: F841 -- holds the context
    renderer = ForwardRenderer(None)
    renderer.shadows_enabled = False
    renderer.set_grid(4096, 64)
    renderer.set_sprite_textures({})
    renderer.water_quality = args.quality
    renderer.view_distance = ViewDistance(8000.0)
    renderer.set_background_image(SKIES[args.sky])

    timing = {'water': [], 'water_cpu': [], 'capture': [], 'fragments': []}
    query = int(np.asarray(gl.glGenQueries(1)).reshape(-1)[0])
    real_draw = renderer.draw_water_brushes
    real_glass, real_depth = renderer._capture_glass_scene, renderer._capture_scene_depth

    def timed_capture(real):
        def capture(*a, **k):
            t0 = time.perf_counter()
            try:
                return real(*a, **k)
            finally:
                timing['_capture'] += time.perf_counter() - t0
        return capture

    renderer._capture_glass_scene = timed_capture(real_glass)
    renderer._capture_scene_depth = timed_capture(real_depth)

    def draw_water(*a, **k):
        gl.glFinish()
        timing['_capture'] = 0.0
        gl.glBeginQuery(gl.GL_SAMPLES_PASSED, query)
        t0 = time.perf_counter()
        result = real_draw(*a, **k)
        t1 = time.perf_counter()
        gl.glEndQuery(gl.GL_SAMPLES_PASSED)
        gl.glFinish()
        t2 = time.perf_counter()
        timing['water'].append((t2 - t0) * 1e3)
        timing['water_cpu'].append((t1 - t0) * 1e3)
        timing['capture'].append(timing['_capture'] * 1e3)
        timing['fragments'].append(int(gl.glGetQueryObjectuiv(query, gl.GL_QUERY_RESULT)))
        return result

    renderer.draw_water_brushes = draw_water

    def grab():
        data = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)[::-1, :, :3].copy()

    results = {}
    for scene in [s for s in args.scenes.split(',') if s]:
        mapname, cameras = SCENES[scene]
        brushes, lights = _pool_world() if mapname is None else _map_world(mapname)
        table = RenderTable()
        table.begin_frame(brushes, epoch=1)
        etable = EntityTable()
        hidden = etable.begin_frame(lights, epoch=1)
        config = {'render_mode': RENDER_MODE_LIT, 'brush_display_mode': 'Textured',
                  'play_mode': True, 'grid_visible': False, 'camera_distance_cull': False,
                  'shadows_enabled': False, 'render_table': table,
                  'all_brush_slots': np.arange(table.count, dtype=np.int32),
                  'entity_table': etable,
                  'visible_thing_slots': np.arange(etable.count, dtype=np.int32),
                  'thing_hidden': hidden, 'all_brushes': brushes, 'all_things': lights,
                  'all_lights': lights, 'time': args.time}
        projection = glm.perspective(glm.radians(70.0), width / height, 1.0,
                                     renderer.view_distance.far_plane)
        for cam, eye, target in cameras:
            eye_v = glm.vec3(*eye)
            view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
            frame_ms = []
            for key in timing:
                if not key.startswith('_'):
                    timing[key].clear()
            for k in range(args.frames + 5):
                t0 = time.perf_counter()
                renderer.render_scene(projection, view, eye_v, None, config,
                                      brush_slots=config['all_brush_slots'])
                gl.glFinish()
                frame_ms.append((time.perf_counter() - t0) * 1e3)
            for key in ('water', 'water_cpu', 'capture', 'fragments'):
                del timing[key][:5]
            del frame_ms[:5]
            image = grab()
            label = '%s_%s' % (scene, cam)
            # The pixels the water covers: this frame against one without it.
            renderer.water_enabled = False
            renderer.draw_water_brushes = real_draw
            renderer.render_scene(projection, view, eye_v, None, config,
                                  brush_slots=config['all_brush_slots'])
            gl.glFinish()
            dry = grab()
            renderer.water_enabled = True
            renderer.draw_water_brushes = draw_water
            covered = int((np.abs(dry.astype(int) - image.astype(int)).max(axis=2) > 0).sum())
            row = {'frame_ms': round(statistics.median(frame_ms), 3)}
            for key in ('water', 'water_cpu', 'capture'):
                row[key + '_ms'] = round(statistics.median(timing[key]), 3) if timing[key] else 0.0
            fragments = timing['fragments'][-1] if timing['fragments'] else 0
            row['water_fragments'] = fragments
            row['water_pixels'] = covered
            row['overdraw'] = round(fragments / covered, 2) if covered else 0.0
            if args.images:
                from PIL import Image
                os.makedirs(args.images, exist_ok=True)
                Image.fromarray(image).save(os.path.join(args.images, label + '.png'))
            if args.diff:
                from PIL import Image
                ref = np.asarray(Image.open(os.path.join(args.diff, label + '.png')).convert('RGB'))
                delta = np.abs(ref.astype(int) - image.astype(int)).max(axis=2)
                row['diff_max'] = int(delta.max())
                row['diff_mean'] = round(float(delta.mean()), 4)
                row['diff_px_over_2'] = int((delta > 2).sum())
            results[label] = row
            print(label, json.dumps(row), flush=True)
    renderer.cleanup()
    if args.json:
        with open(args.json, 'w') as handle:
            json.dump({'size': [width, height], 'sky': args.sky, 'quality': args.quality,
                       'frames': args.frames, 'results': results}, handle, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
