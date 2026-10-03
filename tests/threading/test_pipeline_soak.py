"""The whole threaded pipeline at once, against a world that keeps changing.

The logic thread publishes frames and the monster AI moves monsters. Another
thread spawns, removes, moves and hides entities, the way the editor, I/O and
plugins do. A reader borrows each new frame the way the renderer does and
checks it for internal consistency.

Frame preparation takes no lock against the AI or the editor: it freezes
the entity list with one atomic copy and builds everything from that. This
test is the evidence that doing so holds up. Every published frame must be
self-consistent, and no thread may raise.
"""

import random
import threading
import time
import traceback

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import Light, Monster, PlayerStart     # noqa: E402
from engine.change_journal import touch                   # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import make_thing, pillar_grid  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration, pytest.mark.slow]

SECONDS = 2.0


def _check_frame(frame):
    entities, brushes = frame.entity_table, frame.render_table
    n = entities.count
    assert len(entities.things) == len(entities.refs) == len(entities.ids) == n
    for slots in (frame.visible_thing_slots, entities.light_slots,
                  entities.monster_slots):
        assert len(slots) == 0 or (0 <= slots.min() and slots.max() < n)
    for slot, thing in enumerate(entities.things):
        assert entities.refs[slot] is thing
        assert entities.slot_of_id[thing.properties["id"]] == slot
    assert len(frame.thing_hidden) == n
    visible = frame.visible_brush_slots
    assert len(visible) == 0 or visible.max() < brushes.count
    assert not brushes.hidden[visible].any()
    assert np.isfinite(entities.pos[:n]).all()


def test_frames_stay_consistent_while_everything_changes():
    state = EditorState()
    state.brushes = pillar_grid(6, 6, spacing=300.0)
    state.things = ([make_thing(PlayerStart, "spawn", (0, 64, 0))]
                    + [make_thing(Monster, "m%d" % i, (i * 90.0, 96, 400))
                       for i in range(12)])
    game_state = ThreadedGameState()
    logic = LogicThread(game_state, state)
    logic.player = Player(0.0, 0.0)
    logic.set_play_mode(True)

    errors = []
    stop = threading.Event()
    checked = [0]
    rng = random.Random(3)

    def guarded(body):
        def run():
            try:
                body()
            except Exception:
                errors.append(traceback.format_exc())
                stop.set()
        return run

    def churn():
        spawned = []
        while not stop.is_set():
            roll = rng.random()
            if roll < 0.3:
                thing = (Light if rng.random() < 0.5 else Monster)(
                    pos=[rng.uniform(-900, 900), 64.0, rng.uniform(-900, 900)])
                state.things.append(thing)
                spawned.append(thing)
            elif roll < 0.5 and spawned:
                victim = spawned.pop(rng.randrange(len(spawned)))
                state.things.remove(victim)
            elif roll < 0.8:
                thing = state.things[rng.randrange(len(state.things))]
                thing.pos = [thing.pos[0] + 1.0, thing.pos[1], thing.pos[2]]
            else:
                thing = state.things[rng.randrange(len(state.things))]
                thing.properties["hidden"] = not thing.properties.get("hidden")
                touch(thing)
            time.sleep(0.0005)

    def reader():
        while not stop.is_set():
            if not game_state.try_swap():
                time.sleep(0.001)
                continue
            frame = game_state.get_render_state()
            try:
                _check_frame(frame)
                checked[0] += 1
            finally:
                game_state.release_render_state(frame)

    workers = [threading.Thread(target=guarded(churn), daemon=True),
               threading.Thread(target=guarded(reader), daemon=True)]
    logic.start()
    try:
        for worker in workers:
            worker.start()
        stop.wait(SECONDS)
    finally:
        stop.set()
        for worker in workers:
            worker.join(5.0)
        logic.set_play_mode(False)
        logic.stop()
        logic.join(5.0)

    assert not errors, errors[0]
    assert checked[0] > 20, "only %d frames were published in %.0f s" % (
        checked[0], SECONDS)
