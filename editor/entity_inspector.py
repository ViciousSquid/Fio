"""
Entity Inspector: a live, read-only view of one entity (plugin API 1.5.0).

What the inspector shows is supplied by plugins through
``EditorAPI.register_entity_inspector``: a provider returns an *inspection
document* for an entity, and this panel draws it. Fio contributes only the
panel and a fallback -- with no provider, or none with anything to say about
this entity, the inspector lists the entity's public properties -- so the
inspector never needs to know what any plugin's entities mean.

An inspection document is a plain dict::

    {"title":    "Gate Keeper",
     "subtitle": "patrolling · awake",
     "sections": [("Vitals", [("Health", 80), ("Speed", 1.5)]),
                  ("Goals",  [("Patrol", "", 0.9), ("Rest", "", 0.2)])]}

A row is ``(label, value)`` or ``(label, value, fraction)``; a fraction in
0..1 is drawn as a bar with the value as its text. :func:`normalise_document`
turns anything a provider returns into that shape, so a sloppy provider costs
a blank row, never an exception in the editor.

The panel refreshes itself a few times a second while it is visible, so values
a running play session changes are seen changing.
"""

from __future__ import annotations

import weakref

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QLabel, QProgressBar, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout)

#: Properties the fallback document leaves out: identity it already shows in
#: the title, and editor bookkeeping.
_FALLBACK_SKIP = frozenset({"name", "type", "id", "_io_connections"})


def _row(raw):
    """``(label, value, fraction|None)`` from a provider row, or None."""
    if not isinstance(raw, (tuple, list)) or len(raw) < 2:
        return None
    label, value = raw[0], raw[1]
    fraction = None
    if len(raw) > 2 and raw[2] is not None:
        try:
            fraction = max(0.0, min(1.0, float(raw[2])))
        except (TypeError, ValueError):
            fraction = None
    return str(label), "" if value is None else str(value), fraction


def normalise_document(document) -> dict:
    """Coerce a provider's document into the shape the panel draws."""
    if not isinstance(document, dict):
        document = {}
    sections = []
    for section in document.get("sections") or ():
        if not isinstance(section, (tuple, list)) or len(section) < 2:
            continue
        heading, rows = section[0], section[1]
        if not isinstance(rows, (tuple, list)):
            continue
        clean = [r for r in (_row(raw) for raw in rows) if r is not None]
        sections.append((str(heading), clean))
    return {
        "title": str(document.get("title") or "Inspector"),
        "subtitle": str(document.get("subtitle") or ""),
        "sections": sections,
    }


def generic_document(entity) -> dict:
    """What the inspector shows when no plugin describes *entity*."""
    props = getattr(entity, "properties", None)
    props = props if isinstance(props, dict) else {}
    name = str(props.get("name") or getattr(entity, "name", "") or "Entity")
    etype = str(props.get("type") or type(entity).__name__)
    rows = [(key, props[key]) for key in sorted(props, key=str)
            if not str(key).startswith("_") and key not in _FALLBACK_SKIP]
    sections = []
    pos = getattr(entity, "pos", None)
    if pos is not None:
        try:
            x, y, z = (float(pos[0]), float(pos[1]), float(pos[2]))
            sections.append(("Position", [("x", f"{x:.1f}"), ("y", f"{y:.1f}"),
                                          ("z", f"{z:.1f}")]))
        except (TypeError, ValueError, IndexError):
            pass
    sections.append(("Properties", rows))
    return normalise_document({"title": name, "subtitle": etype,
                               "sections": sections})


def inspect(entity, logic=None, manager=None) -> dict:
    """The inspection document for *entity*: a plugin's, else the fallback."""
    document = None
    try:
        if manager is None:
            from plugins.manager import get_manager
            manager = get_manager()
        document = manager.inspect_entity(entity, logic)
    except Exception:
        document = None
    return normalise_document(document) if document else generic_document(entity)


def _shape(document):
    """What must match for a refresh to update values in place."""
    return tuple((heading, tuple((label, fraction is not None)
                                 for label, _value, fraction in rows))
                 for heading, rows in document["sections"])


class EntityInspector(QDialog):
    """Floating panel showing one entity's inspection document, kept live.

    *logic* is a zero-argument callable returning the running logic thread,
    or None outside Play Mode. *alive* is a zero-argument callable that says
    whether the entity is still in the scene; once it is not, the panel says
    so and stops refreshing.
    """

    #: ms between refreshes while visible.
    REFRESH_MS = 250

    def __init__(self, entity, logic=None, alive=None, parent=None):
        super().__init__(parent)
        try:
            self._entity_ref = weakref.ref(entity)
        except TypeError:
            self._entity_ref = lambda _e=entity: _e
        self._logic = logic if callable(logic) else (lambda: None)
        self._alive = alive if callable(alive) else (lambda: True)
        self._shape = None
        self._value_cells = []    # [(item, bar|None)] in document row order

        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setMinimumSize(320, 240)
        self.resize(380, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setStyleSheet("color: #aaa;")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.subtitle_label)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 150)
        layout.addWidget(self.tree, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    @property
    def entity(self):
        return self._entity_ref()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._timer.isActive() and self.entity is not None:
            self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self):
        """Re-read the entity's document and redraw it."""
        entity = self.entity
        gone = entity is None
        if not gone:
            try:
                gone = not self._alive()
            except Exception:
                gone = False
        if gone:
            self._timer.stop()
            self.subtitle_label.setText("This entity is no longer in the scene.")
            return
        try:
            logic = self._logic()
        except Exception:
            logic = None
        document = inspect(entity, logic)
        self.setWindowTitle(f"Inspector - {document['title']}")
        self.subtitle_label.setText(document["subtitle"])
        self.subtitle_label.setVisible(bool(document["subtitle"]))
        shape = _shape(document)
        if shape == self._shape:
            self._update_values(document)
        else:
            self._rebuild(document)
            self._shape = shape

    def _rebuild(self, document):
        self.tree.clear()
        self._value_cells = []
        for heading, rows in document["sections"]:
            parent = QTreeWidgetItem(self.tree, [heading])
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            parent.setFirstColumnSpanned(True)
            for label, value, fraction in rows:
                item = QTreeWidgetItem(parent, [label, value])
                bar = None
                if fraction is not None:
                    bar = QProgressBar()
                    bar.setRange(0, 1000)
                    bar.setTextVisible(True)
                    bar.setMaximumHeight(16)
                    self.tree.setItemWidget(item, 1, bar)
                    self._set_bar(bar, value, fraction)
                self._value_cells.append((item, bar))
            parent.setExpanded(True)

    def _update_values(self, document):
        cells = iter(self._value_cells)
        for _heading, rows in document["sections"]:
            for _label, value, fraction in rows:
                item, bar = next(cells)
                if bar is not None:
                    self._set_bar(bar, value, fraction)
                elif item.text(1) != value:
                    item.setText(1, value)

    @staticmethod
    def _set_bar(bar, value, fraction):
        bar.setValue(int(round(fraction * 1000)))
        bar.setFormat(value or f"{int(round(fraction * 100))}%")

    def row_values(self) -> list:
        """``[(section, label, value), ...]`` as currently shown (for tests)."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for j in range(parent.childCount()):
                item = parent.child(j)
                bar = self.tree.itemWidget(item, 1)
                value = bar.format() if isinstance(bar, QProgressBar) else item.text(1)
                out.append((parent.text(0), item.text(0), value))
        return out
