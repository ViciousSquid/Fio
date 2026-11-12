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
from engine.change_journal import touch                           # noqa: E402

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
    return (name, (0.0, 0.0, z), dict(texture='wet_full.png',
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
    touch(decal)            # as whoever changes a rendered value does
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
    touch(decal)            # the Decal tool does this when it selects
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


def test_decalfade_sets_how_far_wet_decals_reflect(fio_session, monkeypatch):
    session = _session(fio_session, [_wet('near', -250.0)])
    renderer = session.view.renderer
    console = session.window.console_handler

    def reflection():
        on = session.paint()
        renderer.reflections_enabled = False
        off = session.paint()
        renderer.reflections_enabled = True
        return _changed(on, off)

    assert renderer.decal_reflection_fade == REFLECTION_FADE_END
    assert reflection() > 200
    console.handle_command("decalfade 120")          # nearer than the decal
    assert renderer.decal_reflection_fade == 120.0
    assert reflection() == 0
    console.handle_command("decalfade 0")             # off
    assert reflection() == 0
    console.handle_command("decalfade 4000")
    assert reflection() > 200
    import editor.console_commands as commands
    said = []
    monkeypatch.setattr(commands, 'debug_log', lambda kind, text: said.append(text))
    console.handle_command("decalfade")
    assert any("4000 units" in text for text in said)
    console.handle_command("decalfade far")
    assert renderer.decal_reflection_fade == 4000.0   # unchanged


def _trigger_level(with_decal):
    from editor.things import Decal, Light, PlayerStart
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush, level_data, make_thing
    tex = lambda n: {t: n for t in bg.FACE_TAGS}
    brushes = [box_brush('floor', (0, -16, -400), (1600, 32, 1600), textures=tex('concrete02.png')),
               box_brush('trig', (0, 80, -700), (200, 160, 100), textures=tex('concrete02.png'),
                         is_trigger=True)]
    things = [make_thing(PlayerStart, 'spawn', (0, 50, 0), angle=90.0, primary=True),
              make_thing(Light, 'lamp', (0, 140, -620), radius=600.0, intensity=2.0,
                         color=[0, 255, 220])]
    if with_decal:
        things.append(make_thing(Decal, 'd', (0, 0, -200), texture='scorch_01.png',
                                 width=300.0, height=300.0, depth=48.0))
    return level_data(brushes=brushes, things=things)


def test_a_decal_on_screen_leaves_the_brushes_drawn_after_it_alone(fio_session):
    # The decal pass used to put the blend function back to its own default
    # rather than the frame's, so a trigger brush (drawn after it) went dark
    # whenever any decal -- blood included -- was in view.
    import glm
    frames = []
    for with_decal in (True, False):
        session = fio_session(_trigger_level(with_decal), size=(320, 200))
        session.view.sysmon.set_active(False)
        camera = session.logic.camera.get_editor_camera()
        camera.pos = glm.vec3(0, 100, 0)
        camera.yaw, camera.pitch = -90.0, -8.0
        session.publish()
        frames.append(session.paint())
        session.close()
    trigger = (slice(40, 110), slice(110, 210))        # above the decal
    assert np.abs(frames[0][trigger].astype(int) - frames[1][trigger].astype(int)).max() <= 2


def test_decals_are_batched_one_draw_whatever_their_count():
    """Every image and blood decal is one instanced draw; wet ones one more;
    none when none can be seen."""
    import OpenGL.GL as gl
    from engine import decal_textures
    from engine.decal_table import placed_row
    from tests.helpers import gl as glh
    from tests.helpers.worlds import box_brush
    glh.reset_texture_cache()
    with glh.GLTestContext(160, 120) as context:
        renderer = glh.make_renderer()
        floor = box_brush('floor', (0, -16, -500), (2000, 32, 2000))
        projection, view, eye = glh.camera_matrices(eye=(0, 500, 300), target=(0, 0, -500))
        names = decal_textures.images()[1]

        def calls(n, images, z=-500.0):
            images = (images,) if isinstance(images, str) else images
            # All within the view: 300 of them over 600 units of floor.
            rows = [placed_row((x * 600.0 / max(n, 1) - 300.0, 0.0, z),
                               {'texture': images[x % len(images)]},
                               names.index(images[x % len(images)])) for x in range(n)]
            rows = None if not n else (np.stack([r[0] for r in rows]),
                                       np.stack([r[1] for r in rows]))
            cfg = glh.render_config(all_brushes=[floor], all_things=[], decals=rows)
            context.bind()
            renderer.render_stats.draw_calls = 0
            renderer.render_scene(projection, view, eye, None, cfg,
                                  brush_slots=cfg['all_brush_slots'])
            gl.glFinish()
            return renderer.render_stats.draw_calls, renderer.decals_drawn

        base, _ = calls(0, 'scorch_01.png')
        assert calls(10, 'scorch_01.png') == (base + 1, 10)
        assert calls(300, 'scorch_01.png') == (base + 1, 300)
        wet = next(n for n in names if decal_textures.is_wet_image(n))
        assert calls(300, wet) == (base + 1, 300)               # the wet draw alone
        assert calls(300, (wet, 'scorch_01.png')) == (base + 2, 300)   # one of each
        # Behind the camera: left out before anything is drawn or copied.
        assert calls(300, 'scorch_01.png', z=900.0) == (base, 0)
        assert renderer.decals_culled == 300
        renderer.cleanup()
    glh.reset_texture_cache()
