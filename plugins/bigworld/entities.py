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


class BigWorldSettings(Thing):
    """Map-level Big World configuration (one per map, optional).

    Properties
    ----------
    enabled:              master switch for streaming on this map (default True).
    activation_radius:    world units; cells within this of the player activate.
    deactivation_radius:  world units; active cells drop only beyond this
                          (the hysteresis band that prevents boundary thrash).
    show_cell_debug:      draw the Big World debug overlay / cell grid in play.
    terrain_fill:         if the map has a procedural terrain, expand it to cover
                          every cell of the world and stream its chunks around
                          the player instead of tessellating the whole grid
                          up-front (default False — terrain is left as authored).
    terrain_infinite:     with terrain_fill on, stream the terrain **forever**
                          around the camera/player instead of stopping at the
                          world's content bounds — so you never walk off an edge
                          (default False). Heights are a pure function of world
                          position, so the ground is deterministic everywhere.
    terrain_stream_radius: world units of terrain kept resident around the
                          player; 0 derives it from the activation radius.
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
        self.properties.setdefault("enabled", True)
        self.properties.setdefault("activation_radius", 2048.0)
        self.properties.setdefault("deactivation_radius", 2304.0)
        self.properties.setdefault("show_cell_debug", True)
        self.properties.setdefault("terrain_fill", False)
        self.properties.setdefault("terrain_infinite", False)
        self.properties.setdefault("terrain_stream_radius", 0.0)
        self.properties.setdefault("disk_streaming", False)

    # -- typed accessors ----------------------------------------------------
    def disk_streaming(self) -> bool:
        val = self.properties.get("disk_streaming", False)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def is_enabled(self) -> bool:
        val = self.properties.get("enabled", True)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def activation_radius(self) -> float:
        try:
            return float(self.properties.get("activation_radius", 2048.0))
        except (TypeError, ValueError):
            return 2048.0

    def deactivation_radius(self) -> float:
        try:
            r = float(self.properties.get("deactivation_radius", 2304.0))
        except (TypeError, ValueError):
            r = 2304.0
        return max(r, self.activation_radius())

    def show_cell_debug(self) -> bool:
        val = self.properties.get("show_cell_debug", True)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def terrain_fill(self) -> bool:
        val = self.properties.get("terrain_fill", False)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def terrain_infinite(self) -> bool:
        val = self.properties.get("terrain_infinite", False)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def terrain_stream_radius(self) -> float:
        try:
            return float(self.properties.get("terrain_stream_radius", 0.0))
        except (TypeError, ValueError):
            return 0.0
