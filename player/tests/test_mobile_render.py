"""Work-reduction contracts and conservative mobile visibility rules."""
from dataclasses import replace
import math

import numpy as np

from player.render import glmath
from player.render.lighting import batch_lights
from player.render.quality import MOBILE, REFERENCE
from player.render.scene import spatial_batches
from player.render.shadows import face_mask
from player.platform.adaptive import AdaptiveResolution
from player.main import parse_args


def test_light_budget_keeps_influence_from_behind_camera_and_distant_large_lights():
    lights = [dict(pos=(0,0,10), radius=50, intensity=1, color=(1,1,1)),
              dict(pos=(500,0,0), radius=600, intensity=2, color=(1,1,1)),
              dict(pos=(100,0,0), radius=10, intensity=10, color=(1,1,1))]
    # Light 2 cannot reach the batch; light 1 is distant but still contributes.
    assert batch_lights(lights, np.zeros(3), np.ones(3), 8) == (0,1)
    assert batch_lights(lights, np.zeros(3), np.ones(3), 1) == (0,)
    assert batch_lights([], np.zeros(3), np.ones(3), 8) == ()


def test_shadow_face_culling_keeps_boundary_crossings_and_rejects_opposite_side():
    clip = glmath.perspective(math.pi/2,1,1,100) @ glmath.look_at((0,0,0),(1,0,0),(0,-1,0))
    centers = np.array([[10,0,0],[-10,0,0],[10,0,11],[10,0,25]])
    halves = np.full((4,3),2.)
    np.testing.assert_array_equal(face_mask(centers,halves,clip), [True,False,True,False])


def test_static_material_batches_preserve_vertices_and_separate_movers_and_cells():
    brushes = [dict(pos=[x,0,-10],size=[8,8,8]) for x in (1,20,600)]
    mover = dict(pos=[30,0,-10],size=[8,8,8],is_mover=True)
    brushes.append(mover)
    batches = list(spatial_batches(brushes,512))
    assert len(batches) == 3
    assert sum(sum(v.size for v in groups.values()) for groups,_ in batches) == 4*36*8
    assert batches[-1][1][1] is mover
    assert sum(source[0] == 'static' for _,source in batches) == 2
    brushes[0]['hidden'] = True
    rebuilt = list(spatial_batches(brushes,512))
    assert sum(sum(v.size for v in groups.values()) for groups,_ in rebuilt) == 3*36*8


def test_capped_frame_cadence_can_recover_resolution_and_slower_cadence_reduces_it():
    controller = AdaptiveResolution(scale=.6)
    for _ in range(130):
        controller.submit_frame(1000/60)
    assert controller.scale > .6
    for _ in range(130):
        controller.submit_frame(1000/30)
    assert controller.scale == .5


def test_quality_budgets_and_explicit_fixed_resolution_cli():
    assert MOBILE.lights_per_batch == 8 < REFERENCE.lights_per_batch
    assert MOBILE.shadow_samples < REFERENCE.shadow_samples
    assert MOBILE.fog_steps < REFERENCE.fog_steps
    assert parse_args(['--quality','mobile','--render-scale','.5']).render_scale == .5
    assert replace(MOBILE, adaptive=False).render_scale == .67


def test_android_automatically_enables_mobile_quality():
    from types import SimpleNamespace
    from unittest.mock import patch
    from player.app import FioPlayerApp
    for android, expected in ((True,MOBILE),(False,REFERENCE)):
        with patch('player.app.is_android',return_value=android):
            with patch('player.render.renderer.GLESRenderer') as renderer:
                app = FioPlayerApp()
                app._on_gl_ready(SimpleNamespace(width=128,height=128))
                assert renderer.call_args.kwargs['quality'] == expected


def test_mobile_horizon_applies_to_live_levels_and_fallback_culling_and_fog():
    from types import SimpleNamespace
    from unittest.mock import patch
    import pytest
    from engine.logic_render import LogicRender
    from player.render.renderer import GLESRenderer
    from player.render.visibility import environment
    from player.tests.test_visibility import camera

    bounds = np.zeros(3), np.ones(3)
    for quality, horizon in ((MOBILE,1280.),(REFERENCE,4096.)):
        renderer = GLESRenderer(quality=quality)
        runtimes = [renderer._render_runtime()]
        # Each linked level receives the preset, including replacement worlds.
        with patch.object(renderer, '_upload_scene'):
            for _ in range(2):
                runtime = LogicRender(None)
                world = SimpleNamespace(logic=SimpleNamespace(render_runtime=runtime))
                renderer.load_scene({}, world=world)
                assert renderer._render_runtime() is runtime
                runtimes.append(runtime)
        for runtime in runtimes:
            assert runtime.view_distance.far_plane == horizon
            env = environment(runtime.view_distance, (0,0,0))
            assert env['uFogStart'] == pytest.approx(horizon*.6)
            assert env['uFogEnd'] == pytest.approx(horizon*.92)
            assert camera(runtime).visible(bounds, glmath.translate(0,0,-1200), (0,0,-1200))
            assert camera(runtime).visible(bounds, glmath.translate(0,0,-1300), (0,0,-1300)) == (quality is REFERENCE)


def test_phase_shifted_gifs_decode_sequentially_and_keep_bounded_caches():
    import io
    from unittest.mock import patch
    from PIL import Image, GifImagePlugin
    from player.render.assets import GifFrames
    raw = io.BytesIO()
    images = [Image.new('RGB',(4,4),(20*i,0,0)) for i in range(12)]
    images[0].save(raw,format='GIF',save_all=True,append_images=images[1:],duration=100,loop=0)
    frames = GifFrames(raw.getvalue())
    original = GifImagePlugin.GifImageFile._seek
    seeks = []
    def counted(source, frame, *args, **kwargs):
        seeks.append(frame)
        return original(source,frame,*args,**kwargs)
    with patch.object(GifImagePlugin.GifImageFile,'_seek',counted):
        for index in range(6):
            for phase, offset in ((0.,0),(.5,6)):
                image = frames.get_frame(index+offset,phase)
                assert image.getpixel((0,0)) == (20*(index+offset),0,0,255)
    assert len(seeks) < 30, 'Phase-shifted instances repeatedly rewound the GIF'
    assert len(frames.recent) <= 4
    for index in range(20):
        frames.get_frame(index%12,index/21)
    assert len(frames.cursors) <= 8
