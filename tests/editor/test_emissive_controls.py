"""The Emissive controls are the same wherever something can glow.

A decal, a Glow brush and water each get the Emissive checkbox. Light Intensity
and Light Radius are available only while Emissive is enabled.
"""
import pytest
from PyQt5.QtWidgets import QCheckBox, QDoubleSpinBox, QLabel, QSlider

pytestmark = pytest.mark.qt


def _controls(editor, prefix):
    widgets = editor._widgets
    return (widgets.get(prefix + '_emissive_cb'),
            widgets.get(prefix + '_emissive_intensity'),
            widgets.get(prefix + '_emissive_radius'))


def _labels(editor):
    return {label.text() for label in editor.findChildren(QLabel)
            if not label.isHidden()}


def _select(main_window, obj):
    main_window.set_selected_objects([obj])
    main_window.property_editor.set_object(obj)
    return main_window.property_editor


def _brush(shader):
    return {'id': 'b-' + shader, 'name': shader, 'pos': [0, 0, 0],
            'size': [64, 64, 64], 'shader': shader}


def _subjects(main_window):
    from editor.things import Decal
    glow, water = _brush('Glow'), _brush('Water')
    decal = Decal(pos=[0, 0, 0], properties={'id': 'd-1'})
    main_window.state.brushes += [glow, water]
    main_window.state.things.append(decal)
    return (('glow', glow, glow), ('water', water, water),
            ('decal', decal, decal.properties))


def test_every_emissive_thing_has_the_same_numeric_controls(main_window):
    seen = {}
    for prefix, obj, props in _subjects(main_window):
        editor = _select(main_window, obj)
        checkbox, intensity, radius = _controls(editor, prefix)

        assert isinstance(checkbox, QCheckBox), prefix + ": no Emissive checkbox"
        assert isinstance(intensity, QDoubleSpinBox), (
            prefix + ": the amount of light is not a numeric field")
        assert isinstance(radius, QDoubleSpinBox), prefix
        assert not isinstance(intensity, QSlider)
        seen[prefix] = ((intensity.minimum(), intensity.maximum(), intensity.decimals()),
                        (radius.minimum(), radius.maximum(), radius.decimals()))
        assert intensity.isHidden() and radius.isHidden(), prefix
        assert {"Light Intensity:", "Light Radius:"}.isdisjoint(_labels(editor)), (
            prefix + ": light controls should be hidden while Emissive is off")

        checkbox.setChecked(True)
        assert not intensity.isHidden() and not radius.isHidden(), prefix
        assert {"Light Intensity:", "Light Radius:"} <= _labels(editor), prefix
        assert "Emissive Intensity:" not in _labels(editor), (
            prefix + ": a second, differently named amount field")

        checkbox.setChecked(False)
        assert intensity.isHidden() and radius.isHidden(), prefix
        assert {"Light Intensity:", "Light Radius:"}.isdisjoint(_labels(editor)), prefix

    assert len(set(seen.values())) == 1, f"the controls differ: {seen}"


def test_decal_width_and_height_are_adjacent(main_window):
    from PyQt5.QtWidgets import QFormLayout
    from editor.things import Decal

    decal = Decal(pos=[0, 0, 0], properties={'id': 'd-dimensions'})
    editor = _select(main_window, decal)

    for form in editor.findChildren(QFormLayout):
        labels = []
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.LabelRole)
            widget = item.widget() if item is not None else None
            labels.append(widget.text() if isinstance(widget, QLabel) else '')
        if 'Width:' in labels and 'Height:' in labels:
            assert labels.index('Height:') == labels.index('Width:') + 1
            break
    else:
        pytest.fail("The decal Width and Height rows were not found together")


def test_the_amount_fields_write_the_properties_the_light_reads(main_window):
    for prefix, obj, props in _subjects(main_window):
        editor = _select(main_window, obj)
        checkbox, intensity, radius = _controls(editor, prefix)

        checkbox.setChecked(True)
        editor = main_window.property_editor
        _, intensity, radius = _controls(editor, prefix)
        intensity.setValue(3.5)
        _, intensity, radius = _controls(main_window.property_editor, prefix)
        radius.setValue(400.0)

        assert props.get('emissive') is True, prefix
        assert props.get('emissive_intensity') == pytest.approx(3.5), prefix
        assert props.get('emissive_radius') == pytest.approx(400.0), prefix
    main_window.unsaved_changes = False


# ---------------------------------------------------------------------------
# Editor accents
# ---------------------------------------------------------------------------

def test_a_wet_decal_is_told_from_a_painted_one_in_the_asset_browser():
    from editor.asset_browser import WET_DECAL_ACCENT, is_wet_decal

    assert is_wet_decal('assets/textures/decals/puddle_01.png')
    assert is_wet_decal('assets/textures/decals/Wet_Patch.PNG')
    assert not is_wet_decal('assets/textures/decals/Signage.png')
    assert not is_wet_decal('assets/textures/wet_concrete.png'), (
        "a texture outside the decals folder is not a decal")
    assert WET_DECAL_ACCENT.lower() != '#f08000'


def test_face_mode_wears_fio_s_orange_not_purple():
    from editor.surface_inspector import FACE_BUTTON_STYLE

    assert '#F08000' in FACE_BUTTON_STYLE
    for purple in ('#7B1FA2', '#8E24AA', '#4A148C', '#D500F9'):
        assert purple not in FACE_BUTTON_STYLE
