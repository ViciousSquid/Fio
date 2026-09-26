#version 330 core
precision highp float;

layout (location = 0) in vec2 aPos;
layout (location = 1) in vec3 iEffectPos;
layout (location = 2) in vec4 iEffectParams;  // size, intensity, elapsed, lifetime
layout (location = 3) in vec4 iEffectMeta;    // seed, type, spare, spare
layout (location = 4) in vec4 iEffectColor;

uniform mat4 projection;
uniform mat4 view;

out vec2 TexCoords;
out vec3 FragPos;
out vec4 EffectParams;
out vec4 EffectMeta;
out vec3 EffectColor;

void main() {
    float size = max(iEffectParams.x, 0.01);
    float elapsed = max(iEffectParams.z, 0.0);
    float lifetime = max(iEffectParams.w, 0.001);
    float effectType = iEffectMeta.y;

    float t = clamp(elapsed / lifetime, 0.0, 1.0);

    // FIRE stays at authored size. EXPLOSION rapidly expands from the same
    // primitive, then leaves the fragment shader to handle the bright/fade
    // envelope. The quad's bottom edge is anchored at the authored origin.
    float growth = 1.0;
    if (effectType > 0.5) {
        growth = mix(1.0, 3.0, smoothstep(0.0, 0.28, t));
    }

    // Upright cylindrical billboard: world Y keeps the flame vertical
    // while cameraRight makes the sheet face the camera in the horizontal plane.
    // This is more appropriate for fire than pitching the flame with cameraUp.
    vec3 cameraRight = normalize(vec3(view[0][0], view[1][0], view[2][0]));
    const vec3 worldUp = vec3(0.0, 1.0, 0.0);

    float vertical = aPos.y + 0.5;
    vec3 worldPos = iEffectPos
                  + cameraRight * aPos.x * size * growth
                  + worldUp * vertical * size * 1.25 * growth;

    TexCoords = aPos + 0.5;
    FragPos = worldPos;
    EffectParams = iEffectParams;
    EffectMeta = iEffectMeta;
    EffectColor = iEffectColor.rgb;

    gl_Position = projection * view * vec4(worldPos, 1.0);
}
