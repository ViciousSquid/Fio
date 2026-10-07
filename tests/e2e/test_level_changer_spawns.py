"""LevelChangers into named PlayerStarts, end to end: a hub and a dungeon.

Two real map files in maps/, opened by the editor and played. The player
goes through LevelChangers both ways a map can trigger one -- walking up and
pressing Use (the level-complete prompt the view confirms), and the
ChangeLevel input -- and must arrive at the named start each time, with the
weapons they carry, without ever being placed at another start first.

    Hub:     HubStart (primary), DungeonEntrance
             LevelChanger -> Dungeon, destination DungeonStart
    Dungeon: DungeonStart (primary)
             LevelChanger -> Hub, destination DungeonEntrance
"""

import json
import os
import uuid

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt                          # noqa: E402
from PyQt5.QtGui import QKeyEvent                            # noqa: E402

from editor.things import LevelChanger, PlayerStart         # noqa: E402
from tests.helpers.session import REPO_ROOT                  # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

# angle 270 faces +z (see QtGameView.toggle_play_mode).
HUB_START = (0.0, 40.0, 0.0)
DUNGEON_ENTRANCE = (480.0, 40.0, 320.0)
DUNGEON_START = (-256.0, 40.0, -192.0)


def _start(name, pos, primary, index, angle=270.0):
    return make_thing(PlayerStart, name, pos, angle=angle, primary=primary,
                      creation_index=index)


def _changer(name, pos, target, destination):
    return make_thing(LevelChanger, name, pos, target_map=target,
                      destination_spawn=destination, usable=True, radius=128.0)


@pytest.fixture
def maps():
    """``(hub path, dungeon path)``: two maps written to maps/, removed after."""
    tag = uuid.uuid4().hex[:8]
    hub, dungeon = f"maps/_e2e_hub_{tag}.json", f"maps/_e2e_dungeon_{tag}.json"
    files = {
        hub: level_data(brushes=room(size=2048.0), things=[
            _start("HubStart", HUB_START, True, 0),
            _start("DungeonEntrance", DUNGEON_ENTRANCE, False, 1, angle=90.0),
            # Right in front of HubStart, so Use reaches it.
            _changer("ToDungeon", (0.0, 40.0, 64.0), dungeon, "DungeonStart"),
        ]),
        dungeon: level_data(brushes=room(size=2048.0), things=[
            _start("DungeonStart", DUNGEON_START, True, 0),
            _changer("ToHub", (512.0, 40.0, 512.0), hub, "DungeonEntrance"),
        ]),
    }
    for path, data in files.items():
        with open(os.path.join(REPO_ROOT, path), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    try:
        yield hub, dungeon
    finally:
        for path in files:
            os.remove(os.path.join(REPO_ROOT, path))


def _spawned_at(session):
    """``(start name, (x, z))`` of the session the player is now in."""
    logic = session.logic
    player = logic.player_runtime.player
    start = logic.session_runtime.spawn_start
    return start.properties["name"], (float(player.pos.x), float(player.pos.z))


def _thing(session, name):
    return next(t for t in session.state.things if t.properties.get("name") == name)


def _confirm_level_complete(session):
    """The player presses E on the level-complete prompt the view shows."""
    view = session.view
    session.publish()
    session.game_state.try_swap()
    # What the view's frame read caches from the published frame.
    view._cached_level_complete_ui = session.game_state.published("level_complete_ui")
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_E, Qt.NoModifier))
    session.app.processEvents()


def _spawns_seen(session):
    """Record every start a play session begins at."""
    seen = []
    runtime = session.logic.session_runtime
    original = runtime.apply_play_mode

    def record(enabled, spawn_start=None):
        result = original(enabled, spawn_start)
        if enabled:
            seen.append(runtime.spawn_start.properties["name"])
        return result

    runtime.apply_play_mode = record
    return seen


def test_the_hub_round_trip(fio_session, maps):
    hub, dungeon = maps
    session = fio_session(hub).start_play()
    seen = _spawns_seen(session)
    assert _spawned_at(session) == ("HubStart", (HUB_START[0], HUB_START[2]))

    combat = session.logic.combat_runtime
    combat.give_weapon(combat.items.resolve("gun2"))
    combat.player_ammo = 5

    # Hub -> Dungeon: walk up to the LevelChanger, Use, confirm.
    session.step(1, use=True)
    ui = session.logic.interaction_runtime.level_complete_ui
    assert (ui["target_map"], ui["destination_spawn"]) == (dungeon, "DungeonStart")
    _confirm_level_complete(session)
    session.logic.session_runtime.stop_monster_ai()

    assert session.window.file_path == dungeon
    assert _spawned_at(session) == ("DungeonStart", (DUNGEON_START[0], DUNGEON_START[2]))
    assert (combat.active_weapon, combat.weapons, combat.player_ammo) == ("gun2", {"gun2"}, 5)

    # Dungeon -> Hub, by the ChangeLevel input: to the non-primary entrance.
    assert _thing(session, "ToHub").on_input("ChangeLevel") is True
    session.app.processEvents()
    session.logic.session_runtime.stop_monster_ai()

    assert session.window.file_path == hub
    assert _spawned_at(session) == ("DungeonEntrance", (DUNGEON_ENTRANCE[0], DUNGEON_ENTRANCE[2]))
    assert (combat.active_weapon, combat.weapons, combat.player_ammo) == ("gun2", {"gun2"}, 5)
    # Each session began at its destination: never at a primary first.
    assert seen == ["DungeonStart", "DungeonEntrance"]


def test_an_empty_destination_spawn_uses_the_destination_primary(fio_session, maps):
    hub, dungeon = maps
    session = fio_session(dungeon).start_play()
    changer = _thing(session, "ToHub")
    changer.properties["destination_spawn"] = ""
    assert changer.on_input("ChangeLevel") is True
    session.app.processEvents()
    session.logic.session_runtime.stop_monster_ai()
    assert _spawned_at(session) == ("HubStart", (HUB_START[0], HUB_START[2]))


def test_a_missing_destination_spawn_fails_and_leaves_the_level_playing(fio_session, maps):
    hub, dungeon = maps
    session = fio_session(dungeon).start_play()
    changer = _thing(session, "ToHub")
    changer.properties["destination_spawn"] = "Nowhere"
    things_before = list(session.state.things)
    toasts = []
    session.window.show_toast = lambda text, is_error=False: toasts.append((text, is_error))

    changer.on_input("ChangeLevel")
    session.app.processEvents()

    assert toasts == [(f"LevelChanger: destination spawn 'Nowhere' was not found in '{hub}'",
                       True)]
    # Still the dungeon, still playing, at the same start, nothing replaced.
    assert session.window.file_path == os.path.join(REPO_ROOT, dungeon)
    assert session.playing
    assert session.state.things == things_before
    assert _spawned_at(session)[0] == "DungeonStart"
    session.step(2)                          # and the session still runs


def test_the_load_api_takes_the_destination_spawn(fio_session, maps):
    hub, _dungeon = maps
    session = fio_session(hub).start_play()
    assert session.window.load_level_file(os.path.join(REPO_ROOT, hub), "DungeonEntrance")
    session.logic.session_runtime.stop_monster_ai()
    assert _spawned_at(session)[0] == "DungeonEntrance"
    assert session.window.load_level_file(os.path.join(REPO_ROOT, hub))
    session.logic.session_runtime.stop_monster_ai()
    assert _spawned_at(session)[0] == "HubStart"
