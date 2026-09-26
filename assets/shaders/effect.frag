#version 330 core
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

// ------------------------------------------------------------------
// HASH / NOISE
// ------------------------------------------------------------------
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

// 3-D value noise: third axis is time, so the flame evolves in place
// rather than scrolling a static 2-D pattern. Two hash lookups per
// lattice point is enough for the turbulence scales used below; the
// classic gradient-noise artefacts never get a chance to show through
// the density field.
highp float valueNoise3(highp vec3 p, highp float seed) {
    highp vec3 i = floor(p);
    highp vec3 f = fract(p);
    highp vec3 u = f * f * (3.0 - 2.0 * f);

    float n000 = hash21(i.xy + i.z * 17.0, seed);
    float n100 = hash21(i.xy + vec2(1.0, 0.0) + i.z * 17.0, seed);
    float n010 = hash21(i.xy + vec2(0.0, 1.0) + i.z * 17.0, seed);
    float n110 = hash21(i.xy + vec2(1.0, 1.0) + i.z * 17.0, seed);
    float n001 = hash21(i.xy + (i.z + 1.0) * 17.0, seed);
    float n101 = hash21(i.xy + vec2(1.0, 0.0) + (i.z + 1.0) * 17.0, seed);
    float n011 = hash21(i.xy + vec2(0.0, 1.0) + (i.z + 1.0) * 17.0, seed);
    float n111 = hash21(i.xy + vec2(1.0, 1.0) + (i.z + 1.0) * 17.0, seed);

    float nx00 = mix(n000, n100, u.x);
    float nx10 = mix(n010, n110, u.x);
    float nx01 = mix(n001, n101, u.x);
    float nx11 = mix(n011, n111, u.x);

    float nxy0 = mix(nx00, nx10, u.y);
    float nxy1 = mix(nx01, nx11, u.y);

    return mix(nxy0, nxy1, u.z);
}

// ------------------------------------------------------------------
// BLACKBODY COLOUR RAMP
// Maps 0..1 temperature to physically plausible fire colours:
// dark red -> red -> orange -> yellow -> white.
// ------------------------------------------------------------------
vec3 blackbody(float t) {
    t = clamp(t, 0.0, 1.0);
    vec3 dark   = vec3(0.12, 0.01, 0.00);
    vec3 red    = vec3(0.95, 0.10, 0.00);
    vec3 orange = vec3(1.00, 0.45, 0.03);
    vec3 yellow = vec3(1.00, 0.82, 0.30);
    vec3 white  = vec3(1.00, 0.97, 0.88);

    if (t < 0.20) return mix(dark,   red,    t / 0.20);
    if (t < 0.48) return mix(red,    orange, (t - 0.20) / 0.28);
    if (t < 0.76) return mix(orange, yellow, (t - 0.48) / 0.28);
    return                mix(yellow, white,  (t - 0.76) / 0.24);
}

// ------------------------------------------------------------------
// FOG
// ------------------------------------------------------------------
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

// ------------------------------------------------------------------
// EXPLOSION ATLAS
// ------------------------------------------------------------------
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

// ------------------------------------------------------------------
// PROCEDURAL FIRE TEXTURE
// Source-style: a base flame profile (wide at the base, tapering to a
// point) is perturbed by 3-D noise that scrolls upward over time. No
// texture atlas is needed; the density, temperature and colour are all
// generated in the shader, which is exactly how the classic
// env_fire sprites worked before the flipbook pipeline.
// ------------------------------------------------------------------
vec4 sampleFire(vec2 uv, float elapsed, float seed, float intensity) {
    float x = uv.x - 0.5;   // -0.5 .. 0.5
    float y = uv.y;         //  0.0 .. 1.0

    // ---- base profile --------------------------------------------
    float baseRadius = mix(0.48, 0.06, pow(y, 0.75));

    // A gentle sway so the flame is never perfectly vertical.
    float sway = sin(y * 3.8 + elapsed * 0.6 + seed * 2.1) * 0.07 * y;
    float lateral = abs(x + sway);

    float profile = 1.0 - smoothstep(0.0, baseRadius, lateral);
    profile = pow(profile, 1.6);

    profile *= smoothstep(0.0, 0.08, y);
    profile *= 1.0 - smoothstep(0.85, 1.0, y);

    if (profile <= 0.0) return vec4(0.0);

    // ---- 3-D noise perturbation ----------------------------------
    float speed = 0.9 + 0.4 * hash21(vec2(seed, 0.0), seed);

    vec3 p;
    p.x = x * 2.8;
    p.y = y * 4.2 - elapsed * speed;
    p.z = seed * 7.3;

    // Large-scale turbulence: breaks the smooth profile into tongues.
    float nLarge = valueNoise3(p * 0.9, seed + 11.0);
    nLarge = mix(0.5, 1.5, nLarge);

    // Medium detail: ragged edges and secondary tongues.
    float nMid = valueNoise3(p * 2.3 + vec3(4.1, 1.7, 0.0), seed + 37.0);
    nMid = mix(0.6, 1.4, nMid);

    // Fine detail: small hot spots and holes.
    float nFine = valueNoise3(p * 5.5 + vec3(11.0, -3.0, 5.0), seed + 73.0);
    nFine = mix(0.7, 1.3, nFine);

    float density = profile * nLarge * (0.7 + 0.3 * nMid) * (0.8 + 0.2 * nFine);

    // A second, independent field carves holes so the flame is not a
    // solid mass.
    float holes = valueNoise3(p * 1.7 + vec3(2.9, -1.1, 8.0), seed + 91.0);
    density *= mix(0.55, 1.0, smoothstep(0.35, 0.75, holes));

    // Embers / detached hot fragments near the upper part.
    float emberNoise = valueNoise3(p * 8.0 + vec3(3.0, 2.0, 1.0), seed + 127.0);
    float embers = smoothstep(0.86, 0.98, emberNoise)
                 * smoothstep(0.35, 0.75, y)
                 * (1.0 - smoothstep(0.80, 1.0, y));
    density = clamp(density + embers * 0.45, 0.0, 1.0);

    // ---- temperature / colour ------------------------------------
    float core = exp(-lateral * lateral * 3.2);
    core *= smoothstep(0.02, 0.45, y);
    core *= (0.65 + 0.35 * nLarge);

    float temperature = clamp(
        0.15
        + core * 0.95
        + nMid * 0.15
        - y * 0.15,
        0.0, 1.0
    );

    vec3 rgb = blackbody(temperature);

    // ---- alpha / softness ----------------------------------------
    float alpha = density * clamp(0.55 + temperature * 0.40, 0.0, 1.0);

    // ---- flicker -------------------------------------------------
    float slow = valueNoise3(vec3(elapsed * 0.35, seed * 0.01, 0.0), seed + 151.0);
    float fast = valueNoise3(vec3(elapsed * 2.8, seed * 0.07, 0.0), seed + 173.0);
    float flicker = mix(0.82, 1.18, clamp(slow * 0.7 + fast * 0.3, 0.0, 1.0));

    rgb *= intensity * flicker;
    alpha *= clamp(intensity, 0.0, 1.5);

    return vec4(rgb, alpha);
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
    // The entire flame - shape, colour, turbulence, embers - is generated
    // procedurally in sampleFire(). No texture atlas is needed.
    vec4 fire = sampleFire(TexCoords, elapsed, seed, visualIntensity);

    if (fire.a < 0.012 || visualIntensity <= 0.0) discard;

    FragColor = vec4(applyFog(fire.rgb, FragPos), fire.a);
}
