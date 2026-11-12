"""Real pixel checks for projected decals in the player (ES 3 or desktop GL).

A package with a floor, a placed image decal and a placed wet decal is played
by a real PlayerWorld: the logic side makes the decal rows (DecalTable) from
the package's own images, publishes them, and the renderer paints them. The
image decal stains the floor its colour, the wet one darkens it, and a frame
with no decal in view draws nothing for them and stays on the window.
"""
import io
from pathlib import Path

import numpy as np
from OpenGL import GL
from PIL import Image

from .check_rendering import package, pixels
from player.render import glmath


def _png(colour, size=(16, 16)):
    out = io.BytesIO()
    Image.new('RGBA', size, colour).save(out, format='PNG')
    return out.getvalue()


def run(renderer, evidence=None):
    from player.input.state import InputState
    from player.world import PlayerWorld
    floor = dict(pos=[0, -10, 0], size=[1200, 20, 1200], textures={'top': 'white.png'})
    decal = lambda x, texture: dict(type='decal', pos=[x, 0, 0], properties=dict(
        texture=texture, width=160.0, height=160.0, depth=48.0, pitch=-90.0, opacity=1.0))
    data = dict(version=4, brushes=[floor],
                things=[decal(-200, 'blue_stain.png'), decal(200, 'wet_patch.png')])
    assets = {'assets/textures/white.png': _png((255, 255, 255, 255)),
              'assets/textures/decals/blue_stain.png': _png((0, 0, 128, 255)),
              'assets/textures/decals/wet_patch.png': _png((255, 255, 255, 255))}
    with package(data, assets) as archive:
        world = PlayerWorld(archive.load_start_map(), archive)
        try:
            renderer.load_scene(archive.load_start_map(), archive, world=world)
            world.logic.render_runtime.view_distance.fog_enabled = False
            world.step(1 / 30, InputState(), -90, 0)
            published = world.decal_frame()
            assert published is not None and len(published[0]) == 2, \
                f'The logic side published no placed decals: {published!r}'
            eye = (0.0, 700.0, 0.0)
            state = dict(cam_pos=eye, view=glmath.look_at(eye, (0, 0, 0), (0, 0, -1)), fov=70)
            renderer.render(state)
            image = pixels()
            assert renderer.frame_stats.get('decals') == 2, renderer.frame_stats
            assert GL.glGetError() == GL.GL_NO_ERROR
            if evidence:
                Path(evidence).mkdir(parents=True, exist_ok=True)
                Image.fromarray(image[::-1]).save(Path(evidence) / 'decals.png')
            # x = -200 is left of centre on screen, x = +200 right of it; the
            # floor between them is lit plain white-ish.
            left, middle, right = (image[64, c, :3].astype(int) for c in (40, 64, 88))
            assert middle[0] > 15 and abs(middle[0] - middle[2]) < 8, f'floor {middle}'
            assert left[2] > left[0] + 10, f'The image decal did not stain the floor: {left}'
            assert right.sum() < middle.sum() * 0.8, f'The wet decal did not darken: {right}'
            # Looking away: no decal is drawn, and none costs anything.
            away = dict(cam_pos=eye, view=glmath.look_at(eye, (0, 1400, -10), (0, 1, 0)), fov=70)
            renderer.render(away)
            assert renderer.frame_stats.get('decals') == 0, renderer.frame_stats
            assert renderer.decals.culled == 2
            assert GL.glGetError() == GL.GL_NO_ERROR
        finally:
            world.close()
            renderer.clear_scene()
            from engine import decal_textures
            decal_textures.use_source(None)
    print('PASS: placed image and wet decals from the package, drawn through the '
          'depth copy; culled when out of view')
