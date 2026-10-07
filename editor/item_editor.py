"""The Custom Items editor (Tools > Custom Items...).

Configures the two user-defined item slots, ``custom1`` and ``custom2``: what
each is called, whether it is a weapon or a pickup, how it looks in the world
and in the player's hand, and what it does. The definitions belong to the open
world and are saved with the map (see :mod:`engine.items`); every Prop that
gives the item follows the definition.
"""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from engine.items import (
    CUSTOM_ITEM_IDS, HUD_ALIGNS, ITEM_IDS, PICKUP_ACTIVATIONS, PICKUP_EFFECTS,
    WEAPON_MODES, ItemDefinitionError, compile_definition, complete_definition,
    default_definition,
)
from engine.prop_entity import KEY_NAMES
from editor.property_editor import _normalise_project_asset_path, _project_root

_KIND_LABELS = (('Weapon', 'weapon'), ('Pickup', 'pickup'))
_MODE_LABELS = (('None (held, never fires)', 'none'), ('Hitscan', 'hitscan'),
                ('Projectile', 'projectile'), ('Melee', 'melee'))
_ALIGN_LABELS = (('Right hand', 'right'), ('Centre', 'center'), ('Left hand', 'left'))
_ACTIVATION_LABELS = (('Walk over', 'walk_over'), ('Use', 'use'))
_EFFECT_LABELS = (('Health', 'health'), ('Ammo', 'ammo'), ('Armor', 'armor'),
                  ('Weapon', 'weapon'), ('Key', 'key'))

assert tuple(v for _, v in _MODE_LABELS) == WEAPON_MODES
assert tuple(v for _, v in _ALIGN_LABELS) == HUD_ALIGNS
assert tuple(v for _, v in _ACTIVATION_LABELS) == PICKUP_ACTIVATIONS
assert tuple(v for _, v in _EFFECT_LABELS) == PICKUP_EFFECTS


def slot_title(item_id, name):
    """``Custom 1 — Cigarette``: a custom slot as the editor names it."""
    return f"Custom {CUSTOM_ITEM_IDS.index(item_id) + 1} — {name}"


class _Choice(QComboBox):
    """A combo of ``(label, value)`` pairs that reads and writes the value."""

    def __init__(self, pairs):
        super().__init__()
        self._values = [v for _, v in pairs]
        self.addItems([label for label, _ in pairs])

    def value(self):
        return self._values[self.currentIndex()]

    def set_value(self, value):
        if value in self._values:
            self.setCurrentIndex(self._values.index(value))


class _AssetField(QWidget):
    """A project-relative asset path with a Choose... button and a preview."""

    def __init__(self, folder, file_filter, preview=None, hint=""):
        super().__init__()
        self._folder, self._filter, self._preview_size = folder, file_filter, preview
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit()
        self.path.setPlaceholderText(hint)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose)
        layout.addWidget(self.path, 1)
        layout.addWidget(choose)
        self.preview = None
        if preview:
            self.preview = QLabel()
            self.preview.setFixedSize(preview, preview)
            self.preview.setAlignment(Qt.AlignCenter)
            layout.addWidget(self.preview)
            self.path.textChanged.connect(self._show_preview)

    def _choose(self):
        start = os.path.join(_project_root(), *self._folder.split('/'))
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", start, self._filter)
        if path:
            self.path.setText(self.relative(path))

    def relative(self, path):
        return _normalise_project_asset_path(path)

    def _show_preview(self, text):
        pixmap = QPixmap(os.path.join(_project_root(), text)) if text else QPixmap()
        if pixmap.isNull():
            self.preview.clear()
        else:
            self.preview.setPixmap(pixmap.scaled(
                self._preview_size, self._preview_size, Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def value(self):
        return self.path.text().strip()

    def set_value(self, value):
        self.path.setText(value or "")


class _SoundField(_AssetField):
    """A sound file, stored by name inside ``assets/sounds``."""

    def __init__(self):
        super().__init__('assets/sounds', "Sounds (*.wav *.ogg *.mp3)", hint="(silent)")

    def relative(self, path):
        rel = _normalise_project_asset_path(path)
        return rel[len('assets/sounds/'):] if rel.startswith('assets/sounds/') else rel


def _spin(minimum, maximum, decimals=0, step=1.0, suffix=""):
    box = QDoubleSpinBox() if decimals else QSpinBox()
    box.setRange(minimum, maximum)
    if decimals:
        box.setDecimals(decimals)
        box.setSingleStep(step)
    if suffix:
        box.setSuffix(suffix)
    return box


class ItemPage(QWidget):
    """The settings of one custom item slot."""

    def __init__(self, item_id, definition, item_names, on_name_changed=None,
                 error=None):
        super().__init__()
        self.item_id = item_id
        outer = QVBoxLayout(self)
        if error:
            # The map's definition does not compile; the item does nothing in
            # play until it is fixed. Start from the shipped definition.
            warning = QLabel(f"The map's definition of this item is invalid ({error}), "
                             "so it does nothing in play. Showing the default; "
                             "apply to replace it.")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #c62828;")
            outer.addWidget(warning)

        common = QFormLayout()
        self.name = QLineEdit()
        self.kind = _Choice(_KIND_LABELS)
        self.world_sprite = _AssetField(
            'assets/sprites', "Images (*.png *.jpg *.jpeg *.bmp *.tga)", preview=60,
            hint="60×60 world / pickup sprite")
        self.hud_sprite = _AssetField(
            'assets/sprites', "Images (*.png *.jpg *.jpeg *.bmp *.tga)", preview=60,
            hint="sprite held in the player's view")
        self.hud_align = _Choice(_ALIGN_LABELS)
        self.hud_height = _spin(1, 600, decimals=1, step=10.0, suffix=" px")
        common.addRow("Name:", self.name)
        common.addRow("Type:", self.kind)
        common.addRow("World sprite:", self.world_sprite)
        common.addRow("HUD sprite:", self.hud_sprite)
        common.addRow("HUD position:", self.hud_align)
        common.addRow("HUD height:", self.hud_height)
        outer.addLayout(common)

        self.weapon_box = QGroupBox("Weapon")
        weapon = QFormLayout(self.weapon_box)
        self.mode = _Choice(_MODE_LABELS)
        self.damage = _spin(0, 100000)
        self.range = _spin(1, 100000, 1, 100.0, " units")
        self.cooldown = _spin(0, 60, 2, 0.05, " s")
        self.pellets = _spin(1, 64)
        self.spread = _spin(0, 45, 1, 0.5, "°")
        self.ammo = _spin(0, 100000)
        self.ammo_per_shot = _spin(0, 1000)
        self.sound = _SoundField()
        self.noise = _spin(0, 10, 2, 0.1)
        self.muzzle_flash = _AssetField(
            'assets/sprites', "Images (*.png *.jpg *.jpeg *.bmp *.tga)", hint="(no flash)")
        self.projectile_speed = _spin(1, 100000, 1, 50.0, " units/s")
        weapon.addRow("Mode:", self.mode)
        weapon.addRow("Damage:", self.damage)
        weapon.addRow("Range:", self.range)
        weapon.addRow("Cooldown:", self.cooldown)
        weapon.addRow("Pellets:", self.pellets)
        weapon.addRow("Spread:", self.spread)
        weapon.addRow("Ammo on first pickup:", self.ammo)
        weapon.addRow("Ammo per shot (0 = infinite):", self.ammo_per_shot)
        weapon.addRow("Sound:", self.sound)
        weapon.addRow("Noise (monster hearing):", self.noise)
        weapon.addRow("Muzzle flash:", self.muzzle_flash)
        weapon.addRow("Projectile speed:", self.projectile_speed)
        outer.addWidget(self.weapon_box)

        self.pickup_box = QGroupBox("Pickup")
        pickup = QFormLayout(self.pickup_box)
        self._pickup_form = pickup
        self.activation = _Choice(_ACTIVATION_LABELS)
        self.effect = _Choice(_EFFECT_LABELS)
        self.amount = _spin(0, 100000)
        self.item_id_choice = _Choice([(name, i) for i, name in item_names])
        self.key_name = QComboBox()
        self.key_name.setEditable(True)
        self.key_name.addItems(KEY_NAMES)
        self.respawns = QCheckBox("Respawns")
        self.respawn_time = _spin(0, 3600, 1, 1.0, " s")
        pickup.addRow("Collect on:", self.activation)
        pickup.addRow("Effect:", self.effect)
        pickup.addRow("Amount:", self.amount)
        pickup.addRow("Gives weapon:", self.item_id_choice)
        pickup.addRow("Key:", self.key_name)
        pickup.addRow("", self.respawns)
        pickup.addRow("Respawn after:", self.respawn_time)
        outer.addWidget(self.pickup_box)
        outer.addStretch(1)

        self.set_definition(definition)
        #: The definition the page started from; apply writes it only if
        #: changed. An invalid one was not shown, so applying replaces it.
        self.shown = None if error else self.definition()
        self.kind.currentIndexChanged.connect(self._refresh)
        self.effect.currentIndexChanged.connect(self._refresh)
        self.mode.currentIndexChanged.connect(self._refresh)
        if on_name_changed is not None:
            self.name.textChanged.connect(lambda text: on_name_changed(self, text))
        self._refresh()

    def set_definition(self, definition):
        w, p = definition['weapon'], definition['pickup']
        self.name.setText(definition['name'])
        self.kind.set_value(definition['kind'])
        self.world_sprite.set_value(definition['world_sprite'])
        self.hud_sprite.set_value(definition['hud_sprite'])
        self.hud_align.set_value(definition['hud_align'])
        self.hud_height.setValue(definition['hud_height'])
        self.mode.set_value(w['mode'])
        self.damage.setValue(w['damage'])
        self.range.setValue(w['range'])
        self.cooldown.setValue(w['cooldown'])
        self.pellets.setValue(w['pellets'])
        self.spread.setValue(w['spread'])
        self.ammo.setValue(w['ammo'])
        self.ammo_per_shot.setValue(w['ammo_per_shot'])
        self.sound.set_value(w['sound'])
        self.noise.setValue(w['noise'])
        self.muzzle_flash.set_value(w['muzzle_flash'])
        self.projectile_speed.setValue(w['projectile_speed'])
        self.activation.set_value(p['activation'])
        self.effect.set_value(p['effect'])
        self.amount.setValue(p['amount'])
        self.item_id_choice.set_value(p['item_id'] or 'gun1')
        self.key_name.setCurrentText(p['key_name'])
        self.respawns.setChecked(p['respawns'])
        self.respawn_time.setValue(p['respawn_time'])

    def definition(self):
        """The authoring definition the page shows."""
        return {
            'name': self.name.text().strip(),
            'kind': self.kind.value(),
            'world_sprite': self.world_sprite.value(),
            'hud_sprite': self.hud_sprite.value(),
            'hud_align': self.hud_align.value(),
            'hud_height': float(self.hud_height.value()),
            'weapon': {
                'mode': self.mode.value(),
                'damage': int(self.damage.value()),
                'range': float(self.range.value()),
                'cooldown': float(self.cooldown.value()),
                'pellets': int(self.pellets.value()),
                'spread': float(self.spread.value()),
                'ammo': int(self.ammo.value()),
                'ammo_per_shot': int(self.ammo_per_shot.value()),
                'sound': self.sound.value(),
                'noise': float(self.noise.value()),
                'muzzle_flash': self.muzzle_flash.value(),
                'projectile_speed': float(self.projectile_speed.value()),
            },
            'pickup': {
                'activation': self.activation.value(),
                'effect': self.effect.value(),
                'amount': int(self.amount.value()),
                'item_id': self.item_id_choice.value() if self.effect.value() == 'weapon' else '',
                'key_name': self.key_name.currentText().strip(),
                'respawns': self.respawns.isChecked(),
                'respawn_time': float(self.respawn_time.value()),
            },
        }

    def _refresh(self):
        weapon = self.kind.value() == 'weapon'
        self.weapon_box.setVisible(weapon)
        self.pickup_box.setVisible(not weapon)
        firing = self.mode.value() != 'none'
        for widget in (self.damage, self.range, self.cooldown, self.pellets,
                       self.spread, self.ammo_per_shot, self.sound, self.noise,
                       self.muzzle_flash, self.projectile_speed):
            widget.setEnabled(firing)
        self.projectile_speed.setEnabled(self.mode.value() == 'projectile')
        effect = self.effect.value()
        self._show_row(self.amount, effect in ('health', 'ammo', 'armor'))
        self._show_row(self.item_id_choice, effect == 'weapon')
        self._show_row(self.key_name, effect == 'key')

    def _show_row(self, field, visible):
        """Show or hide *field*'s row of the pickup form, label and all."""
        field.setVisible(visible)
        self._pickup_form.labelForField(field).setVisible(visible)


class ItemEditorDialog(QDialog):
    """Edit the custom item slots of the open world."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("Custom Items")
        self.setMinimumWidth(560)
        definitions = main_window.state.item_definitions
        item_names = [(i, definitions.name(i)) for i in ITEM_IDS]
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Custom 1 and Custom 2 are item slots this world can redefine. Props "
            "that give one (Collect as: Item) show and give what it is here.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        self.pages = {}
        registry = definitions.registry()
        for item_id in CUSTOM_ITEM_IDS:
            item = registry.resolve(item_id)
            definition = (complete_definition(item) if item is not None
                          else default_definition(item_id))
            page = ItemPage(item_id, definition, item_names, self._name_changed,
                            error=registry.errors.get(item_id))
            self.pages[item_id] = page
            self.tabs.addTab(page, slot_title(item_id, definition['name']))
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        reset = buttons.addButton("Reset to Default", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset_current)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _name_changed(self, page, text):
        index = self.tabs.indexOf(page)
        self.tabs.setTabText(index, slot_title(page.item_id, text.strip() or "(unnamed)"))

    def _reset_current(self):
        page = self.tabs.currentWidget()
        page.set_definition(default_definition(page.item_id))
        page._refresh()

    def _ok(self):
        if self.apply():
            self.accept()

    def apply(self):
        """Validate and apply both pages. Returns False (nothing applied) on error."""
        changed = {}
        for item_id, page in self.pages.items():
            definition = page.definition()
            try:
                compile_definition(item_id, definition)
            except ItemDefinitionError as exc:
                self.tabs.setCurrentWidget(page)
                QMessageBox.warning(self, "Custom Items",
                                    f"{slot_title(item_id, definition['name'] or '?')}: {exc}")
                return False
            if definition != page.shown:
                changed[item_id] = definition
        if not changed:
            return True
        window = self.main_window
        window.save_state()
        for item_id, definition in changed.items():
            window.state.set_item_definition(item_id, definition)
            self.pages[item_id].shown = definition
        window.update_all_ui()
        window.mark_as_modified()
        return True
