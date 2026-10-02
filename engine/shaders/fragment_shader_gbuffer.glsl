#version 330 core

layout(location = 0) out vec3 gPosition;
layout(location = 1) out vec3 gNormal;
layout(location = 2) out vec4 gAlbedoSpec;

in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoord;
in mat3 TBN;

uniform sampler2D texture_diffuse1;
uniform sampler2D texture_specular1;
uniform vec3 materialColor;
uniform float specularStrength;

void main()
{
    // Position
    gPosition = FragPos;
    
    // Normal (in view space)
    gNormal = normalize(Normal);
    
    // Albedo and specular
    vec4 diffuseColor = texture(texture_diffuse1, TexCoord);
    if(diffuseColor.a < 0.1)
        discard;
    
    gAlbedoSpec.rgb = diffuseColor.rgb * materialColor;
    gAlbedoSpec.a = texture(texture_specular1, TexCoord).r * specularStrength;
}