import math

import numpy as np

from engine.logic_render import LogicRender
from player.render import glmath
from player.render.scene import build_textured_batches
from player.render.visibility import CameraVisibility, environment


def camera(runtime, eye=(0, 0, 0)):
    eye = np.asarray(eye)
    return CameraVisibility(runtime, glmath.look_at(eye, eye+(0, 0, -1)),
                            glmath.perspective(math.radians(70), 1, .5,
                                               runtime.view_distance.far_plane), eye)


def test_distance_frustum_and_live_view_limit_use_engine_rules():
    runtime = LogicRender(None)
    runtime.view_distance.distance = 1000
    bounds = np.zeros(3), np.ones(3)
    assert camera(runtime).visible(bounds, glmath.translate(0, 0, -900), (0, 0, -900))
    assert not camera(runtime).visible(bounds, glmath.translate(0, 0, -1100), (0, 0, -1100))
    assert not camera(runtime).visible(bounds, glmath.translate(0, 0, 10), (0, 0, 10))
    # Desktop's centre cull is XZ, not 3D; elevated objects stay when in view.
    assert camera(runtime, (0, 500, 0)).visible(bounds, glmath.translate(0, 500, -900), (0, 500, -900))
    runtime.view_distance.limit = 600
    assert not camera(runtime).visible(bounds, glmath.translate(0, 0, -900), (0, 0, -900))
    runtime.view_distance.limit = None
    runtime.view_distance.distance = 2000
    assert camera(runtime).visible(bounds, glmath.translate(0, 0, -1100), (0, 0, -1100))


def test_transformed_bounds_keep_large_edge_and_moving_brushes():
    runtime = LogicRender(None)
    bounds = np.zeros(3), np.array((40, 1, 1))
    assert camera(runtime).visible(bounds, glmath.translate(30, 0, -10), (30, 0, -10))
    assert not camera(runtime).visible(bounds, glmath.translate(100, 0, -10), (100, 0, -10))


def test_brush_triangle_winding_agrees_with_outward_normals():
    vertices = build_textured_batches({'brushes':[{'pos':[0,0,0], 'size':[10,20,30]}]})[''].reshape(-1, 3, 8)
    for triangle in vertices:
        normal = np.cross(triangle[1,:3]-triangle[0,:3], triangle[2,:3]-triangle[0,:3])
        assert normal @ triangle[0,3:6] > 0


def test_fog_uniforms_resolve_engine_limits_density_color_and_ambient():
    distance = LogicRender(None).view_distance
    distance.distance = 1000
    assert environment(distance, (1,2,3)) == {
        'uFogEnabled':1, 'uFogColor':(.1,.1,.15), 'uFogStart':600., 'uFogEnd':920.,
        'uFogDensity':0., 'uFogCamPos':(1.,2.,3.), 'uAmbient':(0.,0.,0.)}
    distance.fog_end = 5000
    distance.fog_density = .02
    distance.fog_color = (.2,.3,.4)
    distance.limit = 700
    values = environment(distance, (0,0,0))
    assert values['uFogEnd'] <= 700
    assert values['uFogEnd'] < distance.far_plane
    assert values['uFogDensity'] == .02
    assert values['uFogColor'] == (.2,.3,.4)
