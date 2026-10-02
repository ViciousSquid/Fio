#version 330 core

layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aTexCoord;
layout(location = 3) in vec3 aTangent;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;

out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoord;
out mat3 TBN;

void main()
{
    vec4 worldPos = model * vec4(aPos, 1.0);
    FragPos = worldPos.xyz;
    
    gl_Position = projection * view * worldPos;
    
    Normal = normalize(normalMatrix * aNormal);
    TexCoord = aTexCoord;
    
    vec3 T = normalize(normalMatrix * aTangent);
    vec3 N = Normal;
    T = normalize(T - dot(T, N) * N);
    vec3 B = cross(N, T);
    TBN = mat3(T, B, N);
}