"""The "HD Sky" section of the primary PlayerStart's property page.

The map's HD sky lives where its sky image does: in ``hd_*`` properties of
the primary PlayerStart (engine/hd_graphics.py: the fields, their defaults
and how they are resolved). This builds the section that edits them, beside
the sky image row. It shows only the fields the chosen sky uses, is always
editable -- an author may be on a machine that runs ``graphics = low`` --
and says that it only shows in the HD tiers.

A field left at its default is not stored: the map keeps only what its
author set.
"""

import math

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
                             QFormLayout, QHBoxLayout, QLabel, QPushButton, QWidget)

from engine import hd_graphics


def _display_rgb(field, stored):
    """The 0..255 colour a colour field shows: what is stored, else its
    default (which is linear light), else None for "from the atmosphere"."""
    if isinstance(stored, (list, tuple)) and len(stored) >= 3:
        try:
            return [int(max(0, min(255, round(float(c))))) for c in stored[:3]]
        except (TypeError, ValueError):
            pass
    if field.default is None:
        return None
    return [int(round(255.0 * max(0.0, c) ** (1.0 / 2.2))) for c in field.default]


def _decimals(step):
    return max(0, min(6, int(math.ceil(-math.log10(step))) if step < 1 else 1))


def build_hd_sky_section(panel, form, thing, section_class):
    """Add the HD Sky section for *thing* (the primary PlayerStart) to
    *form*. *panel* is the PropertyEditor; edits go through its
    ``update_object_prop`` (undo, journal, shared selection) and then make
    the primary the one owner of the values, as a sky image edit does."""
    editor = panel.editor
    if editor.state.resolve_player_start() is not thing:
        return None
    stored = editor.hd_sky_properties_from_map()
    settings = hd_graphics.sky_settings(stored, editor.sky_image_path_from_map())
    authored = bool(stored)
    section = section_class("HD Sky", expanded=authored)
    note = QLabel("Shown in the HD graphics tiers (Settings > Display > Graphics: "
                  "Medium, High or Ultra). At Low these are kept with the map "
                  "and change nothing.")
    note.setWordWrap(True)
    note.setStyleSheet("color: #9aa0a6;")
    section.addWidget(note)
    holder = QWidget()
    rows = QFormLayout(holder)
    rows.setContentsMargins(0, 0, 0, 0)
    rows.setSpacing(4)
    section.addWidget(holder)

    widgets = {}
    row_widgets = {}        # key -> (label, editor widget)

    def store(key, value):
        if panel._populating:
            return
        field = hd_graphics.FIELD_BY_KEY[key]
        default = field.default
        is_default = False
        if field.kind == "float" and default is not None:
            is_default = abs(float(value) - float(default)) <= field.step * 1e-3
        panel.update_object_prop(key, "" if is_default else value)
        if is_default:
            # Not stored at all: the map keeps only what its author set.
            for start in editor.state.player_starts():
                start.properties.pop(key, None)
        editor.canonicalize_hd_sky_ownership()
        refresh_visibility()

    def current_mode():
        return widgets["hd_sky"].currentData()

    def sun_on():
        box = widgets.get("hd_sun")
        return bool(box.isChecked()) if box is not None else False

    def refresh_visibility():
        mode = current_mode()
        for key, (label, widget) in row_widgets.items():
            field = hd_graphics.FIELD_BY_KEY[key]
            shown = mode in field.modes and (not field.needs_sun or sun_on())
            label.setVisible(shown)
            widget.setVisible(shown)

    def add_row(field, widget):
        label = QLabel(field.label + ":")
        if field.tip:
            label.setToolTip(field.tip)
            widget.setToolTip(field.tip)
        rows.addRow(label, widget)
        row_widgets[field.key] = (label, widget)
        widgets[field.key] = widget
        panel._widgets["hd_sky:" + field.key] = widget

    for field in hd_graphics.FIELDS:
        key = field.key
        name = key[3:]
        if key == "hd_sky":
            combo = QComboBox()
            for mode in hd_graphics.SKY_MODES:
                combo.addItem(hd_graphics.SKY_LABELS[mode], mode)
            combo.setCurrentIndex(max(0, combo.findData(settings["sky"])))

            def on_mode(_index, combo=combo):
                mode = combo.currentData()
                store("hd_sky", mode)
                # The sun follows the sky unless the author has said otherwise.
                if "hd_sun" not in editor.hd_sky_properties_from_map():
                    box = widgets["hd_sun"]
                    box.blockSignals(True)
                    box.setChecked(mode == hd_graphics.SKY_PROCEDURAL)
                    box.blockSignals(False)
                    refresh_visibility()
            combo.currentIndexChanged.connect(on_mode)
            add_row(field, combo)
        elif field.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(settings[name]))
            box.toggled.connect(lambda on, key=key: store(key, bool(on)))
            add_row(field, box)
        elif field.kind == "float":
            spin = QDoubleSpinBox()
            spin.setDecimals(_decimals(field.step))
            spin.setRange(field.low, field.high)
            spin.setSingleStep(field.step)
            spin.setKeyboardTracking(False)
            spin.setValue(float(settings[name]))
            spin.valueChanged.connect(lambda value, key=key: store(key, float(value)))
            add_row(field, spin)
        else:       # colour
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            button = QPushButton()
            button.setFixedSize(100, 24)
            reset = QPushButton("From atmosphere" if field.default is None else "Default")
            layout.addWidget(button)
            layout.addWidget(reset)
            layout.addStretch()

            def paint(button=button, field=field, key=key):
                rgb = _display_rgb(field, editor.hd_sky_properties_from_map().get(key))
                if rgb is None:
                    button.setText("auto")
                    button.setStyleSheet("")
                else:
                    button.setText("")
                    button.setStyleSheet(
                        "background-color: rgb(%d, %d, %d); border: 2px solid #555; "
                        "border-radius: 4px;" % tuple(rgb))

            def choose(_checked=False, field=field, key=key, paint=paint):
                rgb = _display_rgb(field, editor.hd_sky_properties_from_map().get(key))
                colour = QColorDialog.getColor(QColor(*(rgb or (255, 244, 220))), panel,
                                               "Choose " + field.label)
                if colour.isValid():
                    store(key, [colour.red(), colour.green(), colour.blue()])
                    paint()

            def clear(_checked=False, key=key, paint=paint):
                panel.update_object_prop(key, "")
                for start in editor.state.player_starts():
                    start.properties.pop(key, None)
                editor.canonicalize_hd_sky_ownership()
                paint()

            button.clicked.connect(choose)
            reset.clicked.connect(clear)
            paint()
            add_row(field, row)
            panel._widgets["hd_sky:" + key + ":button"] = button
            panel._widgets["hd_sky:" + key + ":reset"] = reset

    refresh_visibility()
    form.addRow(section)
    panel._widgets["hd_sky_section"] = section
    panel._hd_sky_refresh = refresh_visibility
    return section
