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
// This yields 16 actual frames; the empty atlas cells are never sampled.
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

    highp vec2 uv = TexCoords;
    highp float y = uv.y;
    highp float x = uv.x - 0.5;

    if (effectType > 0.5) {
        float t = clamp(elapsed / lifetime, 0.0, 1.0);

        // EXPLOSION uses the authored 16-frame atlas once over its lifetime.
        float frame = min(
            floor(t * EXPLOSION_FRAME_COUNT),
            EXPLOSION_FRAME_COUNT - 1.0
        );
        vec4 sheet = texture(explosion_texture, explosionAtlasUV(TexCoords, frame));

        if (sheet.a < 0.02 || visualIntensity <= 0.0) {
            discard;
        }

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

    // FIRE remains procedural. The explosion atlas is not used for FIRE.
    highp float drift = elapsed * 1.7 + seed * 0.013;
    highp float n0 = valueNoise(
        vec2(uv.x * 3.0, uv.y * 2.7 - drift), seed
    );
    highp float n1 = valueNoise(
        vec2(uv.x * 7.0 + drift * 0.65,
             uv.y * 6.0 - drift * 1.9),
        seed + 9.17
    );

    float width = mix(0.44, 0.10, smoothstep(0.05, 1.0, y));
    float warp = (n0 - 0.5) * 0.20 * (0.2 + 0.8 * y)
               + (n1 - 0.5) * 0.065;
    float lateral = abs(x + warp) / max(width, 0.015);

    float body = 1.0 - smoothstep(0.55, 1.0, lateral);
    float baseFade = smoothstep(0.0, 0.10, y);
    float topFade = 1.0 - smoothstep(0.70, 1.0, y);

    float emberNoise = valueNoise(
        vec2(uv.x * 13.0 + seed * 0.07,
             uv.y * 11.0 - elapsed * 2.6),
        seed + 21.3
    );
    float embers = smoothstep(0.91, 0.985, emberNoise)
                 * smoothstep(0.35, 0.9, y)
                 * (1.0 - smoothstep(0.78, 1.0, y));

    float alpha = clamp(
        body * baseFade * topFade + embers * 0.85, 0.0, 1.0
    );

    float core = exp(-lateral * lateral * 4.8)
               * smoothstep(0.0, 0.28, y);

    vec3 base = max(EffectColor, vec3(0.001));
    vec3 outer = base * 0.42;
    vec3 hot = mix(base, vec3(1.0, 0.90, 0.55), core);
    vec3 rgb = mix(
        outer,
        hot,
        clamp(core + embers * 0.7, 0.0, 1.0)
    );

    highp float phase = elapsed * 10.0 + seed * 0.013;
    highp float cell = floor(phase);
    highp float fracPart = phase - cell;
    highp float smoothPart = fracPart * fracPart * (3.0 - 2.0 * fracPart);
    highp float flickerA =
        fract(sin((cell + seed) * 12.9898) * 43758.5453123);
    highp float flickerB =
        fract(sin((cell + 1.0 + seed) * 12.9898) * 43758.5453123);
    float flicker = mix(flickerA, flickerB, smoothPart);

    rgb *= visualIntensity * (0.80 + 0.20 * flicker);
    if (alpha < 0.01 || visualIntensity <= 0.0) discard;

    FragColor = vec4(applyFog(rgb, FragPos), alpha);
}
