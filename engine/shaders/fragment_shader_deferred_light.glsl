#version 330 core

out vec4 FragColor;

in vec2 TexCoord;

uniform sampler2D gPosition;
uniform sampler2D gNormal;
uniform sampler2D gAlbedoSpec;

// Light data
const int MAX_LIGHTS = 32;
uniform int numLights;
uniform vec3 lightPositions[MAX_LIGHTS];
uniform vec3 lightColors[MAX_LIGHTS];
uniform float lightIntensities[MAX_LIGHTS];
uniform float lightRadii[MAX_LIGHTS];

uniform vec3 viewPos;
uniform vec3 ambientColor;

void main() {
    // Retrieve G-Buffer data
    vec3 FragPos = texture(gPosition, TexCoord).rgb;
    vec3 Normal = texture(gNormal, TexCoord).rgb;
    vec3 Albedo = texture(gAlbedoSpec, TexCoord).rgb;
    float Specular = texture(gAlbedoSpec, TexCoord).a;
    
    // Skip background pixels (position = 0,0,0)
    if (length(FragPos) < 0.001) {
        discard;
    }
    
    // Ambient
    vec3 lighting = Albedo * ambientColor;
    vec3 viewDir = normalize(viewPos - FragPos);
    
    // Process each light
    for (int i = 0; i < numLights && i < MAX_LIGHTS; i++) {
        vec3 lightDir = lightPositions[i] - FragPos;
        float distance = length(lightDir);
        
        // Skip if outside light radius
        if (distance > lightRadii[i]) continue;
        
        lightDir = normalize(lightDir);
        
        // Attenuation
        float attenuation = 1.0 - (distance / lightRadii[i]);
        attenuation = attenuation * attenuation * lightIntensities[i];
        
        // Diffuse
        float diff = max(dot(Normal, lightDir), 0.0);
        vec3 diffuse = diff * Albedo * lightColors[i];
        
        // Specular (Blinn-Phong)
        vec3 halfwayDir = normalize(lightDir + viewDir);
        float spec = pow(max(dot(Normal, halfwayDir), 0.0), 32.0);
        vec3 specular = spec * Specular * lightColors[i];
        
        lighting += (diffuse + specular) * attenuation;
    }
    
    FragColor = vec4(lighting, 1.0);
}