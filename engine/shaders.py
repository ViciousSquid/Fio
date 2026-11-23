import os
import platform
import re
import sys



# ==============================================================================
# PLATFORM: which lighting shader variant this machine should run
# ------------------------------------------------------------------------------
# The ``*_arm`` variants trade light capacity and per-fragment precision for a
# smaller uniform footprint and less shader work. That is the right trade on
# low-power hardware and the wrong one everywhere else, so what this answers is
# "is this a low-power part?" — *not* "is this ARM?". The two are not the same
# question and never were: a Surface Pro X's SQ3 (a Snapdragon 8cx) wants the
# cheap shaders, while Apple Silicon and a Snapdragon X Elite have desktop-class
# GPUs and want the full ones, and all three are aarch64.
#
# The rule is therefore "ARM, minus the parts known to be fast". A new fast ARM
# part that is not yet listed gets the conservative treatment rather than a
# broken one, and anybody can override the guess outright in settings.ini.
#
# One implementation, used by both the renderer and the Settings window — they
# used to detect this separately and could disagree about the same machine.
# ==============================================================================

#: Substrings of a CPU's model name that mark it as *not* low-power, matched
#: case-insensitively. Snapdragon X Elite/Plus report as "Snapdragon(R) X Elite
#: - X1E..." / "X Plus - X1P..."; Oryon is their core. Ampere/Graviton/Neoverse
#: are server parts that turn up in CI and remote desktops.
_FAST_ARM_MARKERS = (
    'x elite', 'x1e', 'x plus', 'x1p', 'oryon',
    'ampere', 'graviton', 'neoverse',
)


def _arm_cpu_name():
    """The CPU's model name, as specifically as this OS will give it.

    Windows on ARM reports a useless generic string in ``PROCESSOR_IDENTIFIER``
    ("ARMv8 (64-bit) Family 8 Model 1..."), so the friendly name is read from
    the registry where the part is actually identified. Best-effort throughout:
    an empty string just means the caller falls back to the conservative guess.
    """
    if sys.platform == 'win32':
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            try:
                name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            finally:
                winreg.CloseKey(key)
            if name:
                return str(name)
        except Exception:
            pass
        return os.environ.get('PROCESSOR_IDENTIFIER', '')

    if sys.platform.startswith('linux'):
        try:
            with open('/proc/cpuinfo', 'r') as handle:
                fields = []
                for line in handle:
                    label = line.split(':', 1)
                    if len(label) == 2 and label[0].strip().lower() in (
                            'model name', 'hardware', 'cpu part', 'cpu implementer'):
                        fields.append(label[1].strip())
            if fields:
                return ' '.join(fields)
        except OSError:
            pass

    try:
        return platform.processor() or ''
    except Exception:
        return ''


def detect_low_power_arm():
    """``(is_low_power, reason)`` for this machine.

    ``reason`` is a short human-readable phrase the Settings window shows.
    """
    override = os.environ.get('FIO_ARM_MODE', '').strip().lower()
    if override in ('1', 'true', 'yes', 'on'):
        return True, "forced by FIO_ARM_MODE"
    if override in ('0', 'false', 'no', 'off'):
        return False, "disabled by FIO_ARM_MODE"

    machine = platform.machine().lower()
    is_arm_cpu = 'arm' in machine or 'aarch' in machine
    if not is_arm_cpu and sys.platform == 'win32':
        # Windows lies about the architecture to an emulated x64 process.
        is_arm_cpu = (
            os.environ.get('PROCESSOR_ARCHITECTURE', '').upper() == 'ARM64'
            or os.environ.get('PROCESSOR_ARCHITEW6432', '').upper() == 'ARM64'
        )

    if not is_arm_cpu:
        return False, "x64/x86 processor detected"

    if sys.platform == 'darwin':
        return False, "Apple Silicon detected - desktop-class GPU"

    name = _arm_cpu_name().lower()
    for marker in _FAST_ARM_MARKERS:
        if marker in name:
            return False, "high-performance ARM detected - desktop-class GPU"

    return True, "low-power ARM detected"


# ==============================================================================
# SHADOW MAPPING (depth cube-map, omnidirectional point-light shadows)
# ------------------------------------------------------------------------------
# Shared GLSL injected into every lighting fragment shader that *receives*
# shadows.  A shadow-casting point light renders scene depth into a cube-map
# (linear distance / far_plane stored per texel); receivers reconstruct the
# distance and compare it against the fragment's distance to the light.
#
# NOTE: In GLSL 3.30 a sampler array may only be indexed with a *constant*
# expression, so the cube lookup uses an explicit if-ladder instead of
# dynamic indexing (which is only legal from GLSL 4.00 onwards).  Keeping the
# indices constant makes the shaders portable across desktop GL 3.3 drivers.
# ==============================================================================
MAX_SHADOW_LIGHTS = 8

# A shadow-casting Light's default filter (engine.constants).
from engine.constants import SHADOW_BIAS_DEFAULT, SHADOW_SOFTNESS_DEFAULT  # noqa: E402

# ==============================================================================
# DYNAMIC LIGHT CAPACITY
# ------------------------------------------------------------------------------
# How many point lights a lighting shader can hold. This is the *only* place the
# number is written down: the shader sources below are built from it and
# `ForwardRenderer.MAX_LIGHTS` reads it, because the renderer's budget and the
# shader's array have to be the same number.
#
# They used not to be. The renderer uploaded up to 32 lights and set
# `active_lights` to that count, while `lit.frag` and `textured.frag` declared
# `lights[8]` and looped to `active_lights` with no bound — so any scene with
# more than eight lights had the shader read past the end of the array, which is
# undefined behaviour, and 24 of the "32" lights never worked in the first
# place. Every loop over `active_lights` is now clamped to its own array as
# well, so a mismatch can never be more than lights quietly not contributing.
MAX_LIGHTS = 128

# The Android/GLES player keeps the previous 64. Its shared light block packs
# each light into three vec4s, and some Android GLES translators count block
# members against the default-uniform budget, whose ES 3.0 minimum is 224
# vectors: 64 lights (192 vectors) fit, 128 (384) would not link there.
MAX_LIGHTS_PLAYER = 64

# The ARM/low-power variants keep a smaller array deliberately. Uniform storage
# is the scarce resource on those GPUs, and the per-fragment loop runs
# `active_lights` times either way, so a lower cap costs a dense scene some of
# its lights and costs an ordinary one nothing at all.
MAX_LIGHTS_ARM = 16

# Water and terrain light themselves from a handful of the nearest lights rather
# than the whole set; their shaders are sized for that on purpose and the
# renderer clamps `active_lights` to match (see ForwardRenderer._shader_light_cap).
MAX_LIGHTS_WATER = 8
MAX_LIGHTS_TERRAIN = 8
MAX_TERRAIN_STAMPS = 32
MAX_TERRAIN_STAMP_TEXTURES = 16

# GL 3.3 UBO binding used by every lighting shader.  The binding is assigned
# from Python with glUniformBlockBinding rather than using a GLSL 4.2-style
# explicit binding qualifier, keeping this portable to the engine's GL 3.3
# target.
LIGHT_UBO_BINDING = 2

_LIGHT_DECL_RE = re.compile(
    r"struct\s+Light\s*\{.*?\};\s*uniform\s+Light\s+lights\s*\[\s*(\d+)\s*\]\s*;",
    re.DOTALL,
)


#: A light whose source has a size is not a point. A point light is at its
#: brightest on whatever is nearest it -- a spot on the wall behind a neon
#: tube -- where a tube lights that wall evenly and faintly along its length.
#: So such a light fades in over its source's half-size: what is right next
#: to it gets little, what is a tube-length away gets all of it. Size 0 (every
#: Light, Effect and decal) is a point, exactly as before.
LIGHT_SOURCE_SIZE_GLSL = """
float fioSourceFade(float size, highp vec3 toLight) {
    if (size <= 0.0) return 1.0;
    return smoothstep(0.0, size, length(toLight));
}
"""


#: The texture unit of the shared light-cookie array (engine.light_cookies).
#: Above every unit a fragment stage already uses (0..15: diffuse, terrain,
#: shadow cube-maps, heights, paint, geometry transforms, panorama); GL 3.3
#: guarantees 48 combined units and GLES 3.0 32, and the fragment-stage
#: sampler count stays within 16 because only one more sampler is added.
COOKIE_TEXTURE_UNIT = 16

#: How many mip levels blurrier a cookie is at the light's radius than at its
#: centre. Linear in distance / radius: the near image is sharp, the far one
#: soft but still recognisable (a level is half the resolution).
COOKIE_SOFTEN_LODS = 3.0

#: Projected-image lighting (engine.light_cookies). ``fioLightColor`` stands
#: in for a light's colour in every light loop. For an ordinary light --
#: ``indices.z`` 0, the dynamically uniform branch every fragment of the draw
#: takes together -- it returns the colour itself and samples nothing.
#:
#: A cookie light reads its image along the direction from the light's centre
#: to the fragment, with an octahedral map (centre = down, corners = up), so
#: the image stretches with distance as a projection does. The level of
#: detail is chosen explicitly -- the light loop is non-uniform control flow,
#: where implicit derivatives are undefined, and the octahedral fold would
#: give a seam if they were taken there: the pixel's world size (taken once
#: at the top of main, in uniform flow) over the texel's world size at that
#: distance picks the level that does not alias, and a distance bias of up to
#: COOKIE_SOFTEN_LODS levels softens it toward the radius. One textureLod:
#: the blur is the hardware's trilinear filter over the mip chain.
LIGHT_COOKIE_GLSL = """
uniform mediump sampler2DArray fioCookies;
highp float fioCookiePixel;
vec2 fioOctahedral(highp vec3 d) {
    d /= (abs(d.x) + abs(d.y) + abs(d.z));
    vec2 p = d.xz;
    if (d.y > 0.0) {
        vec2 s = vec2(p.x >= 0.0 ? 1.0 : -1.0, p.y >= 0.0 ? 1.0 : -1.0);
        p = (1.0 - abs(p.yx)) * s;
    }
    return p * 0.5 + 0.5;
}
vec3 fioLightColor(int i, highp vec3 fragPos) {
    int layer = lights[i].indices.z;
    if (layer <= 0) return lights[i].color.xyz;
    highp vec3 fromLight = fragPos - lights[i].position.xyz;
    highp float dist = max(length(fromLight), 0.001);
    vec2 uv = fioOctahedral(fromLight / dist);
    // An octahedral texel spans about sqrt(4 pi) / size radians.
    highp float texel = dist * 3.5449 / float(textureSize(fioCookies, 0).x);
    float lod = log2(max(fioCookiePixel / texel, 1.0))
              + """ + repr(COOKIE_SOFTEN_LODS) + """ * clamp(dist / max(lights[i].params.y, 1.0), 0.0, 1.0);
    return lights[i].color.xyz * textureLod(fioCookies, vec3(uv, float(layer - 1)), lod).rgb;
}
// A cookie light's own shadow darkens its image but never erases it: its
// visibility is max(1 - shadow, floor), the floor being the light's
// cookie_shadow_floor (params.w). Only this light's shadow is involved; an
// ordinary light's shadow is returned unchanged.
float fioCookieShadow(int i, float shadow) {
    if (lights[i].indices.z <= 0) return shadow;
    return min(shadow, 1.0 - clamp(lights[i].params.w, 0.0, 1.0));
}
"""

_COOKIE_MARKER = "\n// <fio light cookies>\n"
_MAIN_RE = re.compile(r"void\s+main\s*\(\s*(?:void)?\s*\)\s*\{")


#: The cookie shadow floor a Light starts with (``cookie_shadow_floor``).
COOKIE_SHADOW_FLOOR = 0.15

_SHADOW_CALL_RE = re.compile(r"calcPointShadow\(\s*lights\[([^]]+)\]\.shadowIndex\b")


def _floor_cookie_shadows(source):
    """Wrap each light loop's ``calcPointShadow(lights[i].shadowIndex, ...)``
    in ``fioCookieShadow(i, ...)`` (LIGHT_COOKIE_GLSL)."""
    out, pos = [], 0
    for match in _SHADOW_CALL_RE.finditer(source):
        depth, end = 0, match.start() + len("calcPointShadow")
        for end in range(end, len(source)):
            if source[end] == "(":
                depth += 1
            elif source[end] == ")":
                depth -= 1
                if not depth:
                    break
        out.append(source[pos:match.start()])
        out.append("fioCookieShadow(" + match.group(1) + ", "
                   + source[match.start():end + 1] + ")")
        pos = end + 1
    out.append(source[pos:])
    return "".join(out)


def light_ubo_source(source):
    """Rewrite a legacy Light[] fragment shader to the shared std140 UBO.

    The original shader interface is intentionally accepted here so the source
    files remain readable and the same transform applies to loose shaders,
    fallback strings, ARM variants and instanced variants alike.
    """
    if not source or 'uniform Light lights[' not in source:
        return source

    match = _LIGHT_DECL_RE.search(source)
    if match is None:
        return source

    count = int(match.group(1))
    # The name the shader gives the fragment's world position: every light
    # loop takes ``lights[i].position - <that>``. FragPos here; the Android
    # player's adaptation of these shaders renames it first.
    where = re.search(r"lights\[[^]]+\]\.position\s*-\s*([A-Za-z_]\w*)", source)
    fragment_position = where.group(1) if where else "FragPos"
    block = (
        "struct Light {\n"
        "    highp vec4 position;\n"
        "    vec4 color;\n"
        "    vec4 params;       // x=intensity, y=radius, z=source size, w=cookie shadow floor\n"
        "    ivec4 indices;     // x=shadow index, z=cookie layer + 1 (0: none)\n"
        "};\n"
        "layout(std140) uniform FioLightBlock {\n"
        f"    Light lights[{count}];\n"
        "};\n"
        + LIGHT_SOURCE_SIZE_GLSL
        + _COOKIE_MARKER
    )
    result = _LIGHT_DECL_RE.sub(lambda _m: block, source, count=1)

    result = re.sub(r"lights\[([^]]+)\]\.position\b", r"lights[\1].position.xyz", result)
    # A light's colour comes through its cookie (identity without one). The
    # helper is spliced in after these rewrites, so its own reads stay as
    # written; it needs the pixel footprint from the top of main().
    main = _MAIN_RE.search(result)
    if main is not None:
        result = _floor_cookie_shadows(result)
        result = re.sub(r"lights\[([^]]+)\]\.color\b",
                        r"fioLightColor(\1, " + fragment_position + ")", result)
        result = (result[:main.end()]
                  + "\n    fioCookiePixel = max(length(dFdx(" + fragment_position + ")), "
                  "length(dFdy(" + fragment_position + ")));"
                  + result[main.end():])
    else:
        result = re.sub(r"lights\[([^]]+)\]\.color\b", r"lights[\1].color.xyz", result)
    # A light with a source size (an emissive brush: a tube, not a point)
    # fades in over that distance. Every light loop takes the fragment's
    # offset from the light as ``lights[i].position - FragPos``.
    result = re.sub(
        r"lights\[([^]]+)\]\.intensity\b",
        r"(lights[\1].params.x * fioSourceFade("
        r"lights[\1].params.z, lights[\1].position.xyz - " + fragment_position + "))",
        result)
    result = re.sub(r"lights\[([^]]+)\]\.radius\b", r"lights[\1].params.y", result)
    result = re.sub(
        r"lights\[([^]]+)\]\.shadowIndex\b",
        r"int(lights[\1].indices.x)",
        result,
    )
    return result.replace(_COOKIE_MARKER, LIGHT_COOKIE_GLSL if main is not None else "")

SHADOW_GLSL = """
#define MAX_SHADOW_LIGHTS """ + str(MAX_SHADOW_LIGHTS) + """
uniform samplerCube shadowMaps[""" + str(MAX_SHADOW_LIGHTS) + """];

highp float _sampleShadowCube(int idx, highp vec3 dir) {
    if (idx == 0) return texture(shadowMaps[0], dir).r;
    else if (idx == 1) return texture(shadowMaps[1], dir).r;
    else if (idx == 2) return texture(shadowMaps[2], dir).r;
    else if (idx == 3) return texture(shadowMaps[3], dir).r;
    else if (idx == 4) return texture(shadowMaps[4], dir).r;
    else if (idx == 5) return texture(shadowMaps[5], dir).r;
    else if (idx == 6) return texture(shadowMaps[6], dir).r;
    return texture(shadowMaps[7], dir).r;
}

// The cube-map's face width in texels (every face of a cube is square).
highp float _shadowCubeTexels(int idx) {
    if (idx == 0) return float(textureSize(shadowMaps[0], 0).x);
    else if (idx == 1) return float(textureSize(shadowMaps[1], 0).x);
    else if (idx == 2) return float(textureSize(shadowMaps[2], 0).x);
    else if (idx == 3) return float(textureSize(shadowMaps[3], 0).x);
    else if (idx == 4) return float(textureSize(shadowMaps[4], 0).x);
    else if (idx == 5) return float(textureSize(shadowMaps[5], 0).x);
    else if (idx == 6) return float(textureSize(shadowMaps[6], 0).x);
    return float(textureSize(shadowMaps[7], 0).x);
}

// Each shadow slot's filter, from the Light that owns it: x = softness (the
// PCF disk radius in cube-map texels), y = bias multiplier. 0 stands for the
// default, so a program nobody has given values to still filters sensibly.
uniform vec2 uShadowFilter[MAX_SHADOW_LIGHTS];
vec2 _shadowFilter(int idx) {
    vec2 f = uShadowFilter[idx];
    return vec2(f.x > 0.0 ? f.x : """ + repr(SHADOW_SOFTNESS_DEFAULT) + """,
                f.y > 0.0 ? f.y : """ + repr(SHADOW_BIAS_DEFAULT) + """);
}

// idx          : which cube-map (0..7), or <0 for a non-shadow-casting light
// fragToLight  : lightPos - fragmentWorldPos (world space)
// farPlane     : the light radius used when the cube-map was rendered
// ndotl        : diffuse term, used to scale the slope bias
// Returns 0 (fully lit) .. 1 (fully shadowed).  highp throughout because the
// world coordinates can be in the thousands and mediump would band badly.
float calcPointShadow(int idx, highp vec3 fragToLight, highp float farPlane, float ndotl) {
    if (idx < 0) return 0.0;
    highp float currentDepth = length(fragToLight);
    if (currentDepth >= farPlane) return 0.0;   // beyond the light's reach
    // The cube-map was rendered from the light looking outward, so the lookup
    // direction runs light -> fragment, i.e. the negation of fragToLight.
    highp vec3 lookDir = -fragToLight;
    // One cube-map texel at this distance, in world units: a face spans 90
    // degrees -- 2 units across at unit distance -- over its width in texels.
    // The filter and the bias are measured in texels, so a shadow is as sharp
    // as its map resolves and keeps thin casters (railings, grating) solid.
    // They used to be fractions of the light's radius -- the same blur for a
    // 2048-unit light at any resolution, ~20 world units wide, which smeared
    // a catwalk's bars into a faint grey and dropped thin ones altogether.
    highp float texel = currentDepth * 2.0 / _shadowCubeTexels(idx);
    vec2 shadowFilter = _shadowFilter(idx);
    highp float diskRadius = texel * shadowFilter.x;
    // Bias covers the depth spread across one texel and the PCF disk on a
    // surface sloped away from the light (the tangent of its angle, capped),
    // so lit surfaces don't self-shadow ("shadow acne"), scaled by the
    // light's bias multiplier.
    highp float slope = clamp(sqrt(max(1.0 - ndotl * ndotl, 0.0)) / max(ndotl, 0.1),
                              0.0, 10.0);
    highp float bias = (diskRadius + (texel + diskRadius) * (1.0 + slope)) * shadowFilter.y;
    vec3 sampleDirs[20] = vec3[](
        vec3( 1, 1, 1), vec3( 1,-1, 1), vec3(-1,-1, 1), vec3(-1, 1, 1),
        vec3( 1, 1,-1), vec3( 1,-1,-1), vec3(-1,-1,-1), vec3(-1, 1,-1),
        vec3( 1, 1, 0), vec3( 1,-1, 0), vec3(-1,-1, 0), vec3(-1, 1, 0),
        vec3( 1, 0, 1), vec3(-1, 0, 1), vec3( 1, 0,-1), vec3(-1, 0,-1),
        vec3( 0, 1, 1), vec3( 0,-1, 1), vec3( 0,-1,-1), vec3( 0, 1,-1)
    );
    float shadow = 0.0;
    for (int s = 0; s < 20; ++s) {
        highp float closest = _sampleShadowCube(idx, lookDir + sampleDirs[s] * diskRadius) * farPlane;
        if (currentDepth - bias > closest) shadow += 1.0;
    }
    return shadow / 20.0;
}
"""
# ==============================================================================
# DISTANCE FOG + GLOBAL AMBIENT
# ------------------------------------------------------------------------------
# Shared GLSL injected into every fragment shader that draws world geometry.
#
# Far-plane fog. The camera's far plane is adjustable (engine.view_distance), and
# a far plane on its own pops geometry out of existence at a hard edge. So every
# surface fades toward `uFogColor` as it recedes, and the CPU side guarantees
# `uFogEnd` lands strictly before the clip -- by default at 92% of the view
# distance -- so a fragment is already fully fogged by the time the depth test
# would have discarded it. The frame is cleared to the same colour, so what the
# fog dissolves into and what lies past the far plane are the same pixel value
# and the boundary is not visible at all.
#
# Distance is radial from the eye (`length(FragPos - uFogCamPos)`), not
# view-space depth: the fog wall is then a sphere concentric with the cull
# sphere the broad phase already uses, so a surface does not lighten or darken
# just because the camera turned to put it off-axis.
#
# `uFogDensity` > 0 layers an exponential-squared curve on top of the linear
# ramp (taking whichever is thicker), which deepens the near half of the band
# without moving the opaque point -- the clip stays hidden at any density.
#
# Global ambient. `uAmbient` is a flat omnidirectional term added to every lit
# surface: the `ambient` console command, i.e. a level-wide Light entity that
# does not exist in the world. It is *added* to each shader's own baked ambient
# constant rather than replacing it, so the default of black leaves every
# existing map rendering exactly as before.
#
# Both are declared in one chunk so a shader opts into the pair with a single
# splice, and both are inert at their defaults (uFogEnabled 0, uAmbient black).
# ==============================================================================
FOG_GLSL = """
uniform int   uFogEnabled;
uniform vec3  uFogColor;
uniform float uFogStart;
uniform float uFogEnd;
uniform float uFogDensity;
uniform highp vec3 uFogCamPos;
uniform vec3  uAmbient;

// 0 at uFogStart, 1 at uFogEnd and beyond. Returns 0 outright when fog is off
// so the branch costs a uniform read and nothing else.
float fogFactor(highp vec3 fragPos) {
    if (uFogEnabled == 0) return 0.0;
    highp float d = length(fragPos - uFogCamPos);
    float band = max(uFogEnd - uFogStart, 1e-4);
    float f = clamp((d - uFogStart) / band, 0.0, 1.0);
    if (uFogDensity > 0.0) {
        float e = uFogDensity * max(d - uFogStart, 0.0);
        f = max(f, clamp(1.0 - exp(-e * e), 0.0, 1.0));
    }
    return f;
}

vec3 applyFog(vec3 color, highp vec3 fragPos) {
    return mix(color, uFogColor, fogFactor(fragPos));
}
"""

# ==============================================================================
# DEFAULT SHADER SOURCES
# These are the fallback strings used if the .vert/.frag files are missing from
# disk (e.g. in a packaged build that doesn't include loose shader files).
# Keep these in sync with the files under assets/shaders/.
# ==============================================================================
#: Screen-space reflections fade out with distance from the camera: full
#: strength out to REFLECTION_FADE_START world units, none from
#: REFLECTION_FADE_END -- where no ray is marched at all. A far reflection
#: is a few pixels, each a full ray march; nobody can see it.
REFLECTION_FADE_START = 1024.0
REFLECTION_FADE_END = 1536.0

#: Screen-space reflection, shared by the water and wet decals. The host
#: shader declares ``sceneDepth``, ``sceneColor`` (copies of the frame so
#: far), ``projection`` and ``invProjection``.
SSR_GLSL = """
// 1 near the camera, 0 from REFLECTION_FADE_END on (see engine/shaders.py):
// a reflection is not traced where this is 0.
float reflectionFade(highp vec3 viewPos) {
    return 1.0 - smoothstep(""" + repr(REFLECTION_FADE_START) + """, """ + \
    repr(REFLECTION_FADE_END) + """, length(viewPos));
}
highp vec3 viewPosFromDepth(highp vec2 uv, highp float depth) {
    highp vec4 p = invProjection * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return p.xyz / p.w;
}

// Screen-space reflection: march the reflected ray through the copied depth
// buffer for the surface it hits. True with the hit's uv in the frame copy,
// how far along the ray it lies (view units) and a confidence; false for a
// miss. 10 linear steps with a growing stride, then a short binary
// refinement.
bool marchReflection(highp vec3 originView, highp vec3 dirView,
                     highp float firstStride, out highp vec2 hitUV,
                     out highp float hitS, out float hitFade) {
    hitUV = vec2(0.0);
    hitS = 0.0;
    hitFade = 0.0;
    // The ray's clip-space position is affine in the distance travelled
    // along it, so a step is a multiply-add, not a matrix multiply: the same
    // points as stepping the view-space position and projecting each.
    highp vec4 clipOrigin = projection * vec4(originView, 1.0);
    highp vec4 clipDir = projection * vec4(dirView, 0.0);
    highp float stride = firstStride;
    highp float s = 0.0;
    highp float prevS = 0.0;
    for (int i = 0; i < 10; i++) {
        prevS = s;
        s += stride;
        stride *= 1.22;
        highp float pz = originView.z + dirView.z * s;
        if (pz > -0.5) break;
        highp vec4 c = clipOrigin + clipDir * s;
        highp vec2 uv = c.xy / c.w * 0.5 + 0.5;
        if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) break;
        // textureLod: no implicit derivatives inside the loop.
        highp float d = textureLod(sceneDepth, uv, 0.0).r;
        if (d >= 0.99999) continue;
        highp float sceneZ = viewPosFromDepth(uv, d).z;
        highp float gap = sceneZ - pz;
        // Only a ray that has just gone behind a surface hits it. Without a
        // cap on how far behind, a ray passing behind a thin object (a box
        // standing in the water) counted as hitting it and smeared a long
        // streak of it across the water.
        highp float thickness = clamp(stride * 1.5, 2.0, 48.0);
        if (gap > 0.0 && gap < thickness) {
            highp float lo = prevS;
            highp float hi = s;
            for (int j = 0; j < 5; j++) {
                highp float mid = (lo + hi) * 0.5;
                highp vec4 mc = clipOrigin + clipDir * mid;
                highp vec2 muv = mc.xy / mc.w * 0.5 + 0.5;
                highp float mz = viewPosFromDepth(muv, textureLod(sceneDepth, muv, 0.0).r).z;
                if (mz > originView.z + dirView.z * mid) hi = mid; else lo = mid;
            }
            highp vec4 hc = clipOrigin + clipDir * hi;
            highp vec2 huv = hc.xy / hc.w * 0.5 + 0.5;
            highp float hz = viewPosFromDepth(huv, textureLod(sceneDepth, huv, 0.0).r).z;
            if (hz - (originView.z + dirView.z * hi) > thickness) return false;
            vec2 edge = smoothstep(vec2(0.0), vec2(0.08), huv)
                      * smoothstep(vec2(0.0), vec2(0.08), vec2(1.0) - huv);
            hitUV = huv;
            hitS = hi;
            hitFade = edge.x * edge.y * (1.0 - smoothstep(7.0, 10.0, float(i)));
            return true;
        }
    }
    return false;
}

// The scene colour the reflected ray hits (rgb), sharp, and a confidence
// (a); 0 for a miss.
vec4 traceReflectionStride(highp vec3 originView, highp vec3 dirView,
                           highp float firstStride) {
    highp vec2 uv;
    highp float s;
    float fade;
    if (!marchReflection(originView, dirView, firstStride, uv, s, fade))
        return vec4(0.0);
    return vec4(textureLod(sceneColor, uv, 0.0).rgb, fade);
}

// The water's march: its first stride grows with distance. A decal that
// must reach a few hundred units starts with a longer one.
vec4 traceReflection(highp vec3 originView, highp vec3 dirView) {
    return traceReflectionStride(originView, dirView, max(2.0, -originView.z * 0.02));
}
"""

#: The sky panorama's equirectangular lookup, shared by the sky pass
#: (``void_background.frag``) and the water that reflects it, so a reflection
#: samples the very texel the sky shows in that direction.
PANORAMA_GLSL = """
const float PANORAMA_PI = 3.14159265358979323846;
// Texture coordinates of the panorama in the unit world direction *dir*.
highp vec2 panoramaUV(highp vec3 dir) {
    return vec2(atan(dir.x, -dir.z) / (2.0 * PANORAMA_PI) + 0.5,
                0.5 - asin(clamp(dir.y, -1.0, 1.0)) / PANORAMA_PI);
}
"""


DEFAULT_SHADERS = {
    'simple.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
void main() {
    gl_Position = projection * view * model * vec4(aPos, 1.0);
}""",
    'simple.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;
uniform vec3 color;
uniform float alpha;
void main() {
    FragColor = vec4(color, alpha);
}""",

    'lit.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
out vec3 FragPos;         // implicitly highp
out mediump vec3 Normal;  // explicit mediump to match frag default
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalize(normalMatrix * aNormal);
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",
    'lit.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;
in highp vec3 FragPos;
in vec3 Normal;
uniform vec3 object_color;
uniform float alpha;
uniform vec3 emissive_factor;
struct Light { highp vec3 position; vec3 color; float intensity; highp float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = (vec3(0.1) + uAmbient) * object_color;
    for(int i = 0; i < active_lights && i < """ + str(MAX_LIGHTS) + """; i++) {
        highp vec3  toLight  = lights[i].position - FragPos;
        highp float distSq   = dot(toLight, toLight);
        highp float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            highp float dist = sqrt(distSq);
            vec3  lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att  = 1.0 - (dist / lights[i].radius);
            att = att * att;
            float shadow = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius, diff);
            result += (1.0 - shadow) * (diff * lights[i].color * lights[i].intensity * att) * object_color;
        }
    }
    result += emissive_factor;
    FragColor = vec4(applyFog(result, FragPos), alpha);
}""",

    'textured.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out mediump vec3 Normal;
out vec2 TexCoords;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform vec2 tex_scale;   // per-face stretch / tiling factor
uniform float tex_angle;  // per-face free rotation in radians
uniform vec2 tex_shift;   // per-face UV offset (in texture repeats)
uniform mat3 normalMatrix;

void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalize(normalMatrix * aNormal);
    // Surface-inspector transform: rotate the base 0..1 face UVs about their
    // centre, then apply stretch and shift (Radiant-style free controls).
    vec2 uv = aTexCoords - vec2(0.5);
    float s = sin(tex_angle);
    float c = cos(tex_angle);
    uv = vec2(uv.x * c - uv.y * s, uv.x * s + uv.y * c);
    TexCoords = (uv + vec2(0.5)) * tex_scale + tex_shift;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",
    'textured.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;

in highp vec3 FragPos;
in vec3 Normal;
in highp vec2 TexCoords;

uniform sampler2D texture_diffuse;
uniform vec4 model_tint;
uniform vec3 emissive_factor;
struct Light { highp vec3 position; vec3 color; float intensity; highp float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """
void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
    // A model's tint; all zeros (the default) leaves the texture as it is.
    texColor.rgb *= mix(vec3(1.0), model_tint.rgb, model_tint.a);

    vec3 norm = normalize(Normal);
    vec3 result = (vec3(0.1) + uAmbient) * texColor.rgb;

    for(int i = 0; i < active_lights && i < """ + str(MAX_LIGHTS) + """; i++) {
        highp vec3  toLight  = lights[i].position - FragPos;
        highp float distSq   = dot(toLight, toLight);
        highp float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            highp float dist = sqrt(distSq);
            vec3  lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att  = 1.0 - (dist / lights[i].radius);
            att = att * att;
            float shadow = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius, diff);
            result += (1.0 - shadow) * (diff * lights[i].color * lights[i].intensity * att) * texColor.rgb;
        }
    }
    result += emissive_factor;
    FragColor = vec4(applyFog(result, FragPos), texColor.a);
}""",

    'sprite.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec2 aPos;
out vec2 TexCoords;
out vec3 FragPos;
uniform mat4 projection;
uniform mat4 view;
uniform vec3 sprite_pos_world;
uniform vec2 sprite_size;
uniform bool use_fixed_facing;
layout (location = 3) in float sprite_fixed_yaw;
void main() {
    TexCoords = aPos + 0.5;
    vec3 cameraRight = vec3(view[0][0], view[1][0], view[2][0]);
    vec3 cameraUp = vec3(view[0][1], view[1][1], view[2][1]);
    if (use_fixed_facing && sprite_fixed_yaw > -9999.0) {
        float yaw = sprite_fixed_yaw;
        cameraRight = vec3(cos(yaw), 0.0, -sin(yaw));
        cameraUp = vec3(0.0, 1.0, 0.0);
    }
    vec3 worldPos = sprite_pos_world
                  + cameraRight * aPos.x * sprite_size.x
                  + cameraUp * aPos.y * sprite_size.y;
    FragPos = worldPos;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}""",
    'sprite.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;
in highp vec2 TexCoords;
uniform sampler2D sprite_texture;
in highp vec3 FragPos;""" + FOG_GLSL + """
void main() {
    vec4 texColor = texture(sprite_texture, TexCoords);
    if(texColor.a < 0.1) discard;
    FragColor = vec4(applyFog(texColor.rgb, FragPos), texColor.a);
}""",

    'fog.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 a_pos;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

out vec3 localPos;

void main() {
    localPos = a_pos;
    gl_Position = projection * view * model * vec4(a_pos, 1.0);
}""",

    'fog.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;
in highp vec3 localPos;

uniform highp mat4 model;
uniform highp mat4 inverseModel;
uniform highp vec3 viewPos;

uniform float density;
uniform vec3 fogColor;
uniform sampler3D noiseTexture;
uniform float noiseScale;
uniform highp float time;

highp vec2 intersectBox(highp vec3 rayOrigin, highp vec3 rayDir) {
    highp vec3 tMin = (-0.5 - rayOrigin) / rayDir;
    highp vec3 tMax = ( 0.5 - rayOrigin) / rayDir;
    highp vec3 t1 = min(tMin, tMax);
    highp vec3 t2 = max(tMin, tMax);
    return vec2(max(max(t1.x, t1.y), t1.z),
                min(min(t2.x, t2.y), t2.z));
}

void main() {
    highp vec3 fragWorldPos = vec3(model * vec4(localPos, 1.0));
    highp vec3 rayDirWorld  = normalize(fragWorldPos - viewPos);

    highp vec3 rayOriginLocal = (inverseModel * vec4(viewPos,       1.0)).xyz;
    highp vec3 rayDirLocal    = normalize((inverseModel * vec4(rayDirWorld, 0.0)).xyz);

    highp vec2 t = intersectBox(rayOriginLocal, rayDirLocal);
    if (t.x >= t.y) discard;

    highp float tNear    = max(0.0, t.x);
    highp float stepSize = (t.y - tNear) / 16.0;

    vec4  acc        = vec4(0.0);
    highp float timeOffset = time * 0.1;

    for (int i = 0; i < 16; ++i) {
        highp vec3 sp = rayOriginLocal + rayDirLocal * (tNear + float(i) * stepSize);
        float n       = texture(noiseTexture, sp * noiseScale + vec3(0.0, 0.0, timeOffset)).r;
        float tr      = exp(-density * n * stepSize);
        acc.rgb      += fogColor * (1.0 - tr) * (1.0 - acc.a);
        acc.a        += (1.0 - tr);
        if (acc.a > 0.99) break;
    }

    FragColor = vec4(acc.rgb, clamp(acc.a, 0.0, 1.0));
}""",

    'water.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out vec2 TexCoords;
out mediump vec3 Normal;
out mediump float WaveCrest;
out mediump float ShoreDist;
out highp float RestY;       // height of the still surface under this vertex
out highp vec3 LocalPos;     // the vertex in the brush's unit box (box water)
out highp vec2 SkyUV;        // the sky panorama texel this vertex reflects

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform highp float time;
uniform mat3 normalMatrix;

uniform float waveAmp;    // total wave amplitude in world units
uniform highp vec3 brushSize;   // world-space brush dimensions (also water.frag)
uniform highp int reflectSky;   // 1: the map has a sky panorama to reflect (also water.frag)
uniform float waveSpread; // "Wave Spacing": 0 tight and choppy, 1 broad
// "Direction": how far the surface has flowed (world x, z), so its pattern
// is sampled that far upstream. Zero -- still water -- leaves every
// position exactly as it is (also water.frag).
uniform highp vec2 flowOffset;

""" + PANORAMA_GLSL + """
// One Gerstner wave: displaces the vertex and accumulates normal derivatives.
void addWave(vec2 dir, float wavelength, float amp, float speed, vec2 p,
             inout float dy, inout vec2 dxz, inout vec3 n)
{
    float k = 6.2831853 / wavelength;
    float f = k * dot(dir, p) + speed * time;
    float s = sin(f);
    float c = cos(f);
    float steep = min(0.8 / (k * max(amp, 0.0001) * 4.0), 1.2);
    dy  += amp * s;
    dxz += steep * amp * c * dir;
    n.x -= dir.x * k * amp * c;
    n.z -= dir.y * k * amp * c;
    n.y -= steep * k * amp * s * 0.25;
}

void main()
{
    vec3 worldPos = vec3(model * vec4(aPos, 1.0));
    RestY = worldPos.y;

    // World-space distance from this vertex to the nearest lateral brush edge.
    // Waves are pinned to zero at the edges so the surface always meets the
    // side faces / pool walls exactly (keeps the volume watertight).
    vec2 edgeLocal = vec2(0.5) - abs(aPos.xz);
    float edgeWorld = min(edgeLocal.x * brushSize.x, edgeLocal.y * brushSize.z);
    float fadeW = clamp(min(brushSize.x, brushSize.z) * 0.25, 4.0, 48.0);
    float edgeFade = smoothstep(0.0, fadeW, edgeWorld);

    float topVert = step(0.49, aPos.y);   // only the top surface deforms
    float amp = waveAmp * edgeFade * topVert;

    float dy = 0.0;
    vec2 dxz = vec2(0.0);
    vec3 n = vec3(0.0, 1.0, 0.0);
    if (amp > 0.001) {
        // Wavelengths in world units (a player is ~64 tall). At 190, 118, 74
        // and 38 -- "Wave Spacing" fully left -- crests are a couple of
        // strides apart: tight, choppy water. Fully right (the default)
        // each is 2.2x longer, and slower by sqrt(2.2), as longer waves on
        // real water are: a broad, calm swell.
        float longer = mix(1.0, 2.2, clamp(waveSpread, 0.0, 1.0));
        float slower = inversesqrt(longer);
        highp vec2 wavePos = worldPos.xz - flowOffset;
        addWave(vec2( 0.788,  0.616), 190.0 * longer, amp * 0.42, 1.05 * slower, wavePos, dy, dxz, n);
        addWave(vec2(-0.552,  0.834), 118.0 * longer, amp * 0.28, 1.45 * slower, wavePos, dy, dxz, n);
        addWave(vec2( 0.943, -0.333),  74.0 * longer, amp * 0.19, 1.95 * slower, wavePos, dy, dxz, n);
        addWave(vec2(-0.673, -0.740),  38.0 * longer, amp * 0.11, 2.70 * slower, wavePos, dy, dxz, n);
        worldPos.y  += dy;
        worldPos.xz += dxz * 0.75 * edgeFade;
    }

    vec3 baseNormal = normalize(normalMatrix * aNormal);
    // Only the upward-facing surface takes the wave normal; side walls keep
    // their flat normals even at their top verts (which do get displaced)
    float topFaceVert = topVert * step(0.5, aNormal.y);
    Normal    = normalize(mix(baseNormal, normalize(n), topFaceVert));
    FragPos   = worldPos;
    TexCoords = aTexCoords;
    WaveCrest = clamp(dy / max(waveAmp * 0.85, 0.001) * 0.5 + 0.5, 0.0, 1.0);
    ShoreDist = edgeWorld;
    LocalPos  = aPos;
    SkyUV     = vec2(0.0);
    if (reflectSky == 1) {
        // Where in the panorama this vertex's reflection points, by the sky
        // pass's own mapping: worked out here, per vertex, so a pixel pays
        // one texture read and no trigonometry (water.frag skyColor). The
        // horizontal coordinate is taken relative to the camera's heading,
        // so the texture's seam (GL_REPEAT wraps it) stays behind the
        // camera instead of cutting across a triangle in view.
        vec3 eyePos = -transpose(mat3(view)) * view[3].xyz;
        vec3 reflected = reflect(normalize(worldPos - eyePos), Normal);
        vec3 forward = -vec3(view[0][2], view[1][2], view[2][2]);
        highp vec2 uv = panoramaUV(reflected);
        highp float headingU = panoramaUV(forward).x;
        highp float relative = uv.x - headingU;
        relative -= floor(relative + 0.5);
        SkyUV = vec2(headingU + relative, uv.y);
    }

    gl_Position = projection * view * vec4(worldPos, 1.0);
}""",

    'water.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;

in highp vec3 FragPos;
in highp vec2 TexCoords;
in vec3 Normal;
in float WaveCrest;
in float ShoreDist;
in highp float RestY;
in highp vec3 LocalPos;
in highp vec2 SkyUV;

struct Light {
    highp vec3 position;
    vec3 color;
    float intensity;
    highp float radius;
};

#define MAX_LIGHTS """ + str(MAX_LIGHTS_WATER) + """
uniform Light lights[MAX_LIGHTS];
uniform int active_lights;
// highp as in the vertex stage: GLSL ES links a shared uniform only when
// both stages declare the same precision (the player's GLES path).
uniform highp mat4 view;
uniform highp mat4 projection;
uniform highp vec3 viewPos;
uniform sampler2D normalMap;
uniform sampler2D sceneColor;
uniform highp float time;
// The map's sky panorama, reflected instead of the procedural sky whenever
// the map has one (reflectSky 1, high quality and cheap alike); 0 is the
// procedural sky, as always.
uniform sampler2D skyPanorama;
uniform highp int reflectSky;
// Box water's side walls, high quality: localEye is the camera in the box's
// unit space, sideSkipMargin the highest crest above its still top (box
// units). See main().
uniform int skipCoveredSides;
uniform highp vec3 localEye;
uniform float sideSkipMargin;
uniform highp vec3 brushSize;   // world size of the brush (shared with water.vert)

uniform highp float waveSpread;   // "Wave Spacing": 0 tight and choppy, 1 broad
uniform highp vec2 flowOffset;    // "Direction": how far it has flowed (water.vert)
uniform float waterOpacity;
uniform float waterReflectivity;
uniform float distortionStrength;
uniform float refractionIndex;
uniform float roughness;
uniform float fresnelIntensity;
uniform vec2 screenSize;
uniform vec3 waterTint;
// 1 for emissive water (it glows in its tint, whatever light falls on it);
// 0 for all other water, which this then leaves exactly as it was.
uniform float waterEmissive;

// Scene depth, copied once per water pass alongside sceneColor. With it the
// water knows how much of it lies in front of the bed: colour absorption by
// depth, soft foamy shorelines, caustics and screen-space reflections. With
// hasSceneDepth == 0 (no depth buffer to copy) the shader falls back to the
// depth-less look.
uniform highp sampler2D sceneDepth;
uniform int hasSceneDepth;
uniform int ssrEnabled;
uniform highp mat4 invProjection;
// Effects (ORB, FIRE, custom GIFs) reflected directly: the nearest few drawn
// this frame, as the camera-facing quads they are. xyz centre (world), w
// half width; the half heights beside them.
#define MAX_REFLECTED_EFFECTS 4
uniform int effectCount;
uniform highp vec4 effectQuads[MAX_REFLECTED_EFFECTS];
uniform float effectHalfHeights[MAX_REFLECTED_EFFECTS];""" + FOG_GLSL + """

const vec3 SUN_DIR   = vec3(0.4767, 0.6555, 0.5859);  // pre-normalized
const vec3 SUN_COLOR = vec3(1.00, 0.95, 0.82);

highp float hash21(highp vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}
highp float vnoise(highp vec2 p) {
    highp vec2 i = floor(p);
    highp vec2 f = fract(p);
    highp vec2 u = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

""" + SSR_GLSL + """
// An effect is a flat, camera-facing quad: the ray march above steps through
// or past it. It is reflected directly instead. Where the reflected ray
// passes the effect's centre, its offset from the centre says which texel of
// the quad it meets; that texel is on screen, in the scene copy. The depth
// copy says whether the effect's solid part is really what is drawn there
// (the effect pass writes it for texels at least half opaque), so neither
// the transparent corners of the quad nor something standing in front of it
// is reflected. Returns the colour (rgb) and a confidence (a).
vec4 reflectEffects(highp vec3 P, vec3 R) {
    vec3 camRight = vec3(view[0][0], view[1][0], view[2][0]);
    vec3 camUp = vec3(view[0][1], view[1][1], view[2][1]);
    // "Up" and "right" across the ray.
    vec3 upAcross = vec3(0.0, 1.0, 0.0) - R * R.y;
    float upLength = length(upAcross);
    if (upLength < 0.001) return vec4(0.0);
    upAcross /= upLength;
    vec3 rightAcross = cross(upAcross, R);
    if (dot(rightAcross, camRight) < 0.0) rightAcross = -rightAcross;
    for (int i = 0; i < effectCount && i < MAX_REFLECTED_EFFECTS; i++) {
        highp vec3 centre = effectQuads[i].xyz;
        float halfWidth = effectQuads[i].w;
        float halfHeight = effectHalfHeights[i];
        highp float along = dot(centre - P, R);
        if (along <= 0.0) continue;
        highp vec3 offset = P + R * along - centre;
        float ox = dot(offset, rightAcross);
        float oy = dot(offset, upAcross);
        if (abs(ox) >= halfWidth || abs(oy) >= halfHeight) continue;
        highp vec3 texel = centre + camRight * ox + camUp * oy;
        highp vec4 clip = projection * view * vec4(texel, 1.0);
        if (clip.w <= 0.0) continue;
        highp vec2 uv = clip.xy / clip.w * 0.5 + 0.5;
        if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) continue;
        // Is the effect's solid part drawn there? At that pixel or beside
        // it: an animated texture has pin-holes just under half opaque,
        // which would otherwise speckle the reflection.
        highp float texelZ = (view * vec4(texel, 1.0)).z;
        highp float tolerance = 2.0 + abs(texelZ) * 0.004;
        highp vec2 px = 1.5 / vec2(textureSize(sceneDepth, 0));
        float solid = 0.0;
        for (int k = 0; k < 5; k++) {
            highp vec2 at = uv + px * (k == 0 ? vec2(0.0)
                                    : k == 1 ? vec2(1.0, 0.0) : k == 2 ? vec2(-1.0, 0.0)
                                    : k == 3 ? vec2(0.0, 1.0) : vec2(0.0, -1.0));
            highp float d = textureLod(sceneDepth, at, 0.0).r;
            if (d < 0.99999 && abs(viewPosFromDepth(at, d).z - texelZ) <= tolerance) {
                solid = 1.0;
                break;
            }
        }
        if (solid == 0.0) continue;
        vec2 edge = smoothstep(vec2(0.0), vec2(0.04), uv)
                  * smoothstep(vec2(0.0), vec2(0.04), vec2(1.0) - uv);
        return vec4(textureLod(sceneColor, uv, 0.0).rgb, edge.x * edge.y);
    }
    return vec4(0.0);
}

// The ripple of this pixel (main), which nudges the reflected panorama.
vec2 skyRipple = vec2(0.0);

// The sky the water reflects: the map's panorama when it has one, else the
// procedural sky it has always reflected. The
// panorama is looked up where the vertices worked out (SkyUV), shimmering
// with the ripple: one texture read and no trigonometry, cheaper than the
// procedural sky itself -- not the exact per-pixel direction, and nobody
// can tell on moving water.
vec3 skyColor(vec3 dir) {
    if (reflectSky == 1) {
        return textureLod(skyPanorama, SkyUV + skyRipple * 0.03, 0.0).rgb;
    }
    float h = clamp(dir.y, 0.0, 1.0);
    vec3 sky = mix(vec3(0.66, 0.76, 0.83),
                   vec3(0.19, 0.38, 0.66),
                   pow(h, 0.55));
    float sunAmount = max(dot(dir, SUN_DIR), 0.0);
    sky += SUN_COLOR * (
        pow(sunAmount, 350.0) * 3.0 +
        pow(sunAmount, 24.0) * 0.18
    );
    return sky;
}

void main()
{
    if (skipCoveredSides == 1) {
        // A high-quality top surface is opaque and drawn after the walls: a
        // wall fragment the view ray reaches through the top is overwritten
        // by it, so it is not worth shading (it writes nothing instead). The ray from the eye enters the
        // box's column (|x|, |z| <= 0.5) at tEnter; above the highest crest
        // there, it must pass the top surface before it reaches this wall --
        // and, to leave the top's own edge pixels alone, cross the still top
        // plane at least two of the top's pixels inside the column (its
        // footprint, which at a grazing angle is far more than the wall's).
        highp vec3 ray = LocalPos - localEye;
        highp vec2 top = localEye.xz
            + ray.xz * ((0.5 - localEye.y) / min(ray.y, -1.0e-6));
        highp vec2 topPerPixel = fwidth(top);
        highp float tEnter = 0.0;
        if (abs(localEye.x) > 0.5 && abs(ray.x) > 1.0e-6)
            tEnter = max(tEnter, (sign(localEye.x) * 0.5 - localEye.x) / ray.x);
        if (abs(localEye.z) > 0.5 && abs(ray.z) > 1.0e-6)
            tEnter = max(tEnter, (sign(localEye.z) * 0.5 - localEye.z) / ray.z);
        highp float twoPixels = 2.0 * max(topPerPixel.x, topPerPixel.y);
        highp float inset = 0.5 - twoPixels;
        // Wave troughs dip below the still top: the ray must also be below
        // the deepest of them two pixels before it reaches this wall, or it
        // could pass over a trough and see the wall's upper strip.
        highp float before = LocalPos.y
            - ray.y * twoPixels / max(length(ray.xz), 1.0e-6);
        // Not at a grazing angle (under about 11 degrees): seen that flat,
        // the wave mesh can leave pixel-wide cracks the wall shows through.
        highp vec3 worldRay = ray * brushSize;
        bool steep = -worldRay.y > 0.2 * length(worldRay.xz);
        if (steep && localEye.y + tEnter * ray.y > 0.5 + sideSkipMargin
                && before < 0.5 - sideSkipMargin
                && abs(top.x) < inset && abs(top.y) < inset) {
            // Not discard: a shader that can discard loses early depth
            // testing for every draw made with it, the top surface's too.
            // Alpha 0 under SRC_ALPHA / ONE_MINUS_SRC_ALPHA leaves the
            // pixel exactly as it was.
            FragColor = vec4(0.0);
            return;
        }
    }
    highp vec3 toView = viewPos - FragPos;
    highp float viewDist = length(toView);
    vec3 viewDir = toView / max(viewDist, 0.0001);

    vec3 geoN = normalize(Normal);
    // Above or below the water is decided against the still surface, not
    // per pixel: with the eye near the waterline, wave crests rise above it,
    // and a per-pixel test flipped those crests to the underwater look.
    bool topSurface = abs(geoN.y) > 0.5;
    bool backside = topSurface ? (viewPos.y < RestY) : (dot(geoN, viewDir) < 0.0);
    if (backside) geoN = -geoN;

    float topFace = step(0.35, abs(geoN.y));

    // World-space animated normal-map detail.  This is also the normal used
    // by Snell/Fresnel, so the optical effects follow the visible ripples.
    // Flowing water ("Direction") samples it all upstream, by flowOffset:
    // ripples, foam and the broad variation drift with the waves.
    highp vec2 flowPos = FragPos.xz - flowOffset;
    highp vec2 wuv;
    if (topFace > 0.5) {
        wuv = flowPos;
    } else if (abs(geoN.x) > abs(geoN.z)) {
        wuv = vec2(flowPos.y, FragPos.y - time * 6.0);
    } else {
        wuv = vec2(flowPos.x, FragPos.y - time * 6.0);
    }
    // The ripple detail is spread with the waves ("Wave Spacing"): the same
    // pattern, up to 1.7x broader.
    highp vec2 ruv = wuv * mix(1.0, 0.59, clamp(waveSpread, 0.0, 1.0));
    vec2 r1 = texture(normalMap, ruv * 0.0110 + time * vec2( 0.021,  0.014)).xy - 0.5;
    vec2 r2 = texture(normalMap, ruv * 0.0047 + time * vec2(-0.011,  0.008)).xy - 0.5;
    vec2 r3 = texture(normalMap, ruv * 0.0310 + time * vec2( 0.016, -0.029)).xy - 0.5;
    vec2 ripple = r1 + r2 * 0.65 + r3 * 0.35;

    // Broad world-space variation breaks up normal-map repetition on large water brushes.
    highp vec2 broadNoise = vec2(
        vnoise(flowPos * 0.006 + vec2(time * 0.004, -time * 0.003)),
        vnoise(flowPos * 0.008 + vec2(-time * 0.003, time * 0.005))
    ) - 0.5;
    broadNoise += (vec2(
        vnoise(flowPos * 0.0037 + vec2(-time * 0.002, time * 0.001)),
        vnoise(flowPos * 0.0051 + vec2(time * 0.001, time * 0.002))
    ) - 0.5) * 0.55;
    ripple += broadNoise * 0.55;
    skyRipple = ripple;

    float detailFade = 1.0 / (1.0 + viewDist * 0.0009);
    float rippleStrength = (0.34 + WaveCrest * 0.18) * detailFade;

    vec3 N;
    if (topFace > 0.5) {
        N = normalize(vec3(
            geoN.x + ripple.x * rippleStrength,
            geoN.y,
            geoN.z + ripple.y * rippleStrength
        ));
    } else {
        vec3 up = vec3(0.0, 1.0, 0.0);
        vec3 tangent = normalize(cross(up, geoN));
        N = normalize(
            geoN +
            (tangent * ripple.x + up * ripple.y) *
            rippleStrength * 0.6
        );
    }

    // ------------------------------------------------------------------
    // Refraction / transmission: the same screen-space optical treatment
    // used by Glass, but driven by the water's perturbed surface normal.
    // ------------------------------------------------------------------
    float ior = max(refractionIndex, 1.0);
    // Air -> water above the surface; water -> air when viewed from below.
    float eta = backside ? ior : (1.0 / ior);
    highp vec3 straightDir = -viewDir;
    highp vec3 refractDir = refract(straightDir, N, eta);
    // Past the critical angle (looking up at the surface from below at a
    // grazing angle) there is no refracted ray: total internal reflection.
    // refract() returns zero there, which used to fling the screen-space
    // offset across the frame; look straight through instead and let the
    // water body colour take over (see totalInternal below).
    float totalInternal = 0.0;
    if (dot(refractDir, refractDir) < 0.25) {
        refractDir = straightDir;
        totalInternal = 1.0;
    }
    highp vec3 refractDeltaView = mat3(view) * (refractDir - straightDir);
    highp float refractDeltaLen = length(refractDeltaView);
    if (refractDeltaLen > 1.0e-5) {
        refractDeltaView /= refractDeltaLen;
    } else {
        refractDeltaView = vec3(0.0);
    }

    highp vec2 projectionScale =
        vec2(projection[0][0], projection[1][1]);
    highp vec2 normalView =
        normalize(mat3(view) * N).xy;
    highp float grazing =
        1.0 - clamp(abs(dot(viewDir, N)), 0.0, 1.0);

    highp vec2 refractionWarp =
        refractDeltaView.xy *
        projectionScale *
        (0.055 + 0.035 * grazing);
    highp vec2 normalWarp =
        normalView *
        (0.012 + 0.010 * grazing);

    highp float nWarp1 =
        vnoise(FragPos.xz * 0.08 + TexCoords * 3.0);
    highp float nWarp2 =
        vnoise(FragPos.xy * 0.11 + TexCoords * 5.0);
    highp vec2 microWarp =
        (vec2(nWarp1, nWarp2) - 0.5) * 0.020;

    highp vec2 screenUV =
        gl_FragCoord.xy / max(screenSize, vec2(1.0));
    highp vec2 uvOffset =
        (refractionWarp + normalWarp + microWarp) *
        distortionStrength;
    highp vec2 refractUV = clamp(
        screenUV + uvOffset,
        vec2(0.001),
        vec2(0.999)
    );

    // How much water the view ray crosses before it reaches the bed, and
    // where that bed is (for caustics). Refracted samples of anything in
    // front of the water - a bank, a wading monster - would smear it into
    // the surface, so those fall back to the unrefracted pixel.
    bool haveDepth = hasSceneDepth == 1 && !backside;
    highp vec3 surfaceView = (view * vec4(FragPos, 1.0)).xyz;
    highp float thickness = 1.0e4;
    highp float bedDepth = 1.0e4;       // vertical depth of the bed below the surface
    highp vec3 bedWorld = FragPos - vec3(0.0, 1.0e4, 0.0);
    bool bedVisible = false;
    if (haveDepth) {
        // The refracted sample is the bed unless it lies in front of the
        // surface; only then is the unrefracted pixel read instead.
        highp float dBed = textureLod(sceneDepth, refractUV, 0.0).r;
        highp vec3 bedView = viewPosFromDepth(refractUV, dBed);
        if (bedView.z > surfaceView.z) {
            refractUV = clamp(screenUV, vec2(0.001), vec2(0.999));
            dBed = textureLod(sceneDepth, refractUV, 0.0).r;
            bedView = viewPosFromDepth(refractUV, dBed);
        }
        if (dBed < 0.99999) {
            thickness = max(length(bedView) - length(surfaceView), 0.0);
            bedWorld = viewPos - viewDir * length(bedView);
            bedDepth = max(FragPos.y - bedWorld.y, 0.0);
            bedVisible = true;
        }
    }

    vec3 transmitted = texture(sceneColor, refractUV).rgb;
    if (roughness > 0.001) {
        highp vec2 blurStep = roughness * 4.0 / max(screenSize, vec2(1.0));
        transmitted += texture(
            sceneColor,
            clamp(refractUV + vec2(blurStep.x, 0.0),
                  vec2(0.001), vec2(0.999))).rgb;
        transmitted += texture(
            sceneColor,
            clamp(refractUV - vec2(blurStep.x, 0.0),
                  vec2(0.001), vec2(0.999))).rgb;
        transmitted += texture(
            sceneColor,
            clamp(refractUV + vec2(0.0, blurStep.y),
                  vec2(0.001), vec2(0.999))).rgb;
        transmitted /= 4.0;
    }

    // ------------------------------------------------------------------
    // Water body / shallow colour and subsurface-looking crest scatter.
    // ------------------------------------------------------------------
    float NdV = max(dot(N, viewDir), 0.0);
    vec3 deepCol = waterTint * 0.55;
    vec3 shallowCol =
        waterTint * 1.25 +
        vec3(0.02, 0.10, 0.09);
    vec3 bodyCol =
        mix(deepCol, shallowCol,
            pow(1.0 - NdV, 1.5) * 0.7 + 0.15);

    float sss =
        pow(WaveCrest, 2.0) *
        pow(
            max(
                dot(
                    viewDir,
                    -normalize(vec3(SUN_DIR.x, 0.0, SUN_DIR.z))
                ),
                0.0
            ),
            2.0
        );
    bodyCol +=
        (waterTint * 0.8 + vec3(0.05, 0.22, 0.18)) * sss;

    bodyCol *=
        0.45 + 0.55 * max(dot(N, SUN_DIR), 0.0);

    vec3 transmission;
    // Caustics: light focused by the ripples dances over the bed, strongest
    // in the shallows. Sampled on the top surface only, where they are used:
    // haveDepth and topFace hold for a whole primitive, so the mipmapped
    // lookups stay in uniform control flow (bedVisible does not, and stays
    // inside).
    if (haveDepth) {
        if (topFace > 0.5) {
            highp vec2 cuv = (bedWorld.xz - flowOffset) * 0.03;
            float c1 = texture(normalMap, cuv + time * vec2(0.031, 0.022)).x;
            float c2 = texture(normalMap, cuv * 1.37 - time * vec2(0.024, 0.037)).y;
            if (bedVisible) {
                float caustic = pow(1.0 - abs(c1 + c2 - 1.0), 10.0);
                transmitted *= 1.0 + caustic * 0.55 * exp(-bedDepth * 0.03) * detailFade;
            }
        }
        // Beer-Lambert absorption: red goes first, so the shallows stay clear
        // and deeper water turns to the tint colour.
        // Authored opacity sets how murky the water is: 0.5 is the default.
        vec3 absorb = ((vec3(1.0) - clamp(waterTint, 0.0, 1.0)) * 0.018 + 0.002)
                    * clamp(waterOpacity, 0.05, 1.0) * 2.0;
        vec3 extinction = exp(-absorb * thickness);
        transmission = transmitted * extinction + bodyCol * (vec3(1.0) - extinction);
    } else {
        // Mix the refracted scene with the water's own body colour so shallow
        // geometry remains visible instead of replacing the water with a flat
        // post-process image.
        vec3 transmittedTinted =
            transmitted * mix(vec3(1.0), waterTint, 0.35);
        transmission =
            mix(transmittedTinted, bodyCol, 0.45);
    }

    // ------------------------------------------------------------------
    // Fresnel: physical R0 for water (~0.0204), multiplied by the authored
    // Fresnel/reflectivity intensity so existing maps retain their control.
    // ------------------------------------------------------------------
    float f0 = 0.020373;
    float fresnel =
        f0 + (1.0 - f0) * pow(1.0 - NdV, 5.0);
    fresnel = clamp(
        fresnel * clamp(fresnelIntensity, 0.0, 4.0),
        0.0, 1.0
    );

    vec3 R = reflect(-viewDir, N);
    vec3 reflection = skyColor(R);
    float sceneReflection = 0.0;
    // The scene's reflection fades out with distance; past the fade no ray
    // is marched, and the sky is what the far water reflects.
    float ssrFade = reflectionFade(surfaceView);
    if (haveDepth && ssrEnabled == 1 && topFace > 0.5 && ssrFade > 0.0) {
        // Reflect the scene itself where the ray finds it on screen; the sky
        // fills in everywhere else. Rougher water blurs towards the sky.
        vec4 ssr = traceReflection(surfaceView, normalize(mat3(view) * R));
        sceneReflection = ssr.a * (1.0 - roughness * 0.6) * ssrFade;
        reflection = mix(reflection, ssr.rgb, sceneReflection);
        if (effectCount > 0) {
            // By the wave's own normal, with only a little of the ripple
            // detail: a small object a few hundred units off, reflected by
            // the full detail normal, scatters into speckle.
            vec3 calm = normalize(mix(geoN, N, 0.2));
            vec4 effect = reflectEffects(FragPos, reflect(-viewDir, calm));
            effect.a *= ssrFade;
            if (effect.a > 0.0) {
                reflection = mix(reflection, effect.rgb, effect.a);
                sceneReflection = max(sceneReflection, effect.a);
            }
        }
    }

    // Keep authored reflectivity visible at normal viewing angles. Physical
    // water Fresnel starts around 2%, which is too weak to make the optional
    // reflection perceptible from above on its own; grazing angles still get
    // the full Fresnel response.
    float reflectionWeight = max(
        fresnel,
        clamp(waterReflectivity, 0.0, 1.0) * 0.5
    );
    // Reflections of the scene are what make water read as water from
    // normal viewing heights, where physical Fresnel is only a few percent:
    // let the authored reflectivity carry them further.
    reflectionWeight = max(reflectionWeight,
        clamp(waterReflectivity, 0.0, 1.0) * 0.8 * sceneReflection);
    vec3 color = mix(transmission, reflection, reflectionWeight);

    // ------------------------------------------------------------------
    // Dynamic lights / specular / foam.
    // ------------------------------------------------------------------
    vec3 diffuseAcc = vec3(0.0);
    vec3 specAcc = vec3(0.0);
    for (int i = 0; i < active_lights && i < MAX_LIGHTS; i++) {
        highp vec3 toL = lights[i].position - FragPos;
        highp float dist = length(toL);
        if (dist < lights[i].radius) {
            vec3 Ldir = toL / dist;
            float att =
                1.0 - smoothstep(0.0, lights[i].radius, dist);
            vec3 lc =
                lights[i].color * lights[i].intensity * att;
            diffuseAcc += max(dot(N, Ldir), 0.0) * lc;
            vec3 Hl = normalize(Ldir + viewDir);
            float ndh = max(dot(N, Hl), 0.0);
            specAcc +=
                (pow(ndh, 240.0) * 1.6 +
                 pow(ndh, 28.0) * 0.15) * lc;
        }
    }
    color += color * diffuseAcc * 0.45;

    vec3 Hs = normalize(SUN_DIR + viewDir);
    float sunSpec =
        pow(max(dot(N, Hs), 0.0), 320.0);
    float sparkle =
        vnoise(wuv * 0.9 +
               vec2(time * 1.7, -time * 1.3)) *
        vnoise(wuv * 1.7 -
               vec2(time * 0.9, -time * 1.1));
    sunSpec *=
        (1.0 + sparkle * 6.0) *
        detailFade;
    vec3 specular =
        SUN_COLOR * sunSpec * 2.2 + specAcc;

    // Only the top surface foams (topFace is 0 or 1): the walls skip it.
    float foam = 0.0;
    if (topFace > 0.5) {
        float foamNoise =
            vnoise(wuv * 0.16 +
                   vec2(time * 0.05, -time * 0.04)) * 0.6 +
            vnoise(wuv * 0.45 -
                   vec2(time * 0.07, time * 0.06)) * 0.4;
        float crestFoam =
            smoothstep(0.68, 0.92, WaveCrest) *
            smoothstep(0.35, 0.75, foamNoise);
        float shoreWave =
            0.5 + 0.5 * sin(ShoreDist * 0.30 - time * 1.8);
        float shoreFoam =
            (1.0 - smoothstep(2.0, 26.0, ShoreDist)) *
            (0.30 + 0.70 * shoreWave) *
            smoothstep(0.25, 0.60, foamNoise + 0.15);
        if (haveDepth) {
            // Foam along the line where the water touches anything, not only at
            // the brush edges. Measured vertically, so a shallow flat stays clear
            // and only the waterline itself foams.
            float contact = 1.0 - smoothstep(0.0, 2.5, bedDepth);
            float lapping = 0.5 + 0.5 * sin(bedDepth * 2.5 - time * 2.2);
            shoreFoam = max(shoreFoam,
                            contact * (0.35 + 0.65 * lapping) *
                            smoothstep(0.2, 0.55, foamNoise + 0.1));
        }
        foam = clamp(crestFoam + shoreFoam, 0.0, 1.0);
    }

    color += specular * (0.35 + 0.65 * waterReflectivity);
    color = mix(
        color,
        vec3(0.90, 0.95, 0.96),
        foam * 0.85
    );

    float alpha =
        clamp(waterOpacity, 0.05, 1.0) *
        (0.60 + 0.40 * (1.0 - NdV));
    alpha = clamp(
        alpha +
        fresnel * 0.35 +
        foam * 0.45 +
        sunSpec * 0.4,
        0.05,
        1.0
    );

    if (haveDepth && topFace > 0.5) {
        // The refracted, absorbed scene is already in the colour, so the
        // surface is opaque - blending it over the scene again would let the
        // unabsorbed bed show through. It only fades out where the water
        // meets the shore, so there is no hard seam.
        alpha = smoothstep(0.0, 1.5, bedDepth);
    }

    if (backside && totalInternal > 0.5) {
        // Total internal reflection mirrors the underwater world, which is
        // mostly the water's own body colour.
        color = mix(color, bodyCol, 0.85);
    }

    if (backside) {
        color = mix(
            color,
            waterTint * 1.4 + vec3(0.10, 0.18, 0.20),            0.35
        );
        alpha = min(alpha + 0.15, 1.0);
    }

    if (waterEmissive > 0.0) {
        // Glowing water: its own colour shows through the reflections and
        // the dark, strongest where the surface is seen square on.
        color = mix(color, waterTint * 1.6 + vec3(0.04), 0.7 * waterEmissive);
        alpha = min(alpha + 0.3 * waterEmissive, 1.0);
    }

    FragColor = vec4(applyFog(color, FragPos), alpha);
}""",

    'glass.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out highp vec3 FragPos;
out mediump vec3 Normal;
out highp vec2 TexCoords;
out highp vec3 ViewFragPos;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;

void main() {
    FragPos     = vec3(model * vec4(aPos, 1.0));
    Normal      = normalize(normalMatrix * aNormal);
    TexCoords   = aTexCoords;
    ViewFragPos = vec3(view * vec4(FragPos, 1.0));
    gl_Position = projection * vec4(ViewFragPos, 1.0);
}""",

    'glass.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;

in highp vec3 FragPos;
in vec3 Normal;
in highp vec2 TexCoords;
in highp vec3 ViewFragPos;

uniform highp vec3 viewPos;
// highp as in the vertex stage: GLSL ES links a shared uniform only when
// both stages declare the same precision (the player's GLES path).
uniform highp mat4 view;
uniform highp mat4 projection;
uniform vec3 waterColor;
uniform float distortionStrength;
uniform float fresnelIntensity;
uniform float glassOpacity;
uniform float refractionIndex;
uniform float roughness;
uniform sampler2D sceneColor;
uniform vec2 screenSize;""" + FOG_GLSL + """

void main() {
    highp vec3 viewDir = normalize(viewPos - FragPos);
    highp vec3 baseNormal = normalize(Normal);

    // Screen-space transmission needs an authored, visible warp. The old
    // implementation projected a unit ray delta and then divided it by
    // scene depth, which leaves the resulting UV displacement well below a
    // pixel on normal editor/game distances. Keep the Snell direction so the
    // IOR slider still has a physical relationship to the result, but map the
    // angular change into a bounded screen-space offset controlled directly by
    // the distortion slider.
    float ior = max(refractionIndex, 1.0);
    float eta = 1.0 / ior;
    highp vec3 straightDir = -viewDir;
    highp vec3 refractDir = refract(straightDir, baseNormal, eta);
    highp vec3 refractDeltaView = mat3(view) * (refractDir - straightDir);
    highp float refractDeltaLen = length(refractDeltaView);
    if (refractDeltaLen > 1.0e-5) {
        refractDeltaView /= refractDeltaLen;
    } else {
        refractDeltaView = vec3(0.0);
    }

    highp vec2 projectionScale = vec2(projection[0][0], projection[1][1]);
    highp vec2 normalView = normalize(mat3(view) * baseNormal).xy;
    highp float grazing = 1.0 - clamp(abs(dot(viewDir, baseNormal)), 0.0, 1.0);

    // The refracted direction supplies the broad warp; the view-space normal
    // keeps a flat sheet of glass visibly responsive even when the ray delta is
    // tiny; grazing angles get a little more displacement, like real glass.
    highp vec2 refractionWarp =
        refractDeltaView.xy * projectionScale * (0.055 + 0.035 * grazing);
    highp vec2 normalWarp = normalView * (0.012 + 0.010 * grazing);

    highp vec2 screenUV = gl_FragCoord.xy / screenSize;

    // Keep the warp entirely on the core refraction/normal path. The former
    // procedural hash/noise helper triggered an Intel GLSL compiler failure on
    // some older integrated-GPU drivers.
    highp vec2 uvOffset =
        (refractionWarp + normalWarp) * distortionStrength;

    highp vec2 refractUV = clamp(
        screenUV + uvOffset,
        vec2(0.001),
        vec2(0.999)
    );

    // Roughness is a real filter over the transmitted scene rather than merely
    // changing a highlight exponent. Four taps are cheap, deterministic, and
    // make the control visibly useful on GL 3.3 hardware.
    highp vec2 blurStep = roughness * 4.0 / screenSize;
    vec3 transmitted = texture(sceneColor, refractUV).rgb;
    if (roughness > 0.001) {
        transmitted += texture(sceneColor, clamp(refractUV + vec2(blurStep.x, 0.0),
                                                 vec2(0.001), vec2(0.999))).rgb;
        transmitted += texture(sceneColor, clamp(refractUV - vec2(blurStep.x, 0.0),
                                                 vec2(0.001), vec2(0.999))).rgb;
        transmitted += texture(sceneColor, clamp(refractUV + vec2(0.0, blurStep.y),
                                                 vec2(0.001), vec2(0.999))).rgb;
        transmitted /= 4.0;
    }

    // Tint the transmitted scene without replacing it with a flat glass colour.
    vec3 filteredScene = transmitted * mix(vec3(1.0), waterColor, 0.35);

    // Schlick Fresnel: refractionIndex supplies the physically derived F0, while
    // the authored Fresnel slider controls its strength.
    float cosTheta = clamp(dot(viewDir, baseNormal), 0.0, 1.0);
    float f0 = pow((ior - 1.0) / (ior + 1.0), 2.0);
    float fresnel = f0 + (1.0 - f0) * pow(1.0 - cosTheta, 5.0);
    fresnel = clamp(fresnel * fresnelIntensity, 0.0, 1.0);

    vec3 reflectionColor = vec3(0.96, 0.99, 1.0);
    vec3 finalRGB = mix(filteredScene, reflectionColor, fresnel);

    // A compact view-dependent glint keeps Fresnel readable without requiring
    // the glass pass to upload the whole light table a second time.
    highp vec3 halfDir = normalize(viewDir + vec3(0.35, 0.9, 0.2));
    float shininess = mix(128.0, 12.0, roughness);
    float specular = pow(max(dot(baseNormal, halfDir), 0.0), shininess);
    finalRGB += vec3(specular * (0.15 + 0.55 * fresnel));

    float edgeAlpha = fresnel * 0.65 + roughness * 0.20;
    float alpha = clamp(
        glassOpacity + edgeAlpha * (1.0 - glassOpacity),
        0.05,
        1.0
    );

    FragColor = vec4(applyFog(finalRGB, FragPos), alpha);
}""",

    # Depth cube-map pass: renders scene geometry from a point light's position
    # into one cube face, storing linear distance (0..1 = 0..far_plane) so the
    # lighting shaders can do an omnidirectional shadow test.  One draw per face
    # (6 faces) keeps this portable to GL 3.3 with no geometry-shader dependency.
    'depth_cube.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
uniform mat4 model;
uniform mat4 lightSpaceMatrix;   // proj * view for the current cube face
out vec3 FragPos;
void main() {
    vec4 world = model * vec4(aPos, 1.0);
    FragPos = world.xyz;
    gl_Position = lightSpaceMatrix * world;
}""",
    'depth_cube.frag': """#version 330 core
in vec3 FragPos;
uniform vec3 lightPos;
uniform float far_plane;
void main() {
    // Store distance to the light, normalised into [0, 1].
    gl_FragDepth = length(FragPos - lightPos) / far_plane;
}""",

    'terrain.vert': """#version 330 core
precision highp float;
precision highp int;
// Terrain drawn from a heightfield: no vertex buffer at all.
//
// Each chunk is drawn as 6 vertices per quad, in exactly the order the CPU
// mesh used -- quad q = ix * res + iz, triangle 1 at (ix,iz)(ix+1,iz)(ix,iz+1)
// and triangle 2 at (ix+1,iz)(ix+1,iz+1)(ix,iz+1) -- and every attribute the
// 14-float vertex carried is rebuilt here from the chunk's height grid:
// position, the flat face normal, the per-triangle colour, the UV and the
// smooth normal. The arithmetic follows the old NumPy builder step by step
// (float32 throughout), including its quirks, which are preserved on purpose:
// the face normal points down (cross(+x, +z) = -y), colour is normalised by
// the chunk's own height range, and the "variation" seed reduces to iz.
out vec3 FragPos;
out mediump vec3 Normal;
out mediump vec3 VertexColor;
out vec2 TexCoords;
out vec2 PaintCoords;
out mediump vec3 SmoothNormal;

uniform mat4 projection;
uniform mat4 view;

// Height grid of the chunk: texel (x = k + 1, y = i + 1) holds grid point
// (i, k). The one-texel ring around it (i or k = -1 or res + 1) lies beyond
// the chunk's edges and is sampled only for smooth normals.
uniform highp sampler2DArray uHeights;
uniform ivec2 uChunkI;     // (quads per side, texture layer)
uniform vec3  uChunkX;     // (world x, world z, grid step), as float32
uniform vec2  uChunkY;     // (chunk min height, height range; 1 when flat)
uniform float uTiling;     // Terrain.TILING_SCALE
uniform int   uFlatMode;

// The biome colour gradient, pre-cast exactly as the CPU cast it: stop
// heights and colours, and per segment the float32 width (h1 - h0, or 0 when
// h1 <= h0) and colour delta (c1 - c0).
#define MAX_GRADIENT_STOPS 32
uniform int   uGradCount;
uniform float uGradH[MAX_GRADIENT_STOPS];
uniform vec3  uGradC[MAX_GRADIENT_STOPS];
uniform float uGradW[MAX_GRADIENT_STOPS];
uniform vec3  uGradD[MAX_GRADIENT_STOPS];

float heightAt(int i, int k) {
    return texelFetch(uHeights, ivec3(k + 1, i + 1, uChunkI.y), 0).r;
}

vec3 gridPoint(ivec2 p) {
    return vec3(uChunkX.x + float(p.x) * uChunkX.z,
                heightAt(p.x, p.y),
                uChunkX.y + float(p.y) * uChunkX.z);
}

// Central differences everywhere, the chunk's edge included: the border ring
// supplies the sample beyond it, so both chunks sharing an edge compute the
// same smooth normal there. (The CPU mesh took one-sided differences at the
// edge -- np.gradient's edge_order=1 -- which kinked the normal by a few
// degrees and showed as a seam in the texture blend.)
float gradientAlong(int i, int k, bool alongX) {
    ivec2 a = alongX ? ivec2(i - 1, k) : ivec2(i, k - 1);
    ivec2 b = alongX ? ivec2(i + 1, k) : ivec2(i, k + 1);
    return (heightAt(b.x, b.y) - heightAt(a.x, a.y)) / (2.0 * uChunkX.z);
}

vec3 gradientColour(float h) {
    if (uGradCount == 0) return vec3(0.5);
    vec3 result = vec3(0.0);
    for (int s = 0; s < uGradCount - 1; ++s) {
        if (h >= uGradH[s] && h <= uGradH[s + 1]) {
            float t = uGradW[s] > 0.0 ? (h - uGradH[s]) / uGradW[s] : 0.0;
            result = uGradC[s] + t * uGradD[s];
        }
    }
    if (h > uGradH[uGradCount - 1]) result = uGradC[uGradCount - 1];
    return result;
}

void main() {
    int res = uChunkI.x;
    int quad = gl_VertexID / 6;
    int corner = gl_VertexID - quad * 6;
    int ix = quad / res;
    int iz = quad - ix * res;
    bool second = corner >= 3;

    ivec2 a = second ? ivec2(ix + 1, iz)     : ivec2(ix, iz);
    ivec2 b = second ? ivec2(ix + 1, iz + 1) : ivec2(ix + 1, iz);
    ivec2 c = ivec2(ix, iz + 1);
    int own = second ? corner - 3 : corner;
    ivec2 me = own == 0 ? a : (own == 1 ? b : c);

    vec3 pa = gridPoint(a);
    vec3 pb = gridPoint(b);
    vec3 pc = gridPoint(c);
    vec3 p  = own == 0 ? pa : (own == 1 ? pb : pc);

    // Flat face normal: cross(v1 - v0, v2 - v0) / |.|, as stored.
    vec3 n = cross(pb - pa, pc - pa);
    float nlen = sqrt(dot(n, n));
    Normal = nlen == 0.0 ? n : n / nlen;

    if (uFlatMode != 0) {
        VertexColor = vec3(0.7);
    } else {
        float meanHeight = (pa.y + pb.y + pc.y) / 3.0;   // "centroid" is reserved
        float norm = clamp((meanHeight - uChunkY.x) / uChunkY.y, 0.0, 1.0);
        int seed = second ? ((ix + 1000) * 1000 + iz + 1000) % 100
                          : (ix * 1000 + iz) % 100;
        float variation = (float(seed) / 100.0 - 0.5) * 0.08;
        VertexColor = clamp(gradientColour(norm) + variation, 0.0, 1.0);
    }

    float gx = gradientAlong(me.x, me.y, true);
    float gz = gradientAlong(me.x, me.y, false);
    SmoothNormal = vec3(-gx, 1.0, -gz) / sqrt(gx * gx + 1.0 + gz * gz);

    TexCoords = p.xz / uTiling;
    PaintCoords = vec2(float(me.x), float(me.y)) / float(max(res, 1));
    FragPos = p;
    gl_Position = projection * view * vec4(p, 1.0);
}""",

    # Blocks terracing: square land columns and their walls, built on the
    # CPU per chunk (engine.terrain_style.build_block_mesh), because a height
    # grid cannot hold vertical walls. Same outputs as terrain.vert.
    'terrain_mesh.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec3 aColor;
layout (location = 3) in vec2 aTexCoord;
layout (location = 4) in vec3 aSmoothNormal;

out vec3 FragPos;
out mediump vec3 Normal;
out mediump vec3 VertexColor;
out vec2 TexCoords;
out vec2 PaintCoords;
out mediump vec3 SmoothNormal;

uniform mat4 projection;
uniform mat4 view;

void main() {
    FragPos      = aPos;
    Normal       = aNormal;
    VertexColor  = aColor;
    TexCoords    = aTexCoord;
    PaintCoords  = aTexCoord;
    SmoothNormal = aSmoothNormal;
    gl_Position  = projection * view * vec4(aPos, 1.0);
}""",

    'terrain.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;

in highp vec3 FragPos;
in vec3 Normal;
in vec3 VertexColor;
in highp vec2 TexCoords;
in highp vec2 PaintCoords;
in vec3 SmoothNormal;

uniform sampler2D texGrass;
uniform sampler2D texRock;
uniform sampler2D texSand;
uniform sampler2D texSnow;
uniform int use_textures;

// Baked terrain material paint. The editor composites source images into a
// persistent per-chunk RGBA8 terrain map, so this is one terrain lookup rather
// than a per-fragment loop over independent stamp/decal records.
uniform sampler2DArray terrainPaintMap;
uniform int uPaintLayer;
uniform int uPaintEnabled;

// Height texture layers. uHeightRange is the terrain's world-space height
// range; uLayerHeights are the sand->grass, grass->rock and rock->snow
// boundaries as fractions of it.
uniform highp vec2 uHeightRange;
uniform vec3 uLayerHeights;
uniform float uLayerBlend;
uniform float uSlopeRock;

// Appearance options (engine/terrain_style.py). Each one is independent.
uniform int uColorMode;              // 0 natural, 1 palette by height, 2 bands
uniform vec3 uPalette[8];
uniform int uPaletteSize;
uniform highp float uBandHeight;
uniform float uContour;
uniform float uContourWidth;
uniform float uGrid;
uniform highp float uGridSize;
uniform highp vec2 uGridOrigin;
uniform float uCellVariation;
uniform vec3 uWallColor;
uniform float uWall;
uniform float uWallStripes;
uniform vec3 uSpeckleColor;
uniform float uSpeckle;
uniform vec3 uPatchColor;
uniform float uPatch;
uniform int uSmoothShading;
uniform int uLightSteps;
uniform int uDither;

struct Light {
    highp vec3 position;
    vec3 color;
    float intensity;
    highp float radius;
    int shadowIndex;
};

uniform Light lights[""" + str(MAX_LIGHTS_TERRAIN) + """];
uniform int active_lights;
""" + SHADOW_GLSL + FOG_GLSL + """
highp float hash(highp vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}
highp float noise(highp vec2 p) {
    highp vec2 i = floor(p);
    highp vec2 f = fract(p);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    highp vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}
float fbm2(highp vec2 p) {
    return noise(p) * 0.6 + noise(p * 2.03 + 17.1) * 0.3 + noise(p * 4.1 + 5.3) * 0.1;
}

float heightFraction(highp float y) {
    return clamp((y - uHeightRange.x) / max(uHeightRange.y - uHeightRange.x, 1e-3), 0.0, 1.0);
}

// Sand, grass, rock and snow weights. Each boundary is a smoothstep, so the
// weights always sum to one; a little noise wobbles the edges so they read
// as natural rather than as contour lines. Steep ground turns to rock.
vec4 layerWeights(highp vec3 worldPos, float slope) {
    float h = heightFraction(worldPos.y)
            + (fbm2(worldPos.xz * 0.01) - 0.5) * uLayerBlend * 2.0;
    float b = max(uLayerBlend, 1e-3);
    float s0 = smoothstep(uLayerHeights.x - b, uLayerHeights.x + b, h);
    float s1 = smoothstep(uLayerHeights.y - b, uLayerHeights.y + b, h);
    float s2 = smoothstep(uLayerHeights.z - b, uLayerHeights.z + b, h);
    vec4 w = vec4(1.0 - s0, s0 - s1, s1 - s2, s2);
    float steep = smoothstep(0.25, 0.5, slope) * uSlopeRock;
    return mix(w, vec4(0.0, 0.0, 1.0, 0.0), steep);
}

vec3 paletteAt(float t) {
    int n = max(uPaletteSize, 1);
    float x = clamp(t, 0.0, 1.0) * float(n - 1);
    int i = int(floor(x));
    int j = min(i + 1, n - 1);
    return mix(uPalette[i], uPalette[j], fract(x));
}

// Index of the colour band / terrace a height belongs to. Terrace flats sit
// exactly on multiples of the band height; the offset keeps each riser with
// the level below it until just under the lip, where the contour line runs.
float bandIndex(highp float y) {
    return floor(y / max(uBandHeight, 1e-3) + 0.12);
}

// Ordered-dither threshold in [0, 1) from a 4x4 Bayer matrix, computed
// arithmetically (no arrays, no integer maths) for the widest driver support.
float bayer2(vec2 a) {
    a = floor(a);
    return fract(dot(a, vec2(0.5, a.y * 0.75)));
}
float bayer4(vec2 fragCoord) {
    return bayer2(fragCoord * 0.5) * 0.25 + bayer2(fragCoord);
}

void main() {
    vec3 smoothNorm = normalize(SmoothNormal);
    vec3 faceNorm = normalize(Normal);
    // The ground is seen from above: keep the face normal on the same side
    // as the (always upward) smooth normal.
    if (dot(faceNorm, smoothNorm) < 0.0) faceNorm = -faceNorm;
    vec3 norm = (uSmoothShading == 1) ? smoothNorm : faceNorm;
    float faceSlope = 1.0 - clamp(faceNorm.y, 0.0, 1.0);
    float flatness = 1.0 - smoothstep(0.15, 0.35, faceSlope);
    vec3 texColor;

    // ---- Base colour --------------------------------------------------
    if (uColorMode == 1) {
        // Palette graded by height, stepped with the terraces.
        highp float y = bandIndex(FragPos.y) * uBandHeight;
        texColor = paletteAt(heightFraction(y));
    } else if (uColorMode == 2) {
        // Strata: each band is one flat colour taken from the palette at
        // the band's height, so colours climb the terrain in order (shore,
        // grass, earth, rock, ...). Neighbouring bands alternate slightly
        // lighter and darker, with a little jitter, so the layers read as
        // separate beds of the same material rather than a smooth ramp.
        highp float band = bandIndex(FragPos.y);
        texColor = paletteAt(heightFraction(band * uBandHeight));
        float alternate = mod(band, 2.0) < 0.5 ? 1.0 : 0.93;
        float jitter = 0.97 + 0.06 * hash(vec2(band, 3.7));
        texColor *= alternate * jitter;
    } else if (use_textures == 1) {
        vec4 splat = layerWeights(FragPos, 1.0 - max(smoothNorm.y, 0.0));

        vec4 grass_col = texture(texGrass, TexCoords * 1.0);
        vec4 rock_col  = texture(texRock,  TexCoords * 0.5);
        vec4 sand_col  = texture(texSand,  TexCoords * 1.5);
        vec4 snow_col  = texture(texSnow,  TexCoords * 0.8);

        vec3 splatColor = (
            sand_col.rgb  * splat.x +
            grass_col.rgb * splat.y +
            rock_col.rgb  * splat.z +
            snow_col.rgb  * splat.w
        );
        // The splat textures are the surface colour. (They used to be
        // multiplied by the biome vertex colour as well, which roughly
        // squared the darkness -- green grass times green vertex colour --
        // and carried the vertex colour's per-chunk seams into textured mode.)
        texColor = splatColor * 1.1;
    } else {
        texColor = VertexColor * 1.1;
    }

        // ---- Baked terrain material paint -------------------------------
    if (uPaintEnabled == 1) {
        vec4 paint = texture(
            terrainPaintMap, vec3(clamp(PaintCoords.yx, 0.0, 1.0),
                                  float(uPaintLayer)));
        texColor = mix(texColor, paint.rgb * 1.1, paint.a);
    }

// ---- Surface details ---------------------------------------------
    highp vec2 cellCoord = (FragPos.xz - uGridOrigin) / max(uGridSize, 1e-3);
    if (uCellVariation > 0.0) {
        float jitter = hash(floor(cellCoord) + vec2(0.37, 0.71)) - 0.5;
        texColor *= 1.0 + jitter * uCellVariation * 0.5;
    }

    if (uPatch > 0.0) {
        float n = fbm2(FragPos.xz * 0.006);
        float m = smoothstep(0.50, 0.58, n) * uPatch * flatness;
        float grain = mix(0.88, 1.06, noise(FragPos.xz * 0.45));
        vec3 patchCol = uPatchColor * grain * mix(0.8, 1.0, smoothstep(0.5, 0.62, n));
        texColor = mix(texColor, patchCol, m);
    }

    if (uWall > 0.0) {
        float wallMask = smoothstep(0.35, 0.7, faceSlope) * uWall;
        highp float yb = FragPos.y / max(uBandHeight, 1e-3);
        vec3 wallCol = uWallColor * mix(0.78, 1.06, fract(yb));
        float stripe = step(fract(yb * 2.0), 0.18);
        wallCol *= 1.0 - stripe * uWallStripes * 0.35;
        texColor = mix(texColor, wallCol, wallMask);
    }

    if (uSpeckle > 0.0) {
        highp vec2 sc = FragPos.xz / 3.0;
        vec2 ci = floor(sc);
        vec2 cf = fract(sc);
        float density = uSpeckle * smoothstep(0.3, 0.7, noise(FragPos.xz * 0.02)) * flatness;
        vec2 centre = vec2(hash(ci + 7.3), hash(ci + 19.1)) * 0.6 + 0.2;
        float dotMask = step(hash(ci), density)
                      * (1.0 - smoothstep(0.16, 0.26, length(cf - centre)));
        // Far away the dots are smaller than a pixel: use their average.
        float farFade = smoothstep(0.15, 0.5, length(fwidth(sc)));
        float amount = mix(dotMask, density * 0.15, farFade);
        texColor = mix(texColor, uSpeckleColor * mix(0.8, 1.1, hash(ci + 3.1)), amount);
    }

    if (uGrid > 0.0) {
        vec2 fw = max(fwidth(cellCoord), vec2(1e-4));
        vec2 d = abs(fract(cellCoord - 0.5) - 0.5) / fw;
        float line = 1.0 - smoothstep(0.5, 1.5, min(d.x, d.y));
        line *= 1.0 - smoothstep(0.15, 0.4, max(fw.x, fw.y));
        texColor *= 1.0 - line * uGrid * (1.0 - smoothstep(0.3, 0.6, faceSlope));
    }

    if (uContour > 0.0) {
        highp float coord = FragPos.y / max(uBandHeight, 1e-3) + 0.12;
        float fw = max(fwidth(coord), 1e-4);
        float f = fract(coord);
        float d = min(f, 1.0 - f) / fw;
        float line = 1.0 - smoothstep(uContourWidth * 0.5 - 0.5, uContourWidth * 0.5 + 0.5, d);
        line *= 1.0 - smoothstep(0.3, 0.6, fw);
        texColor *= 1.0 - line * uContour;
    }

    // ---- Lighting -----------------------------------------------------
    vec3 skyColor    = vec3(0.6, 0.75, 0.9);
    vec3 groundColor = vec3(0.3, 0.25, 0.2);
    float skyFactor  = (norm.y + 1.0) * 0.5;
    vec3 ambient     = (mix(groundColor, skyColor, skyFactor) * 0.3 + uAmbient) * texColor;

    vec3 result  = ambient;
    vec3 sunDir  = normalize(vec3(0.4, 0.7, 0.3));
    vec3 sunColor = vec3(1.0, 0.95, 0.85);
    float sunDiff    = max(dot(norm, sunDir), 0.0);
    float wrappedDiff = (sunDiff + 0.3) / 1.3;
    if (uLightSteps > 0) {
        float steps = float(uLightSteps);
        wrappedDiff = floor(wrappedDiff * steps + 0.5) / steps;
    }
    result += wrappedDiff * sunColor * 0.7 * texColor;

    vec3 fillDir  = normalize(vec3(-0.3, 0.2, -0.4));
    float fillDiff = max(dot(norm, fillDir), 0.0) * 0.2;
    result += fillDiff * skyColor * texColor;

    for (int i = 0; i < active_lights && i < """ + str(MAX_LIGHTS_TERRAIN) + """; i++) {
        highp vec3  toLight  = lights[i].position - FragPos;
        highp float distance = length(toLight);
        if (distance < lights[i].radius) {
            vec3  lightDir    = toLight / distance;
            float diff        = max(dot(norm, lightDir), 0.0);
            float attenuation = 1.0 - smoothstep(0.0, lights[i].radius, distance);
            attenuation       = attenuation * attenuation;
            float shadow      = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius, diff);
            result += (1.0 - shadow) * diff * lights[i].color * lights[i].intensity * attenuation * texColor;
        }
    }

    float gray = dot(result, vec3(0.299, 0.587, 0.114));
    result = mix(vec3(gray), result, 1.15);

    result = applyFog(result, FragPos);
    if (uDither > 0) {
        float levels = float(uDither);
        result = floor(clamp(result, 0.0, 1.0) * levels + bayer4(gl_FragCoord.xy)) / levels;
    }
    FragColor = vec4(result, 1.0);
}""",

    'grass.vert': """#version 330 core

// One instance is one grass blade. The CPU supplies the root position (already
// sitting on the terrain surface), a size, a yaw/seed and a colour variation;
// the GPU builds the tapered blade from gl_VertexID.
//
// A blade with S segments is (S - 1) quads followed by a tip triangle, so a
// draw uses (S - 1) * 6 + 3 vertices per instance. Near chunks draw several
// segments; far chunks draw the S = 1 blade, which is the tip triangle alone.
// Between uLodStart and uLodEnd every blade morphs its width, bend and wind
// profile onto that single triangle, so the swap is invisible.
layout (location = 2) in vec3 iPosition;
layout (location = 3) in float iSize;
layout (location = 4) in float iPhase;
layout (location = 5) in float iVariation;
layout (location = 6) in vec3 iGround;     // colour of the ground under the blade

out vec3 FragPos;
out vec3 BladeNormal;
out float BladeHeight;
out vec3 BladeTint;
out vec3 GroundColor;

uniform mat4 projection;
uniform mat4 view;
uniform float time;
uniform float windStrength;
uniform vec3 cameraPos;
uniform int uSegments;
uniform float uLodStart;
uniform float uLodEnd;
uniform float uFadeStart;
uniform float uFadeEnd;
uniform float uBladeHeight;
uniform float uBladeWidth;

float hash1(float n) {
    return fract(sin(n) * 43758.5453123);
}

// Classic Perlin 2D noise by Stefan Gustavson (MIT).
vec4 permute(vec4 x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }
vec2 fade(vec2 t) { return t * t * t * (t * (t * 6.0 - 15.0) + 10.0); }
float cnoise(vec2 P) {
    vec4 Pi = floor(P.xyxy) + vec4(0.0, 0.0, 1.0, 1.0);
    vec4 Pf = fract(P.xyxy) - vec4(0.0, 0.0, 1.0, 1.0);
    Pi = mod(Pi, 289.0);
    vec4 ix = Pi.xzxz;
    vec4 iy = Pi.yyww;
    vec4 fx = Pf.xzxz;
    vec4 fy = Pf.yyww;
    vec4 i = permute(permute(ix) + iy);
    vec4 gx = 2.0 * fract(i * 0.0243902439) - 1.0;
    vec4 gy = abs(gx) - 0.5;
    vec4 tx = floor(gx + 0.5);
    gx = gx - tx;
    vec2 g00 = vec2(gx.x, gy.x);
    vec2 g10 = vec2(gx.y, gy.y);
    vec2 g01 = vec2(gx.z, gy.z);
    vec2 g11 = vec2(gx.w, gy.w);
    vec4 norm = 1.79284291400159 - 0.85373472095314 *
                vec4(dot(g00, g00), dot(g01, g01), dot(g10, g10), dot(g11, g11));
    g00 *= norm.x; g01 *= norm.y; g10 *= norm.z; g11 *= norm.w;
    float n00 = dot(g00, vec2(fx.x, fy.x));
    float n10 = dot(g10, vec2(fx.y, fy.y));
    float n01 = dot(g01, vec2(fx.z, fy.z));
    float n11 = dot(g11, vec2(fx.w, fy.w));
    vec2 fade_xy = fade(Pf.xy);
    vec2 n_x = mix(vec2(n00, n01), vec2(n10, n11), fade_xy.x);
    return 2.3 * mix(n_x.x, n_x.y, fade_xy.y);
}

// Quadratic bezier from 0 to 1 with a low control point: a blade that stays
// upright at the root and curls over towards the tip.
float bezier(float t, float p1) {
    float it = 1.0 - t;
    return 2.0 * it * t * p1 + t * t;
}

float bendT(float h, float bendStart) {
    return clamp((h - bendStart) / (1.0 - bendStart), 0.0, 1.0);
}

// Horizontal displacement of the blade's centre line at height fraction h.
vec2 centreOffset(float h, float lod, vec2 forward, vec2 windDir,
                  float bendStrength, float bendStart, float strong, float gentle) {
    float bend = mix(bezier(bendT(h, bendStart), 0.1), h, lod);
    float sway = mix(bendT(h, bendStart), h, lod);
    float windProfile = mix(h * h, h, lod);
    return forward * (bendStrength * bend + gentle * sway)
         + windDir * (strong * windProfile);
}

void main() {
    int segments = max(uSegments, 1);
    int bodyQuads = segments - 1;
    int quad = gl_VertexID / 6;
    int corner = gl_VertexID - quad * 6;

    float level;
    float side;
    if (quad < bodyQuads) {
        // (L,i) (R,i) (L,i+1) / (L,i+1) (R,i) (R,i+1)
        int up = (corner == 2 || corner == 3 || corner == 5) ? 1 : 0;
        side = (corner == 1 || corner == 4 || corner == 5) ? 1.0 : -1.0;
        level = float(quad + up) / float(segments);
    } else {
        level = (corner == 2) ? 1.0 : float(bodyQuads) / float(segments);
        side = (corner == 0) ? -1.0 : ((corner == 1) ? 1.0 : 0.0);
    }

    float dist = distance(cameraPos.xz, iPosition.xz);
    float lod = (segments == 1) ? 1.0 : smoothstep(uLodStart, uLodEnd, dist);
    float distanceScale = 1.0 - smoothstep(uFadeStart, uFadeEnd, dist);

    float seedA = hash1(iPhase * 12.9898 + iVariation * 78.233);
    float seedB = hash1(seedA * 91.7 + 3.1);
    float seedC = hash1(seedB * 47.3 + 7.9);

    float height = uBladeHeight * iSize * mix(0.7, 1.3, seedC) * distanceScale;
    float halfWidth = uBladeWidth * mix(0.8, 1.2, seedB) * (0.5 + 0.5 * distanceScale);

    // Static curl direction is fixed in the world. The flat side of the
    // blade turns towards the camera (with a stable per-blade offset) so a
    // field never shows its blades edge-on.
    vec2 forward = vec2(cos(iPhase), sin(iPhase));
    vec2 toCamera = cameraPos.xz - iPosition.xz;
    toCamera = dot(toCamera, toCamera) > 1e-6 ? normalize(toCamera) : vec2(0.0, 1.0);
    float turn = (seedA - 0.5) * 1.2;
    vec2 cameraSide = vec2(-toCamera.y, toCamera.x);
    vec2 sideDir = vec2(cameraSide.x * cos(turn) - cameraSide.y * sin(turn),
                        cameraSide.x * sin(turn) + cameraSide.y * cos(turn));

    // Wind: a slow Perlin field rolling across the world, biased downwind,
    // plus a small per-blade flutter.
    vec2 windDir = normalize(vec2(1.0, 0.35));
    float wave = cnoise(iPosition.xz * 0.012 - windDir * time * 0.6);
    float strong = (wave * 0.5 + 0.2) * windStrength * height;
    float gentle = sin(time * 1.9 + seedA * 10.0) * 0.08 * height;
    float bendStrength = mix(0.15, 0.45, seedA) * height;
    float bendStart = mix(0.0, 0.3, seedB);

    // Taper: the detailed blade narrows gently and then closes in the tip
    // triangle; the far blade is a plain triangle. Morph between the two.
    float widthHigh = halfWidth * (1.0 - 0.45 * level);
    float widthLow = halfWidth * (1.0 - level);
    float width = mix(widthHigh, widthLow, lod);

    vec2 offset = centreOffset(level, lod, forward, windDir,
                               bendStrength, bendStart, strong, gentle);
    // Keep the blade roughly its own length as it leans over.
    float rise = level * height;
    float y = sqrt(max(rise * rise - dot(offset, offset) * 0.6, 0.2 * rise * rise));

    vec3 root = iPosition;
    vec3 p = vec3(root.x + offset.x + sideDir.x * side * width,
                  root.y + y,
                  root.z + offset.y + sideDir.y * side * width);

    // Surface normal from the bent centre line and the blade's width axis,
    // then tilted across the width so the flat card shades like a rounded
    // blade.
    float h2 = min(level + 0.05, 1.0);
    float h1 = h2 - 0.05;
    vec2 o1 = centreOffset(h1, lod, forward, windDir, bendStrength, bendStart, strong, gentle);
    vec2 o2 = centreOffset(h2, lod, forward, windDir, bendStrength, bendStart, strong, gentle);
    vec3 tangent = vec3(o2.x - o1.x, (h2 - h1) * height, o2.y - o1.y);
    vec3 side3 = vec3(sideDir.x, 0.0, sideDir.y);
    vec3 n = cross(side3, tangent);
    n = dot(n, n) > 1e-10 ? normalize(n) : vec3(0.0, 1.0, 0.0);
    if (dot(n, cameraPos - p) < 0.0) n = -n;
    BladeNormal = normalize(n + side3 * side * 0.6);

    // Colour variation: a large-scale patchiness across the field and a few
    // sun-dried blades.
    float fieldPatch = cnoise(iPosition.xz * 0.0035) * 0.5 + 0.5;
    float dry = smoothstep(0.78, 1.0, seedC) * 0.7;
    BladeTint = mix(vec3(1.0), vec3(1.25, 1.1, 0.55), dry)
              * mix(0.82, 1.12, fieldPatch) * iVariation;

    GroundColor = iGround;
    FragPos = p;
    BladeHeight = level;
    gl_Position = projection * view * vec4(p, 1.0);
}

""",

    'grass.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec3 BladeNormal;
in float BladeHeight;
in vec3 BladeTint;
in vec3 GroundColor;

uniform vec3 grassColor;       // blade colour, when one was chosen
uniform vec3 grassTipColor;    // tip colour, when one was chosen
uniform int uMatchGround;      // 1: blades take the colour of their ground
uniform int uTipAuto;          // 1: tips are a sun-bleached shade of the blade
uniform vec3 cameraPos;
""" + FOG_GLSL + """
void main() {
    // Shaded roots rising through the blade colour to the tip colour.
    float g = smoothstep(0.0, 1.0, BladeHeight);
    vec3 bladeColor = (uMatchGround == 1) ? GroundColor : grassColor;
    vec3 tipColor = (uTipAuto == 1)
        ? mix(min(bladeColor * 1.3, vec3(1.0)), vec3(0.80, 0.78, 0.55), 0.3)
        : grassTipColor;
    vec3 rootColor = bladeColor * 0.45;
    vec3 albedo = (g < 0.5 ? mix(rootColor, bladeColor, g * 2.0)
                           : mix(bladeColor, tipColor, g * 2.0 - 1.0)) * BladeTint;

    vec3 n = normalize(BladeNormal);
    // Lean the lighting normal towards the ground normal so a field reads
    // as one surface lit the same way as the terrain under it.
    vec3 nLight = normalize(mix(n, vec3(0.0, 1.0, 0.0), 0.45));
    vec3 viewDir = normalize(cameraPos - FragPos);

    vec3 sunDir = normalize(vec3(0.4, 0.7, 0.3));
    vec3 sunColor = vec3(1.0, 0.95, 0.85);
    vec3 skyColor = vec3(0.6, 0.75, 0.9);
    vec3 groundColor = vec3(0.3, 0.25, 0.2);

    float diffuse = (max(dot(nLight, sunDir), 0.0) + 0.3) / 1.3;
    // Light shining through the blade when looking towards the sun.
    float translucency = pow(max(dot(-viewDir, sunDir), 0.0), 4.0) * 0.45 * g;
    float specular = pow(max(dot(reflect(-sunDir, n), viewDir), 0.0), 24.0) * 0.12 * g;
    float occlusion = mix(0.55, 1.0, g);

    vec3 ambient = mix(groundColor, skyColor, (nLight.y + 1.0) * 0.5) * 0.3 + uAmbient;
    vec3 color = albedo * (ambient * occlusion + sunColor * (diffuse * 0.7 + translucency))
               + sunColor * specular;

    float gray = dot(color, vec3(0.299, 0.587, 0.114));
    color = mix(vec3(gray), color, 1.15);
    FragColor = vec4(applyFog(color, FragPos), 1.0);
}
""",

}

# ----- Low-power shaders (used by ForwardRenderer when lowpower_mode is True) ----
DEFAULT_SHADERS['lit_arm.vert'] = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
out vec3 FragPos;
out vec3 Normal;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalMatrix * aNormal;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}"""

DEFAULT_SHADERS['lit_arm.frag'] = """#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
uniform vec3 object_color;
uniform float alpha;
uniform vec3 emissive_factor;
struct Light { vec3 position; vec3 color; float intensity; float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS_ARM) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = (vec3(0.12) + uAmbient) * object_color;
    for(int i = 0; i < active_lights && i < """ + str(MAX_LIGHTS_ARM) + """; i++) {
        vec3 toLight = lights[i].position - FragPos;
        float distSq = dot(toLight, toLight);
        float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            float dist = sqrt(distSq);
            vec3 lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - (dist / lights[i].radius);
            att = att * att;
            float shadow = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius, diff);
            result += (1.0 - shadow) * (diff * lights[i].color * lights[i].intensity * att) * object_color;
        }
    }
    result += emissive_factor;
    FragColor = vec4(applyFog(result, FragPos), alpha);
}"""

DEFAULT_SHADERS['textured_arm.vert'] = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;
out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoords;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;
uniform vec2 tex_scale;   // per-face stretch / tiling factor
uniform float tex_angle;  // per-face free rotation in radians
uniform vec2 tex_shift;   // per-face UV offset (in texture repeats)
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalMatrix * aNormal;
    // Surface-inspector transform: rotate the base 0..1 face UVs about their
    // centre, then apply stretch and shift (Radiant-style free controls).
    vec2 uv = aTexCoords - vec2(0.5);
    float s = sin(tex_angle);
    float c = cos(tex_angle);
    uv = vec2(uv.x * c - uv.y * s, uv.x * s + uv.y * c);
    TexCoords = (uv + vec2(0.5)) * tex_scale + tex_shift;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}"""

DEFAULT_SHADERS['textured_arm.frag'] = """#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoords;
uniform sampler2D texture_diffuse;
uniform vec4 model_tint;
uniform vec3 emissive_factor;
struct Light { vec3 position; vec3 color; float intensity; float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS_ARM) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
    // A model's tint; all zeros (the default) leaves the texture as it is.
    texColor.rgb *= mix(vec3(1.0), model_tint.rgb, model_tint.a);
    vec3 norm = normalize(Normal);
    vec3 result = (vec3(0.12) + uAmbient) * texColor.rgb;
    for(int i = 0; i < active_lights && i < """ + str(MAX_LIGHTS_ARM) + """; i++) {
        vec3 toLight = lights[i].position - FragPos;
        float distSq = dot(toLight, toLight);
        float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            float dist = sqrt(distSq);
            vec3 lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - (dist / lights[i].radius);
            att = att * att;
            float shadow = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius, diff);
            result += (1.0 - shadow) * (diff * lights[i].color * lights[i].intensity * att) * texColor.rgb;
        }
    }
    result += emissive_factor;
    FragColor = vec4(applyFog(result, FragPos), texColor.a);
}"""

DEFAULT_SHADERS['fog_arm.frag'] = """#version 330 core
out vec4 FragColor;
in vec3 localPos;
uniform mat4 model;
uniform mat4 inverseModel;
uniform vec3 viewPos;
uniform float density;
uniform vec3 fogColor;
uniform sampler3D noiseTexture;
uniform float noiseScale;
uniform float time;

vec2 intersectBox(vec3 rayOrigin, vec3 rayDir) {
    vec3 tMin = (-0.5 - rayOrigin) / rayDir;
    vec3 tMax = ( 0.5 - rayOrigin) / rayDir;
    vec3 t1 = min(tMin, tMax);
    vec3 t2 = max(tMin, tMax);
    float tNear = max(max(t1.x, t1.y), t1.z);
    float tFar  = min(min(t2.x, t2.y), t2.z);
    return vec2(tNear, tFar);
}

void main() {
    vec3 fragWorldPos   = vec3(model * vec4(localPos, 1.0));
    vec3 rayDirWorld    = normalize(fragWorldPos - viewPos);
    vec3 rayOriginLocal = (inverseModel * vec4(viewPos,       1.0)).xyz;
    vec3 rayDirLocal    = normalize((inverseModel * vec4(rayDirWorld, 0.0)).xyz);
    vec2 t = intersectBox(rayOriginLocal, rayDirLocal);
    float tNear = t.x;
    float tFar  = t.y;
    if (tNear >= tFar) discard;
    tNear = max(0.0, tNear);

    int   num_steps = 16;
    float stepSize  = (tFar - tNear) / float(num_steps);
    vec4  accumulatedColor = vec4(0.0);

    for (int i = 0; i < num_steps; ++i) {
        float currentT   = tNear + float(i) * stepSize;
        vec3  samplePos  = rayOriginLocal + rayDirLocal * currentT;
        vec3  noiseCoord = samplePos * noiseScale + vec3(0.0, 0.0, time * 0.1);
        float noiseValue = texture(noiseTexture, noiseCoord).r;
        float stepDensity   = density * noiseValue;
        float transmittance = exp(-stepDensity * stepSize);
        accumulatedColor.rgb += fogColor * (1.0 - transmittance) * (1.0 - accumulatedColor.a);
        accumulatedColor.a   += (1.0 - transmittance);
        if (accumulatedColor.a > 0.95) break;
    }
    accumulatedColor.a = clamp(accumulatedColor.a, 0.0, 1.0);
    FragColor = accumulatedColor;
}"""

# ----- Portal shaders (used by ForwardRenderer for stencil portals) -----
DEFAULT_SHADERS['portal_mask.vert'] = """#version 330 core
layout(location = 0) in vec3 aPos;
uniform mat4 projection;
uniform mat4 view;
void main() {
    gl_Position = projection * view * vec4(aPos, 1.0);
}"""

DEFAULT_SHADERS['portal_mask.frag'] = """#version 330 core
out vec4 FragColor;
void main() {
    FragColor = vec4(0.0);
}"""

# A fading portal: what was behind the aperture, laid back over the portal's
# view with alpha (1 - fade), so the view's opacity is the portal's fade.
DEFAULT_SHADERS['portal_fade.frag'] = """#version 330 core
out vec4 FragColor;
uniform sampler2D behind;
uniform vec4 viewport_rect;
uniform float alpha;
void main() {
    vec2 uv = (gl_FragCoord.xy - viewport_rect.xy) / viewport_rect.zw;
    FragColor = vec4(texture(behind, uv).rgb, alpha);
}"""

DEFAULT_SHADERS['portal_rim.vert'] = """#version 330 core
layout(location = 0) in vec3 aPos;
uniform mat4 projection;
uniform mat4 view;
void main() {
    gl_Position = projection * view * vec4(aPos, 1.0);
}"""

DEFAULT_SHADERS['portal_rim.frag'] = """#version 330 core
out vec4 FragColor;
uniform vec4 rim_color;
void main() {
    FragColor = rim_color;
}"""


DEFAULT_SHADERS['decal.vert'] = """#version 330 core
precision highp float;
// A projected decal: its box, drawn as the unit cube. One instance a decal.
layout (location = 0) in vec3 aPos;
layout (location = 3) in vec4 iModel0;
layout (location = 4) in vec4 iModel1;
layout (location = 5) in vec4 iModel2;
layout (location = 6) in vec4 iModel3;
layout (location = 7) in vec4 iParams;      // variant, opacity, kind, flags
// The box's inverse, worked out once on the CPU when the row was made
// (DecalTable), not for every vertex of every frame.
layout (location = 8) in vec4 iInverse0;
layout (location = 9) in vec4 iInverse1;
layout (location = 10) in vec4 iInverse2;
layout (location = 11) in vec4 iInverse3;

uniform mat4 projection;
uniform mat4 view;

flat out mat4 WorldToBox;
flat out vec4 Params;

void main() {
    mat4 model = mat4(iModel0, iModel1, iModel2, iModel3);
    WorldToBox = mat4(iInverse0, iInverse1, iInverse2, iInverse3);
    Params = iParams;
    gl_Position = projection * view * model * vec4(aPos, 1.0);
}"""

DEFAULT_SHADERS['decal.frag'] = """#version 330 core
precision highp float;
// The surface under this pixel -- whatever was drawn there, read back from
// the depth copy -- is put in the decal box's own space. Inside the box it
// takes the decal's texture; outside, nothing. So one box paints a wall,
// the floor below it and the corner between, without knowing either.
//
// Kinds (Params.z): 0/1 blood on a wall/floor (a variant within that kind's
// images), 2 a placed image, 3 a placed wet patch (the image is a mask of
// where the surface is wet). The wet rows are drawn by a second draw of the
// same pass (wetPass 1), over a copy of the frame so far, darkening the
// surface and reflecting the scene in it.
out vec4 FragColor;

flat in mat4 WorldToBox;
flat in vec4 Params;

uniform highp sampler2D sceneDepth;
uniform mediump sampler2DArray decalTextures;
// Placed images: each sits unstretched in its layer of their own array, from
// the corner, filling this much of it (useLayerScale 1 while drawing them).
uniform vec2 imageLayerScale[64];
uniform int useLayerScale;
uniform vec4 decalRanges;       // first layer and count: wall images, floor images
uniform mat4 invViewProjection;
uniform vec4 viewport;          // x, y, width, height of the depth copy
uniform vec3 viewPos;
// The wet pass: the frame so far, and what the reflection march needs.
uniform int wetPass;
uniform int reflectionsOn;      // 'reflections 0' turns every one off
uniform float decalFadeEnd;     // 'decalfade': no reflection from here on
uniform sampler2D sceneColor;
uniform highp mat4 view;
uniform highp mat4 projection;
uniform highp mat4 invProjection;
""" + SSR_GLSL + """
// A wet surface's reflection of the hit at *uv*, *travelled* view units up
// the ray from *originView*: glossy, not a mirror. A cone of half-angle
// atan(coneTan) widens along the ray, so the reflection is sharp where the
// reflected thing meets the surface and softer the further away it is --
// read from the frame copy's mip chain at the cone's width on screen -- and
// smeared upright, as light on a wet floor is.
vec3 glossyReflection(highp vec2 uv, highp float travelled,
                      highp vec3 originView, highp vec3 dirView, float coneTan) {
    highp vec3 hit = originView + dirView * travelled;
    highp float height = float(textureSize(sceneColor, 0).y);
    highp float radiusPx = travelled * coneTan * projection[1][1] * 0.5 * height
                         / max(-hit.z, 1.0);
    float lod = clamp(log2(max(radiusPx, 1.0)), 0.0, 7.0);
    highp vec2 smear = vec2(0.0, radiusPx * 1.2 / height);
    return (textureLod(sceneColor, uv, lod).rgb * 2.0
            + textureLod(sceneColor, clamp(uv + smear, 0.0, 1.0), lod).rgb
            + textureLod(sceneColor, clamp(uv - smear, 0.0, 1.0), lod).rgb) * 0.25;
}

const float BLOOD_STRENGTH = 2.6;
const float IMAGE_STRENGTH = 2.0;   // modulate 2x: mid grey leaves it as it is

highp vec3 hash3(highp vec3 p) {
    p = fract(p * vec3(0.1031, 0.1030, 0.0973));
    p += dot(p, p.yxz + 33.33);
    return fract((p.xxy + p.yxx) * p.zyx);
}

void main() {
    bool wetRow = Params.z > 2.5;
    // Flag 8: an emissive decal. Its own draw (wetPass 2), painted as it is.
    bool glow = !wetRow && Params.w > 7.5;
    if ((wetRow ? 1 : (glow ? 2 : 0)) != wetPass) discard;   // another draw's rows
    vec2 uv = (gl_FragCoord.xy - viewport.xy) / viewport.zw;
    float depth = texture(sceneDepth, uv).r;
    if (depth >= 0.99999) discard;                      // sky: no surface
    vec4 clip = vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    vec4 world = invViewProjection * clip;
    world /= world.w;
    vec3 local = (WorldToBox * world).xyz;              // -0.5..0.5 inside
    if (any(greaterThan(abs(local), vec3(0.5)))) discard;

    // The surface's facing, from how the world position changes across the
    // pixel, in the box's space. The box's z is the way the decal was
    // thrown: a surface facing that way (the far side of a thin wall) gets
    // none.
    vec3 normal = normalize(cross(dFdx(world.xyz), dFdy(world.xyz)));
    if (dot(normal, viewPos - world.xyz) < 0.0) normal = -normal;
    vec3 facing = normalize(mat3(WorldToBox) * normal);
    if (facing.z > 0.35) discard;

    // Across a surface square to the throw the texture lies on the box's
    // x and y. On one that turns away -- the floor under a wall, a side
    // wall -- it is unfolded about the box's middle: the distance out from
    // the wall carries on from where the wall left off, so a run that
    // reaches the foot of a wall goes on across the floor and nothing is
    // smeared along the throw.
    vec2 at = local.xy + facing.xy * local.z;
    if (any(greaterThan(abs(at), vec2(0.5)))) discard;
    float layer;
    if (Params.z > 1.5) {
        layer = Params.x;                               // a placed decal's image
    } else {
        vec2 range = Params.z > 0.5 ? decalRanges.zw : decalRanges.xy;
        layer = range.x + mod(Params.x, max(range.y, 1.0));
    }
    // Flags: 1 mirrored, 2 selected by the editor's Decal tool (tinted),
    // 4 a sign's white words (they lighten past the framebuffer's 1; below),
    // 8 emissive (above).
    bool mirrored = mod(Params.w, 2.0) > 0.5;
    bool picked = mod(floor(Params.w * 0.5), 2.0) > 0.5;
    bool lighten = mod(floor(Params.w * 0.25), 2.0) > 0.5;
    // The box's x runs to the left of someone looking along the throw (its
    // up is up, its z into the surface), so the image is read along -x: its
    // left is on the viewer's left, and text reads the right way round.
    float across = mirrored ? at.x : -at.x;
    vec2 st = vec2(across + 0.5, 0.5 - at.y);
    if (useLayerScale == 1) {
        int index = int(layer + 0.5);
        if (index < 64) st *= imageLayerScale[index];
    }
    vec4 texel = texture(decalTextures, vec3(st, layer));
    // A placed decal's tint, less one, rides in the bottom row of its box's
    // inverse -- (0, 0, 0, 1) for any box, and read by nothing above -- so
    // an untinted decal, and blood, multiply by white.
    vec3 tint = vec3(1.0) + vec3(WorldToBox[0].w, WorldToBox[1].w, WorldToBox[2].w);
    const vec3 PICKED = vec3(1.0, 0.55, 0.1);
    // Fade at the box's near and far ends rather than cut.
    float ends = 1.0 - smoothstep(0.38, 0.5, abs(local.z));
    float amount = texel.a * Params.y * ends;
    if (amount <= 0.004) discard;

    if (wetRow) {
        // Wet: where the mask is strongest, standing water -- flat, a glossy
        // reflection, sharp at contact and softening with distance; towards
        // its edges, damp -- a duller, broken-up, blurrier one.
        // Either way the surface darkens, which is most of looking wet. The
        // reflection fades out with distance and past that is not traced.
        float puddle = smoothstep(0.45, 0.9, texel.a);
        vec3 base = texture(sceneColor, uv).rgb;
        vec3 colour = base * mix(0.6, 0.45, puddle) * tint;
        highp vec3 surfaceView = (view * world).xyz;
        // Full strength to two thirds of the fade distance, none from it on.
        float fade = reflectionsOn == 1 && decalFadeEnd > 0.0
            ? 1.0 - smoothstep(decalFadeEnd * 0.6667, decalFadeEnd, length(surfaceView))
            : 0.0;
        if (fade > 0.0) {
            float rough = mix(0.32, 0.015, puddle);
            vec3 n = normalize(normal + (hash3(floor(world.xyz * 4.0)) - 0.5) * rough);
            vec3 nView = normalize(mat3(view) * n);
            vec3 toSurface = normalize(surfaceView);
            float facingCam = clamp(dot(nView, -toSurface), 0.0, 1.0);
            float fresnel = 0.04 + 0.96 * pow(1.0 - facingCam, 5.0);
            float strength = mix(0.3, 0.95, puddle) * mix(0.6, 1.0, fresnel);
            // A longer first stride than the water's: a puddle reflects the
            // crate standing a few hundred units off, not just its own rim.
            highp vec3 dirView = reflect(toSurface, nView);
            highp vec2 hitUV;
            highp float hitS;
            float hitFade;
            if (marchReflection(surfaceView, dirView, max(6.0, -surfaceView.z * 0.06),
                                hitUV, hitS, hitFade)) {
                vec3 glossy = glossyReflection(hitUV, hitS, surfaceView, dirView,
                                               mix(0.30, 0.12, puddle));
                // Far things reflect fainter, as they do on a wet floor.
                float reach = 1.0 - 0.6 * smoothstep(0.0, 900.0, hitS);
                colour = mix(colour, glossy, hitFade * strength * fade * reach);
            }
        }
        if (picked) colour = mix(colour, PICKED, 0.35);
        FragColor = vec4(colour, amount);               // over what is there
        return;
    }
    if (glow) {
        // Emissive: a screen, a lit sign. Painted over the surface in its
        // own colours, whatever light falls there -- the whole face equally
        // bright, in a dark room as in a lit one -- not stained into it.
        vec3 lit = texel.rgb * tint;
        if (picked) lit = mix(lit, PICKED, 0.5);
        FragColor = vec4(lit, amount);                  // over what is there
        return;
    }
    // Stained, not painted: the blend is dst * (src.rgb + 1 - src.a), so
    // the surface keeps its own light and shadow -- blood in a dark room is
    // dark -- while the colour, pushed past 1, stays red on a grey wall
    // instead of going to black. A placed image modulates 2x: grey leaves
    // the surface alone, darker parts darken it, lighter parts lighten it.
    float strength = Params.z > 1.5 ? IMAGE_STRENGTH : BLOOD_STRENGTH;
    vec3 stain = picked ? mix(texel.rgb * tint, PICKED, 0.5) : texel.rgb * tint;
    vec3 colour = stain * (amount * strength);
    float over = max(colour.r, max(colour.g, colour.b)) - 1.0;
    if (lighten && !picked && over > 0.0) {
        // A sign's white words: the framebuffer stops src.rgb at 1, so the
        // part past 1 goes in alpha -- dst * (rgb + 1 - a) is unchanged, up
        // to twice the surface's own brightness, its light still kept.
        FragColor = vec4(max(colour - over, 0.0), amount - over);
        return;
    }
    FragColor = vec4(colour, amount);
}"""


def _terrain_indexed_sources():
    """The heightfield terrain program as it is drawn: ``terrain_indexed.vert``
    and ``terrain_flat.frag``, derived from ``terrain.vert``/``terrain.frag``.

    ``terrain.vert`` draws a chunk as six unshared vertices a quad, and works
    out a triangle's flat normal and colour at each of its three vertices.
    Here the two are ``flat`` outputs, taken from the triangle's provoking
    vertex (its last: GL's default convention, which nothing in Fio changes),
    and the chunk is drawn indexed (``engine.terrain.heightfield_indices``)
    over two layers of the grid's points:

    * layer 0, ``id = i * (res + 1) + k``: grid point (i, k), carrying the
      face values of the first triangle of quad (i, k);
    * layer 1, ``id = (res + 1)^2 + i * (res + 1) + k``: the same point,
      carrying those of the second triangle of quad (i - 1, k - 1).

    So ``2 (res + 1)^2`` vertices are shaded instead of ``6 res^2`` (4,802
    against 13,824 at res 48). Every value that reaches a fragment comes from
    the same expressions on the same operands as in ``terrain.vert``, which
    stays as the reference the parity tests capture vertex by vertex;
    ``terrain.frag`` (smooth inputs) still serves the block-terrace mesh.
    """
    vert = DEFAULT_SHADERS['terrain.vert']
    head, marker, _ = vert.partition('void main() {')
    assert marker, 'terrain.vert: main() not found'
    for name in ('Normal', 'VertexColor'):
        old = 'out mediump vec3 %s;' % name
        assert old in head, name
        head = head.replace(old, 'flat ' + old)
    main = """void main() {
    int res = uChunkI.x;
    int side = res + 1;
    int points = side * side;
    bool second = gl_VertexID >= points;
    int g = second ? gl_VertexID - points : gl_VertexID;
    int i = g / side;
    int k = g - i * side;
    ivec2 me = ivec2(i, k);

    // The triangle this vertex provokes, with terrain.vert's corners a, b, c:
    // the first of quad (i, k), or the second of quad (i - 1, k - 1).
    int ix = second ? i - 1 : i;
    int iz = second ? k - 1 : k;
    ivec2 a = second ? ivec2(ix + 1, iz)     : ivec2(ix, iz);
    ivec2 b = second ? ivec2(ix + 1, iz + 1) : ivec2(ix + 1, iz);
    ivec2 c = ivec2(ix, iz + 1);

    vec3 pa = gridPoint(a);
    vec3 pb = gridPoint(b);
    vec3 pc = gridPoint(c);
    vec3 p  = second ? pb : pa;

    // Flat face normal: cross(v1 - v0, v2 - v0) / |.|, as stored.
    vec3 n = cross(pb - pa, pc - pa);
    float nlen = sqrt(dot(n, n));
    Normal = nlen == 0.0 ? n : n / nlen;

    if (uFlatMode != 0) {
        VertexColor = vec3(0.7);
    } else {
        float meanHeight = (pa.y + pb.y + pc.y) / 3.0;
        float norm = clamp((meanHeight - uChunkY.x) / uChunkY.y, 0.0, 1.0);
        int seed = second ? ((ix + 1000) * 1000 + iz + 1000) % 100
                          : (ix * 1000 + iz) % 100;
        float variation = (float(seed) / 100.0 - 0.5) * 0.08;
        VertexColor = clamp(gradientColour(norm) + variation, 0.0, 1.0);
    }

    float gx = gradientAlong(me.x, me.y, true);
    float gz = gradientAlong(me.x, me.y, false);
    SmoothNormal = vec3(-gx, 1.0, -gz) / sqrt(gx * gx + 1.0 + gz * gz);

    TexCoords = p.xz / uTiling;
    PaintCoords = vec2(float(me.x), float(me.y)) / float(max(res, 1));
    FragPos = p;
    gl_Position = projection * view * vec4(p, 1.0);
}"""
    frag = DEFAULT_SHADERS['terrain.frag']
    for name in ('Normal', 'VertexColor'):
        old = 'in vec3 %s;' % name
        assert frag.count(old) == 1, name
        frag = frag.replace(old, 'flat in vec3 %s;' % name)
    return head + main, frag


(DEFAULT_SHADERS['terrain_indexed.vert'],
 DEFAULT_SHADERS['terrain_flat.frag']) = _terrain_indexed_sources()


# The optional 360-degree panorama is kept in the central shader registry so
# desktop and GLES portability checks compile it with every other shader.
DEFAULT_SHADERS['void_background.vert'] = """#version 330 core
// The sky: a full-screen triangle at the far plane. The view ray is linear
// across the screen, so each vertex works out its world direction and the
// rasteriser interpolates it exactly; a pixel only reads the cube map.
uniform mat4 u_inverse_projection;
uniform mat4 u_inverse_view;
out vec3 v_direction;
void main() {
    vec2 p;
    if (gl_VertexID == 0) {
        p = vec2(-1.0, -1.0);
    } else if (gl_VertexID == 1) {
        p = vec2(3.0, -1.0);
    } else {
        p = vec2(-1.0, 3.0);
    }
    vec4 view_h = u_inverse_projection * vec4(p, 1.0, 1.0);
    v_direction = mat3(u_inverse_view) * (view_h.xyz / view_h.w);
    gl_Position = vec4(p, 1.0, 1.0);
}
"""
DEFAULT_SHADERS['void_background.frag'] = """#version 330 core
in vec3 v_direction;
out vec4 frag_colour;
// The panorama as a cube map (sky_cube.frag made it once, at load): one
// read a pixel, no trigonometry.
uniform samplerCube u_sky;
void main() {
    frag_colour = vec4(texture(u_sky, v_direction).rgb, 1.0);
}
"""
# Made once when a panorama loads: each face of the sky's cube map drawn
# from the equirectangular image, by the mapping the water reflects it with
# (panoramaUV), so the sky and its reflection agree.
DEFAULT_SHADERS['sky_cube.vert'] = """#version 330 core
out vec2 v_uv;
void main() {
    vec2 p;
    if (gl_VertexID == 0) {
        p = vec2(-1.0, -1.0);
    } else if (gl_VertexID == 1) {
        p = vec2(3.0, -1.0);
    } else {
        p = vec2(-1.0, 3.0);
    }
    v_uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
# A reflection probe's snapshot (ForwardRenderer._capture_reflection_probe):
# its cube map drawn out as an equirectangular image, the inverse of
# panoramaUV, so water reads it exactly as it reads the sky panorama.
DEFAULT_SHADERS['probe_equirect.frag'] = """#version 330 core
in vec2 v_uv;
out vec4 frag_colour;
uniform samplerCube u_cube;
void main() {
    // Texel row 0 is straight up, as in an uploaded panorama (panoramaUV).
    float yaw = (v_uv.x - 0.5) * 6.28318531;
    float pitch = (0.5 - v_uv.y) * 3.14159265;
    float across = cos(pitch);
    // Mirrored about the horizon: the water reads this, and a reflection
    // looks up -- one a wave tips below the horizon finds the walls just
    // above it, not the floor and the pool itself.
    vec3 d = vec3(across * sin(yaw), abs(sin(pitch)), -across * cos(yaw));
    frag_colour = vec4(texture(u_cube, d).rgb, 1.0);
}
"""
DEFAULT_SHADERS['sky_cube.frag'] = """#version 330 core
in vec2 v_uv;
out vec4 frag_colour;
uniform sampler2D u_panorama;
uniform int u_face;       // GL_TEXTURE_CUBE_MAP_POSITIVE_X + u_face
""" + PANORAMA_GLSL + """
void main() {
    // The face's texel to its direction (the GL cube map convention).
    vec2 st = v_uv * 2.0 - 1.0;
    vec3 d;
    if (u_face == 0)      d = vec3( 1.0, -st.y, -st.x);
    else if (u_face == 1) d = vec3(-1.0, -st.y,  st.x);
    else if (u_face == 2) d = vec3( st.x,  1.0,  st.y);
    else if (u_face == 3) d = vec3( st.x, -1.0, -st.y);
    else if (u_face == 4) d = vec3( st.x, -st.y,  1.0);
    else                  d = vec3(-st.x, -st.y, -1.0);
    frag_colour = vec4(textureLod(u_panorama, panoramaUV(normalize(d)), 0.0).rgb, 1.0);
}
"""

# ==============================================================================
# EXPLOSION (the Effect's one-shot fireball)
#
# One instance row per playing (or previewed) explosion. The row is authored
# data only -- where, how big, when it started -- and the shader works out the
# playback position from the render clock (uTime), so a row is re-uploaded
# only when an explosion starts, stops or moves, not every frame.
#
# EXPLOSION_INSTANCE_LAYOUT is the row's single definition: the attribute
# declarations in effect.vert are generated from it, and the renderer fills
# columns by these names (EXPLOSION_COLUMNS). Column numbers are never
# written out by hand.
# ==============================================================================
from .effect_entity import (                                    # noqa: E402
    EXPLOSION_CELL_UV, EXPLOSION_FRAME_COUNT, EXPLOSION_PREVIEW_T,
    EXPLOSION_SHEET_COLUMNS, EXPLOSION_SHEET_TOP_UV)

#: (attribute, its four components) per vec4 of an explosion instance row,
#: from attribute location 1 (location 0 is the quad corner).
EXPLOSION_INSTANCE_LAYOUT = (
    ('iPosStart', ('x', 'y', 'z', 'start')),
    ('iSize', ('width', 'height', 'intensity', 'lifetime')),
    ('iMeta', ('seed', 'preview', 'skip', 'spare_meta_w')),
    ('iColor', ('r', 'g', 'b', 'spare_color_w')),
)
#: Column of each component in a row: EXPLOSION_COLUMNS['height'] is 5.
EXPLOSION_COLUMNS = {
    name: 4 * vec + comp
    for vec, (_, names) in enumerate(EXPLOSION_INSTANCE_LAYOUT)
    for comp, name in enumerate(names)
}
EXPLOSION_INSTANCE_FLOATS = 4 * len(EXPLOSION_INSTANCE_LAYOUT)

#: The render clock wraps at this many seconds (as the particles' does), so
#: float32 keeps millisecond precision however long the session runs.
EXPLOSION_TIME_WRAP = 4096.0


def _explosion_attribute_glsl():
    return "\n".join(
        "layout (location = %d) in vec4 %s;   // %s"
        % (vec + 1, attribute, ", ".join(names))
        for vec, (attribute, names) in enumerate(EXPLOSION_INSTANCE_LAYOUT))


DEFAULT_SHADERS['effect.vert'] = """#version 330 core
precision highp float;

layout (location = 0) in vec2 aPos;
""" + _explosion_attribute_glsl() + """

uniform mat4 projection;
uniform mat4 view;
uniform highp float uTime;      // render clock, wrapped like iPosStart.w

out highp vec2 vUV;
out highp vec3 vWorldPos;
out highp float vDepth;         // view depth, for the soft edge
out highp float vSoftRange;
out highp float vT;             // playback position 0..1
out vec3 vColor;
out float vIntensity;

const float TIME_WRAP = %(wrap).1f;
const float PREVIEW_T = %(preview_t).9f;

void main() {
    float width = max(iSize.x, 0.01);
    float height = max(iSize.y, 0.01);
    float lifetime = max(iSize.w, 0.001);
    float t;
    if (iMeta.y > 0.5) {
        // The editor's Preview: one frame, whole and still.
        t = PREVIEW_T;
    } else {
        float elapsed = uTime - iPosStart.w;
        if (elapsed < 0.0) elapsed += TIME_WRAP;
        // Playback may start partway in (iMeta.z, explosion_skip): opening
        // on a fireball already grown and bright, which hides whatever it
        // replaces -- an exploding barrel's model.
        float skip = clamp(iMeta.z, 0.0, 0.9);
        t = skip + (1.0 - skip) * clamp(elapsed / lifetime, 0.0, 1.0);
    }
    float growth = mix(1.0, 3.0, smoothstep(0.0, 0.28, t));
    float w = width * growth;
    float h = height * growth;

    // Faces the camera from every angle (a fireball is round), centred half
    // its height above the origin: from eye level its bottom edge stays on
    // the origin and it rises as it grows, as before; from above it is a
    // full fireball rather than an edge-on card.
    vec3 right = normalize(vec3(view[0][0], view[1][0], view[2][0]));
    vec3 up = normalize(vec3(view[0][1], view[1][1], view[2][1]));
    vec3 centre = iPosStart.xyz + vec3(0.0, 0.5 * h, 0.0);
    vec3 worldPos = centre + right * (aPos.x * w) + up * (aPos.y * h);
    vec4 viewPos = view * vec4(worldPos, 1.0);
    gl_Position = projection * viewPos;

    vUV = aPos + 0.5;
    vWorldPos = worldPos;
    vDepth = -viewPos.z;
    vSoftRange = max(0.25 * min(w, h), 1.0);
    vT = t;
    vColor = iColor.rgb;
    vIntensity = iSize.z;
}
""" % {'wrap': EXPLOSION_TIME_WRAP, 'preview_t': EXPLOSION_PREVIEW_T}

DEFAULT_SHADERS['effect.frag'] = """#version 330 core
precision mediump float;

out vec4 FragColor;

in highp vec2 vUV;
in highp vec3 vWorldPos;
in highp float vDepth;
in highp float vSoftRange;
in highp float vT;
in vec3 vColor;
in float vIntensity;

uniform sampler2D explosion_texture;
uniform highp sampler2D uSceneDepth;
uniform int uSoft;
uniform highp vec4 uViewport;
uniform highp vec2 uDepthParams;   // projection[3][2], projection[2][2]
""" + FOG_GLSL + """
const float SHEET_COLUMNS = %(columns).1f;
const float FRAME_COUNT = %(frames).1f;
const vec2 CELL = vec2(%(cell_u).6f, %(cell_v).6f);   // one frame, in UV
const float SHEET_TOP = %(top).6f;   // sheet top edge to the first row, in UV

vec2 atlasUV(vec2 localUV, float frame) {
    float column = mod(frame, SHEET_COLUMNS);
    float row = floor(frame / SHEET_COLUMNS);            // from the top
    // The image's top edge is v = 1.
    float cellTop = 1.0 - SHEET_TOP - row * CELL.y;
    return vec2((column + localUV.x) * CELL.x,
                cellTop - (1.0 - localUV.y) * CELL.y);
}

void main() {
    if (vIntensity <= 0.0) discard;
    float t = vT;

    // Every frame, each blended into the next: frame n is whole at
    // (n + 0.5) / FRAME_COUNT. Blended premultiplied, so a frame's clear
    // texels add no colour to the other's.
    float p = clamp(t * FRAME_COUNT - 0.5, 0.0, FRAME_COUNT - 1.0);
    float f0 = floor(p);
    float k = p - f0;
    vec4 a = texture(explosion_texture, atlasUV(vUV, f0));
    vec4 b = texture(explosion_texture, atlasUV(vUV, min(f0 + 1.0, FRAME_COUNT - 1.0)));
    float sheetAlpha = mix(a.a, b.a, k);
    if (sheetAlpha < 0.02) discard;
    vec3 sheetRGB = mix(a.rgb * a.a, b.rgb * b.a, k) / sheetAlpha;

    float envelope = 1.0 - smoothstep(0.70, 1.0, t);
    float flash = exp(-t * t * 48.0);
    float burst = 1.0 + flash * 2.2;
    vec3 tint = mix(vec3(1.0), max(vColor, vec3(0.001)), 0.35);
    vec3 rgb = sheetRGB * tint * vIntensity * burst;
    float alpha = sheetAlpha * envelope;

    if (uSoft != 0) {
        // Soft edge: fade where the scene is just behind the fireball, so a
        // floor or wall it passes through leaves no hard line.
        highp vec2 uv = (gl_FragCoord.xy - uViewport.xy) / uViewport.zw;
        highp float d = texture(uSceneDepth, uv).r * 2.0 - 1.0;
        highp float sceneDepth = uDepthParams.x / (d + uDepthParams.y);
        alpha *= clamp((sceneDepth - vDepth) / vSoftRange, 0.0, 1.0);
    }
    if (alpha < 0.01) discard;
    FragColor = vec4(applyFog(rgb, vWorldPos), alpha);
}
""" % {'columns': EXPLOSION_SHEET_COLUMNS, 'frames': EXPLOSION_FRAME_COUNT,
       'cell_u': EXPLOSION_CELL_UV[0], 'cell_v': EXPLOSION_CELL_UV[1],
       'top': EXPLOSION_SHEET_TOP_UV}


# ==============================================================================
# STATELESS GPU PARTICLES (an Effect's emitter)
#
# No particle has CPU state. One instance row per emitter feeds
# PARTICLES_PER_EMITTER instances (glVertexAttribDivisor), and the vertex
# shader derives particle i of emitter e from (seed, i, time) alone:
#
#   t     = elapsed + phase + i / count * life     (looping)
#   cycle = floor(t / life), age = t - cycle * life
#   hash(seed, i, cycle) -> fresh randoms each time the particle respawns
#
# A burst (an EXPLOSION's emitter) does not wrap: age = elapsed - jitter, and
# the particle is gone once age passes its life.
# ==============================================================================
from .effect_entity import EMITTER_MAX_PARTICLES as PARTICLES_PER_EMITTER  # noqa: E402
from .effect_entity import EMITTER_PRESETS as _EMITTER_PRESETS             # noqa: E402

#: The render clock the particle shader runs on wraps at this many seconds, so
#: float32 keeps millisecond precision however long the session runs. A
#: looping emitter reshuffles its particles once per wrap (about 68 minutes).
PARTICLE_TIME_WRAP = 4096.0

#: vec4s per preset in the shader's ``uPresets`` table.
PARTICLE_PRESET_VEC4S = 6

#: Preset parameters, world units and seconds. Per preset:
#:   life, speed, spread (cone half-angle as a fraction of pi), drag (1/s)
#:   gravity (+ rises), turbulence, start size, end size
#:   colour rgb, opacity (1 = alpha blended, 0 = additive)
#:   peak alpha, fade-in (fraction of life), fade-out start, lit (0 emissive)
#:   shape (0 disc, 1 line along the width), spin (rad/s), streak (s), noise
#:   glow from the Effect's light near the base, hardness, -, -
PARTICLE_PRESETS = {
    'SMOKE': ((3.0, 28.0, 0.22, 0.45),
              (14.0, 10.0, 12.0, 48.0),
              (0.42, 0.42, 0.42, 1.0),
              (0.42, 0.12, 0.35, 1.0),
              (0.0, 0.6, 0.0, 1.0),
              (0.55, 0.0, 0.0, 0.0)),
    'STEAM': ((1.2, 95.0, 0.16, 1.6),
              (24.0, 14.0, 6.0, 44.0),
              (0.95, 0.96, 0.98, 1.0),
              (0.32, 0.08, 0.25, 1.0),
              (0.0, 1.0, 0.0, 0.8),
              (0.2, 0.0, 0.0, 0.0)),
    'SPARKS': ((0.8, 180.0, 0.32, 0.7),
               (-420.0, 4.0, 3.2, 1.2),
               (1.0, 0.55, 0.16, 0.0),
               (1.0, 0.02, 0.45, 0.0),
               (0.0, 0.0, 0.035, 0.0),
               (0.0, 0.8, 0.0, 0.0)),
    'SPLASH': ((0.7, 200.0, 0.22, 0.6),
               (-480.0, 6.0, 4.0, 16.0),
               (0.90, 0.94, 1.0, 0.8),
               (0.6, 0.05, 0.4, 1.0),
               (1.0, 1.5, 0.02, 0.2),
               (0.0, 0.6, 0.0, 0.0)),
    'MIST': ((4.0, 6.0, 1.0, 0.3),
             (2.0, 8.0, 40.0, 80.0),
             (0.85, 0.88, 0.92, 1.0),
             (0.16, 0.3, 0.5, 1.0),
             (0.0, 0.15, 0.0, 1.0),
             (0.0, 0.0, 0.0, 0.0)),
}


def particle_preset_table():
    """The ``uPresets`` uniform: PARTICLE_PRESET_VEC4S vec4s per preset id,
    in EMITTER_PRESETS order (NONE's rows are zero)."""
    import numpy as _np
    table = _np.zeros((len(_EMITTER_PRESETS) * PARTICLE_PRESET_VEC4S, 4),
                      dtype=_np.float32)
    for index, name in enumerate(_EMITTER_PRESETS):
        rows = PARTICLE_PRESETS.get(name)
        if rows is not None:
            base = index * PARTICLE_PRESET_VEC4S
            table[base:base + PARTICLE_PRESET_VEC4S] = rows
    return table


def particle_preset_lives():
    """Each preset id's particle life, seconds (0 for NONE)."""
    import numpy as _np
    return _np.asarray([PARTICLE_PRESETS.get(name, ((0.0,),))[0][0]
                        for name in _EMITTER_PRESETS], dtype=_np.float64)


_PARTICLE_CONSTANTS = """
const int PARTICLES_PER_EMITTER = %d;
const int PARTICLE_PRESET_COUNT = %d;
const int PARTICLE_PRESET_VEC4S = %d;
const float PARTICLE_TIME_WRAP = %.1f;
""" % (PARTICLES_PER_EMITTER, len(_EMITTER_PRESETS), PARTICLE_PRESET_VEC4S,
       PARTICLE_TIME_WRAP)

DEFAULT_SHADERS['particle.vert'] = """#version 330 core
precision highp float;
""" + _PARTICLE_CONSTANTS + """
layout (location = 0) in vec2 aPos;
// One row per emitter; glVertexAttribDivisor(loc, PARTICLES_PER_EMITTER).
layout (location = 1) in vec4 iPosSeed;   // position, seed
layout (location = 2) in vec4 iEmit;      // preset id, count, scale, mode
layout (location = 3) in vec4 iExtent;    // width, height, start, host lift
layout (location = 4) in vec4 iTint;      // tint rgb, light glow on/off
layout (location = 5) in vec4 iLight;     // the Effect's light colour, -

uniform mat4 projection;
uniform mat4 view;
uniform float uTime;                       // render clock, wrapped
uniform vec4 uPresets[PARTICLE_PRESET_COUNT * PARTICLE_PRESET_VEC4S];
uniform float uMaxScreen;                  // largest particle, fraction of view height
uniform vec2 uNearFade;                    // view depth: invisible .. full

out vec2 vUV;
out vec4 vColor;      // base rgb, lit amount
out vec4 vGlow;       // additive light rgb, opacity
out vec4 vShape;      // alpha, hardness, noise, soft range
out vec3 vWorldPos;
out float vDepth;
out vec2 vNoiseOffset;

const float TAU = 6.28318530718;
const float PI = 3.14159265359;

uint hashU(uint x) {
    x ^= x >> 16u;
    x *= 0x7feb352du;
    x ^= x >> 15u;
    x *= 0x846ca68bu;
    x ^= x >> 16u;
    return x;
}

vec4 rand4(uint seed, uint index, uint cycle, uint salt) {
    uint h = hashU(seed ^ hashU(index * 0x9E3779B9u
                                ^ hashU(cycle * 0x85EBCA6Bu + salt)));
    uint a = hashU(h);
    uint b = hashU(a ^ 0x68E31DA4u);
    uint c = hashU(b ^ 0xB5297A4Du);
    uint d = hashU(c ^ 0x1B56C4E9u);
    return vec4(uvec4(a, b, c, d)) * (1.0 / 4294967295.0);
}

void collapse() {
    // All four corners at the origin with w = 0: clipped, nothing rasterised.
    gl_Position = vec4(0.0);
    vUV = vec2(0.0);
    vColor = vec4(0.0);
    vGlow = vec4(0.0);
    vShape = vec4(0.0);
    vWorldPos = vec3(0.0);
    vDepth = 0.0;
    vNoiseOffset = vec2(0.0);
}

void main() {
    int i = gl_InstanceID % PARTICLES_PER_EMITTER;
    int preset = int(iEmit.x + 0.5);
    int count = int(iEmit.y + 0.5);
    if (i >= count || preset <= 0 || preset >= PARTICLE_PRESET_COUNT) {
        collapse();
        return;
    }
    int b = preset * PARTICLE_PRESET_VEC4S;
    vec4 p0 = uPresets[b];
    vec4 p1 = uPresets[b + 1];
    vec4 p2 = uPresets[b + 2];
    vec4 p3 = uPresets[b + 3];
    vec4 p4 = uPresets[b + 4];
    vec4 p5 = uPresets[b + 5];

    float scale = iEmit.z;
    float mode = iEmit.w;                 // 0 loop, 1 burst, 2 frozen burst
    float life = max(p0.x, 0.05);
    uint seed = uint(iPosSeed.w);
    uint index = uint(i);

    float elapsed = uTime - iExtent.z;
    if (elapsed < 0.0) elapsed += PARTICLE_TIME_WRAP;

    float age;
    uint cycle;
    if (mode < 0.5) {
        float phase = rand4(seed, 0xFFFFu, 0u, 7u).x * life;
        float t = elapsed + phase + float(i) / float(count) * life;
        float c = floor(t / life);
        age = t - c * life;
        cycle = uint(c);
    } else {
        cycle = 0u;
        age = mode > 1.5 ? life * 0.35 : elapsed;
    }
    vec4 r0 = rand4(seed, index, cycle, 0u);
    vec4 r1 = rand4(seed, index, cycle, 1u);
    vec4 r2 = rand4(seed, index, cycle, 2u);
    if (mode > 0.5) {
        age -= r1.w * life * 0.15;        // a burst is not one frame
    }
    float particleLife = life * (0.7 + 0.3 * r0.w);
    if (age < 0.0 || age > particleLife) {
        collapse();
        return;
    }
    float a = age / particleLife;

    // Spawn across the extent: a disc (or a line along the width) as wide as
    // the Effect and as tall as its height, centred on it -- or, above a
    // flame or explosion (host lift > 0), a smaller patch at its crown.
    float w = iExtent.x;
    float h = iExtent.y;
    vec3 offset;
    if (p4.x > 0.5) {
        offset = vec3((r0.x - 0.5) * w, (r0.y - 0.5) * h,
                      (r0.z - 0.5) * min(w * 0.05, 8.0));
    } else {
        float radius = sqrt(r0.x) * 0.5 * w;
        float angle = r0.y * TAU;
        offset = vec3(cos(angle) * radius, (r0.z - 0.5) * h,
                      sin(angle) * radius);
    }
    if (iExtent.w > 0.0) {
        offset *= vec3(0.45, 0.15, 0.45);
        offset.y += h * iExtent.w;
    }

    // Motion: a cone about +Y, drag in closed form, gravity, turbulence.
    float cosT = 1.0 - r1.x * (1.0 - cos(p0.z * PI));
    float sinT = sqrt(max(0.0, 1.0 - cosT * cosT));
    float phi = r1.y * TAU;
    vec3 dir = vec3(cos(phi) * sinT, cosT, sin(phi) * sinT);
    float speed = p0.y * (0.6 + 0.8 * r1.z) * scale;
    float drag = max(p0.w, 1e-3);
    float decay = exp(-drag * age);
    float gravity = p1.x * scale;
    vec3 pos = iPosSeed.xyz + offset
             + dir * speed * (1.0 - decay) / drag
             + vec3(0.0, 0.5 * gravity * age * age, 0.0);
    vec3 vel = dir * speed * decay + vec3(0.0, gravity * age, 0.0);
    float turbulence = p1.y * scale;
    vec3 ph = r2.xyz * TAU;
    pos += turbulence * age * vec3(sin(age * 1.7 + ph.x),
                                   0.35 * sin(age * 2.3 + ph.y),
                                   cos(age * 1.3 + ph.z));

    float grow = 1.0 - (1.0 - a) * (1.0 - a);
    float size = mix(p1.z, p1.w, grow) * (0.7 + 0.6 * r2.w) * scale;
    float alpha = p3.x * smoothstep(0.0, max(p3.y, 1e-3), a)
                * (1.0 - smoothstep(p3.z, 1.0, a));

    vec4 centre = view * vec4(pos, 1.0);
    float depth = -centre.z;
    // Fade out at the near plane rather than fill the screen with one puff.
    alpha *= smoothstep(uNearFade.x, uNearFade.y, depth);
    if (alpha < 0.002 || depth <= 0.0) {
        collapse();
        return;
    }
    // No particle grows past uMaxScreen of the view's height (overdraw).
    float maxSize = uMaxScreen * 2.0 * depth / max(projection[1][1], 1e-4);

    float rotation = r1.w * TAU + (r2.x - 0.5) * 2.0 * p4.y * age;
    vec2 axisX = vec2(cos(rotation), sin(rotation));
    vec2 axisY = vec2(-axisX.y, axisX.x);
    float sizeX = size;
    float sizeY = size;
    if (p4.z > 0.0) {
        // Streak along the velocity seen on screen (sparks, droplets).
        vec2 v = (mat3(view) * vel).xy;
        float speedOnScreen = length(v);
        if (speedOnScreen > 1e-3) {
            axisY = v / speedOnScreen;
            axisX = vec2(axisY.y, -axisY.x);
            sizeY = size + speedOnScreen * p4.z;
        }
    }
    sizeX = min(sizeX, maxSize);
    sizeY = min(sizeY, maxSize);
    vec2 corner = axisX * aPos.x * sizeX + axisY * aPos.y * sizeY;
    gl_Position = projection * (centre + vec4(corner, 0.0, 0.0));

    vUV = aPos * 2.0;
    vColor = vec4(p2.rgb * iTint.rgb, p3.w);
    float glow = p5.x * iTint.w * (1.0 - a) * (1.0 - a) * (1.0 - a);
    vGlow = vec4(iLight.rgb * glow, p2.w);
    vShape = vec4(alpha, p5.y, p4.w, max(min(sizeX, sizeY) * 0.5, 1.0));
    vWorldPos = pos;
    vDepth = depth;
    vNoiseOffset = r2.yz * 37.0;
}
"""

DEFAULT_SHADERS['particle.frag'] = """#version 330 core
precision mediump float;

out vec4 FragColor;

in highp vec2 vUV;
in vec4 vColor;
in vec4 vGlow;
in highp vec4 vShape;
in highp vec3 vWorldPos;
in highp float vDepth;
in highp vec2 vNoiseOffset;

uniform highp sampler2D uSceneDepth;
uniform int uSoft;
uniform highp vec4 uViewport;
uniform highp vec2 uDepthParams;   // projection[3][2], projection[2][2]
""" + FOG_GLSL + """
// Ambient the particle catches before the map's global ambient is added; the
// world's lit shaders bake 0.1, but a puff of smoke is mostly lit from all
// round, so it starts brighter.
const vec3 PARTICLE_BASE_LIGHT = vec3(0.75);

highp float hash12(highp vec2 p) {
    highp vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

highp float valueNoise(highp vec2 p) {
    highp vec2 i = floor(p);
    highp vec2 f = fract(p);
    highp vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), u.x),
               mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), u.x),
               u.y);
}

void main() {
    highp float r2 = dot(vUV, vUV);
    if (r2 >= 1.0) discard;

    // Procedural sprite: radial falloff times a little value noise.
    float hardness = vShape.y;
    float falloff = pow(1.0 - r2, 1.0 + 2.0 * hardness);
    highp vec2 q = vUV * 1.7 + vNoiseOffset;
    float n = 0.65 * valueNoise(q) + 0.35 * valueNoise(q * 2.3 + 11.0);
    float mask = falloff * mix(1.0, clamp(n * 1.6 - 0.15, 0.0, 1.0), vShape.z);
    float alpha = vShape.x * mask;

    if (uSoft != 0) {
        // Soft particles: fade where the scene is just behind the particle.
        highp vec2 uv = (gl_FragCoord.xy - uViewport.xy) / uViewport.zw;
        highp float d = texture(uSceneDepth, uv).r * 2.0 - 1.0;
        highp float sceneDepth = uDepthParams.x / (d + uDepthParams.y);
        alpha *= clamp((sceneDepth - vDepth) / vShape.w, 0.0, 1.0);
    }
    if (alpha < 0.003) discard;

    vec3 light = mix(vec3(1.0), min(PARTICLE_BASE_LIGHT + uAmbient, vec3(1.5)),
                     vColor.a);
    vec3 rgb = vColor.rgb * light + vGlow.rgb;
    float opacity = vGlow.a;
    // Alpha-blended particles fade into the fog colour; additive ones fade out.
    float fog = fogFactor(vWorldPos);
    rgb = mix(rgb * (1.0 - fog), mix(rgb, uFogColor, fog), opacity);
    // Premultiplied: opacity 1 blends like alpha, 0 adds.
    FragColor = vec4(rgb * alpha, alpha * opacity);
}
"""


# Central Registry: (Shader Name) -> (Vertex Filename, Fragment Filename)
SHADER_MAP = {
    'simple':        ('simple.vert',         'simple.frag'),
    'lit':           ('lit.vert',            'lit.frag'),
    'textured':      ('textured.vert',       'textured.frag'),
    'sprite':        ('sprite.vert',         'sprite.frag'),
    'depth_cube':    ('depth_cube.vert',     'depth_cube.frag'),
    'fog':           ('fog.vert',            'fog.frag'),
    'water':         ('water.vert',          'water.frag'),
    'glass':         ('glass.vert',          'glass.frag'),
    'terrain':       ('terrain_indexed.vert', 'terrain_flat.frag'),
}
