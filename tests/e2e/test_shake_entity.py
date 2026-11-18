"""The Shake entity in a real session: a trigger brush's OnTrigger shakes the
screen; the shake is the camera view's alone (the player and physics are
untouched) and dies away; Stop ends it; Play start/stop clear it; a Shake
saves and loads as one."""

import time

import glm
import pytest

pytest.importorskip("PyQt5")

from editor.things import PlayerStart, Shake, Thing                 # noqa: E402
from engine.prop_entity import Prop                                  # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


@pytest.fixture
def clock(monkeypatch):
    import engine.entity_table as et
    now = [et._CLOCK_ORIGIN + 1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    return now


def _step(session, ticks=1):
    for _ in range(ticks):
        session.clock[0] += TICK
        session.step(1)


def _view(session):
    with session.render_state() as frame:
        m = frame.camera_view_matrix
        return [[float(m[c][r]) for r in range(4)] for c in range(4)]


def _differs(a, b):
    return max(abs(a[c][r] - b[c][r]) for c in range(4) for r in range(4))


def _session(fio_session, clock):
    trigger = box_brush("quake_zone", (300.0, 64.0, 0.0), (128.0, 128.0, 128.0),
                        is_trigger=True, trigger_type="Multiple",
                        trigger_activation="touch", trigger_action="target",
                        trigger_poll_interval=0.25)
    trigger["io_connections"] = [{"output": "OnTrigger", "target": "quake",
                                  "input": "Shake", "parameter": "", "delay": 0.0,
                                  "fire_once": False}]
    level = level_data(brushes=room(size=1600.0) + [trigger], things=[
        make_thing(PlayerStart, "spawn", (-300.0, 40.0, 0.0)),
        make_thing(Shake, "quake", (300.0, 64.0, 0.0), intensity=0.8, duration=1.0),
        make_thing(Prop, "crate", (-200.0, 0.0, 200.0), model_path="Oil_Drum.obj",
                   physics_enabled=True)])
    session = fio_session(level)
    session.clock = clock
    return session


def test_a_trigger_brush_shakes_the_view_and_nothing_else(fio_session, clock):
    session = _session(fio_session, clock).start_play()
    _step(session, 30)
    logic = session.logic
    player = logic.player_runtime.player
    crate = next(t for t in session.state.things if t.properties.get("name") == "crate")
    crate_at = list(crate.pos)

    player.pos = glm.vec3(300.0, player.pos.y, 0.0)          # into the zone
    player.velocity = glm.vec3(0.0)
    for _ in range(30):                                      # within a poll
        _step(session)
        if logic.shake_runtime.shakes:
            break
    assert logic.shake_runtime.shakes                        # it fired
    stood = (float(player.pos.x), float(player.pos.z))
    shaking = [_view(session)]
    for _ in range(5):
        _step(session)
        shaking.append(_view(session))
    # Out of the zone so it does not shake again, then let it die away.
    player.pos = glm.vec3(-300.0, player.pos.y, 0.0)
    _step(session, int(1.2 / TICK))
    still = _view(session)
    _step(session)
    assert _differs(_view(session), still) < 1e-6            # steady again
    assert not logic.shake_runtime.shakes
    # The shaking frames moved the view; the player stood where it was put.
    assert max(_differs(a, b) for a, b in zip(shaking, shaking[1:])) > 1e-3
    assert stood == pytest.approx((300.0, 0.0), abs=1.0)
    assert list(crate.pos) == pytest.approx(crate_at)        # physics untouched


def test_stop_input_and_play_stop_end_the_shake(fio_session, clock):
    session = _session(fio_session, clock).start_play()
    _step(session, 2)
    io = session.logic.io_manager
    io._execute_input("quake", "Shake", "", "test")
    assert session.logic.shake_runtime.shakes
    io._execute_input("quake", "Stop", "", "test")
    assert not session.logic.shake_runtime.shakes
    io._execute_input("quake", "Shake", "0.3", "test")       # an intensity of its own
    assert session.logic.shake_runtime.shakes[0][2] == pytest.approx(0.3)
    session.stop_play()
    assert not session.logic.shake_runtime.shakes


def test_a_shake_saves_and_loads_as_one():
    shake = make_thing(Shake, "quake", (1.0, 2.0, 3.0), intensity=0.4, duration=2.5)
    data = shake.to_dict()
    assert data["type"] == "shake"
    loaded = Thing.from_dict(data)
    assert isinstance(loaded, Shake)
    assert loaded.properties["intensity"] == 0.4 and loaded.properties["duration"] == 2.5
