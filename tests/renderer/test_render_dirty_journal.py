"""Render-dirty journal frame-boundary tests.

The dense renderer consumes a snapshot of the editor's invalidation journal.
Edits made after that snapshot must remain pending for the next frame.
"""

import pytest
import threading
from collections import deque


def _state():
    """Use the real EditorState constructor and its production journal storage."""
    pytest.importorskip("PyQt5")
    from editor.editor_state import EditorState
    return EditorState()


def test_snapshot_preserves_later_object_edit():
    """An edit after capture remains pending for the next frame."""
    state = _state()
    first = object()
    second = object()

    state.mark_world_changed([first])
    snapshot = state.render_dirty_snapshot()
    assert snapshot == (1, {id(first)})

    state.mark_world_changed([second])
    state.clear_render_dirty(snapshot)

    assert state.render_dirty_snapshot() == (2, {id(second)})


def test_snapshot_preserves_same_object_edited_again():
    """Re-dirtying the same row after capture is not consumed early."""
    state = _state()
    brush = object()

    state.mark_world_changed([brush])
    snapshot = state.render_dirty_snapshot()
    state.mark_world_changed([brush])
    state.clear_render_dirty(snapshot)

    assert state.render_dirty_snapshot() == (2, {id(brush)})


def test_snapshot_preserves_global_invalidation_after_capture():
    """A later global invalidation survives the earlier frame boundary."""
    state = _state()
    first = object()

    state.mark_world_changed([first])
    snapshot = state.render_dirty_snapshot()
    state.mark_world_changed()  # global invalidation after the snapshot

    state.clear_render_dirty(snapshot)

    epoch, dirty = state.render_dirty_snapshot()
    assert epoch == 2
    assert dirty is None


def test_dirty_history_replays_for_a_second_render_buffer():
    """A consumed semantic edit remains precise for the other render buffer."""
    from engine.render_table import RenderTable, CLASS_FOG

    state = _state()
    brush = {
        'id': 'brush-a',
        'pos': [0, 0, 0],
        'size': [64, 64, 64],
        'shader': '<None>',
    }

    # Establish the identical starting state in both persistent tables.
    state.mark_world_changed([brush])
    initial = state.render_dirty_snapshot()
    epoch, dirty = state.render_dirty_since(0, through_epoch=initial[0])

    first = RenderTable()
    second = RenderTable()
    first.begin_frame([brush], epoch, dirty_objects=dirty)
    second.begin_frame([brush], epoch, dirty_objects=dirty)
    state.clear_render_dirty(initial)

    # Edit the brush. The first buffer consumes the live journal.
    brush['shader'] = 'Fog'
    brush['is_fog'] = True
    state.mark_world_changed([brush])
    edit = state.render_dirty_snapshot()

    epoch, dirty = state.render_dirty_since(first._epoch, through_epoch=edit[0])
    first.begin_frame([brush], epoch, dirty_objects=dirty)
    assert first.class_bits[0] & CLASS_FOG

    # The live journal is now consumed, but history must let the second buffer
    # replay exactly the same row without falling back to a global rebuild.
    state.clear_render_dirty(edit)
    epoch, dirty = state.render_dirty_since(second._epoch, through_epoch=edit[0])
    second.begin_frame([brush], epoch, dirty_objects=dirty)

    assert second.class_bits[0] & CLASS_FOG
    assert dirty == {id(brush)}


def test_dirty_history_replays_global_invalidation():
    state = _state()
    brush = object()

    state.mark_world_changed([brush])
    initial = state.render_dirty_snapshot()
    state.clear_render_dirty(initial)

    state.mark_world_changed()
    snapshot = state.render_dirty_snapshot()
    epoch, dirty = state.render_dirty_since(1, through_epoch=snapshot[0])

    assert epoch == 2
    assert dirty is None
