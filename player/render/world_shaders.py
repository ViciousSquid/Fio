"""Small ES 3.0 world pass; no desktop-only shadow/sampler requirements."""

from engine.shaders import FOG_GLSL

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
uniform vec3 lightPos[32];
uniform vec3 lightColor[32];
uniform float lightRadius[32];
uniform float lightIntensity[32];
uniform int lightShadow[32];
uniform sampler2D shadowAtlas;
out vec4 fragColor;
""" + FOG_GLSL + """
float visibility(int light, vec3 delta) {
    if(lightShadow[light]<0) return 1.0;
    vec3 a=abs(delta);float face;vec2 uv;float d;
    if(a.x>=a.y && a.x>=a.z) {
        d=a.x;face=delta.x>0.0?0.0:1.0;uv=vec2(delta.x>0.0?-delta.z:delta.z,-delta.y)/d;
    } else if(a.y>=a.z) {
        d=a.y;face=delta.y>0.0?2.0:3.0;uv=vec2(delta.x,delta.y>0.0?delta.z:-delta.z)/d;
    } else {
        d=a.z;face=delta.z>0.0?4.0:5.0;uv=vec2(delta.z>0.0?delta.x:-delta.x,-delta.y)/d;
    }
    float farPlane=max(lightRadius[light],1.0);
    float depth=farPlane/(farPlane-.5)-(farPlane*.5)/((farPlane-.5)*max(d,.001));
    uv=clamp(uv*.5+.5,vec2(.002),vec2(.998));
    uv=(vec2(face,float(lightShadow[light]))+uv)/vec2(6.0,4.0);
    return depth-.001>texture(shadowAtlas,uv).r ? 0.0 : 1.0;
}
void main() {
    vec4 albedo = texture(diffuse, texCoord) * tint;
    if (albedo.a < 0.1) discard;
    vec3 n = normalize(worldNormal);
    vec3 illumination = vec3(0.1) + uAmbient;
    for (int i=0; i<32; ++i) {
        if (i >= lightCount) break;
        vec3 delta = lightPos[i] - worldPos;
        float distanceToLight = length(delta);
        float falloff = max(0.0, 1.0 - distanceToLight / max(lightRadius[i], 0.001));
        illumination += visibility(i, worldPos-lightPos[i]) * lightColor[i] * lightIntensity[i] * falloff * falloff
            * max(dot(n, delta / max(distanceToLight, 0.001)), 0.0);
    }
    fragColor = vec4(applyFog(albedo.rgb * (unlit ? vec3(1.0) : illumination), worldPos), albedo.a);
}
"""
