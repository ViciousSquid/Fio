import os
import platform
import re
import sys

SHADER_DIR = os.path.join(os.path.dirname(__file__), 'shaders')


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
MAX_SHADOW_LIGHTS = 4

# ==============================================================================
# DYNAMIC LIGHT CAPACITY
# ------------------------------------------------------------------------------
# How many point lights a lighting shader can hold. This is the *only* place the
# number is written down: the shader sources below are built from it and
# `BaseRenderer.MAX_LIGHTS` reads it, because the renderer's budget and the
# shader's array have to be the same number.
#
# They used not to be. The renderer uploaded up to 32 lights and set
# `active_lights` to that count, while `lit.frag` and `textured.frag` declared
# `lights[8]` and looped to `active_lights` with no bound — so any scene with
# more than eight lights had the shader read past the end of the array, which is
# undefined behaviour, and 24 of the "32" lights never worked in the first
# place. Every loop over `active_lights` is now clamped to its own array as
# well, so a mismatch can never be more than lights quietly not contributing.
MAX_LIGHTS = 64

# The ARM/low-power variants keep a smaller array deliberately. Uniform storage
# is the scarce resource on those GPUs, and the per-fragment loop runs
# `active_lights` times either way, so a lower cap costs a dense scene some of
# its lights and costs an ordinary one nothing at all.
MAX_LIGHTS_ARM = 16

# Water and terrain light themselves from a handful of the nearest lights rather
# than the whole set; their shaders are sized for that on purpose and the
# renderer clamps `active_lights` to match (see BaseRenderer._shader_light_cap).
MAX_LIGHTS_WATER = 8
MAX_LIGHTS_TERRAIN = 8

# GL 3.3 UBO binding used by every lighting shader.  The binding is assigned
# from Python with glUniformBlockBinding rather than using a GLSL 4.2-style
# explicit binding qualifier, keeping this portable to the engine's GL 3.3
# target.
LIGHT_UBO_BINDING = 2

_LIGHT_DECL_RE = re.compile(
    r"struct\s+Light\s*\{.*?\};\s*uniform\s+Light\s+lights\s*\[\s*(\d+)\s*\]\s*;",
    re.DOTALL,
)


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
    block = (
        "struct Light {\n"
        "    highp vec4 position;\n"
        "    vec4 color;\n"
        "    vec4 params;       // x=intensity, y=radius\n"
        "    ivec4 indices;     // x=shadow index\n"
        "};\n"
        "layout(std140) uniform FioLightBlock {\n"
        f"    Light lights[{count}];\n"
        "};"
    )
    result = _LIGHT_DECL_RE.sub(block, source, count=1)

    result = re.sub(r"lights\[([^]]+)\]\.position\b", r"lights[\1].position.xyz", result)
    result = re.sub(r"lights\[([^]]+)\]\.color\b", r"lights[\1].color.xyz", result)
    result = re.sub(r"lights\[([^]]+)\]\.intensity\b", r"lights[\1].params.x", result)
    result = re.sub(r"lights\[([^]]+)\]\.radius\b", r"lights[\1].params.y", result)
    result = re.sub(
        r"lights\[([^]]+)\]\.shadowIndex\b",
        r"int(lights[\1].indices.x)",
        result,
    )
    return result

SHADOW_GLSL = """
#define MAX_SHADOW_LIGHTS 4
uniform samplerCube shadowMaps[MAX_SHADOW_LIGHTS];

highp float _sampleShadowCube(int idx, highp vec3 dir) {
    if (idx == 0) return texture(shadowMaps[0], dir).r;
    else if (idx == 1) return texture(shadowMaps[1], dir).r;
    else if (idx == 2) return texture(shadowMaps[2], dir).r;
    return texture(shadowMaps[3], dir).r;
}

// idx          : which cube-map (0..3), or <0 for a non-shadow-casting light
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
    highp float diskRadius = farPlane * 0.004 * (1.0 + currentDepth / farPlane);
    // Bias covers surface slope plus the depth spread from the PCF disk, so
    // flat lit surfaces don't self-shadow ("shadow acne").
    highp float bias = diskRadius + clamp(farPlane * 0.03 * (1.0 - ndotl),
                                          farPlane * 0.004, farPlane * 0.04);
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
struct Light { highp vec3 position; vec3 color; float intensity; highp float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """
void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;

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
void main() {
    TexCoords = aPos + 0.5;
    vec3 cameraRight = vec3(view[0][0], view[1][0], view[2][0]);
    vec3 cameraUp = vec3(view[0][1], view[1][1], view[2][1]);
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

    'effect.vert': """#version 330 core
precision highp float;

layout (location = 0) in vec2 aPos;
layout (location = 1) in vec3 iEffectPos;
layout (location = 2) in vec4 iEffectParams;  // size, intensity, elapsed, lifetime
layout (location = 3) in vec4 iEffectMeta;    // seed, type, particle index, spare
layout (location = 4) in vec4 iEffectColor;
layout (location = 5) in float iParticleIndex;

uniform mat4 projection;
uniform mat4 view;

out vec2 TexCoords;
out vec3 FragPos;
out vec4 EffectParams;
out vec4 EffectMeta;
out vec3 EffectColor;

float hash11(float x) {
    return fract(sin(x * 12.9898) * 43758.5453123);
}

void main() {
    float size = max(iEffectParams.x, 0.01);
    float effectType = iEffectMeta.y;

    // EXPLOSION remains the authored one-shot billboard.
    if (effectType > 0.5) {
        float growth = 1.0;
        float elapsed = max(iEffectParams.z, 0.0);
        float lifetime = max(iEffectParams.w, 0.001);
        float t = clamp(elapsed / lifetime, 0.0, 1.0);
        growth = mix(1.0, 3.0, smoothstep(0.0, 0.28, t));

        vec3 cameraRight = normalize(vec3(view[0][0], view[1][0], view[2][0]));
        const vec3 worldUp = vec3(0.0, 1.0, 0.0);
        float vertical = aPos.y + 0.5;

        vec3 worldPos = iEffectPos
                      + cameraRight * aPos.x * size * growth
                      + worldUp * vertical * size * 1.25 * growth;

        TexCoords = aPos + 0.5;
        FragPos = worldPos;
        EffectParams = iEffectParams;
        EffectMeta = vec4(iEffectMeta.x, iEffectMeta.y, iParticleIndex, iEffectMeta.w);
        EffectColor = iEffectColor.rgb;
        gl_Position = projection * view * vec4(worldPos, 1.0);
        return;
    }

    // FIRE is a deterministic collection of virtual flame cards. They are
    const vec3 worldUp = vec3(0.0, 1.0, 0.0);
    // ordinary instanced quads, but each card gets its own height, width,
    // starting height, lean and orientation. No CPU particle simulation exists.
    float card = iParticleIndex;
    float r0 = hash11(iEffectMeta.x + card * 17.173);
    float r1 = hash11(iEffectMeta.x + card * 31.791);
    float r2 = hash11(iEffectMeta.x + card * 53.417);
    float r3 = hash11(iEffectMeta.x + card * 79.133);

    bool baseCard = card < 8.0;
    float base = baseCard ? 1.0 : 0.0;

    float cardHeight = mix(0.58, 1.32, r1) * size;
    float cardWidth = mix(0.13, 0.30, r2) * size;

    // Broad crossed sheets build the burning mass; the remaining cards are
    // narrower tongues which start at different heights and peel away.
    cardWidth *= mix(0.78, 1.55, base);
    cardHeight *= mix(0.82, 0.92, base);

    float lift = base * (r3 - 0.5) * 0.12 * size
               + (1.0 - base) * r3 * 0.38 * size;

    float angle = (r0 * 6.2831853) + floor(card * 0.25) * 0.17;
    vec3 cameraRight = normalize(vec3(view[0][0], view[1][0], view[2][0]));
    vec3 cameraForward = normalize(vec3(-view[0][2], -view[1][2], -view[2][2]));
    cameraForward.y = 0.0;
    cameraForward = normalize(cameraForward);
    vec3 cardRight = normalize(
        cameraRight * cos(angle) + cameraForward * sin(angle)
    );

    float vertical = aPos.y + 0.5;
    float y = vertical;
    float lean = (r1 - 0.5) * size * mix(0.16, 0.52, y * y);
    float sideways = sin(r0 * 17.0 + vertical * 2.3) * size * 0.05 * y;

    vec3 worldPos = iEffectPos
                  + cardRight * aPos.x * cardWidth
                  + worldUp * (lift + vertical * cardHeight)
                  + normalize(vec3(cardRight.z, 0.0, -cardRight.x)) * (lean + sideways);

    TexCoords = aPos + 0.5;
    FragPos = worldPos;
    EffectParams = iEffectParams;
    EffectMeta = vec4(iEffectMeta.x, iEffectMeta.y, iParticleIndex, iEffectMeta.w);
    EffectColor = iEffectColor.rgb;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}
""",

    'effect.frag': """#version 330 core
precision mediump float;

out vec4 FragColor;

in highp vec2 TexCoords;
in highp vec3 FragPos;
in highp vec4 EffectParams;
in highp vec4 EffectMeta;
in highp vec3 EffectColor;

uniform sampler2D explosion_texture;

uniform int uFogEnabled;
uniform vec3 uFogColor;
uniform float uFogStart;
uniform float uFogEnd;
uniform float uFogDensity;
uniform highp vec3 uFogCamPos;
uniform vec3 uAmbient;

// explosion.png: 5 columns x 4 rows, with one used cell on the final row.
const float EXPLOSION_SHEET_COLUMNS = 5.0;
const float EXPLOSION_SHEET_ROWS = 4.0;
const float EXPLOSION_FRAME_COUNT = 16.0;
const float EXPLOSION_FRAME_RATE = 16.0;

highp float hash21(highp vec2 p, highp float seed) {
    return fract(
        sin(dot(p + vec2(seed, seed * 0.731), vec2(127.1, 311.7)))
        * 43758.5453123
    );
}

highp float valueNoise(highp vec2 p, highp float seed) {
    highp vec2 i = floor(p);
    highp vec2 f = fract(p);
    highp vec2 u = f * f * (3.0 - 2.0 * f);
    float a = hash21(i, seed);
    float b = hash21(i + vec2(1.0, 0.0), seed);
    float c = hash21(i + vec2(0.0, 1.0), seed);
    float d = hash21(i + vec2(1.0, 1.0), seed);
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

highp float fbm3(highp vec2 p, highp float seed) {
    float value = 0.0;
    float amplitude = 0.5;
    for (int i = 0; i < 3; ++i) {
        value += valueNoise(p, seed + float(i) * 13.71) * amplitude;
        p = p * 2.03 + vec2(7.1, 3.7);
        amplitude *= 0.5;
    }
    return value;
}

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

vec2 explosionAtlasUV(vec2 localUV, float frameIndex) {
    float column = mod(frameIndex, EXPLOSION_SHEET_COLUMNS);
    float rowTop = floor(frameIndex / EXPLOSION_SHEET_COLUMNS);
    float rowBottom = EXPLOSION_SHEET_ROWS - 1.0 - rowTop;
    vec2 cellSize = vec2(
        1.0 / EXPLOSION_SHEET_COLUMNS,
        1.0 / EXPLOSION_SHEET_ROWS
    );
    return (vec2(column, rowBottom) + localUV) * cellSize;
}

void main() {
    float visualIntensity = max(EffectParams.y, 0.0);
    float elapsed = max(EffectParams.z, 0.0);
    float lifetime = max(EffectParams.w, 0.001);
    float seed = EffectMeta.x;
    float effectType = EffectMeta.y;

    if (effectType > 0.5) {
        float t = clamp(elapsed / lifetime, 0.0, 1.0);
        float frame = min(
            floor(t * EXPLOSION_FRAME_COUNT),
            EXPLOSION_FRAME_COUNT - 1.0
        );
        vec4 sheet = texture(explosion_texture, explosionAtlasUV(TexCoords, frame));

        if (sheet.a < 0.02 || visualIntensity <= 0.0) discard;

        float envelope = 1.0 - smoothstep(0.70, 1.0, t);
        float flash = exp(-t * t * 48.0);
        float burst = 1.0 + flash * 2.2;
        vec3 tint = mix(vec3(1.0), max(EffectColor, vec3(0.001)), 0.35);
        vec3 rgb = sheet.rgb * tint * visualIntensity * burst;
        float alpha = sheet.a * envelope;

        if (alpha < 0.01) discard;
        FragColor = vec4(applyFog(rgb, FragPos), alpha);
        return;
    }

    // ------------------------------------------------------------------
    // FIRE
    // ------------------------------------------------------------------
    // The flame is a turbulent density field rather than a scrolling picture.
    // Three noise scales establish the classic fire hierarchy:
    //   large  = broad tongues and global body motion
    //   detail = breakup into secondary tongues
    //   fine   = ragged edge / small hot holes
    float card = EffectMeta.z;
    float cardSeed = seed + card * 19.37;
    float cardRand = hash21(vec2(card, seed), cardSeed);

    float y = clamp(TexCoords.y, 0.0, 1.0);
    float x = TexCoords.x - 0.5;

    float speed = mix(0.72, 1.55, fract(cardRand * 17.0));
    float phase = hash21(vec2(card * 3.17, seed + 8.1), cardSeed + 4.7) * 6.2831853;

    highp vec2 p = vec2(x * 2.35, y * 3.15);
    p.y -= elapsed * speed + phase;
    p.x += sin(elapsed * 0.9 + phase + y * 2.7) * 0.10 * y;

    highp vec2 warp = vec2(
        fbm3(p * 0.82 + vec2(0.0, -elapsed * 0.25), cardSeed + 11.0),
        fbm3(p * 0.67 + vec2(3.7, elapsed * 0.18), cardSeed + 27.0)
    ) - 0.5;
    p += warp * vec2(0.95, 0.62);

    float large = fbm3(p * 1.05, cardSeed + 41.0);
    float detail = fbm3(p * 3.15 + vec2(4.2, -1.6), cardSeed + 57.0);
    float fine = valueNoise(p * 9.0 + vec2(11.0, -7.0), cardSeed + 73.0);

    // A flame is broad at the base and contracts aggressively toward the tips.
    float baseWidth = mix(0.53, 0.075, pow(y, 0.68));
    float silhouetteShift =
        (large - 0.5) * 0.23 * (0.22 + 0.78 * y)
        + (detail - 0.5) * 0.095;

    float lateral = abs(x + silhouetteShift) / max(baseWidth, 0.012);
    float body = 1.0 - smoothstep(0.54, 1.0, lateral);

    // Domain-warped holes break the single sausage shape into separate tongues.
    float voids = smoothstep(0.68, 0.94, fine);
    float tongue = smoothstep(0.36, 0.88, detail)
                 * smoothstep(0.12, 0.82, y);
    body *= mix(1.0, 0.32, voids * (0.35 + 0.65 * tongue));

    float bottomFade = smoothstep(0.0, 0.075, y);
    float tipFade = 1.0 - smoothstep(0.68, 1.0, y);
    float density = body * bottomFade * tipFade;

    // A few tiny detached hot fragments make the upper edge lively without
    // requiring another particle system.
    float embers = smoothstep(0.965, 0.998, fine)
                 * smoothstep(0.46, 0.88, y)
                 * (1.0 - smoothstep(0.82, 1.0, y));
    density = clamp(density + embers * 0.34, 0.0, 1.0);

    // Temperature is concentrated in the inner combustion channel and falls
    // toward the turbulent edge. The result is dark red -> orange -> yellow ->
    // almost white, rather than a flat orange sprite.
    float coreDistance = exp(-lateral * lateral * 4.6);
    float coreMask = coreDistance
                   * smoothstep(0.035, 0.52, y)
                   * (0.70 + 0.30 * large);
    float temperature = clamp(
        0.20
        + coreMask * 0.90
        + detail * 0.20
        - y * 0.10,
        0.0, 1.0
    );

    vec3 outer = max(EffectColor, vec3(0.001)) * 0.55;
    vec3 orange = vec3(1.0, 0.10, 0.008);
    vec3 yellow = vec3(1.0, 0.60, 0.055);
    vec3 hot = vec3(1.0, 0.93, 0.62);

    vec3 rgb = mix(outer, orange, smoothstep(0.08, 0.34, temperature));
    rgb = mix(rgb, yellow, smoothstep(0.30, 0.67, temperature));
    rgb = mix(rgb, hot, smoothstep(0.58, 0.88, temperature));

    // Low-frequency temporal modulation gives the light-looking flame a
    // coherent pulse; a faster term supplies the small chaotic flicker.
    float slowFlicker = fbm3(
        vec2(elapsed * 0.42 + seed * 0.013, seed * 0.0011),
        seed + 91.0
    );
    float fastFlicker = valueNoise(
        vec2(elapsed * 3.6 + seed * 0.07, card * 0.31),
        seed + 103.0
    );
    float flicker = mix(0.78, 1.16, clamp(
        slowFlicker * 0.72 + fastFlicker * 0.28, 0.0, 1.0
    ));

    rgb *= visualIntensity * flicker;
    float alpha = density * clamp(0.84 + temperature * 0.18, 0.0, 1.0);

    if (alpha < 0.012 || visualIntensity <= 0.0) discard;

    FragColor = vec4(applyFog(rgb, FragPos), alpha);
}
""",
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
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec3 aColor;
layout (location = 3) in vec2 aTexCoord;
layout (location = 4) in vec3 aSmoothNormal;

out vec3 FragPos;
out mediump vec3 Normal;
out mediump vec3 VertexColor;
out vec2 TexCoords;
out mediump vec3 SmoothNormal;

uniform mat4 projection;
uniform mat4 view;

void main() {
    FragPos      = aPos;
    Normal       = aNormal;
    VertexColor  = aColor;
    TexCoords    = aTexCoord;
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
in vec3 SmoothNormal;

uniform sampler2D texGrass;
uniform sampler2D texRock;
uniform sampler2D texSand;
uniform sampler2D texSnow;
uniform vec4 biomeWeights;
uniform float terrainHeightScale;
uniform int use_textures;

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

vec4 get_splat_weights(highp vec3 worldPos, vec3 smoothNorm) {
    float height = clamp(worldPos.y * terrainHeightScale, 0.0, 1.0);
    float slope  = 1.0 - max(smoothNorm.y, 0.0);  
    float n      = noise(worldPos.xz * 0.02 + height * 5.0) * 0.5 + 0.5;

    float grass_w = (1.0 - slope * 1.5) * (1.0 - height * 0.6) * biomeWeights.r;
    float rock_w  = slope * 0.8 + n * 0.4 * biomeWeights.g;
    float sand_w  = (1.0 - height * 0.4) * (1.0 - slope * 0.5) * biomeWeights.b;
    float snow_w  = smoothstep(0.6, 1.0, height) * biomeWeights.a;

    vec4 weights = vec4(grass_w, rock_w, sand_w, snow_w);
    return weights / (dot(weights, vec4(1.0)) + 0.001);
}

void main() {
    vec3 norm = normalize(Normal);
    vec3 texColor;
    
    if (use_textures == 1) {
        vec3 smoothNorm = normalize(SmoothNormal);
        vec4 splat = get_splat_weights(FragPos, smoothNorm);
        
        vec4 grass_col = texture(texGrass, TexCoords * 1.0);
        vec4 rock_col  = texture(texRock,  TexCoords * 0.5 + vec2(splat.g * 0.5, 0.0));
        vec4 sand_col  = texture(texSand,  TexCoords * 1.5 + vec2(splat.b * 0.3, splat.b * 0.2));
        vec4 snow_col  = texture(texSnow,  TexCoords * 0.8);
        
        vec3 splatColor = (
            grass_col.rgb * splat.r +
            rock_col.rgb  * splat.g +
            sand_col.rgb  * splat.b +
            snow_col.rgb  * splat.a
        );
        texColor = splatColor * VertexColor * 1.1;
    } else {
        texColor = VertexColor * 1.1;
    }
    
    vec3 skyColor    = vec3(0.6, 0.75, 0.9);
    vec3 groundColor = vec3(0.3, 0.25, 0.2);
    float skyFactor  = (norm.y + 1.0) * 0.5;
    vec3 ambient     = (mix(groundColor, skyColor, skyFactor) * 0.3 + uAmbient) * texColor;
    
    vec3 result  = ambient;
    vec3 sunDir  = normalize(vec3(0.4, 0.7, 0.3));
    vec3 sunColor = vec3(1.0, 0.95, 0.85);
    float sunDiff    = max(dot(norm, sunDir), 0.0);
    float wrappedDiff = (sunDiff + 0.3) / 1.3;
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
    
    FragColor = vec4(applyFog(result, FragPos), 1.0);
}""",

    'grass.vert': """#version 330 core

layout (location = 2) in vec3 iPosition;
layout (location = 3) in float iSize;
layout (location = 4) in float iPhase;
layout (location = 5) in float iVariation;

out vec3 FragPos;
out float BladeHeight;
out float ColorVariation;

uniform mat4 projection;
uniform mat4 view;
uniform float time;
uniform float windStrength;

void main() {
    // One instance is a small crossed pair of ordinary grass blades.
    // The CPU supplies only the tuft position and a few cheap random values;
    // the GPU builds the 12 vertices for the two blades.
    int blade = gl_VertexID / 6;
    int vertex = gl_VertexID - blade * 6;

    float y;
    float sideAmount;

    if (vertex == 0) {
        y = 0.0; sideAmount = -1.0;
    } else if (vertex == 1) {
        y = 0.0; sideAmount = 1.0;
    } else if (vertex == 2) {
        y = 1.0; sideAmount = 1.0;
    } else if (vertex == 3) {
        y = 0.0; sideAmount = -1.0;
    } else if (vertex == 4) {
        y = 1.0; sideAmount = 1.0;
    } else {
        y = 1.0; sideAmount = -1.0;
    }

    float angle = iPhase + float(blade) * 1.5707963;
    vec2 forward = vec2(cos(angle), sin(angle));
    vec2 side = vec2(-forward.y, forward.x);

    // Tall, thin blades: this is intentionally simple geometry.
    float height = iSize * (2.0 + 0.55 * iVariation);
    float width = iSize * (0.16 + 0.04 * iVariation);

    // Slightly separate the crossed blades so their bases do not z-fight.
    vec2 root = iPosition.xz + forward * (float(blade) - 0.5) * width * 0.25;

    // Cheap, spatially varying wind. The root stays planted.
    float spatial = dot(iPosition.xz, vec2(0.021, 0.017));
    float wave = sin(time * 1.1 + spatial + iPhase);
    float gust = sin(time * 0.47 + iPosition.x * 0.009
                     - iPosition.z * 0.011 + iPhase * 1.7);
    float bend = (wave * 0.72 + gust * 0.28) * windStrength;

    float bendAmount = bend * height * y * y;
    vec2 horizontal = side * sideAmount * width;
    horizontal += forward * bendAmount;

    vec3 p = vec3(root + horizontal, iPosition.y + 0.02 + y * height);
    FragPos = p;
    BladeHeight = y;
    ColorVariation = iVariation;
    gl_Position = projection * view * vec4(p, 1.0);
}

""",

    'grass.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in float BladeHeight;
in float ColorVariation;

uniform vec3 grassColor;
uniform vec3 cameraPos;
""" + FOG_GLSL + """
void main() {
    // Geometry supplies the silhouette; unlike the old billboard pass there
    // is no alpha-card coverage to discard. Fade is handled by fog.
    float heightShade = mix(0.82, 1.08, clamp(BladeHeight, 0.0, 1.0));
    
    // Grass uses a constant upward normal by design: terrain slope does not
    // make blades lie down and no per-blade normal data is uploaded.
    vec3 upwardNormal = vec3(0.0, 1.0, 0.0);
    vec3 sunDir = normalize(vec3(0.4, 0.7, 0.3));
    float sun = 0.45 + 0.55 * max(dot(upwardNormal, sunDir), 0.0);
    float variation = mix(0.88, 1.08, clamp((ColorVariation - 0.82) / 0.30, 0.0, 1.0));
    vec3 color = grassColor * sun * heightShade * variation + uAmbient * grassColor;
    FragColor = vec4(applyFog(color, FragPos), 1.0);
}
""",

}

# ----- Low-power shaders (used by BaseRenderer when lowpower_mode is True) ----
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
struct Light { vec3 position; vec3 color; float intensity; float radius; int shadowIndex; };
uniform Light lights[""" + str(MAX_LIGHTS_ARM) + """];
uniform int active_lights;""" + SHADOW_GLSL + FOG_GLSL + """void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
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

# ----- Portal shaders (used by BaseRenderer for stencil portals) -----
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
    'terrain':       ('terrain.vert',        'terrain.frag'),
    # Procedural is available but not yet integrated into the main render loop
    'procedural':    ('procedural_vert.glsl', 'procedural_frag.glsl'),
}

def load_shader_source(filename):
    """Loads a shader source string from the shader directory."""
    filepath = os.path.join(SHADER_DIR, filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"FATAL: Shader file not found: {filepath}")
        return ""
    except Exception as e:
        print(f"FATAL: Error reading shader file {filepath}: {e}")
        return ""