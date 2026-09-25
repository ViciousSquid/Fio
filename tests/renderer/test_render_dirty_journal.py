"""Render-dirty journal frame-boundary tests.

The dense renderer consumes a snapshot of the editor's invalidation journal.
Edits made after that snapshot must remain pending for the next frame.
"""

from editor.editor_state import EditorState


def _state():
    state = EditorState.__new__(EditorState)
    state.world_epoch = 0
    state._render_dirty_objects = set()
    state._render_dirty_epoch_by_id = {}
    state._render_dirty_all = False
    state._render_dirty_all_epoch = -1
    return state


def test_snapshot_preserves_later_object_edit():
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
    state = _state()
    brush = object()

    state.mark_world_changed([brush])
    snapshot = state.render_dirty_snapshot()
    state.mark_world_changed([brush])
    state.clear_render_dirty(snapshot)

    assert state.render_dirty_snapshot() == (2, {id(brush)})


def test_snapshot_preserves_global_invalidation_after_capture():
    state = _state()
    first = object()

    state.mark_world_changed([first])
    snapshot = state.render_dirty_snapshot()
    state.mark_world_changed()  # global invalidation after the snapshot

    state.clear_render_dirty(snapshot)

    epoch, dirty = state.render_dirty_snapshot()
    assert epoch == 2
    assert dirty is None
