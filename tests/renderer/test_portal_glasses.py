"""Which player glasses a portal's virtual scene draws, and where."""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="renderer tests import editor Things")
pytest.importorskip("OpenGL")

pytestmark = pytest.mark.qt

from engine.portal_transform import basis_from_rotation, map_point, mirror_point
from engine.renderer_F import Renderer_F

A_POS = (1072.0, 224.0, -800.0)
B_POS = (480.0, 224.0, -448.0)
A_BASIS = basis_from_rotation([180.0, 0.0, 0.0])
B_BASIS = basis_from_rotation([270.0, 0.0, 0.0])
SELF_EYE = (1000.0, 154.0, -1100.0)
OTHER_EYE = (300.0, 154.0, -500.0)


def _cfg(*positions, mirror=True):
    basis = np.zeros((2, 3, 3))
    basis[0] = A_BASIS
    basis[1] = B_BASIS
    table = SimpleNamespace(pos=np.asarray([A_POS, B_POS]), portal_basis=basis)
    return {'player_glasses_positions': tuple(positions), 'entity_table': table,
            'portal_mirror': mirror}


def _view(depth=1):
    return SimpleNamespace(aperture_slot=0, clip_slot=1, recursion_depth=depth)


def _positions(cfg, view, camera):
    renderer = Renderer_F.__new__(Renderer_F)
    return renderer._portal_glasses_positions(cfg, view, camera)


def test_own_glasses_are_mirrored_and_others_stay_where_they_are():
    got = _positions(_cfg(SELF_EYE, OTHER_EYE), _view(), SELF_EYE)
    expected_self = mirror_point(A_POS, A_BASIS, B_POS, B_BASIS, SELF_EYE)
    assert len(got) == 2
    assert np.allclose(got[0], OTHER_EYE)
    assert np.allclose(got[1], expected_self)
    # Never at the virtual eye, where it could not be seen.
    cam = map_point(A_POS, A_BASIS, B_POS, B_BASIS, SELF_EYE)
    assert np.linalg.norm(np.subtract(got[1], cam)) > 100.0


def test_nested_portal_views_drop_the_self_reflection():
    got = _positions(_cfg(SELF_EYE, OTHER_EYE), _view(depth=2), SELF_EYE)
    assert len(got) == 1
    assert np.allclose(got[0], OTHER_EYE)


def test_camera_away_from_every_player_draws_them_all_unmirrored():
    far_camera = (0.0, 2000.0, 0.0)
    got = _positions(_cfg(SELF_EYE, OTHER_EYE), _view(), far_camera)
    assert np.allclose(got, (SELF_EYE, OTHER_EYE))


def test_no_players_draw_nothing():
    assert _positions(_cfg(), _view(), SELF_EYE) == ()


def test_portal_mirror_off_keeps_everyone_at_their_real_position():
    got = _positions(_cfg(SELF_EYE, OTHER_EYE, mirror=False), _view(), SELF_EYE)
    assert np.allclose(got, (SELF_EYE, OTHER_EYE))
