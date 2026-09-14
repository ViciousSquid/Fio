"""What still points at a live object after an undo or a redo.

Undo and redo in Fio do not modify objects — they rebuild them.  Every brush
dict and every ``Thing`` in the scene is replaced by a fresh one deserialised
from the checkpoint, so anything holding a reference from before is left holding
an object that is no longer in the scene.  Reads from it still work, writes to it
go nowhere, and it stays alive for as long as whatever is holding it does.

That is what these tests are about.  The headline case is the selection itself:
``selected_object`` was re-pointed by index, but ``selected_objects`` — the list
every editor operation actually acts on (component picking, the clip tool, the
Surface Inspector's whole-brush scope, group transforms, delete) — was not.  The
sequences below are the ones a mapper actually performs:

    select -> component edit -> undo -> redo -> change selection -> edit again
    clone -> split -> component edit -> undo -> redo
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from editor import component_edit as ce  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


def make_box(name, pos=(0, 0, 0), size=(64, 64, 64), **extra):
    brush = {'name': name, 'pos': list(pos), 'size': list(size)}
    brush.update(extra)
    return brush


@pytest.fixture
def state():
    editor_state = EditorState()
    editor_state.brushes.append(make_box('wall'))
    editor_state.brushes.append(make_box('floor', pos=(256, 0, 0)))
    editor_state.save_state()
    return editor_state


def in_scene(state, obj):
    return any(obj is brush for brush in state.brushes)


# ---------------------------------------------------------------------------
# The selection itself
# ---------------------------------------------------------------------------

def test_undo_re_points_the_whole_selection_not_just_the_primary(state):
    state.selected_objects = list(state.brushes)
    state.selected_object = state.brushes[0]
    state.save_state()

    state.brushes[0]['size'] = [128, 64, 64]
    state.save_state()
    assert state.undo()

    assert len(state.selected_objects) == 2
    for obj in state.selected_objects:
        assert in_scene(state, obj)
    assert state.selected_object is state.brushes[0]


def test_redo_re_points_the_selection_too(state):
    state.selected_objects = list(state.brushes)
    state.selected_object = state.brushes[0]
    state.save_state()
    state.brushes[0]['size'] = [128, 64, 64]
    state.save_state()

    assert state.undo()
    assert state.redo()

    assert len(state.selected_objects) == 2
    assert all(in_scene(state, obj) for obj in state.selected_objects)


def test_a_selection_follows_its_object_when_the_brush_order_changes(state):
    """Stable ids, not list positions: an insert must not shift the selection."""
    state.selected_objects = [state.brushes[1]]
    state.selected_object = state.brushes[1]
    state.save_state()
    selected_id = state.brushes[1]['id']

    state.brushes.insert(0, make_box('inserted', pos=(-256, 0, 0)))
    state.save_state()
    assert state.undo()

    assert len(state.selected_objects) == 1
    assert state.selected_objects[0]['id'] == selected_id
    assert in_scene(state, state.selected_objects[0])


def test_a_selection_whose_object_is_undone_away_is_dropped(state):
    state.save_state()
    added = make_box('added', pos=(-256, 0, 0))
    state.brushes.append(added)
    state.selected_objects = [added]
    state.selected_object = added
    state.save_state()

    assert state.undo()
    assert state.selected_objects == []
    assert state.selected_object is None


def test_every_brush_gets_a_stable_id_at_its_first_checkpoint(state):
    """A brush drawn in a view has no id until something saves it."""
    fresh = make_box('fresh', pos=(-512, 0, 0))
    assert 'id' not in fresh
    state.brushes.append(fresh)
    state.save_state()
    assert fresh.get('id')


# ---------------------------------------------------------------------------
# select -> component edit -> undo -> redo -> reselect -> edit again
# ---------------------------------------------------------------------------

def test_component_editing_still_reaches_the_scene_after_an_undo(state):
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)

    state.selected_objects = [state.brushes[0]]
    state.selected_object = state.brushes[0]
    state.save_state()

    # First edit.
    ref = ce.components(state.selected_objects[0], ce.MODE_VERTEX)[0]
    controller.press(ref)
    controller.update_drag(np.array([32.0, 0.0, 0.0]))
    controller.commit_drag()
    edited = [round(v, 3) for v in state.brushes[0]['size']]
    state.save_state()

    assert state.undo()
    assert state.redo()
    assert state.undo()
    controller.prune(state.brushes)

    # The selection must still be part of the scene, or the next edit is lost.
    assert state.selected_objects
    target = state.selected_objects[0]
    assert in_scene(state, target)

    ref = ce.components(target, ce.MODE_VERTEX)[0]
    controller.press(ref)
    controller.update_drag(np.array([32.0, 0.0, 0.0]))
    assert controller.commit_drag()

    # The brush that changed is the one in the scene, not a detached copy.
    assert [round(v, 3) for v in state.brushes[0]['size']] == edited


def test_component_references_do_not_survive_an_undo_as_ghosts(state):
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([ce.components(state.brushes[0], ce.MODE_VERTEX)[0]])
    ghost = controller.selection[0].brush

    state.brushes[0]['size'] = [128, 64, 64]
    state.save_state()
    assert state.undo()

    controller.prune(state.brushes)
    assert not in_scene(state, ghost)
    assert all(in_scene(state, ref.brush) for ref in controller.selection)


# ---------------------------------------------------------------------------
# clone -> split -> component edit -> undo -> redo
# ---------------------------------------------------------------------------

def test_clone_split_component_edit_undo_redo(state):
    import copy
    import uuid

    original = state.brushes[0]
    state.selected_objects = [original]
    state.selected_object = original
    state.save_state()

    # Clone.
    clone = copy.deepcopy({k: v for k, v in original.items()
                           if k not in bg.GEO_RUNTIME_KEYS})
    clone['id'] = str(uuid.uuid4())
    clone['name'] = 'wall_copy'
    clone['pos'] = [0, 0, 128]
    state.brushes.append(clone)
    state.selected_objects = [clone]
    state.selected_object = clone
    state.save_state()

    # Split (clip in place) — the clone becomes an angled brush.
    assert state.clip_brush(clone, (1.0, 1.0, 0.0), 0.0)
    state.save_state()
    plane_count = len(clone['geometry']['planes'])

    # Component edit on the clipped half.
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.press(ce.components(clone, ce.MODE_VERTEX)[0])
    controller.update_drag(np.array([0.0, 16.0, 0.0]))
    controller.commit_drag()
    state.save_state()

    # Two undos back to just after the clip, then forward again.
    assert state.undo()
    controller.prune(state.brushes)
    live = state.selected_objects[0]
    assert in_scene(state, live)
    assert len(live['geometry']['planes']) == plane_count

    assert state.redo()
    controller.prune(state.brushes)
    assert state.selected_objects
    assert in_scene(state, state.selected_objects[0])
    assert bg.get_convex(state.selected_objects[0]).is_valid


def test_undoing_a_clip_leaves_the_brush_a_plain_box_again(state):
    brush = state.brushes[0]
    state.selected_objects = [brush]
    state.selected_object = brush
    state.save_state()

    assert state.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    assert bg.brush_has_geometry(state.brushes[0])
    state.save_state()

    assert state.undo()
    assert not bg.brush_has_geometry(state.brushes[0])
    assert bg.get_shape(state.brushes[0]).is_valid


def test_runtime_geometry_caches_never_reach_the_undo_stack(state):
    """They hold NumPy objects and are meaningless once the brush is replaced."""
    import json

    brush = state.brushes[0]
    assert state.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    bg.get_convex(brush)                      # populate every runtime cache
    ce.components(brush, ce.MODE_VERTEX)
    state.save_state()

    checkpoint = json.loads(state.undo_stack[-1])
    for serialised in checkpoint['brushes']:
        assert not (set(serialised) & set(bg.GEO_RUNTIME_KEYS))


# ---------------------------------------------------------------------------
# Redo actually redoes
# ---------------------------------------------------------------------------

def test_redo_re_applies_the_operation_undo_stepped_back_over(state):
    """Every Fio tool checkpoints at the *start* of a gesture.

    So the top of the undo stack is the state before the operation, and the
    operation's result exists only in the live scene. Redo used to be handed
    that popped checkpoint, which meant it re-applied the state undo had just
    restored — redo did nothing, and the undone work was gone for good.
    """
    brush = state.brushes[0]
    state.save_state()                       # checkpoint at "mouse-down"
    brush['size'] = [128, 64, 64]            # the gesture

    assert state.undo()
    assert state.brushes[0]['size'] == [64, 64, 64]

    assert state.redo()
    assert state.brushes[0]['size'] == [128, 64, 64]


def test_a_chain_of_operations_undoes_and_redoes_back_to_where_it_started(state):
    sizes = ([128, 64, 64], [192, 64, 64], [256, 64, 64])
    for size in sizes:
        state.save_state()
        state.brushes[0]['size'] = list(size)

    walked_back = []
    while state.undo():
        walked_back.append(list(state.brushes[0]['size']) if state.brushes else None)

    walked_forward = []
    while state.redo():
        walked_forward.append(list(state.brushes[0]['size']) if state.brushes else None)

    assert walked_back, "nothing was undone"
    assert state.brushes[0]['size'] == list(sizes[-1])


def test_redo_survives_a_geometry_operation(state):
    brush = state.brushes[0]
    state.selected_objects = [brush]
    state.selected_object = brush
    state.save_state()

    assert state.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    planes = len(state.brushes[0]['geometry']['planes'])

    assert state.undo()
    assert not bg.brush_has_geometry(state.brushes[0])

    assert state.redo()
    assert bg.brush_has_geometry(state.brushes[0])
    assert len(state.brushes[0]['geometry']['planes']) == planes
    assert bg.get_convex(state.brushes[0]).is_valid


def test_a_new_operation_after_an_undo_drops_the_redo_branch(state):
    state.save_state()
    state.brushes[0]['size'] = [128, 64, 64]
    assert state.undo()
    assert state.redo_stack

    state.save_state()                       # a different operation
    state.brushes[0]['size'] = [64, 64, 128]
    assert state.redo_stack == []


# ---------------------------------------------------------------------------
# Checkpoints that guard no change
# ---------------------------------------------------------------------------

def test_discarding_a_checkpoint_restores_the_redo_branch(state):
    state.brushes[0]['size'] = [128, 64, 64]
    state.save_state()
    assert state.undo()
    assert state.redo_stack, "precondition: there is something to redo"
    redo_depth = len(state.redo_stack)

    # A gesture that pushes a checkpoint and then turns out to change nothing.
    state.save_state()
    assert state.discard_last_checkpoint()

    assert len(state.redo_stack) == redo_depth
    assert state.redo()


def test_discarding_a_checkpoint_leaves_the_undo_depth_unchanged(state):
    depth = len(state.undo_stack)
    state.save_state()
    state.discard_last_checkpoint()
    assert len(state.undo_stack) == depth


def test_a_refused_clip_leaves_no_undo_step(state):
    depth = len(state.undo_stack)
    # A plane that misses the brush entirely cuts nothing.
    assert state.clip_brush(state.brushes[0], (1.0, 0.0, 0.0), 10_000.0) is False
    assert len(state.undo_stack) == depth
