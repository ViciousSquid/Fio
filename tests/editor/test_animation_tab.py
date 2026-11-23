"""A model Prop's Animation tab: its Spin rows write the spin_* properties,
and its Preview spins the model in the editor (editor.spin_preview) until it
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
    assert prop.properties['spin_enabled'] is False

    w['spin_enabled_cb'].setChecked(True)
    assert prop.properties['spin_enabled'] is True

    for label, value in (("X", 'x'), ("Y", 'y'), ("Z", 'z'),
                         ("Auto (thinnest side)", 'auto')):
        w['spin_axis_combo'].setCurrentText(label)
        assert prop.properties['spin_axis'] == value

    w['spin_speed_spin'].setValue(-90.0)
    assert prop.properties['spin_speed'] == pytest.approx(-90.0)
    assert w['spin_speed_spin'].suffix() == " °/s"
    assert w['spin_speed_spin'].minimum() == pytest.approx(-36000.0)

    w['spin_ramp_spin'].setValue(1.5)
    assert prop.properties['spin_ramp'] == pytest.approx(1.5)
    assert w['spin_ramp_spin'].maximum() == pytest.approx(60.0)

    w['spin_autostart_cb'].setChecked(True)
    assert prop.properties['spin_autostart'] is True
    main_window.unsaved_changes = False


def test_the_tab_is_built_from_the_props_values(main_window):
    prop = _model_prop()
    prop.properties.update(spin_enabled=True, spin_axis='y', spin_speed=45.0,
                           spin_ramp=2.0, spin_autostart=True)
    w = _open_animation(main_window, prop)._widgets
    assert w['spin_enabled_cb'].isChecked()
    assert w['spin_axis_combo'].currentText() == "Y"
    assert w['spin_speed_spin'].value() == pytest.approx(45.0)
    assert w['spin_ramp_spin'].value() == pytest.approx(2.0)
    assert w['spin_autostart_cb'].isChecked()
    main_window.unsaved_changes = False


def test_rows_are_disabled_while_spin_is_off(main_window):
    prop = _model_prop()
    w = _open_animation(main_window, prop)._widgets
    rows = ('spin_axis_combo', 'spin_speed_spin', 'spin_ramp_spin',
            'spin_autostart_cb', 'spin_preview_btn')
    assert all(not w[name].isEnabled() for name in rows)
    w['spin_enabled_cb'].setChecked(True)
    assert all(w[name].isEnabled() for name in rows)
    w['spin_enabled_cb'].setChecked(False)
    assert all(not w[name].isEnabled() for name in rows)
    main_window.unsaved_changes = False


@pytest.fixture
def clock(monkeypatch):
    """The spin preview's clock, moved by hand."""
    import editor.spin_preview as spin_preview
    now = [1000.0]
    monkeypatch.setattr(spin_preview.time, 'perf_counter', lambda: now[0])
    return now


def _run(main_window, clock, seconds, step=0.05):
    for _ in range(int(round(seconds / step))):
        clock[0] += step
        main_window.spin_preview._tick()


def test_preview_spins_the_model_and_stopping_puts_it_back(main_window, clock):
    prop = _model_prop()
    prop.properties.update(spin_enabled=True, spin_speed=90.0, spin_ramp=0.0)
    w = _open_animation(main_window, prop)._widgets
    btn = w['spin_preview_btn']
    assert btn.text() == "▶ Preview Spin"

    btn.setChecked(True)
    assert btn.text() == "■ Stop Preview"
    assert main_window.spin_preview.active
    assert main_window.spin_preview.prop is prop
    _run(main_window, clock, 0.5)
    assert prop._spin_angle == pytest.approx(45.0, abs=1e-6)

    # Speed changes take effect while it runs.
    w['spin_speed_spin'].setValue(180.0)
    before = prop._spin_angle
    _run(main_window, clock, 0.5)
    assert prop._spin_angle == pytest.approx(before + 90.0, abs=1e-6)

    # Axis changes restart it about the new axis.
    w['spin_axis_combo'].setCurrentText("X")
    assert main_window.spin_preview.active
    assert prop._spin_axis == 0
    assert btn.isChecked()

    btn.setChecked(False)
    assert btn.text() == "▶ Preview Spin"
    assert not main_window.spin_preview.active
    assert prop._spin_angle == 0.0
    main_window.unsaved_changes = False


def test_turning_spin_off_stops_the_preview(main_window, clock):
    prop = _model_prop()
    prop.properties['spin_enabled'] = True
    w = _open_animation(main_window, prop)._widgets
    w['spin_preview_btn'].setChecked(True)
    _run(main_window, clock, 0.5)
    w['spin_enabled_cb'].setChecked(False)
    assert not main_window.spin_preview.active
    assert not w['spin_preview_btn'].isChecked()
    assert prop._spin_angle == 0.0
    main_window.unsaved_changes = False


def test_selecting_another_object_stops_the_preview(main_window, clock):
    from editor.things import Light
    prop = _model_prop()
    prop.properties.update(spin_enabled=True, spin_ramp=0.0)
    w = _open_animation(main_window, prop)._widgets
    btn = w['spin_preview_btn']
    btn.setChecked(True)
    _run(main_window, clock, 0.5)
    assert prop._spin_angle != 0.0

    _select(main_window, Light(pos=[0, 64, 0]))
    assert not main_window.spin_preview.active
    assert prop._spin_angle == 0.0
    # The button on the parked page was put back up too.
    assert not btn.isChecked()
    assert btn.text() == "▶ Preview Spin"

    panel = _select(main_window, prop)
    assert not panel._widgets['spin_preview_btn'].isChecked()
    main_window.unsaved_changes = False
