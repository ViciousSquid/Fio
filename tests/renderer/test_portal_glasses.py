"""Portal glasses projection through a real Renderer_F and EntityTable."""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="renderer tests need the production Qt/OpenGL runtime")
pytest.importorskip("OpenGL")

from editor.things import Portal
from tests.helpers.worlds import make_thing
from engine.entity_table import EntityTable
from engine.renderer_core import RenderView
from tests.helpers import gl as glh
from engine.portal_transform import map_point, mirror_point

pytestmark = pytest.mark.gl

A_POS = (1072.0, 224.0, -800.0)
B_POS = (480.0, 224.0, -448.0)
SELF_EYE = (1000.0, 154.0, -1100.0)
OTHER_EYE = (300.0, 154.0, -500.0)


@pytest.fixture
def renderer():
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64):
        renderer = glh.make_renderer()
        try:
            yield renderer
        finally:
            renderer.cleanup()
            glh.reset_texture_cache()


def _portals(glasses=True):
    first = make_thing(Portal, "Portal_A", A_POS, id="portal-a", glasses=glasses, portal_target="Portal_B", rotation=[180.0, 0.0, 0.0])
    second = make_thing(Portal, "Portal_B", B_POS, id="portal-b", glasses=True, portal_target="Portal_A", rotation=[270.0, 0.0, 0.0])
    table = EntityTable()
    table.begin_frame([first, second], epoch=1)
    return first, second, table


def _cfg(*positions, glasses=True, sprites=None):
    _first, _second, table = _portals(glasses=glasses)
    cfg = {
        "player_glasses_positions": tuple(positions),
        "player_glasses_sprites": tuple(sprites or ()),
        "entity_table": table,
    }
    return cfg


def _view(depth=1, aperture=0, clip=1):
    return RenderView(
        projection=None,
        view=None,
        camera_pos=None,
        aperture_slot=aperture,
        clip_slot=clip,
        recursion_depth=depth,
    )


def _entries(renderer, cfg, view, camera):
    return renderer._portal_glasses_positions(cfg, view, camera)


def _positions(renderer, cfg, view, camera):
    return tuple(pos for pos, _sprite in _entries(renderer, cfg, view, camera))


def test_own_glasses_are_mirrored_and_others_stay_where_they_are(renderer):
    cfg = _cfg(SELF_EYE, OTHER_EYE)
    got = _positions(renderer, cfg, _view(), SELF_EYE)
    table = cfg["entity_table"]
    expected_self = mirror_point(
        table.pos[0], table.portal_basis[0],
        table.pos[1], table.portal_basis[1], SELF_EYE,
    )
    assert len(got) == 2
    assert np.allclose(got[0], OTHER_EYE)
    assert np.allclose(got[1], expected_self)
    cam = map_point(
        table.pos[0], table.portal_basis[0],
        table.pos[1], table.portal_basis[1], SELF_EYE,
    )
    assert np.linalg.norm(np.subtract(got[1], cam)) > 100.0


def test_nested_portal_views_drop_the_self_reflection(renderer):
    got = _positions(renderer, _cfg(SELF_EYE, OTHER_EYE), _view(depth=2), SELF_EYE)
    assert len(got) == 1
    assert np.allclose(got[0], OTHER_EYE)


def test_camera_away_from_every_player_draws_them_all_unmirrored(renderer):
    far_camera = (0.0, 2000.0, 0.0)
    got = _positions(renderer, _cfg(SELF_EYE, OTHER_EYE), _view(), far_camera)
    assert np.allclose(got, (SELF_EYE, OTHER_EYE))


def test_no_players_draw_nothing(renderer):
    assert _positions(renderer, _cfg(), _view(), SELF_EYE) == ()


def test_portal_with_glasses_off_keeps_everyone_at_their_real_position(renderer):
    got = _positions(
        renderer,
        _cfg(SELF_EYE, OTHER_EYE, glasses=False),
        _view(),
        SELF_EYE,
    )
    assert np.allclose(got, (SELF_EYE, OTHER_EYE))


def test_glasses_is_read_from_the_portal_being_looked_into(renderer):
    cfg = _cfg(SELF_EYE, glasses=False)
    assert np.allclose(_positions(renderer, cfg, _view(), SELF_EYE), (SELF_EYE,))

    into_b = _view(aperture=1, clip=0, depth=1)
    got = _positions(renderer, cfg, into_b, SELF_EYE)
    assert len(got) == 1 and not np.allclose(got[0], SELF_EYE)


def test_each_player_keeps_their_own_glasses_sprite(renderer):
    cfg = _cfg(
        SELF_EYE, OTHER_EYE,
        sprites=("Glasses:pixel_shades", "Glasses"),
    )
    got = _entries(renderer, cfg, _view(), SELF_EYE)
    assert [sprite for _pos, sprite in got] == [
        "Glasses", "Glasses:pixel_shades"
    ]


def test_missing_sprites_fall_back_to_the_default_pair(renderer):
    got = _entries(
        renderer,
        _cfg(SELF_EYE, OTHER_EYE),
        _view(),
        (0.0, 2000.0, 0.0),
    )
    assert [sprite for _pos, sprite in got] == ["Glasses", "Glasses"]
