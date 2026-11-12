"""Visual tier: placed decals, wet reflections and their distance fade.

Image decals stain the surface; wet decals darken it and reflect the scene
by screen-space reflection, which fades out with distance from the camera
(engine.shaders.REFLECTION_FADE_START / END) and is not traced beyond it;
``reflections 0`` turns every reflection off. Portals fade their view out
over the last quarter of their own "Fade Distance".
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("OpenGL")

from engine.shaders import REFLECTION_FADE_END                     # noqa: E402

pytestmark = pytest.mark.gl


def _level(decals):
    from editor.things import Decal, Light, PlayerStart
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush, level_data, make_thing

    def tex(name):
        return {tag: name for tag in bg.FACE_TAGS}
    brushes = [
        box_brush('floor', (0.0, -16.0, -1500.0), (2048.0, 32.0, 6000.0),
                  textures=tex('concrete02.png')),
        # Bright boxes for the wet floor to reflect, near and far.
        box_brush('near_box', (0.0, 60.0, -380.0), (200.0, 120.0, 40.0),
                  textures=tex('curtains.png')),
        box_brush('far_box', (0.0, 60.0, -2680.0), (200.0, 120.0, 40.0),
                  textures=tex('curtains.png')),
    ]
    things = [make_thing(PlayerStart, 'spawn', (0.0, 50.0, 0.0), angle=90.0, primary=True),
              make_thing(Light, 'lamp', (0.0, 300.0, -300.0), radius=6000.0, intensity=2.0)]
    things += [make_thing(Decal, name, pos, **props) for name, pos, props in decals]
    return level_data(brushes=brushes, things=things)


def _wet(name, z, size=600.0):
    return (name, (0.0, 0.0, z), dict(decal_type='wet', texture='wet_full.png',
                                       width=size, height=size, depth=48.0))


def _session(fio_session, decals):
    session = fio_session(_level(decals), size=(240, 160))
    session.view.sysmon.set_active(False)
    session.start_play()
    session.step(3)
    return session


def _changed(a, b):
    return int((np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 6).sum())


def test_a_near_wet_floor_reflects_and_reflections_0_turns_it_off(fio_session):
    session = _session(fio_session, [_wet('near', -250.0)])
    renderer = session.view.renderer
    on = session.paint()
    session.window.console_handler.handle_command("reflections 0")
    assert renderer.reflections_enabled is False
    off = session.paint()
    session.window.console_handler.handle_command("reflections 1")
    assert renderer.reflections_enabled is True
    assert _changed(on, off) > 200, "the wet floor reflects nothing"


def test_beyond_the_fade_distance_nothing_is_reflected(fio_session):
    far = REFLECTION_FADE_END + 600.0
    session = _session(fio_session, [_wet('far', -far, size=900.0)])
    renderer = session.view.renderer
    on = session.paint()
    renderer.reflections_enabled = False
    off = session.paint()
    renderer.reflections_enabled = True
    assert _changed(on, off) == 0
    # ...but it is still wet: the floor is darker than without the decal.
    session.state.things[:] = [t for t in session.state.things
                               if type(t).__name__ != 'Decal']
    session.step(1)
    dry = session.paint()
    assert _changed(on, dry) > 50


def test_an_image_decal_stains_the_floor(fio_session):
    from editor.things import Decal
    session = fio_session(_level([('scorch', (0.0, 0.0, -220.0),
                                   dict(texture='scorch_01.png', width=400.0,
                                        height=400.0, depth=48.0))]), size=(240, 160))
    session.view.sysmon.set_active(False)
    session.start_play()
    session.step(2)
    stained = session.paint()
    decal = next(t for t in session.state.things if isinstance(t, Decal))
    decal.properties['opacity'] = 0.0
    session.step(1)
    clean = session.paint()
    assert _changed(stained, clean) > 200
    assert stained.astype(int).sum() < clean.astype(int).sum()      # darker


def test_the_decal_tools_selection_is_tinted_in_the_editor(fio_session):
    import glm
    from editor.things import Decal
    session = fio_session(_level([('scorch', (0.0, 0.0, -220.0),
                                   dict(texture='scorch_01.png', width=400.0,
                                        height=400.0, depth=48.0))]), size=(240, 160))
    session.view.sysmon.set_active(False)
    camera = session.logic.camera.get_editor_camera()
    camera.pos = glm.vec3(0.0, 90.0, 0.0)
    camera.yaw, camera.pitch = -90.0, -20.0
    session.publish()
    plain = session.paint()
    decal = next(t for t in session.state.things if isinstance(t, Decal))
    decal.editor_selected = True
    session.publish()
    tinted = session.paint()
    diff = tinted.astype(int) - plain.astype(int)
    assert _changed(tinted, plain) > 200
    assert diff[..., 0].sum() > diff[..., 2].sum()                 # towards orange


def test_portal_views_fade_out_over_the_last_quarter_of_their_fade_distance():
    from editor.things import Portal
    from engine.entity_table import EntityTable
    from engine.renderer.core.visibility import VisibilityMixin
    import glm
    from tests.helpers.worlds import make_thing
    a = make_thing(Portal, 'A', (0.0, 0.0, 0.0), id='pa', portal_target='B',
                   fade_distance=1000.0)
    b = make_thing(Portal, 'B', (5000.0, 0.0, 0.0), id='pb', portal_target='A')
    table = EntityTable()
    table.begin_frame([a, b], epoch=1)
    assert table.portal_fade_distance[0] == 1000.0
    assert table.portal_fade_distance[1] == 2048.0                  # the default
    vis = VisibilityMixin.__new__(VisibilityMixin)
    fade = lambda z: vis._portal_distance_fade(table, 0, glm.vec3(0.0, 0.0, z))
    assert fade(700.0) == 1.0
    assert 0.0 < fade(875.0) < 1.0
    assert fade(875.0) == pytest.approx(0.5)
    assert fade(1000.0) == pytest.approx(0.0)
    slots = table.portal_slots
    assert list(vis._portal_candidate_slots(table, slots, glm.vec3(0, 0, 900.0))) == [0]
    assert list(vis._portal_candidate_slots(table, slots, glm.vec3(0, 0, 1100.0))) == []
