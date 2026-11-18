"""Water on the lens in a real session: standing in water (the eye above it)
the bottom half of the lens has drops; the eye coming up out of the water --
from deep water, or standing up after crouching under -- wets all of it, gone
within six seconds; under water there is none; leaving Play clears it."""

import time

import glm
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                          # noqa: E402

from editor.things import PlayerStart                                # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

TICK = 1.0 / 60.0
KEY_C = 0x43


@pytest.fixture
def clock(monkeypatch):
    import engine.entity_table as et
    now = [et._CLOCK_ORIGIN + 1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    return now


def _session(fio_session, clock, depth):
    pool = box_brush("pool", (0.0, depth * 0.5, 0.0), (400.0, depth, 400.0),
                     shader="Water", is_water=True)
    level = level_data(brushes=room(size=1600.0) + [pool], things=[
        make_thing(PlayerStart, "spawn", (500.0, 40.0, 500.0))])
    session = fio_session(level, size=(240, 135))
    session.clock = clock
    session.start_play()
    return session


def _frame(session, ticks=1, keys=()):
    """Step *ticks*, painting as a game does (every few ticks)."""
    for k in range(ticks):
        session.clock[0] += TICK
        session.step(1, keys=keys)
        if k % 6 == 5:
            session.paint()
    session.paint()
    return session.view._lens_water


def test_surfacing_wets_the_lens_for_at_most_six_seconds(fio_session, clock):
    session = _session(fio_session, clock, depth=300.0)
    player = session.logic.player_runtime.player
    lens = _frame(session, 2)
    assert not lens.active                                     # dry on land
    player.pos = glm.vec3(0.0, 100.0, 0.0)                     # deep under
    player.velocity = glm.vec3(0.0)
    lens = _frame(session, 2)
    assert session.logic.player_runtime.player.eye_underwater
    assert not lens.active                                     # none under water
    player.pos = glm.vec3(500.0, 40.0, 500.0)                  # out, on land
    lens = _frame(session, 2)
    assert lens.active and lens.drops
    lens = _frame(session, int(6.2 / TICK))                    # six seconds on
    assert not lens.active and lens.frame() == []


def test_crouching_under_then_standing_wets_the_lens(fio_session, clock):
    # Water just below the standing eye: standing, the eye is out; crouched,
    # it is under.
    session = _session(fio_session, clock, depth=80.0)
    player = session.logic.player_runtime.player
    player.pos = glm.vec3(0.0, player.pos.y, 0.0)
    lens = _frame(session, 60)
    assert not player.eye_underwater and player.in_water
    # Standing in the water: drops on the bottom half only.
    assert lens.active and all(y >= 0.45 for _x, y, _r, _w, _s in lens.frame())
    lens = _frame(session, 40, keys={KEY_C})                   # crouch
    assert player.eye_underwater and not lens.active           # under: none
    lens = _frame(session, 40)                                 # stand up
    assert not player.eye_underwater
    # Came up out of the water: the whole lens, not just its bottom half.
    assert lens.active and any(y < 0.4 for _x, y, _r, _w, _s in lens.frame())


def test_leaving_play_clears_the_lens(fio_session, clock):
    session = _session(fio_session, clock, depth=300.0)
    player = session.logic.player_runtime.player
    player.pos = glm.vec3(0.0, 100.0, 0.0)
    _frame(session, 2)
    player.pos = glm.vec3(500.0, 40.0, 500.0)
    lens = _frame(session, 2)
    assert lens.active
    session.stop_play()
    session.paint()
    assert not session.view._lens_water.active


def test_shallow_water_leaves_the_lens_dry(fio_session, clock):
    session = _session(fio_session, clock, depth=20.0)          # ankle deep
    player = session.logic.player_runtime.player
    player.pos = glm.vec3(0.0, player.pos.y, 0.0)
    lens = _frame(session, 60)
    assert 0.0 < player.water_depth_frac < 0.5
    assert not lens.active
