"""A multi-selection edit is one undo step per gesture (audit P15).

With two or more objects selected, an edit in the property panel checkpoints
the whole scene so one undo puts every object back. A slider or spin box
sends valueChanged on every step, and each one checkpointed: 60 ticks of one
drag on a 20k-brush map took 3.4 s and filled the 50-step undo stack with
near-identical entries, pushing the user's real history off the bottom.

The checkpoint is now held open across a run of edits to the same property of
the same objects (until they pause, or anything else touches the history);
a single edit is still exactly one step.
"""

import pytest

from editor.things import Light

pytestmark = pytest.mark.qt


@pytest.fixture
def lights(main_window):
    a, b = Light(pos=[0, 0, 0]), Light(pos=[64, 0, 0])
    a.properties['intensity'] = b.properties['intensity'] = 1.0
    main_window.state.things.extend([a, b])
    main_window.set_selected_objects([a, b])
    assert main_window.property_editor.current_object is a
    return a, b


def _count_checkpoints(main_window, monkeypatch):
    calls = []
    original = main_window.save_state

    def counted():
        calls.append(True)
        return original()
    monkeypatch.setattr(main_window, 'save_state', counted)
    return calls


def test_a_slider_drag_is_one_checkpoint_and_one_undo(main_window, lights, monkeypatch):
    a, b = lights
    panel = main_window.property_editor
    for _ in range(3):                          # the user's earlier history
        main_window.save_state()
    history = list(main_window.state.undo_stack)
    checkpoints = _count_checkpoints(main_window, monkeypatch)

    for tick in range(60):
        panel.update_object_prop('intensity', 1.0 + tick / 10)

    assert len(checkpoints) == 1, "one drag must checkpoint the scene once"
    assert list(main_window.state.undo_stack)[:len(history)] == history
    assert len(main_window.state.undo_stack) == len(history) + 1
    assert b.properties['intensity'] == pytest.approx(6.9)

    main_window.undo()
    things = main_window.state.things
    assert [t.properties['intensity'] for t in things[-2:]] == [1.0, 1.0]


def test_a_pause_starts_a_new_step(main_window, lights, monkeypatch):
    panel = main_window.property_editor
    checkpoints = _count_checkpoints(main_window, monkeypatch)
    panel.update_object_prop('intensity', 2.0)
    panel.update_object_prop('intensity', 3.0)
    panel._shared_edit_idle.timeout.emit()      # the idle timer fires
    panel.update_object_prop('intensity', 4.0)
    assert len(checkpoints) == 2
    main_window.undo()
    assert [t.properties['intensity'] for t in main_window.state.things[-2:]] == [3.0, 3.0]


def test_another_property_or_an_undo_starts_a_new_step(main_window, lights, monkeypatch):
    panel = main_window.property_editor
    checkpoints = _count_checkpoints(main_window, monkeypatch)
    panel.update_object_prop('intensity', 2.0)
    panel.update_object_prop('radius', 300.0)           # a different field
    assert len(checkpoints) == 2
    main_window.undo()                                   # radius back
    a, b = main_window.state.things[-2:]
    main_window.set_selected_objects([a, b])
    panel = main_window.property_editor
    panel.update_object_prop('intensity', 5.0)           # after an undo
    assert len(checkpoints) == 3


def test_a_single_edit_is_still_one_undo_step(main_window, lights):
    panel = main_window.property_editor
    depth = len(main_window.state.undo_stack)
    panel.update_object_prop('intensity', 7.0)
    assert len(main_window.state.undo_stack) == depth + 1
    main_window.undo()
    assert [t.properties['intensity'] for t in main_window.state.things[-2:]] == [1.0, 1.0]


# -- one object selected ---------------------------------------------------------

def test_an_edit_of_one_selected_object_is_an_undo_step(main_window, lights):
    """With a single object selected a panel edit took no checkpoint at all:
    Ctrl+Z did nothing, or took the edit back together with the step before."""
    light = main_window.state.things[-1]
    main_window.set_selected_objects([light])
    panel = main_window.property_editor
    depth = len(main_window.state.undo_stack)
    panel.update_object_prop('intensity', 6.0)
    assert len(main_window.state.undo_stack) == depth + 1
    main_window.undo()
    assert main_window.state.things[-1].properties['intensity'] == 1.0
    main_window.redo()
    assert main_window.state.things[-1].properties['intensity'] == 6.0


def test_an_edit_of_one_object_does_not_take_the_step_before_it_back(main_window, lights):
    light = main_window.state.things[-1]
    main_window.set_selected_objects([light])
    main_window.save_state()
    light.pos = [light.pos[0] + 64.0, light.pos[1], light.pos[2]]
    moved = list(light.pos)
    main_window.property_editor.update_object_prop('intensity', 6.0)
    main_window.undo()
    after = main_window.state.things[-1]
    assert after.properties['intensity'] == 1.0
    assert list(after.pos) == moved, "the move before the edit is its own step"


def test_dragging_one_objects_slider_is_one_step(main_window, lights, monkeypatch):
    main_window.set_selected_objects([main_window.state.things[-1]])
    panel = main_window.property_editor
    checkpoints = _count_checkpoints(main_window, monkeypatch)
    for value in (2.0, 2.5, 3.0, 3.5, 4.0):
        panel.update_object_prop('intensity', value)
    assert len(checkpoints) == 1
    main_window.undo()
    assert main_window.state.things[-1].properties['intensity'] == 1.0
