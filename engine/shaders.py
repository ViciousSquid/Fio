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
MAX_LIGHTS = 64

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

    'effect.vert': """#version 330 core
precision highp float;

layout (location = 0) in vec2 aPos;
layout (location = 1) in vec3 iEffectPos;
layout (location = 2) in vec4 iEffectParams;  // width, intensity, elapsed, lifetime
layout (location = 3) in vec4 iEffectMeta;    // seed, type, height, spare
layout (location = 4) in vec4 iEffectColor;
layout (location = 5) in float iParticleIndex;

uniform mat4 projection;
uniform mat4 view;

out vec2 TexCoords;
out vec3 FragPos;
out vec4 EffectParams;
out vec4 EffectMeta;
out vec3 EffectColor;

void main() {
    float width = max(iEffectParams.x, 0.01);
    float height = max(iEffectMeta.z, 0.01);
    float elapsed = max(iEffectParams.z, 0.0);
    float lifetime = max(iEffectParams.w, 0.001);
    float t = clamp(elapsed / lifetime, 0.0, 1.0);
    if (iEffectMeta.w > 0.5) {
        // Keep editor Preview visually locked to animation frame 10.
        t = (9.5 / 16.0);
    }
    float growth = mix(1.0, 3.0, smoothstep(0.0, 0.28, t));

    vec3 cameraRight = normalize(vec3(view[0][0], view[1][0], view[2][0]));
    const vec3 worldUp = vec3(0.0, 1.0, 0.0);
    float vertical = aPos.y + 0.5;

    vec3 worldPos = iEffectPos
                  + cameraRight * aPos.x * width * growth
                  + worldUp * vertical * height * growth;

    TexCoords = aPos + 0.5;
    FragPos = worldPos;
    EffectParams = iEffectParams;
    EffectMeta = iEffectMeta;
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

const float EXPLOSION_SHEET_COLUMNS = 5.0;
const float EXPLOSION_SHEET_ROWS = 4.0;
const float EXPLOSION_FRAME_COUNT = 16.0;
const float EXPLOSION_PREVIEW_FRAME_INDEX = 9.0; // authoring frame 10

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
    float t = clamp(elapsed / lifetime, 0.0, 1.0);
    if (EffectMeta.w > 0.5) {
        // Keep the visual envelope aligned with the static frame-10 preview.
        t = (9.5 / 16.0);
    }

    float frame;
    if (EffectMeta.w > 0.5) {
        // Editor Preview is deliberately static: always show authoring frame 10.
        frame = EXPLOSION_PREVIEW_FRAME_INDEX;
    } else {
        frame = min(
            floor(t * EXPLOSION_FRAME_COUNT),
            EXPLOSION_FRAME_COUNT - 1.0
        );
    }
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
}
""",
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

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform highp float time;
uniform mat3 normalMatrix;

uniform float waveAmp;    // total wave amplitude in world units
uniform vec3 brushSize;   // world-space brush dimensions

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
        addWave(vec2( 0.788,  0.616), 190.0, amp * 0.42, 1.05, worldPos.xz, dy, dxz, n);
        addWave(vec2(-0.552,  0.834), 118.0, amp * 0.28, 1.45, worldPos.xz, dy, dxz, n);
        addWave(vec2( 0.943, -0.333),  74.0, amp * 0.19, 1.95, worldPos.xz, dy, dxz, n);
        addWave(vec2(-0.673, -0.740),  38.0, amp * 0.11, 2.70, worldPos.xz, dy, dxz, n);
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

uniform float waterOpacity;
uniform float waterReflectivity;
uniform float distortionStrength;
uniform float refractionIndex;
uniform float roughness;
uniform float fresnelIntensity;
uniform vec2 screenSize;
uniform vec3 waterTint;

// Scene depth, copied once per water pass alongside sceneColor. With it the
// water knows how much of it lies in front of the bed: colour absorption by
// depth, soft foamy shorelines, caustics and screen-space reflections. With
// hasSceneDepth == 0 (no depth buffer to copy) the shader falls back to the
// depth-less look.
uniform highp sampler2D sceneDepth;
uniform int hasSceneDepth;
uniform int ssrEnabled;
uniform highp mat4 invProjection;""" + FOG_GLSL + """

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

highp vec3 viewPosFromDepth(highp vec2 uv, highp float depth) {
    highp vec4 p = invProjection * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return p.xyz / p.w;
}

highp vec2 viewToUV(highp vec3 p) {
    highp vec4 c = projection * vec4(p, 1.0);
    return c.xy / c.w * 0.5 + 0.5;
}

// Screen-space reflection: march the reflected ray through the copied depth
// buffer and return the scene colour it hits (rgb) and a confidence (a).
// 20 linear steps with a growing stride, then a short binary refinement.
vec4 traceReflection(highp vec3 originView, highp vec3 dirView) {
    highp float stride = max(2.0, -originView.z * 0.02);
    highp vec3 p = originView;
    highp vec3 prev = p;
    for (int i = 0; i < 20; i++) {
        prev = p;
        p += dirView * stride;
        stride *= 1.22;
        if (p.z > -0.5) break;
        highp vec2 uv = viewToUV(p);
        if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) break;
        // textureLod: no implicit derivatives inside the loop.
        highp float d = textureLod(sceneDepth, uv, 0.0).r;
        if (d >= 0.99999) continue;
        highp float sceneZ = viewPosFromDepth(uv, d).z;
        highp float gap = sceneZ - p.z;
        // Only a ray that has just gone behind a surface hits it. Without a
        // cap on how far behind, a ray passing behind a thin object (a box
        // standing in the water) counted as hitting it and smeared a long
        // streak of it across the water.
        highp float thickness = clamp(stride * 1.5, 2.0, 48.0);
        if (gap > 0.0 && gap < thickness) {
            highp vec3 lo = prev;
            highp vec3 hi = p;
            for (int j = 0; j < 5; j++) {
                highp vec3 mid = (lo + hi) * 0.5;
                highp vec2 muv = viewToUV(mid);
                highp float mz = viewPosFromDepth(muv, textureLod(sceneDepth, muv, 0.0).r).z;
                if (mz > mid.z) hi = mid; else lo = mid;
            }
            highp vec2 huv = viewToUV(hi);
            highp float hz = viewPosFromDepth(huv, textureLod(sceneDepth, huv, 0.0).r).z;
            if (hz - hi.z > thickness) return vec4(0.0);
            vec2 edge = smoothstep(vec2(0.0), vec2(0.08), huv)
                      * smoothstep(vec2(0.0), vec2(0.08), vec2(1.0) - huv);
            float fade = edge.x * edge.y * (1.0 - smoothstep(14.0, 20.0, float(i)));
            return vec4(textureLod(sceneColor, huv, 0.0).rgb, fade);
        }
    }
    return vec4(0.0);
}

// Procedural sky remains the zero-cost fallback when reflections are disabled.
vec3 skyColor(vec3 dir) {
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
    highp vec2 wuv;
    if (topFace > 0.5) {
        wuv = FragPos.xz;
    } else if (abs(geoN.x) > abs(geoN.z)) {
        wuv = vec2(FragPos.z, FragPos.y - time * 6.0);
    } else {
        wuv = vec2(FragPos.x, FragPos.y - time * 6.0);
    }
    vec2 r1 = texture(normalMap, wuv * 0.0110 + time * vec2( 0.021,  0.014)).xy - 0.5;
    vec2 r2 = texture(normalMap, wuv * 0.0047 + time * vec2(-0.011,  0.008)).xy - 0.5;
    vec2 r3 = texture(normalMap, wuv * 0.0310 + time * vec2( 0.016, -0.029)).xy - 0.5;
    vec2 ripple = r1 + r2 * 0.65 + r3 * 0.35;

    // Broad world-space variation breaks up normal-map repetition on large water brushes.
    highp vec2 broadNoise = vec2(
        vnoise(FragPos.xz * 0.006 + vec2(time * 0.004, -time * 0.003)),
        vnoise(FragPos.xz * 0.008 + vec2(-time * 0.003, time * 0.005))
    ) - 0.5;
    broadNoise += (vec2(
        vnoise(FragPos.xz * 0.0037 + vec2(-time * 0.002, time * 0.001)),
        vnoise(FragPos.xz * 0.0051 + vec2(time * 0.001, time * 0.002))
    ) - 0.5) * 0.55;
    ripple += broadNoise * 0.55;

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
        highp float dRefract = textureLod(sceneDepth, refractUV, 0.0).r;
        if (viewPosFromDepth(refractUV, dRefract).z > surfaceView.z) {
            refractUV = clamp(screenUV, vec2(0.001), vec2(0.999));
        }
        highp float dBed = textureLod(sceneDepth, refractUV, 0.0).r;
        if (dBed < 0.99999) {
            highp vec3 bedView = viewPosFromDepth(refractUV, dBed);
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
    // in the shallows. Sampled unconditionally so the mipmapped lookups stay
    // in uniform control flow.
    highp vec2 cuv = bedWorld.xz * 0.03;
    float c1 = texture(normalMap, cuv + time * vec2(0.031, 0.022)).x;
    float c2 = texture(normalMap, cuv * 1.37 - time * vec2(0.024, 0.037)).y;
    if (haveDepth) {
        if (bedVisible && topFace > 0.5) {
            float caustic = pow(1.0 - abs(c1 + c2 - 1.0), 10.0);
            transmitted *= 1.0 + caustic * 0.55 * exp(-bedDepth * 0.03) * detailFade;
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
    if (haveDepth && ssrEnabled == 1 && topFace > 0.5) {
        // Reflect the scene itself where the ray finds it on screen; the sky
        // fills in everywhere else. Rougher water blurs towards the sky.
        vec4 ssr = traceReflection(surfaceView, normalize(mat3(view) * R));
        sceneReflection = ssr.a * (1.0 - roughness * 0.6);
        reflection = mix(reflection, ssr.rgb, sceneReflection);
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
    float foam =
        clamp(crestFoam + shoreFoam, 0.0, 1.0) *
        topFace;

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
}
