"""The one and only Effect entity class.

Effect is a core world primitive: one authored object that describes both a
procedural visual effect and its emitted dynamic light. FIRE and EXPLOSION
are behaviours of this same primitive; there is no second explosion system.

The class contains authored data only. Rendering/runtime state is projected
into engine.entity_table and consumed numerically by the renderer.
"""

from __future__ import annotations

try:
    from editor.things import Thing as _ThingBase
    EDITOR_TIER = True
except ImportError:  # standalone player / Android: no PyQt5
    from plugins.entitybase import Thing as _ThingBase
    EDITOR_TIER = False


EFFECT_FIRE = "FIRE"
EFFECT_EXPLOSION = "EXPLOSION"
EFFECT_TYPES = (EFFECT_FIRE, EFFECT_EXPLOSION)

EFFECT_DEFAULTS = {
    "effect_type": EFFECT_FIRE,
    "size": 32.0,
    "intensity": 1.0,
    "colour": [255, 110, 25],
    "lifetime": 0.5,
    "light_enabled": True,
    "light_radius": 128.0,
    "light_intensity": 2.5,
    "light_colour": [255, 165, 70],
}


def _seed_from_id(value: object) -> int:
    """Derive a deterministic non-zero seed from an entity UUID/string."""
    raw = str(value).encode("utf-8", "replace")
    h = 2166136261
    for byte in raw:
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h or 1


class Effect(_ThingBase):
    """Procedural FIRE/EXPLOSION effect with intrinsic dynamic light.

    properties['type'] remains 'effect' because it is the map entity token.
    The authored behaviour selector is properties['effect_type'].
    """

    pixmap_path = "assets/sprites/light.png"

    # All Effect controls are rendered by the dedicated property panel.
    EDITOR_PRIMARY_PROPERTIES = ()
    EDITOR_ADVANCED_PROPERTIES = ()

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "effect"

        for key, value in EFFECT_DEFAULTS.items():
            if key not in self.properties:
                self.properties[key] = (
                    list(value) if isinstance(value, list) else value
                )

        effect_type = str(
            self.properties.get("effect_type", EFFECT_FIRE)
        ).strip().upper()
        if effect_type not in EFFECT_TYPES:
            effect_type = EFFECT_FIRE
        self.properties["effect_type"] = effect_type

        try:
            seed = int(self.properties.get("effect_seed"))
        except (TypeError, ValueError):
            seed = _seed_from_id(self.properties.get("id", "effect"))
        self.properties["effect_seed"] = int(seed) & 0xFFFFFFFF or 1

        self._effect_spawn_time = 0.0

    def duplicate(self, existing_names=()):
        """Duplicate with a fresh UUID, seed and runtime lifetime origin."""
        clone = super().duplicate(existing_names)
        clone.properties["effect_seed"] = _seed_from_id(
            clone.properties.get("id", "effect")
        )
        clone._effect_spawn_time = 0.0
        return clone

    @property
    def effect_type(self) -> str:
        return self.properties.get("effect_type", EFFECT_FIRE)

    @property
    def is_explosion(self) -> bool:
        return self.effect_type == EFFECT_EXPLOSION

    def reset_runtime(self) -> None:
        """Reset transient runtime timing without changing authored data."""
        self._effect_spawn_time = 0.0
