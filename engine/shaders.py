import os

SHADER_DIR = os.path.join(os.path.dirname(__file__), 'shaders')

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
uniform Light lights[8];
uniform int active_lights;""" + SHADOW_GLSL + """
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.1) * object_color;
    for(int i = 0; i < active_lights; i++) {
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
    FragColor = vec4(result, alpha);
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
uniform Light lights[8];
uniform int active_lights;""" + SHADOW_GLSL + """
void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;

    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.1) * texColor.rgb;

    for(int i = 0; i < active_lights; i++) {
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
    FragColor = vec4(result, texColor.a);
}""",

    'sprite.vert': """#version 330 core
precision highp float;
layout (location = 0) in vec2 aPos;
out vec2 TexCoords;
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
    gl_Position = projection * view * vec4(worldPos, 1.0);
}""",
    'sprite.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;
in highp vec2 TexCoords;
uniform sampler2D sprite_texture;
void main() {
    vec4 texColor = texture(sprite_texture, TexCoords);
    if(texColor.a < 0.1) discard;
    FragColor = texColor;
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

struct Light {
    highp vec3 position;
    vec3 color;
    float intensity;
    highp float radius;
};

#define MAX_LIGHTS 8
uniform Light lights[MAX_LIGHTS];
uniform int active_lights;
uniform highp vec3 viewPos;
uniform sampler2D normalMap;
uniform highp float time;

uniform float waterOpacity;
uniform float waterReflectivity;
uniform vec3 waterTint;

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

// Procedural sky used for reflections: horizon haze -> blue zenith + sun.
vec3 skyColor(vec3 dir) {
    float h = clamp(dir.y, 0.0, 1.0);
    vec3 sky = mix(vec3(0.66, 0.76, 0.83), vec3(0.19, 0.38, 0.66), pow(h, 0.55));
    float sunAmount = max(dot(dir, SUN_DIR), 0.0);
    sky += SUN_COLOR * (pow(sunAmount, 350.0) * 3.0 + pow(sunAmount, 24.0) * 0.18);
    return sky;
}

void main()
{
    highp vec3 toView = viewPos - FragPos;
    highp float viewDist = length(toView);
    vec3 viewDir = toView / max(viewDist, 0.0001);

    vec3 geoN = normalize(Normal);
    bool backside = dot(geoN, viewDir) < 0.0;   // camera inside the volume
    if (backside) geoN = -geoN;

    // Horizontal surface vs. vertical side wall (side normals have y ~= 0)
    float topFace = step(0.35, abs(geoN.y));

    // ---- Detail ripples: three scrolling normal-map layers projected in
    //      world space, so ripple scale is constant regardless of brush size.
    //      Side walls project onto their own plane or the pattern collapses
    //      into 1D streaks. ----
    highp vec2 wuv;
    if (topFace > 0.5) {
        wuv = FragPos.xz;
    } else if (abs(geoN.x) > abs(geoN.z)) {
        wuv = vec2(FragPos.z, FragPos.y - time * 6.0);   // slow downward drift
    } else {
        wuv = vec2(FragPos.x, FragPos.y - time * 6.0);
    }
    vec2 r1 = texture(normalMap, wuv * 0.0110 + time * vec2( 0.021,  0.014)).xy - 0.5;
    vec2 r2 = texture(normalMap, wuv * 0.0047 + time * vec2(-0.011,  0.008)).xy - 0.5;
    vec2 r3 = texture(normalMap, wuv * 0.0310 + time * vec2( 0.016, -0.029)).xy - 0.5;
    vec2 ripple = r1 + r2 * 0.65 + r3 * 0.35;

    // Fade fine detail with distance to stop specular aliasing shimmer
    float detailFade = 1.0 / (1.0 + viewDist * 0.0009);
    float rippleStrength = (0.34 + WaveCrest * 0.18) * detailFade;

    // Perturb tangentially to the face so ripples work on walls too
    vec3 N;
    if (topFace > 0.5) {
        N = normalize(vec3(geoN.x + ripple.x * rippleStrength,
                           geoN.y,
                           geoN.z + ripple.y * rippleStrength));
    } else {
        vec3 up = vec3(0.0, 1.0, 0.0);
        vec3 tangent = normalize(cross(up, geoN));
        N = normalize(geoN + (tangent * ripple.x + up * ripple.y) * rippleStrength * 0.6);
    }

    // ---- Fresnel (Schlick, R0 of water = 0.02) with the real normal ----
    float NdV = max(dot(N, viewDir), 0.0);
    float fresnel = 0.02 + 0.98 * pow(1.0 - NdV, 5.0);
    fresnel = clamp(fresnel * clamp(waterReflectivity * 2.0, 0.0, 1.6), 0.0, 1.0);

    // ---- Sky reflection ----
    vec3 R = reflect(-viewDir, N);
    R.y = abs(R.y);   // reflections that would sample "below horizon" mirror upward
    vec3 reflection = skyColor(R);

    // ---- Water body: deep tint straight down, brighter scatter at angles,
    //      subsurface glow through wave crests ----
    vec3 deepCol    = waterTint * 0.55;
    vec3 shallowCol = waterTint * 1.25 + vec3(0.02, 0.10, 0.09);
    vec3 bodyCol = mix(deepCol, shallowCol, pow(1.0 - NdV, 1.5) * 0.7 + 0.15);

    float sss = pow(WaveCrest, 2.0)
              * pow(max(dot(viewDir, -normalize(vec3(SUN_DIR.x, 0.0, SUN_DIR.z))), 0.0), 2.0);
    bodyCol += (waterTint * 0.8 + vec3(0.05, 0.22, 0.18)) * sss;

    // Constant sun term so water reads correctly even with no point lights
    bodyCol *= (0.45 + 0.55 * max(dot(N, SUN_DIR), 0.0));

    // ---- Dynamic point lights: wide diffuse + tight Blinn specular ----
    vec3 diffuseAcc = vec3(0.0);
    vec3 specAcc = vec3(0.0);
    for (int i = 0; i < active_lights; i++) {
        highp vec3 toL = lights[i].position - FragPos;
        highp float dist = length(toL);
        if (dist < lights[i].radius) {
            vec3 Ldir = toL / dist;
            float att = 1.0 - smoothstep(0.0, lights[i].radius, dist);
            vec3 lc = lights[i].color * lights[i].intensity * att;
            diffuseAcc += max(dot(N, Ldir), 0.0) * lc;
            vec3 Hl = normalize(Ldir + viewDir);
            float ndh = max(dot(N, Hl), 0.0);
            specAcc += (pow(ndh, 240.0) * 1.6 + pow(ndh, 28.0) * 0.15) * lc;
        }
    }
    bodyCol += bodyCol * diffuseAcc * 0.9;

    // ---- Sun glint with sparkle (twinkling micro-facets) ----
    vec3 Hs = normalize(SUN_DIR + viewDir);
    float sunSpec = pow(max(dot(N, Hs), 0.0), 320.0);
    float sparkle = vnoise(wuv * 0.9 + vec2(time * 1.7, -time * 1.3))
                  * vnoise(wuv * 1.7 - vec2(time * 0.9, -time * 1.1));
    sunSpec *= (1.0 + sparkle * 6.0) * detailFade;
    vec3 specular = SUN_COLOR * sunSpec * 2.2 + specAcc;

    // ---- Foam: breaking wave crests + lapping at the shore/walls
    //      (horizontal surface only — walls get none) ----
    float foamNoise = vnoise(wuv * 0.16 + vec2(time * 0.05, -time * 0.04)) * 0.6
                    + vnoise(wuv * 0.45 - vec2(time * 0.07, time * 0.06)) * 0.4;
    float crestFoam = smoothstep(0.68, 0.92, WaveCrest) * smoothstep(0.35, 0.75, foamNoise);
    float shoreWave = 0.5 + 0.5 * sin(ShoreDist * 0.30 - time * 1.8);
    float shoreFoam = (1.0 - smoothstep(2.0, 26.0, ShoreDist))
                    * (0.30 + 0.70 * shoreWave)
                    * smoothstep(0.25, 0.60, foamNoise + 0.15);
    float foam = clamp(crestFoam + shoreFoam, 0.0, 1.0) * topFace;

    // ---- Combine ----
    vec3 color = mix(bodyCol, reflection, fresnel);
    color += specular * (0.35 + 0.65 * waterReflectivity);
    color = mix(color, vec3(0.90, 0.95, 0.96), foam * 0.85);

    // ---- Alpha: more transparent looking straight down, opaque at glancing
    //      angles; foam and glints always read solid ----
    float alpha = clamp(waterOpacity, 0.05, 1.0) * (0.60 + 0.40 * (1.0 - NdV));
    alpha = clamp(alpha + fresnel * 0.35 + foam * 0.45 + sunSpec * 0.4, 0.05, 1.0);

    if (backside) {
        // Seen from underwater: milkier, brighter surface (approx. Snell window)
        color = mix(color, waterTint * 1.4 + vec3(0.10, 0.18, 0.20), 0.35);
        alpha = min(alpha + 0.15, 1.0);
    }

    FragColor = vec4(color, alpha);
}""",

    'glass.vert': """#version 330 core
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
uniform mat3 normalMatrix;

void main() {
    FragPos     = vec3(model * vec4(aPos, 1.0));
    Normal      = normalize(normalMatrix * aNormal);
    TexCoords   = aTexCoords;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",

    'glass.frag': """#version 330 core
precision mediump float;
out vec4 FragColor;

in highp vec3 FragPos;
in vec3 Normal;
in highp vec2 TexCoords;

uniform highp vec3 viewPos;
uniform vec3 waterColor;
uniform float distortionStrength;
uniform float causticStrength;
uniform float glassOpacity;
uniform float refractionIndex;
uniform float roughness;

highp float random(in highp vec2 st) {
    return fract(sin(dot(st, vec2(12.9898, 78.233))) * 43758.5453123);
}

highp float noise(in highp vec2 st) {
    highp vec2 i = floor(st);
    highp vec2 f = fract(st);
    float a = random(i);
    float b = random(i + vec2(1.0, 0.0));
    float c = random(i + vec2(0.0, 1.0));
    float d = random(i + vec2(1.0, 1.0));
    highp vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}

#define NUM_OCTAVES 3
highp float fbm(in highp vec2 st) {
    float v = 0.0;
    float a = 0.5;
    highp vec2 shift = vec2(100.0);
    mat2 rot = mat2(cos(0.5), sin(0.5), -sin(0.5), cos(0.5));
    for (int i = 0; i < NUM_OCTAVES; ++i) {
        v  += a * noise(st);
        st  = rot * st * 2.0 + shift;
        a  *= 0.5;
    }
    return v;
}

highp float pattern(in highp vec2 p) {
    return fbm(p + vec2(fbm(p)));
}

void main() {
    vec3 viewDir    = normalize(viewPos - FragPos);
    vec3 baseNormal = normalize(Normal);
    vec3 lightDir   = normalize(vec3(0.5, 1.0, 0.3));

    highp vec2 surfaceUV = FragPos.xz * 0.5 + FragPos.xy * 0.3;
    
    float iorRatio  = 1.0 / max(refractionIndex, 1.0);
    vec3 refractDir = refract(-viewDir, baseNormal, iorRatio);
    highp vec2 refractUV  = surfaceUV + refractDir.xy * distortionStrength * 0.2;

    float bumpScale     = 1.5 + roughness * 8.0;
    float surfaceHeight = pattern(refractUV * bumpScale);
    float epsilon       = 0.015;
    float hA = pattern((refractUV + vec2(epsilon, 0.0)) * bumpScale);
    float hB = pattern((refractUV + vec2(0.0, epsilon)) * bumpScale);

    float distortMul = (distortionStrength * 4.0) + roughness * 2.0;
    vec3 perturbedNormal = normalize(vec3(
        (surfaceHeight - hA) * distortMul,
        1.0 / max(distortMul * 3.0, 0.1),
        (surfaceHeight - hB) * distortMul
    ));
    
    float normalMix  = 0.5 + roughness * 0.4 + distortionStrength * 0.3;
    vec3 finalNormal = normalize(baseNormal + perturbedNormal * normalMix);

    float combinedPattern = pattern(surfaceUV * 2.0) * 0.7 + pattern(surfaceUV * 8.0) * 0.3;
    
    float fresnelPower       = mix(1.5, 10.0, causticStrength);
    float fresnel            = pow(1.0 - max(dot(viewDir, finalNormal), 0.0), fresnelPower);
    float fresnelWithPattern = fresnel * (0.8 + combinedPattern * 0.4);

    vec3  reflectDir  = reflect(-lightDir, finalNormal);
    float shininess   = mix(256.0, 16.0, roughness);
    float spec        = pow(max(dot(viewDir, reflectDir), 0.0), shininess);
    vec3  reflectDir2 = reflect(-viewDir, finalNormal);
    float envSpec     = pow(max(dot(reflectDir2, vec3(0.0, 1.0, 0.0)), 0.0), 32.0);
    vec3  specular    = vec3(1.0) * (spec * causticStrength * 4.0 + envSpec * 0.5);

    vec3 surfaceColor    = waterColor * (0.85 + combinedPattern * 0.3);
    vec3 reflectionColor = vec3(0.95, 0.98, 1.0) + vec3(combinedPattern * 0.1);
    vec3 baseMix         = mix(surfaceColor, reflectionColor, fresnelWithPattern * min(causticStrength * 2.5, 1.0));
    vec3 angleColor      = vec3(0.9, 0.95, 1.0) * fresnel * 0.2;
    vec3 finalRGB        = baseMix + angleColor;
    finalRGB += specular * (2.5 - roughness * 1.2);
    finalRGB += vec3(combinedPattern * 0.15 * (1.0 - glassOpacity)) * waterColor;

    float alpha = clamp(
        glassOpacity
        + fresnelWithPattern * (0.2 + roughness * 0.1) * (1.0 - glassOpacity)
        + roughness * 0.25
        + combinedPattern * 0.08,
        0.05, 1.0
    );
    
    FragColor = vec4(finalRGB, alpha);
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

uniform Light lights[8];
uniform int active_lights;
""" + SHADOW_GLSL + """
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
        texColor = VertexColor;
    }
    
    vec3 skyColor    = vec3(0.6, 0.75, 0.9);
    vec3 groundColor = vec3(0.3, 0.25, 0.2);
    float skyFactor  = (norm.y + 1.0) * 0.5;
    vec3 ambient     = mix(groundColor, skyColor, skyFactor) * 0.3 * texColor;
    
    vec3 result  = ambient;
    vec3 sunDir  = normalize(vec3(0.4, 0.7, 0.3));
    vec3 sunColor = vec3(1.0, 0.95, 0.85);
    float sunDiff    = max(dot(norm, sunDir), 0.0);
    float wrappedDiff = (sunDiff + 0.3) / 1.3;
    result += wrappedDiff * sunColor * 0.7 * texColor;
    
    vec3 fillDir  = normalize(vec3(-0.3, 0.2, -0.4));
    float fillDiff = max(dot(norm, fillDir), 0.0) * 0.2;
    result += fillDiff * skyColor * texColor;
    
    for (int i = 0; i < active_lights; i++) {
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
    
    FragColor = vec4(result, 1.0);
}"""
}

# ----- ARM‑optimised shaders (used by BaseRenderer when arm_mode is True) -----
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
uniform Light lights[16];
uniform int active_lights;""" + SHADOW_GLSL + """
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.12) * object_color;
    for(int i = 0; i < active_lights && i < 16; i++) {
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
    FragColor = vec4(result, alpha);
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
uniform Light lights[16];
uniform int active_lights;""" + SHADOW_GLSL + """
void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.12) * texColor.rgb;
    for(int i = 0; i < active_lights && i < 16; i++) {
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
    FragColor = vec4(result, texColor.a);
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