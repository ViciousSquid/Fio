"""The Prop panel's Oil Barrel: Representation -> Model picks it (the whole
preset); its Variant (None / Grey) is the first row under the model; and the
asset browser does not list the oil drum models (the preset and Variant are
how they are reached)."""

import os

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from PyQt5.QtWidgets import QComboBox                  # noqa: E402

from editor.main_window import MainWindow              # noqa: E402
from engine.prop_entity import (OIL_BARREL_SETTINGS, OIL_BARREL_VARIANTS,  # noqa: E402
                                Prop, model_preset_name, model_settings,
                                oil_barrel_variant, topples_by_default)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = pytest.mark.qt


@pytest.fixture
def editor(qt_app):
    window = MainWindow(ROOT)
    yield window
    window.unsaved_changes = False
    window.close()
    window.deleteLater()
    qt_app.processEvents()


def _show(editor, thing):
    if thing not in editor.state.things:
        editor.state.things.append(thing)
    panel = editor.property_editor
    panel.current_object = thing
    panel.populate_for_thing(thing)
    return panel


def _combo(panel, items):
    return next(c for c in panel.findChildren(QComboBox)
                if [c.itemText(i) for i in range(c.count())] == list(items))


def _row_of(form, widget):
    return form.getWidgetPosition(widget)[0]


def test_every_variant_is_the_oil_barrel():
    assert list(OIL_BARREL_VARIANTS) == ["None", "Grey"]
    for name, path in OIL_BARREL_VARIANTS.items():
        assert oil_barrel_variant(path) == name
        assert model_preset_name(path) == "Oil Barrel"
        assert topples_by_default(path)
        settings = model_settings(path)
        assert settings["model_path"] == path
        assert settings["collision_shape"] == OIL_BARREL_SETTINGS["collision_shape"]
    assert oil_barrel_variant("assets/models/ball.glb") is None


def test_switching_to_model_picks_the_oil_barrel(editor):
    prop = Prop(properties={"name": "thing", "render_mode": "billboard"})
    prop.properties.pop("model_path", None)
    panel = _show(editor, prop)
    _combo(panel, ["Model", "Sprite"]).setCurrentText("Model")
    assert prop.properties["model_path"] == OIL_BARREL_SETTINGS["model_path"]
    assert prop.properties["topples"] == OIL_BARREL_SETTINGS["topples"]
    assert panel._widgets["prop_model_preset_combo"].currentText() == "Oil Barrel"
    variant = _combo(panel, list(OIL_BARREL_VARIANTS))
    assert variant.isVisibleTo(panel) and variant.currentText() == "None"


def test_an_authored_model_is_kept_when_switching_to_model(editor):
    prop = Prop(properties={"name": "thing", "render_mode": "billboard",
                            "model_path": "assets/models/ball.glb"})
    panel = _show(editor, prop)
    _combo(panel, ["Model", "Sprite"]).setCurrentText("Model")
    assert prop.properties["model_path"] == "assets/models/ball.glb"
    assert not _combo(panel, list(OIL_BARREL_VARIANTS)).isVisibleTo(panel)


def test_the_variant_is_the_first_row_under_the_model_and_changes_it(editor):
    prop = Prop(properties={"name": "barrel", "render_mode": "model",
                            "model_path": OIL_BARREL_VARIANTS["None"]})
    panel = _show(editor, prop)
    form = panel._prop_render_form
    presets = panel._widgets["prop_model_preset_combo"].parentWidget()
    variant = _combo(panel, list(OIL_BARREL_VARIANTS))
    assert _row_of(form, variant) == _row_of(form, presets) + 1
    assert variant.isVisibleTo(panel)
    variant.setCurrentIndex(1)
    variant.activated.emit(1)
    assert prop.properties["model_path"] == OIL_BARREL_VARIANTS["Grey"]
    # Sprite hides it.
    _combo(panel, ["Model", "Sprite"]).setCurrentText("Sprite")
    assert not variant.isVisibleTo(panel)


def test_a_grey_barrel_shows_as_the_oil_barrel_grey(editor):
    prop = Prop(properties={"name": "barrel", "render_mode": "model",
                            "model_path": OIL_BARREL_VARIANTS["Grey"]})
    panel = _show(editor, prop)
    assert panel._widgets["prop_model_preset_combo"].currentText() == "Oil Barrel"
    assert _combo(panel, list(OIL_BARREL_VARIANTS)).currentText() == "Grey"


def test_the_asset_browser_does_not_list_the_oil_drums(editor, qt_app):
    from editor.asset_browser import AssetBrowserTab
    tab = next((t for t in editor.findChildren(AssetBrowserTab) if t.is_model_tab), None)
    if tab is None:
        pytest.skip("no asset browser in this window")
    tab.load_directory(tab.root_path)
    names = [os.path.basename(item.file_path) for item in tab.items]
    assert names, "the Models tab listed nothing"
    assert not any(n.lower().startswith("oil_drum") for n in names)
