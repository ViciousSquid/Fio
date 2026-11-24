"""Which renderer draws Fio, and a map's HD sky: plain data, no OpenGL.

Fio has two renderers, both reached through the renderer registry
(``engine.renderer``) and swapped live:

* **Classic** -- the original forward renderer (registry name ``Forward``).
  The default. It knows nothing of anything in this module.
* **HD_High** -- a separate renderer (``engine/renderer/hd_high``):
  physically based surfaces, a sun and sky, HDR post-processing.

``[Renderer] renderer`` in settings.ini names the one the editor starts with
(:func:`renderer_from`); low-power ARM mode always starts Classic.

What an HD_High frame looks like is map data: the ``hd_*`` properties of the
map's primary PlayerStart (:data:`FIELDS`), resolved with the rules of its
``sky_image`` (the primary wins; a legacy value on another start is
recovered; an edit clears the others). :func:`sky_settings` turns those
properties into validated settings and :func:`apply` writes them, with a
renderer's own configuration (:data:`HD_HIGH`), onto its ``Environment``. Classic
keeps the properties with the map and never reads them.

The frame's exposure is not authored. :func:`base_exposure` derives it from
the light the map's sky, sun, ambient and lamps put on a typical surface;
the PlayerStart's exposure compensation (in EV) is applied on top.
"""

from __future__ import annotations

import math

#: The two renderers, by the names people use, and their registry names.
CLASSIC = "Classic"
HD_HIGH_NAME = "HD_High"
RENDERERS = (CLASSIC, HD_HIGH_NAME)
REGISTRY_NAME = {CLASSIC: "Forward", HD_HIGH_NAME: "HD_High"}
RENDERER_LABELS = {
    CLASSIC: "Classic (the original Fio renderer)",
    HD_HIGH_NAME: "HD_High (physically based: sun, sky, HDR post-processing)",
}
DEFAULT_RENDERER = CLASSIC


def normalise_renderer(value, default=DEFAULT_RENDERER):
    """``Classic`` or ``HD_High`` from a setting's text (any case; the
    registry name ``Forward`` is Classic)."""
    text = str(value or "").strip().lower().replace("-", "_")
    if text in ("classic", "forward"):
        return CLASSIC
    if text in ("hd_high", "hd", "hdhigh"):
        return HD_HIGH_NAME
    return default


def renderer_from(config, *, lowpower=False):
    """The renderer ``[Renderer] renderer`` asks for. Low-power ARM mode
    keeps its own reduced shaders, which HD_High cannot use: Classic."""
    if lowpower or config is None:
        return CLASSIC
    try:
        value = config.get("Renderer", "renderer", fallback=DEFAULT_RENDERER)
    except Exception:
        value = DEFAULT_RENDERER
    return normalise_renderer(value)


def display_name(registry_name):
    """What a person calls the renderer registered as *registry_name*."""
    for name, key in REGISTRY_NAME.items():
        if key == registry_name or name == registry_name:
            return name
    return str(registry_name)


#: What HD_High is: the size of the sun's shadow map, the sample counts of
#: its ambient occlusion and reflection passes, and which effects are on.
#: One configuration -- there are two renderers, Classic and HD_High, not a
#: ladder of tiers -- kept in one table so it is easy to retune.
HD_HIGH = dict(shadow_size=2048, ambient_occlusion=1.0, bloom=1.0, fxaa=True,
               reflections=True, parallax=1.0, sun_shafts=1.0, cloud_shadows=1.0,
               ao_taps=12, ssr_steps=32)


# ---------------------------------------------------------------------------
# The PlayerStart's HD sky properties
# ---------------------------------------------------------------------------

SKY_NONE, SKY_PANORAMA, SKY_PROCEDURAL = "none", "panorama", "procedural"
SKY_MODES = (SKY_NONE, SKY_PANORAMA, SKY_PROCEDURAL)
SKY_LABELS = {SKY_NONE: "None (indoor)", SKY_PANORAMA: "Panorama (the sky image)",
              SKY_PROCEDURAL: "Procedural (sun, atmosphere, clouds)"}

#: Which sky modes show a group of fields in the property editor.
_ALL = SKY_MODES
_SUN = (SKY_PANORAMA, SKY_PROCEDURAL)
_PROC = (SKY_PROCEDURAL,)


class Field:
    """One ``hd_*`` PlayerStart property: its key, kind (``choice``, ``bool``,
    ``float``, ``colour``: an ``[r, g, b]`` of 0..255, or blank), default,
    label, range, the group it is shown in and the sky modes it applies to.
    ``needs_sun`` fields are shown only while the sun is on."""

    __slots__ = ("key", "kind", "default", "label", "low", "high", "step",
                 "group", "modes", "needs_sun", "tip")

    def __init__(self, key, kind, default, label, low=0.0, high=1.0, step=0.01,
                 group="", modes=_ALL, needs_sun=False, tip=""):
        self.key, self.kind, self.default, self.label = key, kind, default, label
        self.low, self.high, self.step = low, high, step
        self.group, self.modes, self.needs_sun, self.tip = group, modes, needs_sun, tip


#: Defaults are those of Levitate's ``Environment``. ``None`` as a default is
#: "decided from the rest" (see :func:`sky_settings`).
FIELDS = (
    Field("hd_sky", "choice", None, "Sky", group="Sky",
          tip="What lights an HD frame from above. Default: Panorama when the "
              "map has a sky image, otherwise None."),
    # -- the sun ------------------------------------------------------------
    Field("hd_sun", "bool", None, "Sun", group="Sun", modes=_SUN,
          tip="A directional sun with a shadow map. On by default for a "
              "procedural sky, off for a panorama (which has no known sun)."),
    Field("hd_sun_azimuth", "float", 135.0, "Azimuth", 0.0, 360.0, 1.0, "Sun", _SUN, True,
          "Compass bearing of the sun in degrees: 0 is +Z, 90 is +X."),
    Field("hd_sun_elevation", "float", 42.0, "Elevation", 1.0, 89.5, 0.5, "Sun", _SUN, True,
          "Height of the sun above the horizon, in degrees."),
    Field("hd_sun_color", "colour", None, "Colour", group="Sun", modes=_SUN, needs_sun=True,
          tip="Colour of sunlight. Blank takes it from the atmosphere."),
    Field("hd_sun_intensity", "float", 3.4, "Intensity", 0.0, 10.0, 0.1, "Sun", _SUN, True),
    Field("hd_shadow_strength", "float", 1.0, "Shadow strength", 0.0, 1.0, 0.05, "Sun",
          _SUN, True),
    # -- the procedural sky -------------------------------------------------
    Field("hd_turbidity", "float", 3.5, "Turbidity", 1.0, 20.0, 0.1, "Atmosphere", _PROC),
    Field("hd_rayleigh", "float", 1.4, "Rayleigh", 0.0, 4.0, 0.05, "Atmosphere", _PROC),
    Field("hd_mie", "float", 0.005, "Mie", 0.0, 0.1, 0.001, "Atmosphere", _PROC),
    Field("hd_mie_g", "float", 0.8, "Mie direction", 0.0, 0.999, 0.01, "Atmosphere", _PROC),
    Field("hd_cloud_cover", "float", 0.42, "Cloud cover", 0.0, 1.0, 0.01, "Clouds", _PROC),
    Field("hd_cloud_softness", "float", 0.22, "Cloud softness", 0.01, 1.0, 0.01, "Clouds",
          _PROC),
    Field("hd_wind_x", "float", 26.0, "Wind X", -200.0, 200.0, 1.0, "Clouds", _PROC,
          tip="Cloud drift along X, world units per second."),
    Field("hd_wind_z", "float", 11.0, "Wind Z", -200.0, 200.0, 1.0, "Clouds", _PROC,
          tip="Cloud drift along Z, world units per second."),
    Field("hd_cloud_shadows", "float", 0.55, "Cloud shadows", 0.0, 1.0, 0.05, "Clouds", _PROC,
          tip="How dark the clouds' moving shadows are on the ground (High and Ultra)."),
    Field("hd_ground_color", "colour", (0.20, 0.19, 0.17), "Ground colour", group="Atmosphere",
          modes=_PROC, tip="The ground's colour, for the light it bounces back up."),
    # -- haze ---------------------------------------------------------------
    Field("hd_haze", "float", 0.00003, "Haze density", 0.0, 0.01, 0.00001, "Haze", _SUN,
          tip="Aerial perspective: extinction per world unit. A map with "
              "distance fog switched on uses its fog instead."),
    Field("hd_haze_falloff", "float", 0.0008, "Haze falloff", 0.0, 0.05, 0.0001, "Haze", _SUN,
          tip="How fast the haze thins with height."),
    # -- the look -----------------------------------------------------------
    Field("hd_exposure", "float", 0.0, "Exposure (EV)", -4.0, 4.0, 0.1, "Look",
          tip="Exposure compensation in stops, on top of the exposure Fio "
              "works out from the map's sky and lights."),
    Field("hd_bloom", "float", 0.10, "Bloom", 0.0, 1.0, 0.01, "Look"),
    Field("hd_ao", "float", 1.0, "Ambient occlusion", 0.0, 2.0, 0.05, "Look"),
    Field("hd_tint", "colour", (1.0, 1.0, 1.0), "Tint", group="Look",
          tip="White balance: a colour multiplied in before tone mapping."),
    Field("hd_saturation", "float", 1.04, "Saturation", 0.0, 2.0, 0.01, "Look"),
    Field("hd_contrast", "float", 1.05, "Contrast", 0.5, 2.0, 0.01, "Look"),
    Field("hd_vignette", "float", 0.22, "Vignette", 0.0, 1.0, 0.01, "Look"),
    Field("hd_grain", "float", 0.012, "Film grain", 0.0, 0.2, 0.002, "Look"),
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}
KEYS = tuple(field.key for field in FIELDS)


def is_hd_key(key):
    return key in FIELD_BY_KEY


def _present(value):
    return value is not None and not (isinstance(value, str) and not value.strip())


def resolve_properties(primary, others=()):
    """The map's effective ``hd_*`` values, as stored: each key from the
    *primary* PlayerStart's properties where it has it, else from the first
    of *others* (the other starts' properties, earliest created first) that
    does. Reads only; changes nothing."""
    resolved = {}
    for key in KEYS:
        for properties in ((primary,) if primary is not None else ()) + tuple(others):
            value = properties.get(key)
            if _present(value):
                resolved[key] = value
                break
    return resolved


def canonicalise_properties(primary, others, resolved=None):
    """Store the effective values on *primary* and clear every ``hd_*`` key
    from *others* (property dicts). Returns the dicts that changed."""
    if resolved is None:
        resolved = resolve_properties(primary, others)
    changed = []
    if primary is not None:
        touched = False
        for key in KEYS:
            if key in resolved:
                if primary.get(key) != resolved[key]:
                    primary[key] = resolved[key]
                    touched = True
            elif key in primary:
                del primary[key]
                touched = True
        if touched:
            changed.append(primary)
    for properties in others:
        if properties is primary:
            continue
        if any(key in properties for key in KEYS):
            for key in KEYS:
                properties.pop(key, None)
            changed.append(properties)
    return changed


def _number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(field.default)
    if not math.isfinite(number):
        return float(field.default)
    return max(field.low, min(field.high, number))


def _truth(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return default


def _colour(value):
    """An authored ``[r, g, b]`` (0..255) or ``#rrggbb`` as display 0..1
    floats, or None."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            except ValueError:
                return None
        return None
    try:
        r, g, b = (float(v) for v in value[:3])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (r, g, b)):
        return None
    return tuple(max(0.0, min(255.0, v)) / 255.0 for v in (r, g, b))


def to_linear(colour):
    """Display (gamma 2.2) colour to linear light, as the shaders decode it."""
    return tuple(max(0.0, float(c)) ** 2.2 for c in colour)


def sky_settings(properties, sky_image=""):
    """Validated HD sky settings from stored ``hd_*`` *properties* (as
    :func:`resolve_properties` returns) and the map's panorama path.

    Keys are the property names without ``hd_``; colours are linear 0..1
    tuples (``sun_color`` None: from the atmosphere). ``sky`` defaults to
    ``panorama`` when the map has a sky image and ``none`` otherwise, and a
    panorama sky without an image is ``none``; ``sun`` defaults on for a
    procedural sky and off otherwise, and is always off for ``none``."""
    properties = properties or {}
    has_image = bool(str(sky_image or "").strip())
    sky = str(properties.get("hd_sky") or "").strip().lower()
    if sky not in SKY_MODES:
        sky = SKY_PANORAMA if has_image else SKY_NONE
    if sky == SKY_PANORAMA and not has_image:
        sky = SKY_NONE
    settings = {"sky": sky}
    for field in FIELDS:
        name = field.key[3:]
        value = properties.get(field.key)
        if field.key == "hd_sky":
            continue
        if field.kind == "bool":
            default = sky == SKY_PROCEDURAL if field.default is None else bool(field.default)
            settings[name] = _truth(value, default) if _present(value) else default
        elif field.kind == "colour":
            colour = _colour(value) if _present(value) else None
            if colour is not None:
                settings[name] = to_linear(colour)
            else:
                settings[name] = (None if field.default is None
                                  else tuple(float(c) for c in field.default))
        else:
            settings[name] = (_number(value, field) if _present(value)
                              else float(field.default))
    if sky == SKY_NONE:
        settings["sun"] = False
    return settings


def settings_key(settings):
    """A hashable form of *settings*, for telling when they changed."""
    return tuple(sorted((k, tuple(v) if isinstance(v, (list, tuple)) else v)
                        for k, v in settings.items()))


#: Values that are decided here rather than authored. A lamp's light on a
#: white surface facing it at no distance is ``intensity * point_light_gain``
#: in the same unit as ``sun_intensity`` (the sun's light on a surface
#: facing it). 3.0 puts a default Fio lamp (intensity 1..2) a little under
#: the default sun (3.4), which is what a lamp beside a sunlit doorway should
#: be. Indoors (no sun, no sky) the gain cancels out of the picture: the
#: exposure is derived from the same lamps (see :func:`base_exposure`), so
#: any gain gives the same frame; it only sets lamps against sun, sky and
#: emissive surfaces.
POINT_LIGHT_GAIN = 3.0
#: A panorama is a display picture: its brightest cloud is 1. As a light it
#: stands for daylight, which is far brighter than any lamp, so its linear
#: colour is multiplied by this: an overcast panorama then lights an open
#: floor about as strongly as a lamp at full level lights the wall beside
#: it, a clear one more so, and a night sky hardly at all. Seen from a
#: lamp-lit room the open part of the map is bright, and the exposure comes
#: down as the view walks out into it.
PANORAMA_SKY_GAIN = 4.0
#: Terrain under a sky that has no sun of its own is lit by the fixed sun
#: classic Fio has always lit terrain with (surface.py); this is its light
#: and height, for the exposure.
TERRAIN_SUN_RADIANCE = (3.6, 3.15, 2.45)
TERRAIN_SUN_ELEVATION = 54.0
#: Emissive surfaces glow at this many times their colour, so that they stay
#: brighter than the lit surfaces around them and feed the bloom.
EMISSIVE_GAIN = 5.0


def apply(environment, settings, preset=None):
    """Write *settings* (from :func:`sky_settings`) onto HD_High's
    ``Environment``, with what HD_High switches on (:data:`HD_HIGH`; *preset*
    replaces it, for measuring an effect's cost). One assignment per field, so a frame in
    progress never sees half a look. Returns whether the environment is on."""
    preset = HD_HIGH if preset is None else preset
    sky = settings["sky"]
    sun = bool(settings["sun"]) and sky != SKY_NONE
    procedural = sky == SKY_PROCEDURAL
    values = dict(
        sky_mode=sky,
        sun_enabled=sun,
        sun_azimuth=settings["sun_azimuth"],
        sun_elevation=settings["sun_elevation"],
        sun_color=settings["sun_color"],
        sun_intensity=settings["sun_intensity"] if sun else 0.0,
        shadow_strength=settings["shadow_strength"] if sun else 0.0,
        shadow_size=preset["shadow_size"],
        turbidity=settings["turbidity"],
        rayleigh=settings["rayleigh"],
        mie=settings["mie"],
        mie_g=settings["mie_g"],
        cloud_cover=settings["cloud_cover"] if procedural else 0.0,
        cloud_softness=settings["cloud_softness"],
        wind=(settings["wind_x"], settings["wind_z"]),
        cloud_shadows=(settings["cloud_shadows"] * preset["cloud_shadows"]
                       if procedural and sun else 0.0),
        ground_color=tuple(settings["ground_color"]),
        haze=settings["haze"] if sky != SKY_NONE else 0.0,
        haze_falloff=settings["haze_falloff"],
        exposure_compensation=settings["exposure"],
        bloom=settings["bloom"] * preset["bloom"],
        ambient_occlusion=settings["ao"] * preset["ambient_occlusion"],
        ao_taps=int(preset["ao_taps"]),
        reflections=bool(preset["reflections"]),
        ssr_steps=int(preset["ssr_steps"]),
        parallax=float(preset["parallax"]),
        sun_shafts=(0.35 * preset["sun_shafts"]) if sun else 0.0,
        fxaa=bool(preset["fxaa"]),
        tint=tuple(settings["tint"]),
        saturation=settings["saturation"],
        contrast=settings["contrast"],
        vignette=settings["vignette"],
        grain=settings["grain"],
        point_light_gain=POINT_LIGHT_GAIN,
        emissive_gain=EMISSIVE_GAIN,
        enabled=True,
    )
    for name, value in values.items():
        setattr(environment, name, value)
    return True


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------

#: Linear albedo of "a typical surface": what a mid-toned texture decodes to.
TYPICAL_ALBEDO = 0.2
#: The share of a sunlit world's surfaces the sun actually reaches, facing
#: it obliquely on average.
SUN_SHARE = 0.5
#: A lamp's light where the map's author looked at it: classic Fio shows a
#: texture at its own brightness where ``intensity * falloff * N.L`` is 1,
#: and maps are lit to look right there, close to their lamps. So the
#: exposure is keyed on a surface three quarters of the way up a lamp's
#: ``(1 - d / radius) ** 2`` falloff and mostly facing it -- not on the
#: average of the lamp's whole ball, most of which is nearly dark.
LAMP_KEY_FALLOFF = 0.75
LAMP_FACING = 0.8
#: Exposure never leaves this range, whatever the map.
EXPOSURE_RANGE = (0.15, 6.0)
#: With nothing to measure under a sky (which cannot really happen).
INDOOR_DEFAULT_EXPOSURE = 1.0
#: Display-linear value a typical surface should end on.
MID_GREY = 0.18


def luminance(colour):
    return 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]


#: White point of the tone curve: the scene value that reaches display white.
TONE_WHITE = 4.0


def tone(x):
    """The compose pass's tone curve (extended Reinhard) for one channel.

    Levitate finished its frames with an ACES fit, whose toe pulls dark
    values down hard. Fio's maps are lit low on purpose -- most of a
    corridor sits in that toe -- and came out far darker than the classic
    renderer shows them. This curve is a straight line through the shadows
    and mid-tones (a dark wall stays as dark as it was lit, no darker) and
    only rolls the highlights off toward :data:`TONE_WHITE`."""
    x = max(0.0, float(x))
    return min(1.0, x * (1.0 + x / (TONE_WHITE * TONE_WHITE)) / (1.0 + x))


def scene_value_for(display_linear):
    """The linear scene value the tone curve maps to *display_linear*."""
    low, high = 0.0, 64.0
    for _ in range(60):
        middle = (low + high) * 0.5
        if tone(middle) < display_linear:
            low = middle
        else:
            high = middle
    return (low + high) * 0.5


#: The scene-linear value that tone-maps to mid grey.
KEY_VALUE = scene_value_for(MID_GREY)


def lamp_light(lamps, gain=POINT_LIGHT_GAIN):
    """The light a map's lamps put on an average surface in their reach.
    *lamps* is an iterable of ``(intensity, radius, linear rgb)``; lamps
    count by the floor they cover (radius squared)."""
    total = weight = 0.0
    for intensity, radius, colour in lamps:
        radius = float(radius)
        if radius <= 0.0 or intensity <= 0.0:
            continue
        area = radius * radius
        # The shaders decode a lamp's level like a colour (surface.py).
        total += area * (float(intensity) * LAMP_KEY_FALLOFF) ** 2.2 * luminance(colour)
        weight += area
    if weight <= 0.0:
        return 0.0
    return (total / weight) * gain * LAMP_FACING


def scene_light(*, sky_irradiance=(0.0, 0.0, 0.0), sun_radiance=(0.0, 0.0, 0.0),
                sun_elevation=0.0, ambient=(0.0, 0.0, 0.0), lamps=()):
    """Light arriving on a typical surface (linear, the shaders' unit): the
    sky's irradiance on an upward surface, the sun's share, the map's ambient
    colour (display 0..1) and its lamps."""
    sun = luminance(sun_radiance) * max(0.0, math.sin(math.radians(sun_elevation))) * SUN_SHARE
    return (luminance(sky_irradiance) + sun + luminance(to_linear(ambient))
            + lamp_light(lamps))


def classic_exposure(gain=POINT_LIGHT_GAIN):
    """The exposure at which an HD frame is as bright as classic Fio: a
    surface a lamp lights fully (``intensity * falloff * N.L`` = 1) shows
    its texture at the texture's own brightness, which is what the classic
    shader's ``texture * light`` does and what every map was lit against."""
    # The tone curve passes about this share of a mid-tone through.
    return 1.0 / (gain * (MID_GREY / KEY_VALUE))


#: Indoors (no sky) the exposure may leave classic brightness by this factor
#: either way and no further: a map lit dimly on purpose stays dim.
INDOOR_LATITUDE = 1.25


def base_exposure(light, *, indoor=False):
    """The exposure that puts a typical surface under *light* (from
    :func:`scene_light`) on mid grey after tone mapping, clamped.

    *indoor* (a map with no sky) holds it near :func:`classic_exposure`:
    such a map's look is entirely its author's lamps, so the frame stays as
    bright or as dark as classic Fio shows it, give or take
    :data:`INDOOR_LATITUDE`. With no light to measure it is exactly that."""
    reflected = TYPICAL_ALBEDO * float(light)
    measurable = math.isfinite(reflected) and reflected > 1e-6
    if indoor:
        classic = classic_exposure()
        if not measurable:
            return classic
        return max(classic / INDOOR_LATITUDE,
                   min(classic * INDOOR_LATITUDE, KEY_VALUE / reflected))
    if not measurable:
        return INDOOR_DEFAULT_EXPOSURE
    low, high = EXPOSURE_RANGE
    return max(low, min(high, KEY_VALUE / reflected))


def final_exposure(base, compensation_ev):
    """Base exposure times ``2 ** EV``."""
    return float(base) * (2.0 ** float(compensation_ev))


def panorama_irradiance(pixels):
    """Sky light from an equirectangular panorama, measured on the CPU.

    *pixels* is an ``(h, w, 3+)`` uint8 array, top row first (straight up).
    Returns ``(up, side, down, mean)``: the cosine-weighted irradiance
    (linear rgb, as a fraction of a uniform white sky) on surfaces facing up,
    sideways and down, and the panorama's solid-angle-weighted mean."""
    import numpy as np
    image = np.asarray(pixels)[..., :3]
    height, width = image.shape[:2]
    # 16 x 8 texels are plenty for a cosine lobe, as the GPU path's mip was.
    rows = np.linspace(0, height, 9).astype(int)
    cols = np.linspace(0, width, 17).astype(int)
    linear = (image.astype(np.float64) / 255.0) ** 2.2
    texels = np.empty((8, 16, 3))
    for j in range(8):
        for i in range(16):
            block = linear[rows[j]:max(rows[j + 1], rows[j] + 1),
                           cols[i]:max(cols[i + 1], cols[i] + 1)]
            texels[j, i] = block.reshape(-1, 3).mean(axis=0)
    return irradiance_from_texels(texels)


def irradiance_from_texels(texels):
    """``(up, side, down, mean)`` from an ``(8, 16, 3)`` linear equirect."""
    import numpy as np
    texels = np.asarray(texels, dtype=np.float64)
    rows, cols = texels.shape[:2]
    v = (np.arange(rows) + 0.5) / rows
    u = (np.arange(cols) + 0.5) / cols
    elevation = (0.5 - v)[:, None] * math.pi
    phi = (u - 0.5)[None, :] * 2.0 * math.pi
    direction = np.stack([np.cos(elevation) * np.sin(phi),
                          np.sin(elevation) * np.ones_like(phi),
                          -np.cos(elevation) * np.cos(phi)], axis=-1)
    solid = np.cos(elevation) * np.ones_like(phi)

    def irradiance(normal):
        cosine = np.clip(direction @ np.asarray(normal, dtype=np.float64), 0.0, None)
        weight = cosine * solid
        return (texels * weight[..., None]).sum(axis=(0, 1)) / max(weight.sum(), 1e-9)

    up = irradiance((0.0, 1.0, 0.0))
    side = sum(irradiance(n) for n in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))) / 4.0
    down = irradiance((0.0, -1.0, 0.0))
    mean = (texels * solid[..., None]).sum(axis=(0, 1)) / max(solid.sum(), 1e-9)
    return (tuple(float(c) for c in up), tuple(float(c) for c in side),
            tuple(float(c) for c in down), tuple(float(c) for c in mean))


# ---------------------------------------------------------------------------
# The procedural sky, on the CPU
# ---------------------------------------------------------------------------

def procedural_sky_texels(*, sun_direction, sun_radiance, turbidity, rayleigh, mie, mie_g,
                          sky_intensity=1.0, cloud_cover=0.0, cloud_softness=0.22,
                          cloud_brightness=1.0, ground=(0.0, 0.0, 0.0), rows=8, cols=16):
    """The procedural sky as a small linear equirectangular image
    (``(rows, cols, 3)``), computed with the sky shader's own formulas
    (environment.SKY_FRAG): Preetham's daylight, the ground below the
    horizon, and the cloud layer as its average cover (the clouds' pattern
    does not matter to a cosine lobe). This is what the sky light is
    measured from, with no read-back from the GPU."""
    import numpy as np
    v = (np.arange(rows) + 0.5) / rows
    u = (np.arange(cols) + 0.5) / cols
    elevation = (0.5 - v)[:, None] * math.pi
    phi = (u - 0.5)[None, :] * 2.0 * math.pi
    direction = np.stack([np.cos(elevation) * np.sin(phi),
                          np.sin(elevation) * np.ones_like(phi),
                          -np.cos(elevation) * np.cos(phi)], axis=-1)
    sun = np.asarray(sun_direction, dtype=np.float64)
    sun_rgb = np.asarray(sun_radiance, dtype=np.float64)
    up = np.maximum(direction[..., 1], 0.0)
    above = direction.copy()
    above[..., 1] = up
    above /= np.linalg.norm(above, axis=-1, keepdims=True)

    e_cut = 1.6110731556870734
    total_rayleigh = np.array([5.804542996261093e-6, 1.3562911419845635e-5,
                               3.0265902468824876e-5])
    mie_const = np.array([1.8399918514433978e14, 2.7798023919660528e14,
                          4.0790479543861094e14])
    sun_y = float(max(-1.0, min(1.0, sun[1])))
    sun_e = 1000.0 * max(0.0, 1.0 - math.exp(-((e_cut - math.acos(sun_y)) / 1.5)))
    sunfade = 1.0 - max(0.0, min(1.0, 1.0 - math.exp(sun_y)))
    beta_r = total_rayleigh * (rayleigh - (1.0 - sunfade))
    beta_m = 0.434 * (0.2 * turbidity) * 10e-18 * mie_const * mie
    zenith = np.arccos(np.maximum(above[..., 1], 0.0))
    inverse = 1.0 / (np.cos(zenith) + 0.15 * np.power(
        np.maximum(93.885 - np.degrees(zenith), 1e-6), -1.253))
    fex = np.exp(-(beta_r * 8.4e3 * inverse[..., None] + beta_m * 1.25e3 * inverse[..., None]))
    cos_theta = above @ sun
    half = cos_theta * 0.5 + 0.5
    beta_r_theta = beta_r * (3.0 / (16.0 * math.pi)) * (1.0 + half * half)[..., None]
    g = mie_g
    beta_m_theta = beta_m * (1.0 / (4.0 * math.pi)) * (
        (1.0 - g * g) / np.power(np.maximum(1.0 - 2.0 * g * cos_theta + g * g, 1e-9), 1.5)
    )[..., None]
    scatter = sun_e * ((beta_r_theta + beta_m_theta) / np.maximum(beta_r + beta_m, 1e-30))
    lin = np.power(np.maximum(scatter * (1.0 - fex), 0.0), 1.5)
    blend = max(0.0, min(1.0, (1.0 - sun_y) ** 5.0))
    lin = lin * (1.0 + (np.power(np.maximum(scatter * fex, 0.0), 0.5) - 1.0) * blend)
    colour = (lin + 0.1 * fex) * 0.04 + np.array([0.0, 0.0003, 0.00075])
    sky = np.power(np.maximum(colour, 0.0), 1.0 / (1.2 + 1.2 * sunfade)) * sky_intensity

    if cloud_cover > 0.0:
        # Share of the layer that is cloud: the noise sits around 0.5 with a
        # spread of about 0.13; cover moves the threshold through it.
        threshold = 0.80 + (0.22 - 0.80) * cloud_cover + 0.5 * max(cloud_softness, 0.01)
        share = 0.5 * math.erfc((threshold - 0.5) / (0.13 * math.sqrt(2.0)))
        grey = (sky @ np.array([0.2126, 0.7152, 0.0722]))[..., None]
        shade = (sky + (grey - sky) * 0.55) * 0.62 + sun_rgb * 0.035
        bright = sun_rgb * 0.34 + (sky + (grey - sky) * 0.4) * 0.55
        cloud = (shade + (bright - shade) * 0.6) * cloud_brightness
        edge = np.clip((direction[..., 1] - 0.004) / (0.16 - 0.004), 0.0, 1.0)
        edge = edge * edge * (3.0 - 2.0 * edge)
        cover = (share * 0.75 * edge)[..., None]
        sky = sky + (cloud - sky) * cover

    below = np.clip(-direction[..., 1] / 0.12, 0.0, 1.0)
    below = (below * below * (3.0 - 2.0 * below))[..., None]
    return sky + (np.asarray(ground, dtype=np.float64) - sky) * below
