"""Real pixel checks for water with an editor TRIGGER material."""
import io
from pathlib import Path
from unittest.mock import patch

import numpy as np
from OpenGL import GL
from PIL import Image

from .check_rendering import package, pixels
from player.render import glmath


def run(renderer, evidence=None, recreate_context=None):
    marker = io.BytesIO()
    Image.new('RGB',(4,4),'magenta').save(marker,format='PNG')
    water = dict(pos=[0,-20,-140],size=[240,80,240],shader='Water',
                 textures={key:'trigger.jpg' for key in ('north','south','east','west','top','down')},
                 water_tint=[0,.4,.6],water_opacity=.8,water_reflectivity=.5)
    floor = dict(pos=[0,-70,-140],size=[600,10,600])
    state = dict(cam_pos=(0,120,70),view=glmath.look_at((0,120,70),(0,0,-140)))
    distance = renderer._fallback_runtime.view_distance
    old_fog,old_start = distance.fog_enabled,renderer._start_time
    distance.fog_enabled = False
    try:
        with package({'brushes':[floor,water]}, {'assets/textures/trigger.jpg':marker.getvalue()}) as archive:
            renderer.load_scene(archive.load_start_map(),archive)
            brush = renderer._scene['map']['brushes'][1]
            with patch('time.perf_counter',return_value=100.):
                renderer._start_time = 98.
                with patch.object(GL,'glCopyTexSubImage2D',wraps=GL.glCopyTexSubImage2D) as copies:
                    renderer.render(state)
                    assert copies.call_count == 1, 'Water should capture the scene only once'
                blue = pixels()
                assert renderer.frame_stats['water_brushes'] == 1
                assert 'trigger.jpg' not in renderer._images.cache, 'Water decoded its editor marker'
                assert np.any(blue[40:90,40:90,2].astype(int) > blue[40:90,40:90,0].astype(int)+15), 'No blue water surface'
                if evidence:
                    Path(evidence).mkdir(parents=True,exist_ok=True)
                    Image.fromarray(blue[::-1]).save(Path(evidence)/'water-trigger-material.png')
                renderer._start_time = 94.
                renderer.render(state)
                assert np.count_nonzero(np.max(np.abs(pixels().astype(int)-blue.astype(int)),axis=2)>3)>50, 'Water never animated'
                renderer._start_time = 98.
                brush['water_tint'] = [.5,.8,.05]
                renderer.render(state)
                assert np.mean(pixels()[40:90,40:90,1].astype(float)-blue[40:90,40:90,1])>3, 'Authored tint was ignored'
                brush['water_tint'] = [0,.4,.6]
                brush['water_opacity'] = .05
                renderer.render(state)
                assert np.mean(np.abs(pixels().astype(int)-blue.astype(int)))>1, 'Authored opacity was ignored'
                brush['water_opacity'] = .8
                # The surface must remain visible from below while swimming.
                below = dict(cam_pos=(0,0,-140),view=glmath.look_at((0,0,-140),(0,40,-200)))
                renderer.render(below)
                assert renderer.frame_stats['water_brushes'] == 1
                assert np.ptp(pixels()[:,:,:3])>20, 'Underwater surface was blank'
                renderer.render(state)
                if recreate_context:
                    recreate_context()
                    renderer._start_time = 98.
                    renderer.render(state)
                    np.testing.assert_allclose(pixels(),blue,atol=2)
                    assert renderer.frame_stats['water_brushes'] == 1
                brush['hidden'] = True
                with patch.object(GL,'glCopyTexSubImage2D',wraps=GL.glCopyTexSubImage2D) as copies:
                    renderer.render(state)
                    assert copies.call_count == 0, 'Hidden water still copied the framebuffer'
                assert renderer.frame_stats['water_brushes'] == 0
                np.testing.assert_allclose(pixels()[64,64,:3],25.5,atol=2)
                brush['hidden'] = False
                brush['pos'] = [0,-20,-5000]
                renderer.render(state)
                assert renderer.frame_stats['water_brushes'] == 0, 'Water ignored distance culling'
                assert GL.glGetError() == GL.GL_NO_ERROR
    finally:
        distance.fog_enabled = old_fog
        renderer._start_time = old_start
    print('PASS: TRIGGER-material water, animation, tint/opacity, underwater surface, '
          'context recreation, hidden/distant rejection and single scene capture')
