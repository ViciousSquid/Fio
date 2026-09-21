"""
Entity types for Big World.

Just one placeable entity: :class:`BigWorldSettings`. Its presence in a map is
the map-level "Big World metadata" the backwards-compatibility rule (§20) turns
on: a map that contains a ``BigWorldSettings`` entity runs with cell-aware
streaming using the radii stored on it; a map without one loads and plays as
ordinary Fio, untouched. Because it is a normal entity it serializes through
Fio's existing save/load with no core change — its properties (and the config
they carry) round-trip like any other entity, and it keeps a stable UUID.

The entity holds *config only*, no runtime behaviour — that lives in
:mod:`plugins.bigworld.runtime`.
"""

from __future__ import annotations

# Editor-backed Thing in the editor; dependency-free fallback in the player.
try:
    from editor.things import Thing
except Exception:  # pragma: no cover - exercised only in the PyQt-free player
    from plugins.entitybase import Thing

from .config import BY_KEY, coerce, defaults


class BigWorldSettings(Thing):
    """Map-level Big World configuration (one per map, optional).

    Holds *config only*, no runtime behaviour — that lives in
    :mod:`plugins.bigworld.runtime`. Its presence in a map is the opt-in; a map
    without one plays as ordinary Fio.

    The properties it carries, their defaults, their editor labels and their
    help text are :data:`plugins.bigworld.config.FIELDS` — not repeated here,
    because a fourth copy of that list is a fourth thing to keep in step. This
    class adds only the ``Thing`` wrapper: the defaults a placed entity is
    seeded with, and typed accessors that read a saved value back through the
    table's own coercion rules.
    """

    #: Reused by the property panel / manager to key its schema.
    TYPE = "bigworldsettings"

    #: 2D-view sprite. Without this the entity draws nothing and is invisible /
    #: unselectable in the top/front/side views; the plugin ships its own icon
    #: (project-root-relative, like every other plugin entity's ``pixmap_path``).
    pixmap_path = "plugins/bigworld/assets/bigworldsettings.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault("type", self.TYPE)
        # Defaults come from the one field table, so a placed entity, the
        # editor's property schema and the runtime config cannot disagree
        # about what this map is configured to do.
        for key, value in defaults().items():
            self.properties.setdefault(key, value)

    # -- typed accessors ----------------------------------------------------
    #
    # Each reads through :func:`plugins.bigworld.config.coerce`, so a value
    # that survived a JSON round trip as ``"true"`` or a hand edit as ``""``
    # answers the same as one the property panel wrote.

    def _get(self, key):
        return coerce(key, self.properties.get(key, BY_KEY[key].default))

    def disk_streaming(self) -> bool:
        return bool(self._get("disk_streaming"))

    def is_enabled(self) -> bool:
        return bool(self._get("enabled"))

    def activation_radius(self) -> float:
        return float(self._get("activation_radius"))

    def deactivation_radius(self) -> float:
        # Never inside the activation radius — the hysteresis band has to be a
        # band. Same rule as config_from_settings, from the same place.
        return max(float(self._get("deactivation_radius")),
                   self.activation_radius())

    def show_cell_debug(self) -> bool:
        return bool(self._get("show_cell_debug"))

    def terrain_fill(self) -> bool:
        return bool(self._get("terrain_fill"))

    def terrain_infinite(self) -> bool:
        return bool(self._get("terrain_infinite"))

    def terrain_stream_radius(self) -> float:
        return float(self._get("terrain_stream_radius"))

    def sim_near_radius(self) -> float:
        return float(self._get("sim_near_radius"))
