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
        brush = {'pos':[0,0,-depth], 'size':[depth,depth,1], 'textures':{'south':'red.png'}}
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
    print('PASS: GPU back-face/distance culling, engine radial/density fog, point-light shadows, animated effect')
