"""Decal benchmark: what 10, 100, 1000 and 10000 placed decals cost.

Opt in with ``--run-benchmarks`` (as the renderer benchmark): it prints a
table rather than asserting timings, which depend on the machine (CI and a
headless session rasterise with llvmpipe, on the CPU, so the drawing numbers
are far above a real GPU's; their *growth* is what to read).

Two costs:

* **Logic thread** -- ``DecalTable.place`` runs every published frame. A
  still tick looks at nothing (the world revision and the change journal
  say nothing changed); one decal moved remakes its one row; a new world
  revision (an undoable edit anywhere) walks the decals once and reuses
  every row whose decal is unchanged.
* **Drawing** -- the decal pass culls boxes outside the view (or too small
  to see), then draws the rest: one instanced draw for the image and blood
  decals, one for the wet ones, whatever the count. Its cost is the screen
  area the boxes cover (and, for wet ones, a reflection march each).

The batching itself -- draw calls independent of the count -- is asserted
in the normal GL tier: ``tests/visual/test_wet_decals.py``.
"""

import time

import numpy as np
import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

COUNTS = (10, 100, 1000, 10000)


def _things(n):
    from editor.things import Decal
    rng = np.random.default_rng(1)
    return [Decal(pos=[float(rng.uniform(-900, 900)), 0.0, float(rng.uniform(-1900, -100))],
                  properties={'texture': 'scorch_01.png', 'width': 64.0, 'height': 64.0})
            for _ in range(n)]


def test_logic_thread_cost_of_placed_decals():
    from engine.decal_table import DecalTable
    print('\n  decals | first build | still tick | one moved (rows) | new revision (rows) | frame()')
    for n in COUNTS:
        things = _things(n)
        table = DecalTable()
        start = time.perf_counter()
        table.place(things, epoch=1)
        build = time.perf_counter() - start
        reps = 20 if n <= 1000 else 5
        start = time.perf_counter()
        for _ in range(reps):
            table.place(things, epoch=1)
        still = (time.perf_counter() - start) / reps
        made = table.rows_made
        start = time.perf_counter()
        for i in range(reps):
            things[0].pos = [float(i), 0.0, -500.0]
            table.place(things, epoch=1)
        moved = (time.perf_counter() - start) / reps
        moved_rows = (table.rows_made - made) / reps
        made = table.rows_made
        start = time.perf_counter()
        for i in range(reps):
            table.place(things, epoch=2 + i)          # an edit elsewhere in the level
        revision = (time.perf_counter() - start) / reps
        revision_rows = (table.rows_made - made) / reps
        start = time.perf_counter()
        for _ in range(reps):
            table.frame(0.0, 10.0, 1.0)
        frame = (time.perf_counter() - start) / reps
        print(f'  {n:6d} | {build * 1e3:8.2f} ms | {still * 1e3:7.3f} ms | '
              f'{moved * 1e3:7.3f} ms ({moved_rows:.0f}) | '
              f'{revision * 1e3:8.2f} ms ({revision_rows:.0f}) | {frame * 1e3:5.3f} ms')
        assert len(table.placed_model) == n
        assert moved_rows == 1 and revision_rows == 0     # only what changed


@pytest.mark.gl
def test_drawing_cost_of_placed_decals():
    import OpenGL.GL as gl
    from engine import decal_textures
    from engine.decal_table import box_inverse, placed_row
    from tests.helpers import gl as glh
    from tests.helpers.worlds import box_brush

    glh.reset_texture_cache()
    with glh.GLTestContext(960, 540) as context:
        renderer = glh.make_renderer()
        floor = box_brush('floor', (0, -16, -1000), (2400, 32, 2400))
        projection, view, eye = glh.camera_matrices(
            aspect=960 / 540, eye=(0, 700, 300), target=(0, 0, -900))
        names = decal_textures.images()[1]

        def decals(n, image):
            rng = np.random.default_rng(2)
            layer = names.index(image)
            rows = [placed_row((rng.uniform(-900, 900), 0.0, rng.uniform(-1900, -100)),
                               {'texture': image, 'width': 64.0, 'height': 64.0,
                                'roll': rng.uniform(0, 360)}, layer) for _ in range(n)]
            models = np.stack([r[0] for r in rows])
            return models, np.stack([r[1] for r in rows]), box_inverse(models)

        def frame(rows):
            cfg = glh.render_config(all_brushes=[floor], all_things=[], decals=rows)
            context.bind()
            renderer.render_scene(projection, view, eye, None, cfg,
                                  brush_slots=cfg['all_brush_slots'])
            gl.glFinish()
            best, calls = float('inf'), 0
            for _ in range(5):
                renderer.render_stats.draw_calls = 0
                start = time.perf_counter()
                renderer.render_scene(projection, view, eye, None, cfg,
                                      brush_slots=cfg['all_brush_slots'])
                gl.glFinish()
                best = min(best, time.perf_counter() - start)
                calls = renderer.render_stats.draw_calls
            return best * 1e3, calls, renderer.decals_drawn

        base, base_calls, _ = frame(None)
        print(f'\n  no decals: {base:.2f} ms, {base_calls} draw calls (960x540)')
        print('  decals | image: frame (+cost) calls drawn | wet: frame (+cost) calls drawn '
              '| facing away')
        wet_image = next(n for n in names if decal_textures.is_wet_image(n))
        behind = glh.camera_matrices(aspect=960 / 540, eye=(0, 700, 300), target=(0, 700, 1300))
        for n in COUNTS:
            rows = decals(n, 'scorch_01.png')
            image_ms, image_calls, image_drawn = frame(rows)
            wet_ms, wet_calls, wet_drawn = frame(decals(n, wet_image))
            projection, view, eye = behind
            away_ms, away_calls, away_drawn = frame(rows)
            projection, view, eye = glh.camera_matrices(
                aspect=960 / 540, eye=(0, 700, 300), target=(0, 0, -900))
            print(f'  {n:6d} | {image_ms:7.2f} ms (+{image_ms - base:6.2f}) {image_calls:3d} '
                  f'{image_drawn:5d} | {wet_ms:7.2f} ms (+{wet_ms - base:6.2f}) {wet_calls:3d} '
                  f'{wet_drawn:5d} | {away_ms:6.2f} ms, {away_drawn} drawn')
            assert image_calls == base_calls + 1      # one draw, any count
            assert wet_calls == base_calls + 1        # the wet draw alone
            assert away_drawn == 0
        renderer.cleanup()
    glh.reset_texture_cache()
