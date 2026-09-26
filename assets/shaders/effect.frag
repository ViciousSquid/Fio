#version 330 core
precision mediump float;

out vec4 FragColor;

in highp vec2 TexCoords;
in highp vec3 FragPos;
in highp vec4 EffectParams;
in highp vec4 EffectMeta;
in highp vec3 EffectColor;

uniform sampler2D fire_texture;

uniform int uFogEnabled;
uniform vec3 uFogColor;
uniform float uFogStart;
uniform float uFogEnd;
uniform float uFogDensity;
uniform highp vec3 uFogCamPos;
uniform vec3 uAmbient;

// firesheet.png: 5 columns x 4 rows, with one used cell on the final row.
const float FIRE_SHEET_COLUMNS = 5.0;
const float FIRE_SHEET_ROWS = 4.0;
const float FIRE_FRAME_COUNT = 16.0;
const float FIRE_FRAME_RATE = 12.0;

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

vec2 atlasUV(vec2 localUV, float frameIndex) {
    float column = mod(frameIndex, FIRE_SHEET_COLUMNS);
    float rowTop = floor(frameIndex / FIRE_SHEET_COLUMNS);
    float rowBottom = FIRE_SHEET_ROWS - 1.0 - rowTop;
    vec2 cellSize = vec2(
        1.0 / FIRE_SHEET_COLUMNS,
        1.0 / FIRE_SHEET_ROWS
    );
    return (vec2(column, rowBottom) + localUV) * cellSize;
}

void main() {
    float visualIntensity = max(EffectParams.y, 0.0);
    float elapsed = max(EffectParams.z, 0.0);
    float lifetime = max(EffectParams.w, 0.001);
    float seed = EffectMeta.x;
    float effectType = EffectMeta.y;

    float t = clamp(elapsed / lifetime, 0.0, 1.0);

    float frame = 0.0;
    if (effectType > 0.5) {
        frame = min(
            floor(t * FIRE_FRAME_COUNT),
            FIRE_FRAME_COUNT - 1.0
        );
    } else {
        float phase = fract(abs(seed) * 0.013) * FIRE_FRAME_COUNT;
        frame = mod(
            floor(elapsed * FIRE_FRAME_RATE + phase),
            FIRE_FRAME_COUNT
        );
    }

    vec2 uv = atlasUV(TexCoords, frame);
    vec4 sheet = texture(fire_texture, uv);

    if (sheet.a < 0.02 || visualIntensity <= 0.0) {
        discard;
    }

    float envelope = 1.0;
    float burst = 1.0;
    if (effectType > 0.5) {
        envelope = 1.0 - smoothstep(0.70, 1.0, t);
        float flash = exp(-t * t * 48.0);
        burst = 1.0 + flash * 2.2;
    }

    // Keep the sheet's authored colours while allowing the Effect colour
    // field to act as a gentle tint rather than replacing the artwork.
    vec3 tint = mix(vec3(1.0), max(EffectColor, vec3(0.001)), 0.35);
    vec3 rgb = sheet.rgb * tint;
    rgb *= visualIntensity * burst;
    float alpha = sheet.a * envelope;

    if (alpha < 0.01) discard;

    FragColor = vec4(applyFog(rgb, FragPos), alpha);
}
