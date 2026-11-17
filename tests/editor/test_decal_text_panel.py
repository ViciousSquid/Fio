"""A decal's words are typed in the Property editor, and its font picked
from a dropdown of font names (Fio UI unless another is chosen).

The Decal tool shows the decal it has selected there: a decal has no icon in
the 2D or 3D views to click, so Shift-click (or placing one) is how its
properties come up.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QComboBox, QLineEdit                  # noqa: E402

from editor.things import Decal                                   # noqa: E402
from engine import decal_text                                     # noqa: E402

pytestmark = pytest.mark.qt


def _widgets(editor):
    from PyQt5.QtCore import QCoreApplication, QEvent
    # A rebuilt panel's old widgets wait for the event loop to delete them.
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    panel = editor
    text = [w for w in panel.findChildren(QLineEdit)
            if 'Words to paint' in w.placeholderText()]
    fonts = [w for w in panel.findChildren(QComboBox)
             if [w.itemText(i) for i in range(w.count())] == decal_text.font_names()]
    return text, fonts


def _show(main_window, decal):
    main_window.state.things = [decal]
    main_window.set_selected_objects([decal])
    editor = main_window.property_editor
    editor.set_object(decal, force=True)
    return editor


def test_a_new_decal_has_no_words_and_never_takes_a_signs_layer_as_its_image():
    from engine import decal_textures
    decal_textures.use_texts([decal_text.entry({'text': 'A', 'texture': ''})])
    try:
        props = Decal(pos=[0.0, 0.0, 0.0]).properties
    finally:
        decal_textures.use_texts(())
    assert not decal_text.is_entry(props['texture'])
    assert (props['text'], props['font'], props['text_colour']) == ('', 'Fio UI', 'Black')
    assert (props['text_scale'], props['text_offset_x'], props['text_offset_y']) == (1.0, 0.0, 0.0)


def test_the_panel_has_a_text_box_and_a_font_dropdown_starting_at_fio_ui(main_window):
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png'})
    editor = _show(main_window, decal)
    (text,), (fonts,) = _widgets(editor)
    assert text.text() == ''
    assert fonts.currentText() == 'Fio UI' and fonts.itemText(0) == 'Fio UI'
    assert 'Hornet Display' in [fonts.itemText(i) for i in range(fonts.count())]


def test_typing_the_words_and_picking_a_font_set_the_decal(main_window):
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png'})
    editor = _show(main_window, decal)
    (text,), (fonts,) = _widgets(editor)
    text.setText('EXIT')
    text.editingFinished.emit()
    assert decal.properties['text'] == 'EXIT'
    fonts.setCurrentText('Hornet Display')
    assert decal.properties['font'] == 'Hornet Display'
    # Shown again, the panel shows what the decal has.
    editor.set_object(decal, force=True)
    (text,), (fonts,) = _widgets(editor)
    assert text.text() == 'EXIT' and fonts.currentText() == 'Hornet Display'


def test_the_text_colour_is_black_or_white(main_window):
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png'})
    editor = _show(main_window, decal)
    editor.findChildren(QComboBox)                       # the panel, built
    from PyQt5.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    colours, = [w for w in editor.findChildren(QComboBox)
                if [w.itemText(i) for i in range(w.count())] == ['Black', 'White']]
    assert colours.currentText() == 'Black'
    colours.setCurrentText('White')
    assert decal.properties['text_colour'] == 'White'


def test_a_text_size_slider_shrinks_the_words(main_window):
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png', 'text': 'EXIT'})
    editor = _show(main_window, decal)
    slider = editor._widgets['decal_text_scale']
    assert (slider.minimum(), slider.maximum(), slider.value()) == (10, 100, 100)
    slider.setValue(40)
    assert decal.properties['text_scale'] == pytest.approx(0.4)
    editor.set_object(decal, force=True)
    assert editor._widgets['decal_text_scale'].value() == 40


def test_text_offset_sliders_move_the_words(main_window):
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png', 'text': 'EXIT'})
    editor = _show(main_window, decal)
    across, up = (editor._widgets['decal_text_offset_x'], editor._widgets['decal_text_offset_y'])
    assert (across.minimum(), across.maximum(), across.value()) == (-50, 50, 0)
    assert (up.minimum(), up.maximum(), up.value()) == (-50, 50, 0)
    up.setValue(12)
    across.setValue(-14)
    assert decal.properties['text_offset_y'] == pytest.approx(0.12)
    assert decal.properties['text_offset_x'] == pytest.approx(-0.14)


def test_the_decal_tool_shows_its_selected_decal_in_the_property_editor(main_window):
    main_window.state.clear_scene()
    decal = Decal(pos=[0.0, 0.0, 0.0], properties={'texture': 'grime_01.png', 'text': 'B-2'})
    main_window.state.things.append(decal)
    tool = main_window.view_3d.decal_tool
    main_window.toggle_decal_tool(True)
    try:
        tool.select({decal.properties['id']})
        editor = main_window.property_editor
        assert editor.current_object is decal
        (text,), _fonts = _widgets(editor)
        assert text.text() == 'B-2'
        tool.clear_selection()
        assert editor.current_object is not decal
    finally:
        tool.set_active(False)
