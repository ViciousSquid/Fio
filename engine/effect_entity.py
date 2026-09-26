"""The one and only Effect entity class.

Effect is a core world primitive: one authored object that describes both a
procedural visual effect and its emitted dynamic light. FIRE, ORB, EXPLOSION and CUSTOM are behaviours of this same primitive; there is no second explosion
system.

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
EFFECT_ORB = "ORB"
EFFECT_EXPLOSION = "EXPLOSION"
EFFECT_CUSTOM = "CUSTOM"
EFFECT_TYPES = (EFFECT_FIRE, EFFECT_ORB, EFFECT_EXPLOSION, EFFECT_CUSTOM)
EFFECT_ANIMATED_TYPES = (EFFECT_FIRE, EFFECT_ORB, EFFECT_CUSTOM)
EFFECT_ORB_LIGHT_COLOUR = [74, 155, 255]
EFFECT_FIRE_TEXTURES = tuple(
    f"assets/textures/effects/fire{i:02d}.gif" for i in range(1, 6)
)
EFFECT_ORB_TEXTURES = tuple(
    f"assets/textures/effects/orb{i:02d}.gif" for i in range(1, 6)
)

EFFECT_DEFAULTS = {
    "effect_type": EFFECT_FIRE,
    "preview": False,
    "fire_texture": EFFECT_FIRE_TEXTURES[0],
    "orb_texture": EFFECT_ORB_TEXTURES[0],
    "custom_gif": "",
    "width": 32.0,
    "height": 46.0,
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
    """Procedural FIRE/ORB/EXPLOSION effect with intrinsic dynamic light.

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
        self.properties.pop("size", None)
        self.properties.pop("scale", None)
        supplied_properties = set(self.properties)

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

        if effect_type == EFFECT_ORB:
            if "width" not in supplied_properties:
                self.properties["width"] = 32.0
            if "height" not in supplied_properties:
                self.properties["height"] = 32.0
            self.properties["light_colour"] = list(EFFECT_ORB_LIGHT_COLOUR)

        fire_texture = str(
            self.properties.get("fire_texture", EFFECT_FIRE_TEXTURES[0])
        ).replace("\\", "/")
        if fire_texture not in EFFECT_FIRE_TEXTURES:
            fire_texture = EFFECT_FIRE_TEXTURES[0]
        self.properties["fire_texture"] = fire_texture

        orb_texture = str(
            self.properties.get("orb_texture", EFFECT_ORB_TEXTURES[0])
        ).replace("\\", "/")
        if orb_texture not in EFFECT_ORB_TEXTURES:
            orb_texture = EFFECT_ORB_TEXTURES[0]
        self.properties["orb_texture"] = orb_texture

        custom_gif = str(self.properties.get("custom_gif", "")).strip().replace("\\", "/")
        self.properties["custom_gif"] = custom_gif

        try:
            seed = int(self.properties.get("effect_seed"))
        except (TypeError, ValueError):
            seed = _seed_from_id(self.properties.get("id", "effect"))
        self.properties["effect_seed"] = int(seed) & 0xFFFFFFFF or 1

        self._effect_spawn_time = 0.0
        self._effect_active = effect_type in EFFECT_ANIMATED_TYPES

    def duplicate(self, existing_names=()):
        """Duplicate with a fresh UUID, seed and runtime lifetime origin."""
        clone = super().duplicate(existing_names)
        clone.properties["effect_seed"] = _seed_from_id(
            clone.properties.get("id", "effect")
        )
        clone._effect_spawn_time = 0.0
        clone._effect_active = clone.effect_type in EFFECT_ANIMATED_TYPES
        return clone

    @property
    def effect_type(self) -> str:
        return self.properties.get("effect_type", EFFECT_FIRE)

    @property
    def is_explosion(self) -> bool:
        return self.effect_type == EFFECT_EXPLOSION

    def set_effect_type(self, value: object) -> bool:
        """Set the authored Effect TYPE and reset its transient behaviour.
        
        Returns True only when the type actually changes. The accepted names
        come from EFFECT_TYPES, so adding a new type extends SetType without
        changing the input handler.
        """
        effect_type = str(value or "").strip().upper()
        if effect_type not in EFFECT_TYPES:
            return False
        if effect_type == self.effect_type:
            return False

        self.properties["effect_type"] = effect_type
        self.properties["preview"] = False
        if effect_type == EFFECT_ORB:
            self.properties["width"] = 32.0
            self.properties["height"] = 32.0
            self.properties["light_colour"] = list(EFFECT_ORB_LIGHT_COLOUR)
        self._effect_spawn_time = 0.0
        self._effect_active = effect_type in EFFECT_ANIMATED_TYPES
        return True

    def reset_runtime(self) -> None:
        """Reset transient runtime state without changing authored data."""
        self._effect_spawn_time = 0.0
        self._effect_active = self.effect_type in EFFECT_ANIMATED_TYPES

    def trigger_explosion(self, now: float) -> bool:
        """Permanently switch to EXPLOSION and start/restart its playback."""
        self.properties["effect_type"] = EFFECT_EXPLOSION
        self.properties["preview"] = False
        self._effect_spawn_time = float(now)
        self._effect_active = True
        return True
