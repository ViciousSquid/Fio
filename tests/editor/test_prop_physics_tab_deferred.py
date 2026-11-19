"""A Prop page's Physics tab is built the first time it is shown.

It is never the tab on screen when a page appears, and building it with
every Prop page (a placed model is a Prop: the commonest selection) cost a
fair share of selecting one. Built on first showing, it reads the Prop's
values as they are then.
"""

import pytest

pytestmark = pytest.mark.qt


def _physics_index(tabs):
    return next(i for i in range(tabs.count()) if tabs.tabText(i) == "Physics")


def test_the_physics_tab_is_built_when_first_shown_from_live_values(main_window):
    from editor.things import Prop
    prop = Prop.for_model("assets/models/chair.obj", pos=[0, 0, 0])
    main_window.state.things.append(prop)
    main_window.set_selected_objects([prop])
    panel = main_window.property_editor
    tabs = panel.tab_widget
    index = _physics_index(tabs)
    assert tabs.currentIndex() != index
    assert 'prop_mass_spin' not in panel._widgets, "built before it was shown"

    prop.properties['mass'] = 7.5          # changed before the tab is opened
    tabs.setCurrentIndex(index)
    spin = panel._widgets['prop_mass_spin']
    assert spin.value() == pytest.approx(7.5)
    spin.setValue(3.0)
    assert prop.properties['mass'] == pytest.approx(3.0)

    # Shown again: the same widgets, not a second build.
    tabs.setCurrentIndex(0)
    tabs.setCurrentIndex(index)
    assert panel._widgets['prop_mass_spin'] is spin
    main_window.unsaved_changes = False
