"""The PlayerStart's HD Sky section, and where the map keeps its HD sky.

The properties live on the primary PlayerStart, as the sky image does: the
section shows only there, only the fields the chosen sky uses, edits through
the editor's own operations, and the settings reach the 3D view as plain
data whichever renderer is drawing.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor")

from editor.things import PlayerStart                     # noqa: E402
from engine import hd_graphics                            # noqa: E402

pytestmark = pytest.mark.qt

CLOUDS = "assets/backgrounds/cloudy-sky-panorama.webp"


def _create(window, name, pos=(0, 40, 0)):
    start = PlayerStart(pos=list(pos), properties={"name": name})
    window.save_state()
    window.state.things.append(start)
    window.set_selected_objects([start])
    return start


@pytest.fixture
def window(main_window):
    main_window.state.clear_scene()
    return main_window


def _widget(window, key):
    return window.property_editor._widgets["hd_sky:" + key]


def _shown(window, key):
    return not _widget(window, key).isHidden()


def test_the_section_is_on_the_primary_start_only_and_always_editable(window):
    first = _create(window, "A")
    panel = window.property_editor
    assert "hd_sky_section" in panel._widgets
    # Collapsed until the author has set something; every field is there.
    assert not panel._widgets["hd_sky_section"].toggle.isChecked()
    for field in hd_graphics.FIELDS:
        assert "hd_sky:" + field.key in panel._widgets, field.key
    assert _widget(window, "hd_sky").currentData() == "none"
    assert _widget(window, "hd_sky").isEnabled()
    _create(window, "B", (100, 40, 0))
    assert "hd_sky_section" not in panel._widgets
    assert first.primary


def test_only_the_chosen_skys_fields_are_shown(window):
    _create(window, "A")
    look = ("hd_exposure", "hd_bloom", "hd_ao", "hd_tint", "hd_saturation")
    sun = ("hd_sun_azimuth", "hd_sun_elevation", "hd_sun_intensity")
    procedural = ("hd_turbidity", "hd_cloud_cover", "hd_wind_x", "hd_ground_color")
    assert all(_shown(window, key) for key in look)
    assert not any(_shown(window, key) for key in sun + procedural + ("hd_sun", "hd_haze"))

    combo = _widget(window, "hd_sky")
    combo.setCurrentIndex(combo.findData("procedural"))
    assert all(_shown(window, key) for key in look + sun + procedural + ("hd_sun", "hd_haze"))
    assert _widget(window, "hd_sun").isChecked()           # on by default under this sky

    _widget(window, "hd_sun").setChecked(False)
    assert not any(_shown(window, key) for key in sun)
    assert all(_shown(window, key) for key in procedural)

    combo = _widget(window, "hd_sky")
    combo.setCurrentIndex(combo.findData("none"))
    assert not any(_shown(window, key) for key in sun + procedural + ("hd_sun",))


def test_an_edit_is_stored_on_the_primary_start_and_reaches_the_view(window):
    start = _create(window, "A")
    combo = _widget(window, "hd_sky")
    combo.setCurrentIndex(combo.findData("procedural"))
    assert start.properties["hd_sky"] == "procedural"
    _widget(window, "hd_sun_elevation").setValue(25.0)
    _widget(window, "hd_exposure").setValue(0.5)
    assert start.properties["hd_sun_elevation"] == 25.0
    assert start.properties["hd_exposure"] == 0.5
    settings = window.view_3d.hd_sky_settings
    assert settings["sky"] == "procedural" and settings["sun"] is True
    assert settings["sun_elevation"] == 25.0 and settings["exposure"] == 0.5
    # A field put back to its default is not kept with the map.
    _widget(window, "hd_exposure").setValue(0.0)
    assert "hd_exposure" not in start.properties
    saved = json.loads(json.dumps(window.state.get_level_data()))
    record = next(r for r in saved["things"] if r.get("type") == "playerstart")
    assert record["properties"]["hd_sky"] == "procedural"
    assert record["properties"]["hd_sun_elevation"] == 25.0
    assert window.unsaved_changes


def test_the_default_sky_follows_the_sky_image(window):
    start = _create(window, "A")
    assert window.view_3d.hd_sky_settings["sky"] == "none"
    start.properties["sky_image"] = CLOUDS
    window.update_all_ui()
    assert window.view_3d.hd_sky_settings["sky"] == "panorama"
    assert window.view_3d.hd_sky_settings["sun"] is False
    assert "hd_sky" not in start.properties                  # a default, not stored


def test_legacy_values_on_another_start_are_recovered_then_moved_by_an_edit(window):
    first = _create(window, "A")
    second = _create(window, "B", (100, 40, 0))
    second.properties.update(hd_sky="procedural", hd_bloom=0.3)
    window.set_selected_objects([first])
    # Read without changing what was authored.
    assert window.hd_sky_properties_from_map() == {"hd_sky": "procedural", "hd_bloom": 0.3}
    window.update_all_ui()
    assert window.view_3d.hd_sky_settings["sky"] == "procedural"
    assert second.properties["hd_sky"] == "procedural" and "hd_sky" not in first.properties
    assert _widget(window, "hd_sky").currentData() == "procedural"
    # The first edit makes the primary the one owner.
    _widget(window, "hd_bloom").setValue(0.5)
    assert first.properties["hd_sky"] == "procedural" and first.properties["hd_bloom"] == 0.5
    assert not any(key.startswith("hd_") for key in second.properties)


def test_making_another_start_primary_takes_the_hd_sky_with_it(window):
    first = _create(window, "A")
    second = _create(window, "B", (100, 40, 0))
    first.properties.update(hd_sky="procedural", hd_sun_azimuth=200.0)
    window.set_selected_objects([second])
    window.property_editor._widgets["player_start_primary"].setChecked(True)
    assert second.primary
    assert second.properties["hd_sky"] == "procedural"
    assert second.properties["hd_sun_azimuth"] == 200.0
    assert not any(key.startswith("hd_") for key in first.properties)
    assert window.view_3d.hd_sky_settings["sun_azimuth"] == 200.0


def test_the_settings_window_offers_the_two_renderers(window):
    from editor.SettingsWindow import SettingsWindow
    dialog = SettingsWindow(window.config, window)
    try:
        combo = dialog.renderer_combo
        assert [combo.itemData(i) for i in range(combo.count())] == ["Classic", "HD_High"]
        assert combo.currentData() == "Classic"
        assert "swaps renderers while running" in combo.toolTip()
    finally:
        dialog.deleteLater()
    assert hasattr(window, "action_hd_preview") and hasattr(window, "hd_readiness_action")
