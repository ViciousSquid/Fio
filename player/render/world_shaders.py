"""Small ES 3.0 world pass; no desktop-only shadow/sampler requirements."""

VERTEX = """#version 300 es
precision highp float;
layout(location=0) in vec3 position;
layout(location=1) in vec3 normal;
layout(location=2) in vec2 uv;
uniform mat4 view;
uniform mat4 projection;
uniform mat4 model;
out vec3 worldPos;
out vec3 worldNormal;
out vec2 texCoord;
void main() {
    worldPos = vec3(model * vec4(position, 1.0));
    worldNormal = transpose(inverse(mat3(model))) * normal;
    texCoord = uv;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}
"""

FRAGMENT = """#version 300 es
precision highp float;
in vec3 worldPos;
in vec3 worldNormal;
in vec2 texCoord;
uniform sampler2D diffuse;
uniform vec4 tint;
uniform bool unlit;
uniform int lightCount;
uniform vec3 lightPos[8];
uniform vec3 lightColor[8];
uniform float lightRadius[8];
uniform float lightIntensity[8];
out vec4 fragColor;
void main() {
    vec4 albedo = texture(diffuse, texCoord) * tint;
    if (albedo.a < 0.1) discard;
    vec3 n = normalize(worldNormal);
    vec3 illumination = vec3(0.30);
    for (int i=0; i<8; ++i) {
        if (i >= lightCount) break;
        vec3 delta = lightPos[i] - worldPos;
        float distanceToLight = length(delta);
        float falloff = max(0.0, 1.0 - distanceToLight / max(lightRadius[i], 0.001));
        illumination += lightColor[i] * lightIntensity[i] * falloff * falloff
            * max(dot(n, delta / max(distanceToLight, 0.001)), 0.0);
    }
    fragColor = vec4(albedo.rgb * (unlit ? vec3(1.0) : illumination), albedo.a);
}
"""
