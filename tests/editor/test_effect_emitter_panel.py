"""The Effect panel's emitter controls and the PARTICLES type."""

import os

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from PyQt5.QtWidgets import QCheckBox, QComboBox      # noqa: E402

from editor.main_window import MainWindow             # noqa: E402
from editor.property_editor import PropertyEditor     # noqa: E402
from editor.things import Effect                      # noqa: E402
from engine.effect_entity import EFFECT_TYPES, EMITTER_PRESETS  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = pytest.mark.qt


@pytest.fixture
def panel(qt_app):
    editor = MainWindow(ROOT)
    widget = PropertyEditor(editor)
    yield widget
    widget.deleteLater()
    editor.unsaved_changes = False
    editor.close()
    editor.deleteLater()
    qt_app.processEvents()


def _show(panel, effect):
    panel.current_object = effect
    panel.populate_for_thing(effect)

    def combo_listing(items):
        for combo in panel.findChildren(QComboBox):
            if [combo.itemText(i) for i in range(combo.count())] == list(items):
                return combo
        raise AssertionError(f"no combo box listing {items}")

    light = next(box for box in panel.findChildren(QCheckBox)
                 if box.text() == "Enable intrinsic light")
    return combo_listing(EFFECT_TYPES), combo_listing(EMITTER_PRESETS), light


def test_the_panel_offers_particles_and_every_emitter_preset(panel):
    effect = Effect(properties={"effect_type": "FIRE"})
    type_combo, emitter_combo, _ = _show(panel, effect)
    assert type_combo.currentText() == "FIRE"
    assert emitter_combo.currentText() == "NONE"


def test_choosing_an_emitter_sets_it_on_the_effect(panel):
    effect = Effect(properties={"effect_type": "FIRE"})
    _, emitter_combo, _ = _show(panel, effect)
    emitter_combo.setCurrentText("SMOKE")
    assert effect.properties["emitter"] == "SMOKE"
    emitter_combo.setCurrentText("NONE")
    assert effect.properties["emitter"] == "NONE"


def test_switching_to_particles_gives_the_emitter_defaults(panel):
    effect = Effect(properties={"effect_type": "FIRE"})
    type_combo, emitter_combo, light = _show(panel, effect)
    assert light.isChecked()
    type_combo.setCurrentText("PARTICLES")
    assert effect.properties["effect_type"] == "PARTICLES"
    assert effect.properties["emitter"] == "SMOKE"
    assert emitter_combo.currentText() == "SMOKE"
    assert effect.properties["light_enabled"] is False
    assert not light.isChecked()
    assert (effect.properties["width"], effect.properties["height"]) == (32.0, 8.0)


def test_switching_to_particles_keeps_a_chosen_emitter(panel):
    effect = Effect(properties={"effect_type": "FIRE", "emitter": "SPARKS"})
    type_combo, emitter_combo, _ = _show(panel, effect)
    type_combo.setCurrentText("PARTICLES")
    assert effect.properties["emitter"] == "SPARKS"
    assert emitter_combo.currentText() == "SPARKS"


def test_switching_to_explosion_gives_a_burst_of_sparks(panel):
    effect = Effect(properties={"effect_type": "FIRE"})
    type_combo, emitter_combo, _ = _show(panel, effect)
    type_combo.setCurrentText("EXPLOSION")
    assert effect.properties["effect_type"] == "EXPLOSION"
    assert effect.properties["emitter"] == "SPARKS"
    assert emitter_combo.currentText() == "SPARKS"


def test_switching_to_explosion_keeps_a_chosen_emitter(panel):
    effect = Effect(properties={"effect_type": "FIRE", "emitter": "SMOKE"})
    type_combo, emitter_combo, _ = _show(panel, effect)
    type_combo.setCurrentText("EXPLOSION")
    assert effect.properties["emitter"] == "SMOKE"
    assert emitter_combo.currentText() == "SMOKE"


def test_a_map_explosion_without_an_emitter_keeps_none(panel):
    """Only the editor's switch adds sparks: an EXPLOSION loaded from a map
    with no emitter shows NONE, and showing it changes nothing."""
    effect = Effect(properties={"effect_type": "EXPLOSION"})
    _, emitter_combo, _ = _show(panel, effect)
    assert effect.properties["emitter"] == "NONE"
    assert emitter_combo.currentText() == "NONE"
