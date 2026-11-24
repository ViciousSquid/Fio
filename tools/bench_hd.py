"""What HD_High costs beside Classic, measured.

    xvfb-run -a python tools/bench_hd.py [--maps Office_Corridor,_SHOWCASE,...]
        [--sizes 1280x720,1920x1080] [--frames 24] [--json OUT] [--images DIR]

Each stock map is loaded as its brushes, things and terrain-free scene (the
way tools/bench_water.py loads one), drawn by each renderer -- Classic and
HD_High, made through the registry with the real textures -- into an
offscreen framebuffer from the map's PlayerStart, and measured over
``--frames`` frames after a warm-up:

* ``gpu ms``: the whole ``render_scene`` in a ``GL_TIME_ELAPSED`` query;
* per pass, for HD_High, from GL timestamps the renderer takes between its
  passes: sun shadows (and the sky light volume), scene (the opaque world), ambient
  occlusion, reflections (and the resolve), blended, bloom, sun shafts,
  compose (tone mapping, grade, FXAA);
* ``cpu ms``: the wall time of ``render_scene`` itself (Python and
  submission), without waiting for the GPU;
* ``frame ms``: wall time including ``glFinish``;
* ``extra VRAM``: what HD_High's targets, shadow maps and material maps add
  (counted from their sizes and formats);
* Forward+: whether the light grid was in use and how many lamps a cluster
  lists against the lamps in the frame.

A second part measures the sun's shadow map under a moving caster
(``dLight_Test``'s mover): frames in which everything was redrawn against
frames in which only the moving layer was.

Under software GL (``LIBGL_ALWAYS_SOFTWARE=1``, llvmpipe) the "GPU" times are
the CPU rasterising: the split between passes and the ratio between the
renderers hold; the absolute numbers are many times a real GPU's. The header
of the report names the driver.
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
if os.environ.get('FIO_GL_DEBUG') != '1':
    os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')

DEFAULT_MAPS = 'Office_Corridor,_SHOWCASE,MonsterTest,Light_Test'
PASSES = ('sun shadows', 'scene', 'ambient occlusion', 'reflections', 'blended',
          'bloom', 'sun shafts', 'compose')


def _load(name):
    """A map's brushes, its things, and the camera at its PlayerStart."""
    from editor.things import Thing
    from engine import brush_placement as bp
    with open(os.path.join(ROOT, 'maps', name + '.json')) as handle:
        doc = json.load(handle)
    brushes = doc['brushes']
    for brush in brushes:
        if 'geometry' in brush and hasattr(bp, 'localise_legacy'):
            bp.localise_legacy(brush)
    things, start, sky, hd = [], None, '', {}
    for record in doc.get('things', []):
        thing = Thing.from_dict(record)
        if thing is None:
            continue
        if record.get('type') == 'playerstart':
            if start is None:
                start = record
            properties = record.get('properties', {})
            sky = sky or str(properties.get('sky_image') or '')
            hd.update({k: v for k, v in properties.items() if k.startswith('hd_')})
            continue
        things.append(thing)
    if start is not None:
        x, y, z = (float(v) for v in start['pos'])
        angle = math.radians(float(start.get('properties', {}).get('angle', 0.0) or 0.0))
        eye = (x, y + 56.0, z)
        target = (x + math.cos(angle), y + 50.0, z - math.sin(angle))
    else:
        eye, target = (0.0, 220.0, 520.0), (0.0, 60.0, 0.0)
    return brushes, things, eye, target, sky, hd


def _measure(gl, renderer, context, frame, matrices, frames, hd):
    import numpy as np
    from engine.renderer.hd_high.postfx import query_result
    projection, view, eye = matrices
    for _ in range(4):                      # textures, shadow maps, material maps
        context.bind()
        renderer.render_scene(projection, view, eye, None, frame,
                              brush_slots=frame['all_brush_slots'])
        gl.glFinish()
        time.sleep(0.05)                    # generated material maps arrive
    # Material maps are decoded off the render thread and uploaded a few a
    # frame: draw until every map the view asked for is in.
    for _ in range(400):
        if not getattr(renderer, '_material_loading', None):
            break
        context.bind()
        renderer.render_scene(projection, view, eye, None, frame,
                              brush_slots=frame['all_brush_slots'])
        gl.glFinish()
        time.sleep(0.01)
    if hd:
        renderer.hd_timing(True)
        renderer.hd_timing_collect()
    query = int(np.asarray(gl.glGenQueries(1)).ravel()[0])
    gpu, cpu, whole = [], [], []
    for _ in range(frames):
        context.bind()
        gl.glFinish()
        started = time.perf_counter()
        gl.glBeginQuery(gl.GL_TIME_ELAPSED, query)
        renderer.render_scene(projection, view, eye, None, frame,
                              brush_slots=frame['all_brush_slots'])
        submitted = time.perf_counter()
        gl.glEndQuery(gl.GL_TIME_ELAPSED)
        gl.glFinish()
        finished = time.perf_counter()
        gpu.append(query_result(query) * 1e-6)
        cpu.append((submitted - started) * 1e3)
        whole.append((finished - started) * 1e3)
    gl.glDeleteQueries(1, [query])
    result = {'gpu_ms': statistics.median(gpu), 'cpu_ms': statistics.median(cpu),
              'frame_ms': statistics.median(whole)}
    if hd:
        collected = renderer.hd_timing_collect()
        renderer.hd_timing(False)
        result['passes'] = {name: statistics.median([f.get(name, 0.0) for f in collected])
                            for name in PASSES}
        result['vram'] = renderer.hd_memory()
        result['forward_plus'] = dict(renderer.light_grid_stats)
        result['exposure'] = renderer._env_base_exposure
        result['sky'] = renderer.environment.sky_mode
    return result


def _movers(gl, glh, np, frames):
    """The sun's shadow map while a caster moves: full redraws against
    redraws of the moving layer only."""
    from engine import hd_graphics
    from tests.helpers.worlds import box_brush
    brushes = [box_brush('floor', (0, -16, 0), (4096, 32, 4096))]
    for index in range(60):
        brushes.append(box_brush('block%d' % index, ((index % 10 - 5) * 300.0, 96.0,
                                                    (index // 10 - 3) * 300.0),
                                 (120.0, 192.0, 120.0)))
    door = box_brush('door', (0.0, 128.0, 900.0), (200.0, 256.0, 32.0), is_door=True)
    brushes.append(door)
    with glh.GLTestContext(640, 360) as context:
        renderer = glh.make_renderer(None, name='HD_High')
        try:
            renderer.set_hd_sky(hd_graphics.sky_settings({'hd_sky': 'procedural'}, ''))
            matrices = glh.camera_matrices(aspect=640 / 360, eye=(0.0, 400.0, 1800.0))
            times = []
            for step in range(frames):
                door['pos'] = [math.sin(step * 0.3) * 200.0, 128.0, 900.0]
                frame = glh.render_config(all_brushes=brushes, all_things=[], play_mode=True)
                context.bind()
                gl.glFinish()
                started = time.perf_counter()
                renderer.render_scene(*matrices, None, frame,
                                      brush_slots=frame['all_brush_slots'])
                gl.glFinish()
                times.append((time.perf_counter() - started) * 1e3)
            stats = dict(renderer.sun_shadow_stats)
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
    return {'frames': frames, 'casters': len(brushes), 'full_redraws': stats['static'],
            'moving_layer_redraws': stats['dynamic'], 'casters_drawn': stats['casters'],
            'casters_a_full_redraw_each_frame_would_draw': frames * len(brushes)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--maps', default=DEFAULT_MAPS)
    parser.add_argument('--sizes', default='1280x720,1920x1080')
    parser.add_argument('--frames', type=int, default=24)
    parser.add_argument('--json')
    parser.add_argument('--images', help='save one frame per map and renderer here (PNG)')
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    sys.path[:0] = [ROOT, HERE]
    import numpy as np
    import OpenGL.GL as gl
    from PIL import Image
    from engine import hd_graphics
    from engine.renderer import renderer_factory
    from tests.helpers import gl as glh
    from tests.helpers import golden

    report = {'maps': {}, 'frames': args.frames}
    for size in args.sizes.split(','):
        width, height = (int(v) for v in size.split('x'))
        for name in args.maps.split(','):
            brushes, things, eye, target, sky, hd = _load(name)
            for label, registry in (('Classic', 'Forward'), ('HD_High', 'HD_High')):
                with glh.GLTestContext(width, height) as context:
                    report.setdefault('driver', golden.driver_name())
                    renderer = renderer_factory(registry)(None)
                    try:
                        renderer.set_grid(4096, 64)
                        renderer.set_sprite_textures({})
                        renderer.set_background_image(sky)
                        if label == 'HD_High':
                            renderer.set_hd_sky(hd_graphics.sky_settings(hd, sky))
                        frame = glh.render_config(all_brushes=brushes, all_things=things,
                                                  play_mode=True)
                        matrices = glh.camera_matrices(aspect=width / height, fov_deg=75.0,
                                                       eye=eye, target=target)
                        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
                        result = _measure(gl, renderer, context, frame, matrices,
                                          args.frames, label == 'HD_High')
                        if args.images:
                            os.makedirs(args.images, exist_ok=True)
                            Image.fromarray(context.read_pixels()).save(os.path.join(
                                args.images, '%s_%s_%s.png' % (name, size, label)))
                    finally:
                        renderer.cleanup()
                report['maps'].setdefault(name, {}).setdefault(size, {})[label] = result
                print('%-18s %-10s %-8s gpu %8.2f ms  cpu %7.2f ms  frame %8.2f ms'
                      % (name, size, label, result['gpu_ms'], result['cpu_ms'],
                         result['frame_ms']), flush=True)
    report['movers'] = _movers(gl, glh, np, 40)

    print()
    print('driver: %s' % report.get('driver'))
    print('| map | size | renderer | GPU ms | CPU ms | frame ms | extra VRAM |')
    print('| --- | --- | --- | ---: | ---: | ---: | ---: |')
    for name, sizes in report['maps'].items():
        for size, both in sizes.items():
            for label, r in both.items():
                vram = sum(r['vram'].values()) / 2 ** 20 if 'vram' in r else 0.0
                print('| %s | %s | %s | %.1f | %.1f | %.1f | %s |' % (
                    name, size, label, r['gpu_ms'], r['cpu_ms'], r['frame_ms'],
                    '%.0f MB' % vram if vram else '-'))
    print()
    print('| map | size | ' + ' | '.join(PASSES) + ' |')
    print('| --- | --- | ' + ' | '.join('---:' for _ in PASSES) + ' |')
    for name, sizes in report['maps'].items():
        for size, both in sizes.items():
            passes = both['HD_High']['passes']
            print('| %s | %s | ' % (name, size)
                  + ' | '.join('%.1f' % passes[p] for p in PASSES) + ' |')
    movers = report['movers']
    print()
    print('sun shadow map, %(frames)d frames with a door moving among %(casters)d casters: '
          '%(full_redraws)d full redraws, %(moving_layer_redraws)d moving-layer redraws, '
          '%(casters_drawn)d casters drawn (a full redraw every frame: '
          '%(casters_a_full_redraw_each_frame_would_draw)d)' % movers)
    if args.json:
        with open(args.json, 'w') as handle:
            json.dump(report, handle, indent=1)
    return 0


if __name__ == '__main__':
    status = main()
    sys.stdout.flush()
    os._exit(status)
