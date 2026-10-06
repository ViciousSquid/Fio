"""EditorState -> LogicThread -> publication -> RenderTable -> renderer -> pixels.

A map authors four pillars around the spawn and an I/O connection that hides
them half a second into Play. The frame is drawn by ``QtGameView.paintGL`` on
a real OpenGL context before and after; the published RenderTable must flag
the pillars hidden and drop them from the visible slots, and the drawn image
must lose them.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from editor.io_system import OutputConnection     # noqa: E402
from editor.things import Light, PlayerStart      # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]

PILLARS = [box_brush("pillar_%d" % i, pos, (96.0, 256.0, 96.0))
           for i, pos in enumerate([(300.0, 128.0, 0.0), (-300.0, 128.0, 0.0),
                                    (0.0, 128.0, 300.0), (0.0, 128.0, -300.0)])]


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 400.0, 0.0), radius=2000.0,
                      intensity=2.0, casts_shadows=False)
    data = level_data(brushes=room(size=2048.0, height=512.0) + PILLARS,
                      things=[spawn, lamp])
    data["things"][0]["io_connections"] = [
        OutputConnection(output_name="OnPlayerSpawn", target_name=p["name"],
                         input_name="Hide", delay=0.5, target_id=p["id"]).to_dict()
        for p in PILLARS]
    return data


def _pillar_rows(session):
    with session.render_state() as frame:
        table = frame.render_table
        slots = [table.slot_of_id[p["id"]] for p in PILLARS]
        visible = set(int(s) for s in frame.visible_brush_slots)
        return ([bool(table.hidden[s]) for s in slots],
                [s in visible for s in slots])


def test_hiding_brushes_by_io_reaches_the_drawn_image(fio_session):
    session = fio_session(_level(), size=(320, 180)).start_play()
    session.step(6)
    hidden, visible = _pillar_rows(session)
    assert hidden == [False] * 4
    assert any(visible), "no pillar is in front of the spawn camera"
    shown = session.paint().astype(np.int16)

    session.step(40)                                    # past the 0.5 s delay
    hidden, visible = _pillar_rows(session)
    assert hidden == [True] * 4
    assert not any(visible)
    assert all(b.get("hidden") for b in session.state.brushes if b["name"].startswith("pillar"))
    gone = session.paint().astype(np.int16)

    changed = np.abs(shown - gone).sum(axis=2) > 24
    assert changed.mean() > 0.02, "hiding the pillars changed %.2f%% of pixels" % (
        100.0 * changed.mean())
    assert shown.std() > 4.0 and gone.std() > 4.0, "a frame came out blank"
