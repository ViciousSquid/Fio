"""
A PyQt-free fallback base for plugin entities.

Plugin entity classes normally subclass ``editor.things.Model`` / ``Thing`` so
they slot into the editor's palette, property panel and serializer. But the
editor package pulls in PyQt5, which is intentionally absent from the
standalone ``.fiopak`` player and its Android build. When ``editor.things``
can't be imported, plugins fall back to the tiny, dependency-free ``Thing`` /
``Model`` defined here so their entities still construct and their gameplay
still runs.

The surface mirrors just enough of ``editor.things`` for runtime use: a
``properties`` dict, ``pos``, a ``name`` accessor, ``type`` handling and simple
``to_dict`` / ``from_dict``. It deliberately has no rendering, no Qt, and no
NumPy — plain Python only.
"""

from __future__ import annotations

import uuid
from typing import Optional


class Thing:
    """Minimal placeable-entity base (no editor/PyQt dependency)."""

    pixmap_path = None
    _counters: dict = {}

    def __init__(self, pos=None, properties=None):
        self.pos = list(pos) if pos is not None else [0, 0, 0]
        self.properties = properties if properties is not None else {}
        self.properties.setdefault("type", self.__class__.__name__.lower())

        if not self.properties.get("name"):
            cls = self.__class__.__name__
            Thing._counters[cls] = Thing._counters.get(cls, 0) + 1
            self.properties["name"] = f"{cls}_{Thing._counters[cls]}"

        self.properties.setdefault("id", str(uuid.uuid4()))
        self.properties.setdefault("_io_connections", [])

    @property
    def name(self) -> str:
        return self.properties.get("name", "")

    @name.setter
    def name(self, value):
        self.properties["name"] = value

    def get_io_connections(self):
        return self.properties.get("_io_connections", [])

    def to_dict(self) -> dict:
        props = {k: v for k, v in self.properties.items() if k != "_io_connections"}
        return {
            "type": self.properties.get("type"),
            "pos": list(self.pos),
            "properties": props,
            "io_connections": [],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["Thing"]:
        """Build a Thing subclass instance from map data by ``type``.

        Matches the same way ``editor.things.from_dict`` does — class name,
        lowercased, underscores stripped — searching this base's subclasses.
        """
        ttype = data.get("type")
        if not ttype:
            return None
        target = str(ttype).replace("_", "").lower()

        def _walk(c):
            for sub in c.__subclasses__():
                yield sub
                yield from _walk(sub)

        for sub in _walk(Thing):
            if sub.__name__.lower() == target:
                return sub(pos=data.get("pos"), properties=dict(data.get("properties", {})))
        return None


class Model(Thing):
    """Fallback for a model-carrying entity."""

    pixmap_path = None

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault("type", "model")
        self.properties.setdefault("model_path", "")
        self.properties.setdefault("rotation", [0, 0, 0])
        self.properties.setdefault("scale", [1, 1, 1])
