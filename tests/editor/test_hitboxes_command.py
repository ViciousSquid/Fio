"""``hitboxes 1`` outlines every shootable monster's actual hit sphere.

The spheres drawn are the ones player shots and melee test
(``LogicCombat._hitscan_monster_snapshot``), published by the logic thread,
outlined in bright pink over the scene; ``hitboxes 0`` removes them.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from engine.view_debug_draw import ViewDebugDraw      # noqa: E402

pytestmark = pytest.mark.qt


def test_the_outline_is_three_great_circles_on_the_sphere():
    centre, radius = (10.0, 96.0, -40.0), 96.0
    points = ViewDebugDraw.hitbox_lines([centre], [radius])
    assert points.shape == (3 * ViewDebugDraw.HITBOX_SEGMENTS * 2, 3)
    np.testing.assert_allclose(np.linalg.norm(points - centre, axis=1), radius, rtol=1e-5)
    for axis in range(3):                     # each axis spans the whole diameter
        assert points[:, axis].min() == pytest.approx(centre[axis] - radius, abs=1e-3)
        assert points[:, axis].max() == pytest.approx(centre[axis] + radius, abs=1e-3)
    assert ViewDebugDraw.hitbox_lines(np.empty((0, 3)), []).shape == (0, 3)


def _level():
    from editor.things import Light, Monster, PlayerStart
    from tests.helpers.worlds import box_brush, level_data, make_thing
    return level_data(
        brushes=[box_brush("floor", (0.0, -16.0, 0.0), (2048.0, 32.0, 2048.0))],
        things=[
            # Angle 90: the camera loads at the spawn facing -Z, at the monster.
            make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=90.0),
            make_thing(Light, "lamp", (0.0, 300.0, 0.0)),
            make_thing(Monster, "grunt", (0.0, 96.0, -200.0), monster_type="human",
                       sprite_width=128, sprite_height=192, awake=False),
        ])


def _pink_pixels(image):
    pink = np.array(ViewDebugDraw.HITBOX_COLOR) * 255
    return int((np.abs(image.astype(int) - pink).max(axis=2) <= 40).sum())


@pytest.mark.gl
def test_hitboxes_publish_the_shot_spheres_and_draw_them_pink(fio_session):
    session = fio_session(_level(), size=(320, 180))
    session.view.sysmon.set_active(False)
    session.start_play()
    console = session.window.console_handler
    combat = session.logic.combat_runtime

    session.step(2)
    with session.render_state() as frame:
        assert frame.monster_hitboxes is None
    off = session.paint()

    console.handle_command("hitboxes 1")
    assert combat.show_hitboxes
    session.step(2)
    _monsters, centres, radii = combat._hitscan_monster_snapshot()
    with session.render_state() as frame:
        drawn_centres, drawn_radii = frame.monster_hitboxes
    np.testing.assert_allclose(drawn_centres, centres)
    np.testing.assert_allclose(drawn_radii, radii)
    # The sphere a bullet must touch: feet + 0.45 H, radius 0.75 W.
    np.testing.assert_allclose(drawn_centres[0][1], 0.45 * 192, atol=0.5)
    assert drawn_radii[0] == pytest.approx(96.0)
    on = session.paint()
    assert _pink_pixels(on) > 50 > _pink_pixels(off)

    console.handle_command("hitboxes 0")
    session.step(2)
    with session.render_state() as frame:
        assert frame.monster_hitboxes is None
    assert _pink_pixels(session.paint()) < 50

    console.handle_command("hitboxes 1")
    session.stop_play()                       # off again for the next session
    assert not combat.show_hitboxes
