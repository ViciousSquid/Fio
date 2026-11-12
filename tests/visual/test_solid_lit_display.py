"""Visual tier: the 'Solid Lit' display shows nodraw brushes textured (what
play will not draw stays plain to see) and never shows water textured: it
is a shaded, see-through brush there, not the water surface."""

import numpy as np
import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("OpenGL")

pytestmark = pytest.mark.gl


def _level():
    import glm  # noqa: F401
    from editor.things import Light, PlayerStart
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush, level_data, make_thing

    def tex(name):
        return {tag: name for tag in bg.FACE_TAGS}
    return level_data(
        brushes=[box_brush('floor', (0.0, -16.0, -300.0), (1200.0, 32.0, 1200.0),
                           textures=tex('concrete02.png')),
                 box_brush('nodraw_box', (-120.0, 60.0, -300.0), (140.0, 120.0, 40.0),
                           textures=tex('nodraw.jpg')),
                 box_brush('plain_box', (120.0, 60.0, -300.0), (140.0, 120.0, 40.0),
                           textures=tex('concrete02.png')),
                 box_brush('pool', (0.0, 10.0, -150.0), (300.0, 20.0, 120.0),
                           textures=tex('concrete02.png'), is_water=True)],
        things=[make_thing(PlayerStart, 'spawn', (0.0, 50.0, 0.0), angle=90.0, primary=True),
                make_thing(Light, 'lamp', (0.0, 300.0, -200.0), radius=3000.0)])


@pytest.fixture
def session(fio_session):
    import glm
    s = fio_session(_level(), size=(240, 160))
    s.view.sysmon.set_active(False)
    camera = s.logic.camera.get_editor_camera()
    camera.pos = glm.vec3(0.0, 120.0, 0.0)
    camera.yaw, camera.pitch = -90.0, -15.0
    return s


def _paint(session, mode):
    session.window.set_brush_display_mode(mode)
    session.publish()
    return session.paint()


def test_solid_lit_draws_nodraw_textured_and_water_untextured(session, monkeypatch):
    renderer = session.view.renderer
    calls = {'water': 0, 'textured': [], 'lit': []}
    real_water = renderer.draw_water_brushes
    real_textured = renderer.draw_textured_brushes_optimized
    real_lit = renderer.draw_lit_brushes_optimized

    def water(*a, **k):
        if len(a[3]):
            calls['water'] += 1
        return real_water(*a, **k)

    def textured(projection, view, camera_pos, brushes, *a, **k):
        calls['textured'].append(set(int(b) for b in brushes))
        return real_textured(projection, view, camera_pos, brushes, *a, **k)

    def lit(projection, view, camera_pos, brushes, *a, **k):
        calls['lit'].append(set(int(b) for b in brushes))
        return real_lit(projection, view, camera_pos, brushes, *a, **k)

    monkeypatch.setattr(renderer, 'draw_water_brushes', water)
    monkeypatch.setattr(renderer, 'draw_textured_brushes_optimized', textured)
    monkeypatch.setattr(renderer, 'draw_lit_brushes_optimized', lit)
    _paint(session, 'Solid Lit')
    names = {b['name']: i for i, b in enumerate(session.state.brushes)}
    table = session.view._render_config['render_table']
    slot = {name: int(table.slot_of_id[b['id']]) for name, b in
            ((b['name'], b) for b in session.state.brushes)}
    textured_slots = set().union(*calls['textured']) if calls['textured'] else set()
    lit_slots = set().union(*calls['lit']) if calls['lit'] else set()
    assert slot['nodraw_box'] in textured_slots                      # textured
    assert slot['plain_box'] not in textured_slots and slot['plain_box'] in lit_slots
    assert calls['water'] == 0                                        # never the water surface
    assert slot['pool'] in lit_slots                                  # a shaded brush instead
    calls.update(water=0, textured=[], lit=[])
    _paint(session, 'Textured')
    assert calls['water'] == 1                                        # Textured: water as ever
    assert names                                                      # (sanity)
