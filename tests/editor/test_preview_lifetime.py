"""A Preview ends when what it previews is no longer what the editor is on.

The mover and door Preview was stopped only by its own button and by Save.
It went on, a 60 Hz timer driving the brush's runtime transform, after
another object was selected, after an undo had replaced the brush, after
another map was loaded (a brush of the closed map, kept alive by the
preview) and into Play, where the mover then ran at the preview's transform.
A model's Rotate preview ended in those cases, but a copy or a clone taken
while it ran carried the angle it had turned to.
"""

import pytest

from editor.things import PathNode
from engine import brush_placement
from engine.prop_entity import Prop
from tests.helpers.worlds import box_brush, level_data, make_thing

pytestmark = pytest.mark.qt

START = [0.0, 64.0, 0.0]


def _mover():
    return box_brush("lift", tuple(START), (32, 32, 32), is_mover=True,
                     path_target="node1", start_on=True, speed=256.0)


def _previewing(main_window):
    mover = _mover()
    other = box_brush("crate", (400.0, 0.0, 0.0), (64, 64, 64))
    main_window.state.brushes.extend([mover, other])
    main_window.state.things.extend([
        make_thing(PathNode, "node1", (192.0, 64.0, 0.0), next_node="node2"),
        make_thing(PathNode, "node2", (192.0, 64.0, 192.0))])
    main_window.set_selected_objects([mover])
    main_window.start_mover_preview(mover)
    for _ in range(5):
        main_window._advance_mover_preview()
    assert main_window.preview_timer.isActive()
    assert brush_placement.PREVIEW.runtime_of(mover) is not None
    return mover, other


def _ended(main_window, mover):
    return (not main_window.preview_timer.isActive()
            and not main_window.preview_data
            and brush_placement.PREVIEW.runtime_of(mover) is None)


def test_selecting_something_else_ends_a_mover_preview(main_window):
    mover, other = _previewing(main_window)
    main_window.set_selected_objects([mover])            # the same one: it goes on
    assert main_window.preview_timer.isActive()
    main_window.set_selected_objects([other])
    assert _ended(main_window, mover)


def test_undo_ends_a_mover_preview(main_window):
    mover, _other = _previewing(main_window)
    main_window.save_state()
    main_window.undo()                                   # the brush is replaced
    assert _ended(main_window, mover)


def test_loading_a_level_ends_a_mover_preview(main_window):
    mover, _other = _previewing(main_window)
    main_window.unsaved_changes = False
    assert main_window._load_level(level_data(brushes=[box_brush("floor", (0, 0, 0),
                                                                 (512, 32, 512))]))
    assert _ended(main_window, mover)
    main_window.unsaved_changes = False


def test_a_copy_taken_during_a_rotate_preview_is_as_placed(main_window):
    prop = Prop.for_model('assets/models/tape_reel_blue.glb', pos=[0, 0, 0],
                          properties={'rotate_enabled': True, 'rotate_speed': 180.0,
                                      'rotate_mode': 'continuous'})
    main_window.state.things.append(prop)
    main_window.set_selected_objects([prop])
    assert main_window.start_rotate_preview(prop)
    preview = main_window.rotate_preview
    prop._rotate_angle = 33.0                            # as a running preview leaves it
    main_window.copy_selection()
    assert not preview.active and prop._rotate_angle == 0.0
    assert all(getattr(copy, '_rotate_angle', 0.0) == 0.0
               for copy in main_window._brush_clipboard)

    main_window.set_selected_objects([prop])
    assert main_window.start_rotate_preview(prop)
    prop._rotate_angle = 33.0
    main_window.clone_selected_object()
    clone = main_window.primary_selection()
    assert clone is not prop and getattr(clone, '_rotate_angle', 0.0) == 0.0
    assert not preview.active
    main_window.cancel_clone_placement()
    main_window.unsaved_changes = False
