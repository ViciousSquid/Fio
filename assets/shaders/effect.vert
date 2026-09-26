#version 330 core
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

    // ------------------------------------------------------------------
    // FIRE - single billboard. The procedural texture in the fragment
    // shader supplies all the internal turbulence, so the eight crossed
    // cards of the earlier version are no longer needed.
    // ------------------------------------------------------------------
    const vec3 worldUp = vec3(0.0, 1.0, 0.0);
    float card = iParticleIndex;

    float r0 = hash11(iEffectMeta.x + card * 17.173);
    float r1 = hash11(iEffectMeta.x + card * 31.791);

    float tilt = (r0 - 0.5) * 0.25;    // small lean
    float yaw  = r1 * 6.2831853;       // random rotation around Y

    vec3 cameraRight = normalize(vec3(view[0][0], view[1][0], view[2][0]));
    vec3 cameraForward = vec3(-view[0][2], -view[1][2], -view[2][2]);
    cameraForward.y = 0.0;
    if (dot(cameraForward, cameraForward) < 0.0001) {
        cameraForward = vec3(-cameraRight.z, 0.0, cameraRight.x);
    } else {
        cameraForward = normalize(cameraForward);
    }

    // Billboard plane with a random yaw so different fire instances
    // don't all face the camera identically.
    vec3 cardRight = normalize(
        cameraRight * cos(yaw) + cameraForward * sin(yaw)
    );

    float vertical = aPos.y + 0.5;

    vec3 worldPos = iEffectPos
                  + cardRight * aPos.x * size
                  + worldUp * vertical * size * 1.35          // slightly taller
                  + vec3(tilt * size * vertical, 0.0, 0.0);   // lean

    TexCoords = aPos + 0.5;
    FragPos = worldPos;
    EffectParams = iEffectParams;
    EffectMeta = vec4(iEffectMeta.x, iEffectMeta.y, iParticleIndex, iEffectMeta.w);
    EffectColor = iEffectColor.rgb;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}
