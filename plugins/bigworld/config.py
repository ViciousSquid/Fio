"""The one description of a Big World map's configuration.

A map's Big World settings are read in three places that have nothing else in
common, and each of them used to carry its own copy of the field list:

* :class:`plugins.bigworld.entities.BigWorldSettings` seeds an entity's
  ``properties`` with the defaults, so a newly placed settings entity is
  already valid;
* :meth:`plugins.bigworld.plugin.BigWorldPlugin.register` declares the editor's
  property schema — label, widget type, range, help text;
* :func:`plugins.bigworld.persistence.config_from_settings` coerces whatever a
  saved map actually carries into the typed config the session is built from.

Three hand-maintained copies of one list is one list too many: they agreed only
because nothing had changed recently, and a field added to the editor schema
but not to the coercer reads back as its *fallback* rather than as what the
mapper typed — a silent wrong radius, not an error.  So the list lives here
once, as data, and all three derive from it.

Deliberately stdlib-only and import-free.  ``entities`` pulls in the editor's
``Thing``, ``plugin`` must not import the streaming runtime at discovery time
and ``persistence`` runs headless; a table of plain tuples is the only shape
all three can read without any of them paying for the others.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional


class Field(NamedTuple):
    """One configuration key, in every form the three readers need it.

    *kind* is the editor's widget/property type and doubles as the coercion
    rule: ``"bool"`` and ``"float"`` are the only two Big World uses, and each
    names both how the property panel edits the value and how
    :func:`coerce` reads it back.
    """

    key: str
    kind: str
    label: str
    default: Any
    help: str
    min: Optional[float] = None
    max: Optional[float] = None


#: Every Big World configuration key, in the order the property panel shows
#: them.  Adding a key here adds it to the entity's defaults, to the editor
#: schema and to the runtime config in one edit — which is the point.
FIELDS: List[Field] = [
    Field("enabled", "bool", "Streaming enabled", True,
          "Turn cell streaming on for this map."),
    Field("activation_radius", "float", "Activation radius", 2048.0,
          "Cells within this distance of the player become active.",
          min=256.0, max=65536.0),
    Field("deactivation_radius", "float", "Deactivation radius", 2304.0,
          "Active cells are only dropped beyond this distance "
          "(hysteresis — must be >= activation radius).",
          min=256.0, max=65536.0),
    Field("sim_near_radius", "float", "Full-simulation radius", 1024.0,
          "Entities within this distance are tiered NEAR (full simulation "
          "fidelity); beyond it, resident entities are ACTIVE out to the "
          "activation radius. Clamped to the activation radius, so the "
          "streamed region and the simulated region always agree. (A DISTANT "
          "band only appears for cells kept resident well past the activation "
          "radius — set a deactivation radius more than an eighth beyond it.)",
          min=0.0, max=65536.0),
    Field("show_cell_debug", "bool", "Show debug overlay", True,
          "Draw the Big World stats panel and active-cell minimap in play mode."),
    Field("terrain_fill", "bool", "Fill world with terrain", False,
          "If the map has a procedural terrain, expand it to cover every cell "
          "of the world and stream its chunks around the player (instead of "
          "tessellating the whole grid up-front). Off by default, so terrain "
          "is left exactly as authored."),
    Field("terrain_infinite", "bool", "Infinite terrain (generate forever)", False,
          "With 'Fill world with terrain' on, keep generating ground around "
          "the camera/player forever instead of stopping at the world's "
          "content bounds — so you never walk off the edge. Only the chunks "
          "near you are ever resident."),
    Field("terrain_stream_radius", "float", "Terrain stream radius", 0.0,
          "World units of terrain kept resident around the player. 0 derives "
          "it from the activation radius.",
          min=0.0, max=65536.0),
    Field("disk_streaming", "bool", "Disk streaming (free unloaded cells)", False,
          "Experimental: instead of only hiding inactive cells, remove an "
          "unloaded cell's objects from memory and re-stream them from a "
          "pristine per-cell source when the cell comes back. Play-session "
          "saves become a delta of the persistent cell registry. Off by "
          "default (keeps the in-RAM behaviour)."),
]

#: ``{key: Field}`` for the readers that want one field rather than the order.
BY_KEY: Dict[str, Field] = {f.key: f for f in FIELDS}


def defaults() -> Dict[str, Any]:
    """``{key: default}`` — what a freshly placed settings entity carries."""
    return {f.key: f.default for f in FIELDS}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce(key: str, value: Any) -> Any:
    """Read one key's value out of whatever a map actually stored.

    A map's properties survive hand-editing, an older Fio and a JSON round trip
    that turns ``True`` into ``"true"``, so every read is a coercion with the
    field's own default as the fallback.
    """
    field = BY_KEY.get(key)
    if field is None:
        return value
    if field.kind == "bool":
        return _as_bool(value, bool(field.default))
    if field.kind == "float":
        return _as_float(value, float(field.default))
    return value


def config_from_properties(props: Optional[dict]) -> Dict[str, Any]:
    """The full typed config for a map, from a settings entity's properties.

    Missing keys take their default, so a partial or hand-written map never
    raises.  The one derived rule lives here too: the deactivation radius can
    never sit inside the activation radius, or a cell would be dropped the
    frame it was added (thrash, not hysteresis).
    """
    props = props or {}
    cfg = {f.key: coerce(f.key, props.get(f.key, f.default)) for f in FIELDS}
    cfg["deactivation_radius"] = max(cfg["deactivation_radius"],
                                     cfg["activation_radius"])
    return cfg
