"""The editor's explosion controls: the Effect panel's Play explosion,
Burns-again and fire spawner; the Prop panel's Explosive section and its
blast radius preview in the 2D views (dark orange)."""

import os

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from PyQt5.QtWidgets import QCheckBox, QPushButton, QSlider, QSpinBox, QToolButton  # noqa: E402

from editor.main_window import MainWindow             # noqa: E402
from editor.things import Effect, LogicSpawner        # noqa: E402
from engine.prop_entity import Prop                   # noqa: E402

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


def _button(panel, starts):
    return next(b for b in panel.findChildren(QPushButton)
                if b.text().startswith(starts) and b.isVisibleTo(panel))


def _check(panel, text):
    # The newest: a rebuilt panel may still hold the rows it replaced.
    return [b for b in panel.findChildren(QCheckBox) if b.text() == text][-1]


def test_the_effect_panel_offers_play_and_burns_again(editor):
    fire = Effect(properties={"name": "torch", "effect_type": "FIRE"})
    panel = _show(editor, fire)
    assert _button(panel, "▶ Play explosion")
    burns = _check(panel, "Burns again after exploding")
    assert burns.isChecked()
    burns.setChecked(False)
    assert fire.properties["return_after_explosion"] is False


def test_an_explosion_has_nothing_to_go_back_to(editor):
    boom = Effect(properties={"name": "boom", "effect_type": "EXPLOSION"})
    panel = _show(editor, boom)
    burns = next(b for b in panel.findChildren(QCheckBox)
                 if b.toolTip().startswith("When sent Explode, play the explosion"))
    assert not burns.isVisibleTo(panel)


def _fires(panel):
    from PyQt5.QtWidgets import QComboBox
    return [c for c in panel.findChildren(QComboBox)
            if c.toolTip() == "Which fire the explosion's fire spawner scatters."][-1]


def _select_buttons(panel):
    return [b for b in panel.findChildren(QPushButton)
            if b.text().startswith("Select fire spawner")]


def test_leaves_flames_behind_wires_a_spawner_and_unticking_removes_it(editor):
    from editor import io_system
    fire = Effect(pos=[10.0, 0.0, 20.0],
                  properties={"name": "torch", "effect_type": "FIRE", "lifetime": 0.5})
    panel = _show(editor, fire)
    count = len(editor.state.things)
    flames = _check(panel, "Leaves flames behind")
    assert not flames.isChecked()
    # Unticked: no Select button, no Fires row (nor its label).
    assert not _fires(panel).isVisibleTo(panel)
    assert not any(b.isVisibleTo(panel) for b in _select_buttons(panel))

    flames.setChecked(True)
    spawners = [t for t in editor.state.things if isinstance(t, LogicSpawner)]
    assert len(editor.state.things) == count + 1 and len(spawners) == 1
    spawner = spawners[0]
    assert spawner.properties["name"] == "torch_fires"
    assert editor.state.selected_objects != [spawner], "ticking does not select it"
    # At the fire's base (its centre is half its height up), lifted so the
    # spawned fires (placed by their centres) stand on the same floor.
    base = 0.0 - fire.properties["height"] * 0.5
    assert list(spawner.pos) == [10.0, base + panel.FIRE_SPAWNER_FIRE["height"] * 0.5, 20.0]
    assert spawner.properties["spawn_type"] == "Effect"
    assert spawner.properties["spawn_properties"]["effect_type"] == "FIRE"
    conns = io_system.get_connections(fire)
    assert [(c.output_name, c.input_name, c.target_id) for c in conns] == [
        ("OnExplode", "Spawn", spawner.properties["id"])]
    assert 0.0 < conns[0].delay < 0.5

    panel = _show(editor, fire)
    flames = _check(panel, "Leaves flames behind")
    assert flames.isChecked() and _fires(panel).isVisibleTo(panel)
    _button(panel, "Select fire spawner (torch_fires)").click()
    assert editor.state.selected_objects == [spawner]

    panel = _show(editor, fire)
    _check(panel, "Leaves flames behind").setChecked(False)
    assert not any(isinstance(t, LogicSpawner) for t in editor.state.things)
    assert io_system.get_connections(fire) == []
    assert not _fires(panel).isVisibleTo(panel)
    # Undo brings it back, wired; undo again takes it away.
    editor.state.undo()
    assert any(isinstance(t, LogicSpawner) for t in editor.state.things)
    editor.state.undo()
    assert not any(isinstance(t, LogicSpawner) for t in editor.state.things)


def test_the_explosive_section_switches_its_settings_on(editor):
    barrel = Prop(properties={"name": "barrel"})
    panel = _show(editor, barrel)
    explosive = _check(panel, "Explodes when damaged")
    spins = [s for s in panel.findChildren(QSpinBox) if s.toolTip().startswith(
        ("Damage it takes", "Damage dealt", "How far the blast", "How hard the blast"))]
    assert len(spins) == 4 and not any(s.isEnabled() for s in spins)
    explosive.setChecked(True)
    assert barrel.properties["explosive"] is True
    assert all(s.isEnabled() for s in spins)
    assert barrel.properties["explosion_radius"] == 192.0       # written out
    radius = next(s for s in spins if s.toolTip().startswith("How far the blast"))
    radius.setValue(300)
    assert barrel.properties["explosion_radius"] == 300.0


def test_a_barrels_fire_spawner_stands_its_fires_on_its_floor(editor):
    barrel = Prop(pos=[0.0, 5.0, 0.0], properties={"name": "barrel", "explosive": True})
    panel = _show(editor, barrel)
    flames = _check(panel, "Leaves flames behind")
    assert flames.isEnabled() and not _fires(panel).isVisibleTo(panel)
    flames.setChecked(True)
    spawner = next(t for t in editor.state.things if isinstance(t, LogicSpawner))
    assert spawner.properties["name"] == "barrel_fires"
    assert spawner.pos[1] == 5.0 + panel.FIRE_SPAWNER_FIRE["height"] * 0.5


def test_the_blast_radius_shows_dark_orange_in_the_2d_view(editor, qt_app):
    barrel = Prop(pos=[0.0, 0.0, 0.0],
                  properties={"name": "barrel", "explosive": True,
                              "explosion_radius": 256.0})
    panel = _show(editor, barrel)
    eye = next(b for b in panel.findChildren(QToolButton)
               if b.toolTip() == "Show the blast radius in the 2D views")
    view = editor.view_top if hasattr(editor, "view_top") else editor.view_2d
    view.resize(400, 400)
    view.show()
    qt_app.processEvents()

    def orange_pixels():
        image = view.grab().toImage()
        found = 0
        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 2):
                c = image.pixelColor(x, y)
                if c.red() > 150 and 50 < c.green() < 110 and c.blue() < 40:
                    found += 1
        return found

    before = orange_pixels()
    eye.setChecked(True)
    assert editor._explosion_preview_thing is barrel
    after = orange_pixels()
    assert after > before + 20, (before, after)
    eye.setChecked(False)
    assert editor._explosion_preview_thing is None


def _volume(panel):
    return next(s for s in panel.findChildren(QSlider)
                if s.toolTip().startswith("How loud its explosion is"))


def test_an_effects_explosion_volume(editor):
    fire = Effect(properties={"name": "torch", "effect_type": "FIRE"})
    panel = _show(editor, fire)
    volume = _volume(panel)
    assert volume.value() == 100 and volume.isVisibleTo(panel)
    volume.setValue(35)
    assert fire.properties["explosion_volume"] == pytest.approx(0.35)
    volume.setValue(0)
    assert fire.properties["silent"] is True


def test_an_old_silent_effect_reads_as_volume_zero(editor):
    boom = Effect(properties={"name": "boom", "effect_type": "EXPLOSION", "silent": True})
    assert _volume(_show(editor, boom)).value() == 0


def test_a_barrels_explosion_volume(editor):
    barrel = Prop(properties={"name": "barrel", "explosive": True})
    panel = _show(editor, barrel)
    volume = _volume(panel)
    assert volume.isEnabled()
    volume.setValue(60)
    assert barrel.properties["explosion_volume"] == pytest.approx(0.6)


def test_the_fires_chooser_sets_what_the_spawner_scatters(editor):
    fire = Effect(properties={"name": "torch", "effect_type": "FIRE"})
    panel = _show(editor, fire)
    _check(panel, "Leaves flames behind").setChecked(True)
    spawner = next(t for t in editor.state.things if isinstance(t, LogicSpawner))
    _fires(panel).setCurrentIndex(3)
    assert spawner.properties["spawn_properties"]["fire_texture"].endswith("fire04.gif")
    panel = _show(editor, fire)
    assert _fires(panel).currentIndex() == 3
    _fires(panel).setCurrentIndex(1)                   # edits the spawner
    assert spawner.properties["spawn_properties"]["fire_texture"].endswith("fire02.gif")
    assert not _fires(panel).itemIcon(0).isNull()      # each shown by a still


def test_the_shake_panel(editor):
    from editor.things import Shake
    shake = Shake(properties={"name": "quake"})
    panel = _show(editor, shake)
    slider = next(s for s in panel.findChildren(QSlider)
                  if s.toolTip().startswith("How hard it shakes"))
    assert slider.value() == 50
    slider.setValue(80)
    assert shake.properties["intensity"] == pytest.approx(0.8)
    view = editor.view_3d
    if view.logic_thread is None:
        pytest.skip("no logic thread without a GL context")
    _button(panel, "▶ Test shake").click()
    assert view.logic_thread.shake_runtime.shakes


def test_an_effect_has_a_blast_section(editor):
    fire = Effect(properties={"name": "torch", "effect_type": "FIRE"})
    panel = _show(editor, fire)
    damage = next(s for s in panel.findChildren(QSpinBox)
                  if s.toolTip().startswith("Damage dealt"))
    assert damage.value() == 0                                   # harmless by default
    damage.setValue(45)
    assert fire.properties["explosion_damage"] == 45
    eye = next(b for b in panel.findChildren(QToolButton)
               if b.toolTip() == "Show the blast radius in the 2D views")
    eye.setChecked(True)
    assert editor._explosion_preview_thing is fire
