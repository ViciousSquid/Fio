"""A model Prop's Animation tab: its Rotate rows write the rotate_* properties,
and its Preview rotates the model in the editor (editor.rotate_preview) until it
is pressed again or the selection changes."""

import pytest

pytestmark = pytest.mark.qt


def _tab_index(tabs, title):
    return next((i for i in range(tabs.count()) if tabs.tabText(i) == title), -1)


def _animation_shown(panel):
    tabs = panel.tab_widget
    index = _tab_index(tabs, "Animation")
    return index >= 0 and tabs.isTabVisible(index)


def _select(main_window, thing):
    if thing not in main_window.state.things:
        main_window.state.things.append(thing)
    main_window.set_selected_objects([thing])
    main_window.property_editor.set_object(thing)
    return main_window.property_editor


def _model_prop():
    from editor.things import Prop
    return Prop.for_model('assets/models/Oil_Drum.obj', pos=[0, 0, 0])


def _open_animation(main_window, prop):
    panel = _select(main_window, prop)
    tabs = panel.tab_widget
    tabs.setCurrentIndex(_tab_index(tabs, "Animation"))
    return panel


def test_only_a_model_prop_has_the_animation_tab(main_window):
    from editor.things import Light, Prop
    panel = _select(main_window, _model_prop())
    tabs = panel.tab_widget
    titles = [tabs.tabText(i) for i in range(tabs.count())]
    assert _animation_shown(panel)
    # After Physics, before Advanced and I/O.
    assert titles.index("Physics") < titles.index("Animation")
    for later in ("Advanced", "I/O"):
        if later in titles:
            assert titles.index("Animation") < titles.index(later)

    billboard = Prop(pos=[64, 0, 0], properties={
        'render_mode': 'billboard', 'sprite_path': 'assets/sprites/pickup.png'})
    assert not billboard.properties.get('model_path')
    panel = _select(main_window, billboard)
    assert not _animation_shown(panel)

    panel = _select(main_window, Light(pos=[0, 64, 0]))
    assert _tab_index(panel.tab_widget, "Animation") < 0
    main_window.unsaved_changes = False


def test_switching_representation_shows_and_hides_the_tab(main_window):
    prop = _model_prop()
    panel = _select(main_window, prop)
    combo = panel._widgets['prop_render_mode_combo']
    combo.setCurrentText('Sprite')
    assert prop.properties['render_mode'] == 'billboard'
    assert not _animation_shown(panel)
    combo.setCurrentText('Model')
    assert _animation_shown(panel)

    # Away and back (the page comes from the cache): still right.
    from editor.things import Light
    _select(main_window, Light(pos=[0, 64, 0]))
    panel = _select(main_window, prop)
    assert _animation_shown(panel)
    main_window.unsaved_changes = False


def test_each_control_writes_its_property(main_window):
    prop = _model_prop()
    panel = _open_animation(main_window, prop)
    w = panel._widgets
    assert prop.properties['rotate_enabled'] is False

    w['rotate_enabled_cb'].setChecked(True)
    assert prop.properties['rotate_enabled'] is True

    for label, value in (("X", 'x'), ("Y", 'y'), ("Z", 'z'),
                         ("Auto (thinnest side)", 'auto')):
        w['rotate_axis_combo'].setCurrentText(label)
        assert prop.properties['rotate_axis'] == value

    w['rotate_speed_spin'].setValue(-90.0)
    assert prop.properties['rotate_speed'] == pytest.approx(-90.0)
    assert w['rotate_speed_spin'].suffix() == " °/s"
    assert w['rotate_speed_spin'].minimum() == pytest.approx(-36000.0)

    w['rotate_ease_spin'].setValue(1.5)
    assert prop.properties['rotate_ease'] == pytest.approx(1.5)
    assert w['rotate_ease_spin'].maximum() == pytest.approx(60.0)

    w['rotate_autostart_cb'].setChecked(True)
    assert prop.properties['rotate_autostart'] is True
    main_window.unsaved_changes = False


def test_the_tab_is_built_from_the_props_values(main_window):
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_axis='y', rotate_speed=45.0,
                           rotate_ease=2.0, rotate_autostart=True)
    w = _open_animation(main_window, prop)._widgets
    assert w['rotate_enabled_cb'].isChecked()
    assert w['rotate_axis_combo'].currentText() == "Y"
    assert w['rotate_speed_spin'].value() == pytest.approx(45.0)
    assert w['rotate_ease_spin'].value() == pytest.approx(2.0)
    assert w['rotate_autostart_cb'].isChecked()
    main_window.unsaved_changes = False


def test_rows_are_disabled_while_rotate_is_off(main_window):
    prop = _model_prop()
    w = _open_animation(main_window, prop)._widgets
    rows = ('rotate_mode_combo', 'rotate_axis_combo', 'rotate_speed_spin',
            'rotate_angle_spin', 'rotate_ease_spin',
            'rotate_autostart_cb', 'rotate_preview_btn')
    assert all(not w[name].isEnabled() for name in rows)
    w['rotate_enabled_cb'].setChecked(True)
    assert all(w[name].isEnabled() for name in rows)
    w['rotate_enabled_cb'].setChecked(False)
    assert all(not w[name].isEnabled() for name in rows)
    main_window.unsaved_changes = False


@pytest.fixture
def clock(monkeypatch):
    """The rotate preview's clock, moved by hand."""
    import editor.rotate_preview as rotate_preview
    now = [1000.0]
    monkeypatch.setattr(rotate_preview.time, 'perf_counter', lambda: now[0])
    return now


def _run(main_window, clock, seconds, step=0.05):
    for _ in range(int(round(seconds / step))):
        clock[0] += step
        main_window.rotate_preview._tick()


def test_preview_rotates_the_model_and_stopping_puts_it_back(main_window, clock):
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_speed=90.0, rotate_ease=0.0)
    w = _open_animation(main_window, prop)._widgets
    btn = w['rotate_preview_btn']
    assert btn.text() == "▶ Preview Rotate"

    btn.setChecked(True)
    assert btn.text() == "■ Stop Preview"
    assert main_window.rotate_preview.active
    assert main_window.rotate_preview.prop is prop
    _run(main_window, clock, 0.5)
    assert prop._rotate_angle == pytest.approx(45.0, abs=1e-6)

    # Speed changes take effect while it runs.
    w['rotate_speed_spin'].setValue(180.0)
    before = prop._rotate_angle
    _run(main_window, clock, 0.5)
    assert prop._rotate_angle == pytest.approx(before + 90.0, abs=1e-6)

    # Axis changes restart it about the new axis.
    w['rotate_axis_combo'].setCurrentText("X")
    assert main_window.rotate_preview.active
    assert prop._rotate_axis == 0
    assert btn.isChecked()

    btn.setChecked(False)
    assert btn.text() == "▶ Preview Rotate"
    assert not main_window.rotate_preview.active
    assert prop._rotate_angle == 0.0
    main_window.unsaved_changes = False


def test_turning_rotate_off_stops_the_preview(main_window, clock):
    prop = _model_prop()
    prop.properties['rotate_enabled'] = True
    w = _open_animation(main_window, prop)._widgets
    w['rotate_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.5)
    w['rotate_enabled_cb'].setChecked(False)
    assert not main_window.rotate_preview.active
    assert not w['rotate_preview_btn'].isChecked()
    assert prop._rotate_angle == 0.0
    main_window.unsaved_changes = False


def test_selecting_another_object_stops_the_preview(main_window, clock):
    from editor.things import Light
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_ease=0.0)
    w = _open_animation(main_window, prop)._widgets
    btn = w['rotate_preview_btn']
    btn.setChecked(True)
    _run(main_window, clock, 0.5)
    assert prop._rotate_angle != 0.0

    _select(main_window, Light(pos=[0, 64, 0]))
    assert not main_window.rotate_preview.active
    assert prop._rotate_angle == 0.0
    # The button on the parked page was put back up too.
    assert not btn.isChecked()
    assert btn.text() == "▶ Preview Rotate"

    panel = _select(main_window, prop)
    assert not panel._widgets['rotate_preview_btn'].isChecked()
    main_window.unsaved_changes = False


def test_the_mode_chooses_which_rows_show(main_window):
    prop = _model_prop()
    w = _open_animation(main_window, prop)._widgets
    w['rotate_enabled_cb'].setChecked(True)
    for label, mode, angle, ease in (("Continuous", 'continuous', False, True),
                                     ("Swing", 'swing', True, True),
                                     ("Turn to angle", 'turn', True, False)):
        w['rotate_mode_combo'].setCurrentText(label)
        assert prop.properties['rotate_mode'] == mode
        assert w['rotate_angle_spin'].isVisibleTo(w['rotate_mode_combo'].parentWidget()) == angle
        assert w['rotate_ease_spin'].isVisibleTo(w['rotate_mode_combo'].parentWidget()) == ease
    w['rotate_angle_spin'].setValue(-45.0)
    assert prop.properties['rotate_angle'] == pytest.approx(-45.0)
    main_window.unsaved_changes = False


def test_a_turn_preview_goes_there_and_back(main_window, clock):
    from editor.rotate_preview import RotatePreview
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_mode='turn',
                           rotate_angle=90.0, rotate_speed=180.0)
    w = _open_animation(main_window, prop)._widgets
    w['rotate_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.6)                # 0.5 s to turn 90 at 180/s
    assert prop._rotate_angle == pytest.approx(90.0)
    _run(main_window, clock, RotatePreview.TURN_HOLD + 0.6)   # holds, then back
    assert prop._rotate_angle == pytest.approx(0.0, abs=1e-6)
    w['rotate_preview_btn'].setChecked(False)
    assert prop._rotate_angle == 0.0
    main_window.unsaved_changes = False


def test_changing_the_mode_restarts_a_running_preview(main_window, clock):
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_ease=0.0)
    w = _open_animation(main_window, prop)._widgets
    w['rotate_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.5)
    w['rotate_mode_combo'].setCurrentText("Swing")
    _run(main_window, clock, 0.05)
    assert main_window.rotate_preview.active
    assert main_window.rotate_preview.motion.mode == 'swing'
    w['rotate_preview_btn'].setChecked(False)
    main_window.unsaved_changes = False


@pytest.mark.parametrize('how', ['deselected', 'removed'])
def test_the_preview_stops_when_its_model_is_no_longer_selected_or_there(
        main_window, clock, how):
    """Picking the terrain clears the selection without set_selected_objects,
    and an undo replaces the scene's objects: either way the Preview stops."""
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, rotate_ease=0.0)
    w = _open_animation(main_window, prop)._widgets
    w['rotate_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.3)
    assert prop._rotate_angle != 0.0
    if how == 'deselected':
        main_window.state.selected_objects = []
    else:
        main_window.state.things.remove(prop)
    _run(main_window, clock, 0.1)
    assert not main_window.rotate_preview.active
    assert prop._rotate_angle == 0.0
    assert not w['rotate_preview_btn'].isChecked()
    main_window.unsaved_changes = False


def test_a_physics_model_says_rotate_does_not_run_and_will_not_preview(main_window, clock):
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, physics_enabled=True)
    w = _open_animation(main_window, prop)._widgets
    assert 'rotate_physics_note' in w
    assert "Physics" in w['rotate_physics_note'].text()
    w['rotate_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.2)
    assert not main_window.rotate_preview.active
    assert prop._rotate_angle == 0.0
    assert not w['rotate_preview_btn'].isChecked()
    main_window.unsaved_changes = False


def test_a_model_without_physics_has_no_physics_note(main_window):
    prop = _model_prop()
    prop.properties.update(rotate_enabled=True, physics_enabled=False)
    w = _open_animation(main_window, prop)._widgets
    assert 'rotate_physics_note' not in w
    main_window.unsaved_changes = False
