"""A physics Prop's body is derived from its model, not a point.

Two solid physics Props meet at their models' bounds -- stacked, the upper one
rests on the lower; side by side, one stops against the other's face. Existing
Prop records and the Ball/Barrel presets use AABB for compatibility; newly
placed generic models use their actual triangles (Mesh). A map that still says
"auto" also remains AABB.
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
    # A box rests exactly a box's height up. On a Mesh it rests on the
    # model's real top, which for the barrel is its rim: a little lower than
    # the top of the box around it, and nowhere near inside it.
    slack = 1.5 if shape != "mesh" else 0.08 * 2.0 * half[1]
    assert gap == pytest.approx(2.0 * half[1], abs=slack), (
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


def _show_physics_tab(main_window):
    """Open the Prop page's Physics tab, as a click on it does (it is built
    the first time it is shown)."""
    tabs = main_window.property_editor.tab_widget
    tabs.setCurrentIndex(next(i for i in range(tabs.count())
                              if tabs.tabText(i) == "Physics"))


def test_existing_prop_records_default_to_aabb_but_can_choose_mesh(main_window):
    from editor.things import Thing
    from PyQt5.QtWidgets import QComboBox

    prop = Thing.from_dict(_prop("p", (0, 0, 0)))
    legacy = Thing.from_dict(_prop("old", (0, 0, 0), collision_shape="auto"))
    main_window.state.things += [prop, legacy]
    main_window.set_selected_objects([prop])
    _show_physics_tab(main_window)
    combo = main_window.property_editor._widgets.get('prop_collision_shape_combo')

    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ['AABB', 'Mesh']
    assert combo.currentText() == 'AABB' and prop.properties['collision_shape'] == 'aabb'
    combo.setCurrentText('Mesh')
    assert prop.properties['collision_shape'] == 'mesh'

    # A map saved when there was an "Automatic": it is AABB.
    main_window.set_selected_objects([legacy])
    _show_physics_tab(main_window)
    combo = main_window.property_editor._widgets.get('prop_collision_shape_combo')
    assert combo.currentText() == 'AABB'
    main_window.unsaved_changes = False
