"""A physics Prop's body is a box derived from its model, not a point.

Two solid physics Props meet at their models' bounds -- stacked, the upper one
rests a model's height above the lower; side by side, one stops against the
other's face -- in both collision shapes a Prop can choose (its model's shape,
or a box derived from the model). The property editor offers that choice as
one checkbox.
"""
import pytest

pytestmark = pytest.mark.qt

BARREL = "assets/models/Oil_Drum.obj"


def _prop(ident, pos, **props):
    base = {"type": "prop", "id": ident, "name": ident, "model_path": BARREL,
            "render_mode": "model", "physics_enabled": True, "no_collision": False,
            "scale": 1.0}
    base.update(props)
    return {"type": "prop", "pos": list(pos), "properties": base}


def _level(*things):
    return {"version": 5,
            "brushes": [{"id": "floor", "name": "floor", "pos": [0, -8, 0],
                         "size": [2000, 16, 2000]}],
            "things": [{"type": "playerstart", "pos": [0, 40, 600],
                        "properties": {"type": "player_start", "name": "s",
                                       "id": "ps", "primary": True}}, *things]}


def _thing(session, ident):
    return next(t for t in session.state.things if t.properties.get("id") == ident)


@pytest.mark.parametrize("shape", ["mesh", "aabb", "auto"])
def test_a_dropped_prop_rests_on_top_of_another_not_inside_it(fio_session, shape):
    session = fio_session(_level(_prop("lower", (0, 40, 0), collision_shape=shape),
                                 _prop("upper", (10, 400, 0), collision_shape=shape)))
    session.start_play()
    session.step(400)
    world = session.logic.session_runtime.physics_world
    lower, upper = _thing(session, "lower"), _thing(session, "upper")
    half = world.get_body(lower).half_extents

    assert min(half) > 10.0, f"the body is not the model's box: half {half}"
    gap = upper.pos[1] - lower.pos[1]
    assert gap == pytest.approx(2.0 * half[1], abs=1.5), (
        "the upper prop came to rest %.1f above the lower one's centre; the "
        "model is %.1f tall" % (gap, 2.0 * half[1]))
    session.stop_play()
    session.window.unsaved_changes = False


def test_a_pushed_prop_stops_at_the_other_s_face(fio_session):
    session = fio_session(_level(_prop("a", (-150, 40, 0), topples=False),
                                 _prop("b", (0, 40, 0), topples=False)))
    session.start_play()
    session.step(30)
    world = session.logic.session_runtime.physics_world
    a, b = _thing(session, "a"), _thing(session, "b")
    reach = world.get_body(a).half_extents[0] + world.get_body(b).half_extents[0]

    world.impulse(a, (120.0, 0.0, 0.0))
    closest = float("inf")
    for _ in range(200):
        session.step(1)
        closest = min(closest, b.pos[0] - a.pos[0])

    assert closest >= reach - 1.0, (
        "the props' centres came within %.1f of each other; their boxes touch "
        "at %.1f" % (closest, reach))
    session.stop_play()
    session.window.unsaved_changes = False


def test_the_collision_shape_is_one_checkbox(main_window):
    from editor.things import Thing
    from PyQt5.QtWidgets import QCheckBox

    prop = Thing.from_dict(_prop("p", (0, 0, 0)))
    main_window.state.things.append(prop)
    main_window.set_selected_objects([prop])
    box = main_window.property_editor._widgets.get('prop_collision_shape_cb')

    assert isinstance(box, QCheckBox)
    assert box.isChecked(), "Automatic (the model's shape where it has one) reads as ticked"
    box.setChecked(False)
    assert prop.properties['collision_shape'] == 'aabb'
    box = main_window.property_editor._widgets.get('prop_collision_shape_cb')
    box.setChecked(True)
    assert prop.properties['collision_shape'] == 'mesh'
    assert main_window.property_editor._widgets.get('prop_collision_shape_combo') is None
    main_window.unsaved_changes = False
