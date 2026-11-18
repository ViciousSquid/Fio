"""A FIRE that explodes, the editor's explosion preview, and the fires a
spawner scatters round an explosion -- in a real session.

* The Effect panel's Play explosion previews in the editor: the explosion
  plays in the 3D view (alive, lit) and the Effect is back to what it was
  authored as once it has played; nothing authored changes.
* In Play, a FIRE sent Explode explodes and burns again afterwards; its
  OnExplode can drive a LogicSpawner that scatters its batch round it.
* What the spawner made is gone again when Play stops.
"""

import math
import time

import pytest

pytest.importorskip("PyQt5")

from editor.things import LogicSpawner, PlayerStart                 # noqa: E402
from engine.effect_entity import Effect                              # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room        # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


@pytest.fixture
def clock(monkeypatch):
    import engine.entity_table as et
    now = [et._CLOCK_ORIGIN + 1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    return now


def _session(fio_session, clock, things):
    level = level_data(brushes=room(size=1600.0), things=[
        make_thing(PlayerStart, "spawn", (500.0, 40.0, 500.0))] + list(things))
    session = fio_session(level)
    session.clock = clock
    return session


def _step(session, ticks=1):
    for _ in range(ticks):
        session.clock[0] += TICK
        session.step(1)


def _row(session, thing):
    with session.render_state() as frame:
        table = frame.entity_table
        slot = table.slot_of_id[thing.properties["id"]]
        return (int(table.effect_type[slot]), bool(table.effect_alive[slot]),
                bool(table.light_enabled[slot]))


def _torch():
    return make_thing(Effect, "torch", (0.0, 0.0, 0.0), effect_type="FIRE",
                      lifetime=0.5)


def test_play_explosion_previews_in_the_editor_and_puts_the_fire_back(fio_session, clock):
    session = _session(fio_session, clock, [_torch()])
    torch = next(t for t in session.state.things if t.properties.get("name") == "torch")
    panel = session.window.property_editor
    _step(session)
    assert _row(session, torch)[:2] == (0, True)                 # burning

    panel._play_effect_explosion(torch)
    _step(session)
    family, alive, lit = _row(session, torch)
    assert (family, alive, lit) == (1, True, True)               # playing, lit
    assert torch.properties["effect_type"] == "FIRE"             # nothing authored changed

    _step(session, int(0.5 / TICK) + 2)
    assert _row(session, torch)[:2] == (0, True)                 # a fire again


def test_an_explosion_previewed_in_the_editor_goes_dormant_again(fio_session, clock):
    boom = make_thing(Effect, "boom", (0.0, 0.0, 0.0), effect_type="EXPLOSION",
                      lifetime=0.4)
    session = _session(fio_session, clock, [boom])
    boom = next(t for t in session.state.things if t.properties.get("name") == "boom")
    _step(session)
    assert _row(session, boom)[1] is False                       # dormant, no Preview
    session.window.property_editor._play_effect_explosion(boom)
    _step(session)
    assert _row(session, boom)[1] is True
    _step(session, int(0.4 / TICK) + 2)
    assert _row(session, boom)[1] is False


def test_a_fire_explodes_and_its_spawner_scatters_fires_round_it(fio_session, clock):
    torch = _torch()
    session = _session(fio_session, clock, [torch])
    torch = next(t for t in session.state.things if t.properties.get("name") == "torch")
    spawner = session.window.property_editor._add_fire_spawner(torch)
    count, radius = spawner.properties["spawn_count"], spawner.properties["spawn_radius"]
    things_before = len(session.state.things)

    session.start_play()
    _step(session, 2)
    session.logic.io_manager._execute_input("torch", "Explode", "", "test")
    _step(session)
    assert _row(session, torch)[0] == 1                          # exploding
    _step(session, int(0.5 / TICK) + 4)                          # past the spawn delay
    fires = [t for t in session.logic.session_runtime.session_things
             if isinstance(t, Effect)]
    assert len(fires) == count
    for fire in fires:
        assert fire.properties["effect_type"] == "FIRE"
        assert math.dist((fire.pos[0], fire.pos[2]), (torch.pos[0], torch.pos[2])) <= radius + 1e-6
    assert _row(session, torch)[:2] == (0, True)                 # the torch burns again

    session.stop_play()
    assert len(session.state.things) == things_before            # the fires are gone
    assert torch.properties["effect_type"] == "FIRE"


def test_the_spawner_scatters_a_batch_and_counts_afresh_each_play(fio_session, clock):
    spawner = make_thing(LogicSpawner, "spawner", (0.0, 0.0, 0.0), spawn_type="Effect",
                         spawn_count=5, spawn_radius=120.0, max_spawn=5,
                         spawn_properties={"effect_type": "FIRE"})
    session = _session(fio_session, clock, [spawner])
    for _ in range(2):
        session.start_play()
        _step(session, 2)
        io = session.logic.io_manager
        io._execute_input("spawner", "Spawn", "", "test")
        io._execute_input("spawner", "Spawn", "", "test")        # max_spawn reached
        made = session.logic.session_runtime.session_things
        assert len(made) == 5
        spots = {(round(t.pos[0], 3), round(t.pos[2], 3)) for t in made}
        assert len(spots) == 5                                   # scattered, not piled
        assert all(math.hypot(x, z) <= 120.0 for x, z in spots)
        session.stop_play()
        assert not any(t in session.state.things for t in made)
