"""The one and only Effect entity class.

Effect is a core world primitive: one authored object that describes both a
procedural visual effect and its emitted dynamic light. FIRE, ORB, EXPLOSION,
CUSTOM and PARTICLES are behaviours of this same primitive; there is no second
explosion system and no separate particle entity.

Any Effect may also carry an optional particle *emitter* (``emitter``, one of
:data:`EMITTER_PRESETS`): FIRE + SMOKE is one entity with a flame, its smoke
and its light. PARTICLES is the emitter alone -- no GIF flame -- and its
width/height are the emitter's extent. Particles are stateless: the renderer
derives every particle from (seed, index, time) in the vertex shader.

The class contains authored data plus non-journaled compatibility mirrors.
Live simulation runtime is owned by EffectStore; EntityTable projects that dense
execution state and the renderer consumes it numerically.
"""

from __future__ import annotations

import math
import random

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
EFFECT_PARTICLES = "PARTICLES"
EFFECT_TYPES = (
    EFFECT_FIRE, EFFECT_ORB, EFFECT_EXPLOSION, EFFECT_CUSTOM, EFFECT_PARTICLES,
)
EFFECT_ANIMATED_TYPES = (
    EFFECT_FIRE, EFFECT_ORB, EFFECT_CUSTOM, EFFECT_PARTICLES,
)

#: Particle emitter presets. The index is the preset id the renderer uses
#: (EntityTable.effect_emitter, the shader's preset table); NONE is 0.
EMITTER_NONE = "NONE"
EMITTER_PRESETS = ("NONE", "SMOKE", "STEAM", "SPARKS", "SPLASH", "MIST")
EMITTER_IDS = {name: index for index, name in enumerate(EMITTER_PRESETS)}
#: Particles per emitter at emitter_rate 1.0 (the shader's N).
EMITTER_MAX_PARTICLES = 48
EMITTER_SCALE_RANGE = (0.1, 8.0)

#: What a PARTICLES Effect starts as when the map does not say otherwise.
PARTICLES_DEFAULT_EMITTER = "SMOKE"
PARTICLES_DEFAULT_SIZE = (32.0, 8.0)
#: What an EXPLOSION is given when the editor switches an Effect to it and it
#: has no emitter of its own: a burst of sparks with the fireball.
EXPLOSION_DEFAULT_EMITTER = "SPARKS"

#: EXPLOSION's sprite sheet (assets/textures/effects/explosion.png): a grid of
#: EXPLOSION_SHEET_COLUMNS x EXPLOSION_SHEET_ROWS square cells, read left to
#: right, top to bottom, of which the first EXPLOSION_FRAME_COUNT hold frames
#: (the last, a pale wisp of smoke, ends the animation; the rest are empty).
#: The cells do not fill the sheet: each is EXPLOSION_CELL_UV of the sheet's
#: width and height (160 x 160 of 800 x 800), and the grid starts
#: EXPLOSION_SHEET_TOP_UV down from its top edge, with as much again below.
#: Reading it as four rows filling the height cut every frame through its art.
EXPLOSION_SHEET_COLUMNS = 5
EXPLOSION_SHEET_ROWS = 4
EXPLOSION_CELL_UV = (0.2, 0.2)
EXPLOSION_SHEET_TOP_UV = 0.1


def explosion_cell_box(frame, width, height):
    """Frame *frame*'s cell (from 0) in a *width* x *height* pixel sheet, as
    ``(left, top, right, bottom)`` pixels, top-left origin."""
    column, row = frame % EXPLOSION_SHEET_COLUMNS, frame // EXPLOSION_SHEET_COLUMNS
    cell_w = width * EXPLOSION_CELL_UV[0]
    cell_h = height * EXPLOSION_CELL_UV[1]
    left = column * cell_w
    top = height * EXPLOSION_SHEET_TOP_UV + row * cell_h
    return (int(round(left)), int(round(top)),
            int(round(left + cell_w)), int(round(top + cell_h)))
EXPLOSION_FRAME_COUNT = 17
#: The editor's Preview shows this frame, numbered from 1 for authoring.
EXPLOSION_PREVIEW_FRAME = 10
#: Playback position (0..1) at which the Preview frame is shown whole: frame
#: ``n`` (from 1) is unblended at ``(n - 0.5) / EXPLOSION_FRAME_COUNT``.
EXPLOSION_PREVIEW_T = (EXPLOSION_PREVIEW_FRAME - 0.5) / EXPLOSION_FRAME_COUNT

#: An explosion's light: ``light_intensity`` times a flash of
#: (1 + GAIN * exp(-elapsed / DECAY)), easing to nothing from FADE_FROM of
#: the lifetime to its end, where the fireball has faded too -- on for the
#: whole explosion, and never switched off while still bright.
EXPLOSION_FLASH_GAIN = 2.0
EXPLOSION_FLASH_DECAY = 0.035
EXPLOSION_LIGHT_FADE_FROM = 0.35


def explosion_light_scale(elapsed, lifetime):
    """How bright an explosion's light is, *elapsed* seconds into a
    *lifetime*, as a multiple of its ``light_intensity`` (0 at the end)."""
    t = elapsed / lifetime if lifetime > 0.0 else 1.0
    if t >= 1.0:
        return 0.0
    flash = 1.0 + EXPLOSION_FLASH_GAIN * math.exp(
        -max(elapsed, 0.0) / EXPLOSION_FLASH_DECAY)
    if t <= EXPLOSION_LIGHT_FADE_FROM:
        return flash
    u = (t - EXPLOSION_LIGHT_FADE_FROM) / (1.0 - EXPLOSION_LIGHT_FADE_FROM)
    return flash * (1.0 - u * u * (3.0 - 2.0 * u))
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
    "silent": False,
    "fire_texture": EFFECT_FIRE_TEXTURES[0],
    "orb_texture": EFFECT_ORB_TEXTURES[0],
    "custom_gif": "",
    "custom_loop": True,
    "width": 32.0,
    "height": 46.0,
    "intensity": 1.0,
    "colour": [255, 110, 25],
    "lifetime": 0.5,
    "light_enabled": True,
    "light_radius": 128.0,
    "light_intensity": 2.5,
    "light_colour": [255, 165, 70],
    "emitter": EMITTER_NONE,
    "emitter_rate": 1.0,
    "emitter_scale": 1.0,
    "emitter_tint": [255, 255, 255],
}


def _clamped_float(value, default, lo, hi):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:            # NaN
        return float(default)
    return max(lo, min(hi, number))


def normalise_emitter(value) -> str:
    """An authored emitter preset name, or NONE when it is not one."""
    name = str(value if value is not None else EMITTER_NONE).strip().upper()
    return name if name in EMITTER_IDS else EMITTER_NONE


def normalise_tint(value, default=(255, 255, 255)):
    """An RGB 0..255 integer triple, or *default* when *value* is not one."""
    try:
        rgb = [int(round(float(c))) for c in list(value)[:3]]
        if len(rgb) != 3:
            raise ValueError
    except (TypeError, ValueError):
        return list(default)
    return [max(0, min(255, c)) for c in rgb]


def emitter_id(props) -> int:
    """The dense preset id of an Effect's emitter (0 = none)."""
    return EMITTER_IDS[normalise_emitter(props.get("emitter", EMITTER_NONE))]


def _seed_from_id(value: object) -> int:
    """Derive a deterministic non-zero seed from an entity UUID/string."""
    raw = str(value).encode("utf-8", "replace")
    h = 2166136261
    for byte in raw:
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h or 1


class Effect(_ThingBase):
    """Procedural FIRE/ORB/EXPLOSION/PARTICLES effect with intrinsic light.

    properties['type'] remains 'effect' because it is the map entity token.
    The authored behaviour selector is properties['effect_type'].
    """

    pixmap_path = "assets/sprites/fire.png"

    # All Effect controls are rendered by the dedicated property panel.
    EDITOR_PRIMARY_PROPERTIES = ()
    EDITOR_ADVANCED_PROPERTIES = ()

    # Legacy non-journaled mirrors kept for standalone/editor callers that
    # inspect Effect runtime state directly. The live simulation owns runtime
    # state in EffectStore; LogicThread render projection reads the store.
    _effect_spawn_time = 0.0
    _effect_animation_phase = 0.0
    _effect_active = False

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
        elif effect_type == EFFECT_PARTICLES:
            # The emitter alone: width/height are its extent, it has no
            # light unless asked for one, and it emits something.
            if "width" not in supplied_properties:
                self.properties["width"] = PARTICLES_DEFAULT_SIZE[0]
            if "height" not in supplied_properties:
                self.properties["height"] = PARTICLES_DEFAULT_SIZE[1]
            if "light_enabled" not in supplied_properties:
                self.properties["light_enabled"] = False
            if "emitter" not in supplied_properties:
                self.properties["emitter"] = PARTICLES_DEFAULT_EMITTER

        self.properties["emitter"] = normalise_emitter(
            self.properties.get("emitter"))
        self.properties["emitter_rate"] = _clamped_float(
            self.properties.get("emitter_rate"), 1.0, 0.0, 1.0)
        self.properties["emitter_scale"] = _clamped_float(
            self.properties.get("emitter_scale"), 1.0, *EMITTER_SCALE_RANGE)
        self.properties["emitter_tint"] = normalise_tint(
            self.properties.get("emitter_tint"))

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
        custom_loop = self.properties.get("custom_loop", True)
        if isinstance(custom_loop, str):
            custom_loop = custom_loop.strip().lower() in ("1", "true", "yes", "on")
        self.properties["custom_loop"] = bool(custom_loop)
        self.properties["silent"] = str(self.properties.get("silent", False)).strip().lower() in ("1", "true", "yes", "on") if isinstance(self.properties.get("silent"), str) else bool(self.properties.get("silent", False))

        try:
            seed = int(self.properties.get("effect_seed"))
        except (TypeError, ValueError):
            seed = _seed_from_id(self.properties.get("id", "effect"))
        self.properties["effect_seed"] = int(seed) & 0xFFFFFFFF or 1

        self._effect_spawn_time = 0.0
        # Runtime animation phase: each playback starts at a random point in
        # the GIF cycle so multiple FIRE effects do not march in lockstep.
        self._effect_animation_phase = random.random()
        self._effect_active = effect_type in EFFECT_ANIMATED_TYPES

    def duplicate(self, existing_names=()):
        """Duplicate with a fresh UUID, seed and runtime lifetime origin."""
        clone = super().duplicate(existing_names)
        clone.properties["effect_seed"] = _seed_from_id(
            clone.properties.get("id", "effect")
        )
        clone._effect_spawn_time = 0.0
        clone._effect_animation_phase = random.random()
        clone._effect_active = clone.effect_type in EFFECT_ANIMATED_TYPES
        return clone

    @property
    def effect_type(self) -> str:
        return self.properties.get("effect_type", EFFECT_FIRE)

    @property
    def is_explosion(self) -> bool:
        return self.effect_type == EFFECT_EXPLOSION


if EDITOR_TIER:
    # Normally editor.things registers Effect itself.  When this module was the
    # process's first import of the two, editor.things ran inside the import
    # above, before Effect existed, and left the registration to here.
    from editor import things as _editor_things
    _editor_things.ENTITY_TYPES.setdefault('Effect', Effect)
