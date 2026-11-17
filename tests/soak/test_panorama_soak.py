"""Soak: switching the map's sky panorama for many frames.

3.5.0.1611 added a camera-locked panorama behind the scene: the forward
renderer decodes and uploads the image lazily on the first frame that needs
it, frees it when the path changes and remembers a path that failed. The
editor sets the path every frame (``QtGameView.paintGL``), so the cheap
paths -- an unchanged sky, a missing file -- must stay cheap forever.

Over many cycles of switching between the shipped presets, an absolute
path, no sky and a missing file, rendering a few frames each:

* GL texture names generated minus deleted stay flat (no texture leak);
* a missing or broken path is looked up once per selection, not per frame;
* frame cost and the traced heap stay flat.
"""

import os

import pytest

from tests.helpers import gl as glh
from tests.helpers.paths import REPO_ROOT
from tests.helpers.soak import Soak, cycles

pytestmark = [pytest.mark.gl, pytest.mark.soak, pytest.mark.slow]

SKIES = (
    "assets/backgrounds/cloudy-sky-panorama.webp",
    "assets/backgrounds/void-panorama-q80.webp",
    "",
    "assets/backgrounds/no-such-panorama.webp",
    os.path.join(REPO_ROOT, "assets", "backgrounds", "cloudy-sky-panorama.webp"),
    "assets/backgrounds/void-panorama-q80.webp",
)
FRAMES_PER_SKY = 3


def test_switching_panoramas_leaks_no_textures_and_stays_cheap(monkeypatch):
    import OpenGL.GL as gl
    from engine import asset_archives

    live = set()
    real_gen, real_delete = gl.glGenTextures, gl.glDeleteTextures

    def gen(count, *args):
        names = real_gen(count, *args)
        try:
            live.update(int(n) for n in names)
        except TypeError:
            live.add(int(names))
        return names

    def delete(names, *args):
        try:
            live.difference_update(int(n) for n in names)
        except TypeError:
            live.discard(int(names))
        return real_delete(names, *args)

    monkeypatch.setattr(gl, "glGenTextures", gen)
    monkeypatch.setattr(gl, "glDeleteTextures", delete)

    lookups = {}
    real_source = asset_archives.asset_source

    def counted_source(path):
        key = os.path.basename(str(path))
        lookups[key] = lookups.get(key, 0) + 1
        return real_source(path)

    monkeypatch.setattr(asset_archives, "asset_source", counted_source)

    brushes, things = glh.lit_cube_scene(shadows=False)
    glh.reset_texture_cache()
    with glh.GLTestContext(256, 144) as context:
        renderer = glh.make_renderer()
        try:
            projection, view, eye = glh.camera_matrices(aspect=256 / 144)
            config = glh.render_config(all_brushes=brushes, all_things=things)

            def frame(path):
                context.bind()
                renderer.set_background_image(path)    # as paintGL does
                renderer.render_scene(projection, view, eye, None, config,
                                      brush_slots=config["all_brush_slots"])
                gl.glFinish()

            total = cycles(120, minimum=24)
            selections = 0
            with Soak("panorama switching", total, warmup=6) as soak:
                for n in range(total):
                    for path in SKIES:
                        selections += 1
                        for _ in range(FRAMES_PER_SKY):
                            frame(path)
                    errors = glh.drain_gl_errors()
                    assert not errors, "cycle %d raised GL errors: %s" % (n, errors)
                    soak.cycle_done(counters={'live GL textures': len(live)})

            soak.assert_counter_flat('live GL textures')
            soak.assert_time_flat(factor=2.0, slack_seconds=0.010)
            soak.assert_heap_flat(per_cycle_bytes=1024)
            # A leaked panorama upload is 0.8-1.1 MiB of driver memory each.
            soak.assert_rss_flat(per_cycle_bytes=128 * 1024)

            # Each switch to the missing file looks it up at most twice (the
            # path itself, then under the project root) -- never per frame.
            missing_selections = selections // len(SKIES)
            assert lookups.get("no-such-panorama.webp", 0) <= 2 * missing_selections
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
