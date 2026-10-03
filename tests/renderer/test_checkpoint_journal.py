"""An undo checkpoint is taken before the change it guards.

``save_state()`` journals the selection so the render projection re-resolves
it -- but it runs *before* the tool changes anything. A frame prepared in that
gap consumed the journal entry while the objects still held their old state,
and the change that followed was never seen: a retexture that stayed on screen
as the old texture until something else touched the brush.

The editor now journals the checkpoint's objects a second time, once the UI
event making the change has finished (``EditorState.post_event``).
"""

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush                # noqa: E402

pytestmark = pytest.mark.qt


def _top_texture(thread):
    table = thread.game_state.get_write_state().render_table
    return table.texture_names()[table.tex_name_id[0, 5]]


def test_a_change_made_after_its_checkpoint_reaches_the_table():
    state = EditorState()
    deferred = []
    state.post_event = deferred.append        # the UI event loop, by hand
    wall = box_brush("wall")
    state.brushes = [wall]
    state.set_selected_object(wall)
    thread = LogicThread(ThreadedGameState(), state)
    thread._prepare_render_state()

    state.save_state()                        # the tool's checkpoint...
    thread._prepare_render_state()            # ...a frame lands in the gap...
    wall["textures"]["top"] = "brick.png"     # ...and then the tool edits.
    for callback in deferred:                 # the event returns
        callback()
    thread._prepare_render_state()

    assert _top_texture(thread) == "brick.png", (
        "the edit made after the checkpoint never reached the render table")

