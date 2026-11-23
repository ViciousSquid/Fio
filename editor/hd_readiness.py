"""Tools > HD Readiness Report: how the open map's textures get their
material maps under the HD_High renderer.

HD_High shades every surface with a normal map and a material map (ambient
occlusion, roughness, metalness, emission). A texture's come from one of
three places, and this lists which for each texture the map uses:

* **hand-made** -- ``<name>_n`` / ``<name>_m`` beside the texture, made by a
  person;
* **generated** -- beside the texture, baked from its picture by
  ``tools/make_material_maps.py`` (tunable in ``<name>.mat.json``);
* **at load** -- none beside it: made from its picture when the map loads
  and kept in the user's cache. Correct, but the first load pays for it and
  the result cannot be tuned; bake it with the tool to change that;
* **tool** -- a tool texture (nodraw, trigger, the Dev grids): flat on
  purpose.

:func:`texture_report` is the data (no Qt); :func:`show_report` the window.
"""

import os

from engine import material_maps

LABELS = {'hand-made': 'Hand-made maps', 'generated': 'Generated maps (committed)',
          'runtime': 'Generated at load (cached)', 'tool': 'Tool texture (flat)',
          None: 'Not a surface (no maps)', 'missing': 'Texture not found'}
ORDER = ('runtime', 'missing', 'generated', 'hand-made', 'tool', None)

TERRAIN_LAYERS = ('terrain/sand.jpg', 'terrain/grass.jpg', 'terrain/rock.jpg',
                  'terrain/snow.jpg')


def map_textures(state):
    """``{texture path relative to assets/textures: [what uses it, ...]}``
    for the open map: its brushes' faces, its decals, and the terrain's
    layers where it has terrain."""
    used = {}
    for brush in getattr(state, 'brushes', ()) or ():
        for name in (brush.get('textures') or {}).values():
            if isinstance(name, str) and name:
                used.setdefault(name.replace('\\', '/'), set()).add('brush')
    for thing in getattr(state, 'things', ()) or ():
        properties = getattr(thing, 'properties', None) or {}
        if type(thing).__name__ == 'Decal' or properties.get('type') == 'decal':
            name = properties.get('texture')
            if isinstance(name, str) and name:
                used.setdefault('decals/' + name.replace('\\', '/'), set()).add('decal')
    terrain = getattr(state, 'terrain', None)
    if terrain is not None and getattr(terrain, 'enabled', True):
        for name in TERRAIN_LAYERS:
            used.setdefault(name, set()).add('terrain')
    return {name: sorted(uses) for name, uses in used.items()}


def texture_report(state, textures_root=None):
    """Rows ``(texture, status, uses)`` for the open map, the ones that most
    want attention first. *status* is ``material_maps.status``'s, or
    ``'missing'`` for a texture whose file is not there."""
    root = textures_root or os.path.join('assets', 'textures')
    rows = []
    for name, uses in map_textures(state).items():
        path = os.path.join(root, name)
        status = material_maps.status(path) if os.path.isfile(path) else 'missing'
        rows.append((name, status, uses))
    rows.sort(key=lambda row: (ORDER.index(row[1]), row[0].lower()))
    return rows


def summary(rows):
    """``{status: count}`` over *rows*."""
    counts = {}
    for _name, status, _uses in rows:
        counts[status] = counts.get(status, 0) + 1
    return counts


def show_report(window):
    """The report for *window*'s open map, as a dialog."""
    from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QHeaderView, QLabel,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout)
    rows = texture_report(window.state)
    counts = summary(rows)
    dialog = QDialog(window)
    dialog.setWindowTitle("HD Readiness Report")
    dialog.resize(640, 480)
    layout = QVBoxLayout(dialog)
    parts = ["%d %s" % (counts[key], LABELS[key].lower()) for key in ORDER if counts.get(key)]
    head = QLabel(
        "How each texture this map uses gets its material maps under the HD_High "
        "renderer: %s.\nTextures generated at load are drawn correctly; bake them with "
        "tools/make_material_maps.py to tune them and to spare the first load."
        % (", ".join(parts) or "the map uses no textures"))
    head.setWordWrap(True)
    layout.addWidget(head)
    table = QTableWidget(len(rows), 3, dialog)
    table.setHorizontalHeaderLabels(["Texture", "Material maps", "Used by"])
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    for index, (name, status, uses) in enumerate(rows):
        table.setItem(index, 0, QTableWidgetItem(name))
        table.setItem(index, 1, QTableWidgetItem(LABELS[status]))
        table.setItem(index, 2, QTableWidgetItem(", ".join(uses)))
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
    layout.addWidget(table)
    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog._table = table
    dialog.exec_()
    return dialog
