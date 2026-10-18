"""Pixel assertions for the Android world pass, in an existing GL context."""
import io
import json
from pathlib import Path
import time
import zipfile

import numpy as np
from OpenGL import GL
from PIL import Image

from player.runtime.fiopak import FioPackage
from player.world import PlayerWorld


def package(data, assets=None):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('metadata.json', json.dumps({'map_path':'maps/test.json'}))
        out.writestr('maps/test.json', json.dumps(data))
        for name, content in (assets or {}).items():
            out.writestr(name, content)
    return FioPackage.from_bytes(archive.getvalue())


def pixels():
    return np.frombuffer(GL.glReadPixels(0,0,128,128,GL.GL_RGBA,GL.GL_UNSIGNED_BYTE), dtype=np.uint8).reshape(128,128,4).copy()


def run(renderer, evidence=None):
    """Exercise draws, not just shader compilation or simulated visibility."""
    state = {'cam_pos':(0,0,0), 'cam_yaw':-90, 'cam_pitch':0}
    distance = renderer._fallback_runtime.view_distance
    distance.distance = 1000
    distance.fog_color = (.1,.2,.3)

    def save(name, image):
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            Image.fromarray(image[::-1]).save(Path(evidence)/name)

    # Solid red makes radial fog measurable against the engine's CPU reference.
    data = io.BytesIO()
    Image.new('RGBA',(2,2),'red').save(data,format='PNG')
    for depth in (100,760,950):
        brush = {'pos':[0,0,-depth], 'size':[depth,depth,1], 'textures':{'north':'red.png'}}
        with package({'brushes':[brush]}, {'assets/textures/red.png':data.getvalue()}) as archive:
            renderer.load_scene(archive.load_start_map(),archive)
            renderer.render(state)
            actual = pixels()
            factor = distance.fog_factor(depth-.5)
            expected = (np.array((.1,0,0))*(1-factor)+np.array(distance.fog_color)*factor)*255
            np.testing.assert_allclose(actual[64,64,:3], expected, atol=2)
            save(f'fog-{depth}.png',actual)
    distance.fog_density = .02
    with package({'brushes':[{'pos':[0,0,-760], 'size':[760,760,1]}]}) as archive:
        renderer.load_scene(archive.load_start_map(),archive)
        renderer.render(state)
        f = distance.fog_factor(759.5)
        np.testing.assert_allclose(pixels()[64,64,:3],(np.array((.1,.1,.1))*(1-f)+np.array(distance.fog_color)*f)*255,atol=2)
    distance.fog_density = 0
    # A nodraw face must reveal the background, even when its brush's other
    # faces remain visible. Exercise both box and authored convex geometry.
    from engine.brush_geometry import box_to_geometry
    for convex in (False, True):
        for texture in ('nodraw', r'assets\textures\NoDraw.PNG'):
            brush = {'pos':[0,0,-100], 'size':[100,100,10], 'textures':{'north':texture}}
            if convex:
                box_to_geometry(brush)
                # Authored convex plane tags use the engine's orientation.
                for plane in brush['geometry']['planes']:
                    plane['texture'] = texture if plane['n'] == [0,0,1] else ''
            with package({'brushes':[brush]}) as archive:
                renderer.load_scene(archive.load_start_map(),archive)
                renderer.render(state)
                np.testing.assert_allclose(pixels()[64,64,:3],np.array(distance.fog_color)*255,atol=1)
                save(f'nodraw-{"convex" if convex else "box"}.png',pixels())
    # A huge brush still protrudes into the frustum, but its centre is past
    # the engine's XZ distance cap. Verify it never reaches a draw call.
    with package({'brushes':[{'pos':[0,0,-1100], 'size':[2000,2000,1000]}]}) as archive:
        renderer.load_scene(archive.load_start_map(),archive)
        renderer.render(state)
        assert renderer.frame_stats['drawn_batches'] == 0
        assert renderer.frame_stats['culled_batches'] > 0
    # Looking from inside a closed brush must reveal no back-facing surfaces.
    with package({'brushes':[{'pos':[0,0,0], 'size':[100,100,100]}]}) as archive:
        renderer.load_scene(archive.load_start_map(),archive)
        renderer.render(state)
        np.testing.assert_allclose(pixels()[64,64,:3],np.array(distance.fog_color)*255,atol=1)
        save('backfaces-culled.png',pixels())
    distance.distance = 4096
    distance.fog_color = (.1,.1,.15)

    # The canonical desktop ambient and the last light in its full budget
    # must reach actual pixels, rather than just appearing in CPU light lists.
    wall = {'pos':[0,0,-200], 'size':[400,400,10]}
    world = PlayerWorld({'brushes':[wall], 'things':[]})
    try:
        renderer.load_scene(world.data,world=world)
        world.logic.render_runtime.view_distance.ambient = (.2,.05,.1)
        renderer.render(state)
        np.testing.assert_allclose(pixels()[64,64,:3],np.array((.3,.15,.2))*255,atol=2)
        save('desktop-ambient.png',pixels())
    finally:
        world.close()
    from engine.shaders import MAX_LIGHTS
    things = [{'type':'Light','pos':[0,0,0], 'properties':{'radius':1}} for _ in range(MAX_LIGHTS-1)]
    things += [{'type':'Light','pos':[0,0,-100], 'properties':{'radius':500}}]
    world = PlayerWorld({'brushes':[wall], 'things':things})
    try:
        renderer.load_scene(world.data,world=world)
        renderer.render(state)
        assert renderer.frame_stats['lights'] == MAX_LIGHTS
        np.testing.assert_allclose(pixels()[64,64,:3],(.1+(1-95/500)**2)*255,atol=2)
        save('desktop-light-budget.png',pixels())
    finally:
        world.close()

    # A receiving plane must not shadow itself into darkness. Use a grazing
    # light so the desktop slope bias, not just a flat frontal case, is tested.
    world = PlayerWorld({'brushes':[wall], 'things':[{'type':'Light','pos':[-80,80,-150],
                         'properties':{'radius':500,'casts_shadows':True}}]})
    try:
        renderer.load_scene(world.data,world=world)
        renderer.render(state);shadowed = pixels()
        world.state.things[0].properties['casts_shadows'] = False
        renderer.render(state);unshadowed = pixels()
        np.testing.assert_allclose(shadowed[32:96,32:96],unshadowed[32:96,32:96],atol=2)
        save('desktop-shadow-bias.png',shadowed)
    finally:
        world.close()

    for inside in (False,True):
        fog = {'pos':[0,0,0 if inside else -100], 'size':[100,100,100],
               'shader':'Fog', 'fog_density':2, 'fog_color':[.5,.6,.7]}
        with package({'brushes':[wall,fog]}) as archive:
            renderer.load_scene(archive.load_start_map(),archive)
            renderer.render(state)
            assert renderer.frame_stats['fog_volumes'] == 1
            assert np.max(np.abs(pixels()[64,64,:3].astype(float)-25.5)) > 3, 'Local fog volume never blended over the wall'
            save('local-fog-'+('inside' if inside else 'outside')+'.png',pixels())

    # Real point-light depth atlas, including an occluder whose shadow lies
    # outside its camera silhouette on the receiving wall.
    data = {'brushes':[{'pos':[0,0,-200], 'size':[400,400,10]},
                       {'pos':[20,0,-100], 'size':[40,80,20]}],
            'things':[{'type':'Light','pos':[-50,50,-50],
                       'properties':{'intensity':3,'radius':500,'casts_shadows':True}}]}
    world = PlayerWorld(data)
    try:
        renderer.load_scene(data,world=world)
        renderer.render(state)
        shadowed = pixels()
        assert renderer.shadows.fbo and renderer.shadows.key
        save('point-light-shadow.png',shadowed)
        world.state.things[0].properties['casts_shadows'] = False
        renderer.render(state)
        unshadowed = pixels()
        delta = unshadowed[:,:,:3].astype(int)-shadowed[:,:,:3].astype(int)
        assert np.count_nonzero(delta.max(2)>20)>30, 'Depth atlas cast no visible shadow'
        save('point-light-no-shadow.png',unshadowed)
    finally:
        world.close()

    # An ORB must light nearby geometry as well as draw its animated sprite.
    # Keep its sprite off-centre so it cannot satisfy the lighting assertion.
    data = {'brushes':[wall], 'things':[{'type':'Effect','pos':[80,0,-100],
            'properties':{'effect_type':'ORB','width':2,'height':2,
                          'light_radius':500,'light_intensity':2,'light_enabled':False}}]}
    world = PlayerWorld(data)
    try:
        renderer.load_scene(data,world=world)
        renderer.render(state);dark = pixels()
        world.state.things[0].properties['light_enabled'] = True
        renderer.render(state);lit = pixels()
        gain = lit[64,64,:3].astype(int)-dark[64,64,:3].astype(int)
        assert gain[2] > 30 and gain[2] > gain[0]+20, ('ORB cast no blue light on the wall',gain)
        save('effect-light-off.png',dark);save('effect-light-on.png',lit)
    finally:
        world.close()

    # An actual engine Effect row must draw animated, unlit, alpha geometry.
    frames = io.BytesIO()
    Image.new('RGBA',(8,8),'red').save(frames,format='GIF',save_all=True,
        append_images=[Image.new('RGBA',(8,8),'lime')],duration=[100,100],loop=0)
    data = {'brushes':[], 'things':[{'type':'Effect','pos':[0,0,-100],
            'properties':{'effect_type':'CUSTOM', 'custom_gif':'assets/textures/effect.gif',
                          'width':60,'height':60,'light_enabled':False}}]}
    with package(data,{'assets/textures/effect.gif':frames.getvalue()}) as archive:
        world = PlayerWorld(data,package=archive)
        try:
            renderer.load_scene(data,archive,world)
            seen = set()
            for i in range(8):
                renderer.render(state)
                image = pixels()
                color = image[64,64,:3]
                assert max(color)>240, color
                seen.add(int(np.argmax(color)))
                if len(seen)==2:
                    save('animated-effect.png',image)
                    break
                time.sleep(.06)
            assert len(seen)==2, 'Effect GIF never changed frame'
        finally:
            world.close()
    assert GL.glGetError()==GL.GL_NO_ERROR
    print('PASS: GPU nodraw faces, desktop ambient/light budget/shadow bias, local fog, effect lighting, back-face/distance culling, radial/density fog, point-light shadows, animated effect')
