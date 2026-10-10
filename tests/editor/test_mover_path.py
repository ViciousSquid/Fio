"""A mover with a Path Target follows the PathNode chain, in Play and in the
editor's Preview Movement.

The preview snapped the mover onto the first node and then fell through to
the back-and-forth preview, which kept that node as the position to restore:
the mover went to pathnode1, stayed, and stopping the preview left it there.
"""

import pytest

pytest.importorskip("PyQt5")

from editor.things import PathNode, PlayerStart                 # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt

START = [0.0, 64.0, 0.0]
P1 = (192.0, 64.0, 0.0)
P2 = (192.0, 64.0, 192.0)


def _mover():
    return box_brush("lift", tuple(START), (32, 32, 32), is_mover=True,
                     path_target="pathnode1", start_on=True, speed=256.0)


def _nodes():
    return [make_thing(PathNode, "pathnode1", P1, next_node="pathnode2"),
            make_thing(PathNode, "pathnode2", P2)]


def _near(pos, point, tol=1.0):
    return all(abs(float(a) - b) <= tol for a, b in zip(pos, point))


def test_preview_movement_follows_the_chain_and_puts_the_mover_back(main_window):
    mover = _mover()
    main_window.state.brushes.append(mover)
    main_window.state.things.extend(_nodes())

    main_window.start_mover_preview(mover)
    data = main_window.preview_data
    assert data.get("is_path") and main_window.preview_timer.isActive()
    assert mover["pos"] == START                         # starts where it is

    path = []
    for _ in range(600):
        if not main_window.preview_timer.isActive():
            break
        main_window._advance_mover_preview()
        path.append(list(mover["pos"]))
    assert not main_window.preview_timer.isActive(), "the preview never finished"
    assert any(_near(p, P1) for p in path), "never reached pathnode1"
    # On to pathnode2: the last leg runs from pathnode1 towards it.
    last_leg = [p for p in path if abs(p[0] - P1[0]) <= 1.0 and p[2] > 1.0]
    assert last_leg and max(p[2] for p in last_leg) > 0.9 * P2[2], "stopped at pathnode1"
    assert mover["pos"] == START                         # put back where it was


def test_stopping_the_preview_midway_puts_the_mover_back(main_window):
    mover = _mover()
    main_window.state.brushes.append(mover)
    main_window.state.things.extend(_nodes())
    main_window.start_mover_preview(mover)
    for _ in range(10):
        main_window._advance_mover_preview()
    assert mover["pos"] != START
    main_window.stop_mover_preview()
    assert mover["pos"] == START


def test_in_play_the_mover_goes_to_pathnode1_then_pathnode2(fio_session):
    start = make_thing(PlayerStart, "spawn", (-300.0, 8.0, -300.0))
    session = fio_session(level_data(brushes=room(size=1024.0) + [_mover()],
                                     things=_nodes() + [start]))
    session.start_play()
    mover = next(b for b in session.window.state.brushes if b.get("name") == "lift")
    seen_p1 = seen_p2 = False
    for _ in range(300):
        session.step(1)
        seen_p1 = seen_p1 or _near(mover["pos"], P1)
        seen_p2 = seen_p2 or _near(mover["pos"], P2)
        if seen_p2:
            break
    assert seen_p1, "never reached pathnode1"
    assert seen_p2, "stopped at pathnode1"
