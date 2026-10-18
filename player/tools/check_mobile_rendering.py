"""GPU checks for reduced-resolution composition and cached shadow/asset work."""
import io
from unittest.mock import patch

import numpy as np
from OpenGL import GL
from PIL import Image

from player.tools.check_rendering import package, pixels
from player.world import PlayerWorld


def run(renderer):
    state = {'cam_pos':(0,0,0), 'cam_yaw':-90, 'cam_pitch':0}
    wall = {'pos':[0,0,-200], 'size':[400,400,10]}
    occluder = {'pos':[20,0,-100], 'size':[40,80,20], 'is_mover':True}
    data = {'brushes':[wall,occluder], 'things':[{'type':'Light','pos':[-50,50,-50],
            'properties':{'intensity':3,'radius':500,'casts_shadows':True}}]}
    world = PlayerWorld(data)
    try:
        renderer.load_scene(data, world=world)
        renderer.render(state)
        first = pixels()
        assert renderer.frame_stats['shadow_static_draws'] > 0
        with patch.object(GL, 'glGetUniformLocation', wraps=GL.glGetUniformLocation) as lookups:
            renderer.render(state)
            assert renderer.frame_stats['shadow_draws'] == 0
            assert renderer.frame_stats['shadow_faces_updated'] == 0
            assert lookups.call_count == 0, 'Uniform locations were looked up again'
        # Moving to another cube face must clear the old face while retaining
        # cached static depth. Moving outside the light then clears the new face.
        world.brushes[1]['pos'] = [-150,0,0]
        renderer.render(state)
        assert renderer.frame_stats['shadow_static_draws'] == 0
        assert renderer.frame_stats['shadow_faces_updated'] >= 2
        world.brushes[1]['pos'] = [2000,0,0]
        renderer.render(state)
        assert renderer.frame_stats['shadow_static_draws'] == 0
        assert renderer.frame_stats['shadow_draws'] == 0
        assert renderer.frame_stats['shadow_faces_updated'] > 0
        cleared = pixels()
        delta = cleared[:,:,:3].astype(int)-first[:,:,:3].astype(int)
        assert np.count_nonzero(delta.max(2)>20)>30, 'Old dynamic shadow was not cleared'
        renderer.render(state)
        assert renderer.frame_stats['shadow_faces_updated'] == 0
        assert GL.glGetError() == GL.GL_NO_ERROR
    finally:
        world.close()

    frames = io.BytesIO()
    Image.new('RGB',(8,8),'red').save(frames,format='GIF',save_all=True,
        append_images=[Image.new('RGB',(8,8),'lime')],duration=[100,100],loop=0)
    name = 'assets/textures/phase-test.gif'
    data = {'brushes':[], 'things':[{'type':'Effect','pos':[1000,0,-100],
            'properties':{'effect_type':'CUSTOM','custom_gif':name,
                          'width':30,'height':30,'light_enabled':False}}]}
    with package(data, {name:frames.getvalue()}) as archive:
        world = PlayerWorld(data, package=archive)
        try:
            renderer.load_scene(data, archive, world)
            renderer.render(state)
            assert renderer.frame_stats['drawn_batches'] == 0
            assert name not in renderer._images.cache, 'Invisible animation was decoded'
            from player.render import assets
            with patch.object(assets, 'upload_rgba', wraps=assets.upload_rgba) as uploads:
                with patch.object(GL, 'glGenerateMipmap', wraps=GL.glGenerateMipmap) as mipmaps:
                    red = renderer._load_texture(name, .01)
                    green = renderer._load_texture(name, .11)
                    assert red != green
                    assert renderer._load_texture(name, .01) == red
                    assert renderer._load_texture(name, .11) == green
                    assert uploads.call_count == 2, 'Animation phases re-uploaded cached frames'
                    assert mipmaps.call_count == (2 if renderer.quality.animation_mipmaps else 0)
            assert GL.glGetError() == GL.GL_NO_ERROR
        finally:
            world.close()

    if renderer.quality.name == 'mobile':
        distance = renderer._fallback_runtime.view_distance
        previous_distance, previous_fog = distance.distance, distance.fog_enabled
        distance.distance, distance.fog_enabled = 100, False
        try:
            # These share a material/cell. Its merged centre is out of range,
            # but the first brush must remain visible at the distance boundary.
            with package({'brushes':[{'pos':[0,0,-95],'size':[60,60,1]},
                                     {'pos':[0,0,-150],'size':[10,10,1]}]}) as archive:
                renderer.load_scene(archive.load_start_map(),archive)
                renderer.render(state)
                assert renderer.frame_stats['drawn_batches'] == 1
                np.testing.assert_allclose(pixels()[64,64,:3],[25.5]*3,atol=2)
        finally:
            distance.distance, distance.fog_enabled = previous_distance, previous_fog
        renderer.adaptive.scale = .5
        with package({'brushes':[wall]}) as archive:
            renderer.load_scene(archive.load_start_map(), archive)
            renderer.render(state)
            assert (renderer.frame_stats['render_width'],renderer.frame_stats['render_height']) == (64,64)
            assert int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING)) == 0
            np.testing.assert_array_equal(GL.glGetIntegerv(GL.GL_VIEWPORT), [0,0,128,128])
            scene = pixels()
            renderer.render_ui({'hud':'Native HUD'},128,128)
            assert np.any(pixels() != scene), 'HUD did not composite at panel resolution'
            renderer.resize(96,64)
            renderer.render(state)
            assert renderer._target.size == (48,32)
            np.testing.assert_array_equal(GL.glGetIntegerv(GL.GL_VIEWPORT), [0,0,96,64])
            renderer.resize(128,128)
            renderer.adaptive.scale = renderer.quality.render_scale
            assert GL.glGetError() == GL.GL_NO_ERROR
    print('PASS: shadow face/static cache, moving-caster depth removal, cached uniforms, '
          'invisible animation, phase texture reuse, scaled world/native HUD and resize')
