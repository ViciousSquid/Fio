import os

SHADER_DIR = os.path.join(os.path.dirname(__file__), 'shaders')

# ==============================================================================
# DEFAULT SHADER SOURCES
# ==============================================================================
DEFAULT_SHADERS = {
    'simple.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
void main() {
    gl_Position = projection * view * model * vec4(aPos, 1.0);
}""",
    'simple.frag': """#version 330 core
out vec4 FragColor;
uniform vec3 color;
uniform float alpha;
void main() {
    FragColor = vec4(color, alpha);
}""",

    'lit.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
out vec3 FragPos;
out vec3 Normal;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = mat3(transpose(inverse(model))) * aNormal;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",
    'lit.frag': """#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
uniform vec3 object_color;
uniform float alpha;
struct Light { vec3 position; vec3 color; float intensity; float radius; };
uniform Light lights[8];
uniform int active_lights;
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.1) * object_color; // Ambient
    for(int i = 0; i < active_lights; i++) {
        float distance = length(lights[i].position - FragPos);
        if(distance < lights[i].radius) {
            vec3 lightDir = normalize(lights[i].position - FragPos);
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - smoothstep(0.0, lights[i].radius, distance);
            result += (diff * lights[i].color * lights[i].intensity * att) * object_color;
        }
    }
    FragColor = vec4(result, alpha);
}""",

    'textured.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoords;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform vec2 tex_scale; // Controls tiling (1.0 = stretch, >1.0 = repeat)

void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = mat3(transpose(inverse(model))) * aNormal;
    
    // Apply tiling scale to UV coordinates
    TexCoords = aTexCoords * tex_scale;
    
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",
    'textured.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoords;

uniform sampler2D texture_diffuse;
struct Light { vec3 position; vec3 color; float intensity; float radius; };
uniform Light lights[8];
uniform int active_lights;

void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
    
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.1) * texColor.rgb; // Ambient
    
    for(int i = 0; i < active_lights; i++) {
        float distance = length(lights[i].position - FragPos);
        if(distance < lights[i].radius) {
            vec3 lightDir = normalize(lights[i].position - FragPos);
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - smoothstep(0.0, lights[i].radius, distance);
            result += (diff * lights[i].color * lights[i].intensity * att) * texColor.rgb;
        }
    }
    FragColor = vec4(result, texColor.a);
}""",

    'sprite.vert': """#version 330 core
layout (location = 0) in vec2 aPos;
out vec2 TexCoords;
uniform mat4 projection;
uniform mat4 view;
uniform vec3 sprite_pos_world;
uniform vec2 sprite_size;
void main() {
    TexCoords = aPos + 0.5;
    vec3 cameraRight = vec3(view[0][0], view[1][0], view[2][0]);
    vec3 cameraUp = vec3(view[0][1], view[1][1], view[2][1]);
    vec3 worldPos = sprite_pos_world 
                  + cameraRight * aPos.x * sprite_size.x 
                  + cameraUp * aPos.y * sprite_size.y;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}""",
    'sprite.frag': """#version 330 core
out vec4 FragColor;
in vec2 TexCoords;
uniform sampler2D sprite_texture;
void main() {
    vec4 texColor = texture(sprite_texture, TexCoords);
    if(texColor.a < 0.1) discard;
    FragColor = texColor;
}""",

    'fog.vert': """#version 330 core
layout (location = 0) in vec3 a_pos;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

out vec3 localPos;

void main() {
    // Pass the local position of the vertex
    localPos = a_pos;
    gl_Position = projection * view * model * vec4(a_pos, 1.0);
}""",
    'fog.frag': """#version 330 core
out vec4 FragColor;

in vec3 localPos; // Interpolated local position of the fragment on the cube surface

uniform mat4 model;
uniform vec3 viewPos; // Camera's world position

uniform float density;
uniform vec3 fogColor;
uniform sampler3D noiseTexture;
uniform float noiseScale;
uniform float time;

// AABB is a unit cube from -0.5 to 0.5
vec2 intersectBox(vec3 rayOrigin, vec3 rayDir) {
    vec3 tMin = (-0.5 - rayOrigin) / rayDir;
    vec3 tMax = (0.5 - rayOrigin) / rayDir;
    vec3 t1 = min(tMin, tMax);
    vec3 t2 = max(tMin, tMax);
    float tNear = max(max(t1.x, t1.y), t1.z);
    float tFar = min(min(t2.x, t2.y), t2.z);
    return vec2(tNear, tFar);
}

void main() {
    // Calculate ray origin and direction in world space first
    vec3 fragWorldPos = vec3(model * vec4(localPos, 1.0));
    vec3 rayDirWorld = normalize(fragWorldPos - viewPos);

    // Now, transform the ray into the local space of the fog volume
    mat4 inverseModel = inverse(model);
    vec3 rayOriginLocal = (inverseModel * vec4(viewPos, 1.0)).xyz;
    vec3 rayDirLocal = normalize((inverseModel * vec4(rayDirWorld, 0.0)).xyz);

    // Calculate the entry and exit points of the ray through the cube
    vec2 t = intersectBox(rayOriginLocal, rayDirLocal);
    float tNear = t.x;
    float tFar = t.y;

    if (tNear >= tFar) {
        discard;
    }

    tNear = max(0.0, tNear);

    int num_steps = 32; // Reduced steps slightly for performance
    float stepSize = (tFar - tNear) / float(num_steps);
    vec4 accumulatedColor = vec4(0.0);

    // Ray Marching Loop
    for (int i = 0; i < num_steps; ++i) {
        float currentT = tNear + float(i) * stepSize;
        vec3 samplePos = rayOriginLocal + rayDirLocal * currentT;
        
        vec3 noiseCoord = samplePos * noiseScale + vec3(0.0, 0.0, time * 0.1);
        float noiseValue = texture(noiseTexture, noiseCoord).r;
        
        float stepDensity = density * noiseValue;
        float transmittance = exp(-stepDensity * stepSize);

        // Correctly blend color based on remaining transparency
        accumulatedColor.rgb += fogColor * (1.0 - transmittance) * (1.0 - accumulatedColor.a);
        accumulatedColor.a += (1.0 - transmittance);

        if (accumulatedColor.a > 0.99) {
            break;
        }
    }
    
    accumulatedColor.a = clamp(accumulatedColor.a, 0.0, 1.0);
    FragColor = accumulatedColor;
}""",

    'water.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out vec2 TexCoords;
out vec3 Normal; // Pass normal to frag for recalculation if needed

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform float time;

// New uniforms for displacement
uniform int useWaveDisplacement;
uniform float waveStrength; // Controls the height/amplitude

void main()
{
    vec3 pos = aPos;
    
    // Sum of Sines Displacement
    if (useWaveDisplacement == 1) {
        float speed = time * 1.5;
        
        // Wave 1 (Large, slow)
        float y = sin(pos.x * 0.5 + speed) * cos(pos.z * 0.5 + speed) * waveStrength;
        
        // Wave 2 (Smaller, diagonal)
        y += sin(pos.x * 1.1 + pos.z * 0.4 + speed * 1.2) * (waveStrength * 0.5);
        
        // Wave 3 (Detail irregularity)
        y += cos(pos.x * 2.1 - speed) * (waveStrength * 0.2);
        
        pos.y += y;
    }
    
    FragPos = vec3(model * vec4(pos, 1.0));
    
    // Scale UVs (4.0) gives good density for the normal map provided
    TexCoords = aTexCoords * 4.0; 
    
    // Recalculate normal based on model rotation
    Normal = mat3(transpose(inverse(model))) * aNormal;
    
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",

    'water.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec2 TexCoords;
in vec3 Normal;

struct Light {
    vec3 position;
    vec3 color;
    float intensity;
    float radius;
};

#define MAX_LIGHTS 8
uniform Light lights[MAX_LIGHTS];
uniform int active_lights;
uniform vec3 viewPos;
uniform sampler2D normalMap; 
uniform float time;

uniform float waterOpacity;
uniform float waterReflectivity;
uniform vec3 waterTint;

void main()
{
    // 1. Animated Normal Mapping (Counter-scrolling layers)
    vec2 speed = vec2(0.04, 0.02);
    
    // Layer 1: Moves diagonally
    vec2 coord1 = TexCoords + time * speed;
    vec3 n1 = texture(normalMap, coord1).rgb;
    
    // Layer 2: Moves opposite direction, slightly larger scale
    vec2 coord2 = (TexCoords * 0.7) - (time * vec2(speed.y, speed.x));
    vec3 n2 = texture(normalMap, coord2).rgb;
    
    // Blend normals for chaotic surface detail
    vec3 norm = normalize((n1 + n2) - 1.0);

    // 2. Base Color & Environment
    vec3 viewDir = normalize(viewPos - FragPos);
    
    // Fresnel Effect: Stronger at grazing angles (Schlick's approximation)
    // R0 is the reflection coefficient at 0 degrees.
    float R0 = 0.02; 
    float fresnel = R0 + (1.0 - R0) * pow(1.0 - max(dot(viewDir, vec3(0.0, 1.0, 0.0)), 0.0), 5.0);
    fresnel = clamp(fresnel * waterReflectivity * 2.5, 0.0, 1.0);

    // 3. Lighting (Blinn-Phong for sharper, wetter highlights)
    vec3 lightAccumulation = vec3(0.0);
    vec3 specularAccum = vec3(0.0);
    
    // Dynamic Shininess: Wet surfaces have high shininess (tight highlights)
    float shininess = 128.0; 

    for(int i = 0; i < active_lights; i++) {
        float distance = length(lights[i].position - FragPos);
        if(distance < lights[i].radius) {
            vec3 lightDir = normalize(lights[i].position - FragPos);
            
            // Attenuation
            float att = 1.0 - smoothstep(0.0, lights[i].radius, distance);
            vec3 lightColor = lights[i].color * lights[i].intensity * att;

            // Diffuse
            float diff = max(dot(norm, lightDir), 0.0);
            lightAccumulation += diff * lightColor;
            
            // Specular (Blinn-Phong)
            vec3 halfwayDir = normalize(lightDir + viewDir);
            float spec = pow(max(dot(norm, halfwayDir), 0.0), shininess);
            specularAccum += spec * lightColor;
        }
    }
    
    // Ambient component (Water isn't pitch black in shadow)
    vec3 ambient = vec3(0.15) * waterTint;
    
    // 4. Composition
    // Mix the water tint with the light calculation
    vec3 diffuseColor = waterTint * (ambient + lightAccumulation);
    
    // Fake Sky Reflection Color
    vec3 skyColor = vec3(0.65, 0.80, 0.95);
    
    // Mix diffuse water with sky reflection based on Fresnel
    vec3 finalColor = mix(diffuseColor, skyColor, fresnel);
    
    // Add Specular Highlights on top (Sun glitter)
    // Multiplied by reflectivity to allow dull water
    finalColor += specularAccum * (waterReflectivity * 2.0);

    // 5. Alpha Calculation
    // Water is more opaque at grazing angles (fresnel) and based on base opacity
    float alpha = clamp(waterOpacity + (fresnel * 0.6), 0.0, 1.0);

    FragColor = vec4(finalColor, alpha);
}""",

    'glass.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoords;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = mat3(transpose(inverse(model))) * aNormal;
    TexCoords = aTexCoords;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}""",

    'glass.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoords;

uniform vec3 viewPos;
uniform vec3 waterColor; // Acts as the base glass tint
uniform float distortionStrength;
uniform float causticStrength;
uniform float glassOpacity;      // Base opacity (0=transparent, 1=opaque)
uniform float refractionIndex;   // Index of refraction (1.0-2.5)
uniform float roughness;          // Surface roughness (0=clear, 1=frosted)

// --- NOISE & PATTERN FUNCTIONS ---
float random(in vec2 _st) {
    return fract(sin(dot(_st.xy, vec2(12.9898,78.233))) * 43758.5453123);
}

float noise(in vec2 _st) {
    vec2 i = floor(_st);
    vec2 f = fract(_st);
    float a = random(i);
    float b = random(i + vec2(1.0, 0.0));
    float c = random(i + vec2(0.0, 1.0));
    float d = random(i + vec2(1.0, 1.0));
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(a, b, u.x) + (c - a)* u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}

#define NUM_OCTAVES 5
float fbm(in vec2 _st) {
    float v = 0.0;
    float a = 0.5;
    vec2 shift = vec2(100.0);
    mat2 rot = mat2(cos(0.5), sin(0.5), -sin(0.5), cos(0.50));
    for (int i = 0; i < NUM_OCTAVES; ++i) {
        v += a * noise(_st);
        _st = rot * _st * 2.0 + shift;
        a *= 0.5;
    }
    return v;
}

float pattern(in vec2 p) {
    // Domain warping pattern for surface irregularities
    return fbm(p + fbm(p + fbm(p)));
}

void main() {
    vec3 viewDir = normalize(viewPos - FragPos);
    vec3 baseNormal = normalize(Normal);
    vec3 lightDir = normalize(vec3(0.5, 1.0, 0.3));

    // --- USE WORLD-SPACE COORDINATES FOR VISIBLE SURFACE TEXTURE ---
    // Using FragPos makes the texture stay fixed on the surface (visible!)
    vec2 surfaceUV = FragPos.xz * 0.5 + FragPos.xy * 0.3; // Combine XZ and XY planes
    
    // --- REFRACTION ---
    float iorRatio = 1.0 / max(refractionIndex, 1.0);
    vec3 refractDir = refract(-viewDir, baseNormal, iorRatio);
    // Use world-space position for distortion calculation
    vec2 refractUV = surfaceUV + refractDir.xy * distortionStrength * 0.2;

    // --- SURFACE BUMP MAPPING ---
    float bumpScale = 1.5 + roughness * 8.0;
    float surfaceHeight = pattern(refractUV * bumpScale);
    float epsilon = 0.015;
    float hA = pattern((refractUV + vec2(epsilon, 0)) * bumpScale);
    float hB = pattern((refractUV + vec2(0, epsilon)) * bumpScale);

    float distortMultiplier = (distortionStrength * 4.0) + roughness * 2.0;
    vec3 perturbedNormal = normalize(vec3(
        (surfaceHeight - hA) * distortMultiplier,
        1.0 / max(distortMultiplier * 3.0, 0.1),
        (surfaceHeight - hB) * distortMultiplier
    ));
    
    // Strong normal mixing for visible surface detail
    float normalMix = 0.5 + roughness * 0.4 + distortionStrength * 0.3;
    vec3 finalNormal = normalize(baseNormal + perturbedNormal * normalMix);

    // --- VISIBLE SURFACE TEXTURE PATTERN ---
    // Add subtle surface variation that's always visible
    float surfacePattern = pattern(surfaceUV * 2.0);
    float detailPattern = pattern(surfaceUV * 8.0) * 0.3;
    float combinedPattern = surfacePattern * 0.7 + detailPattern;
    
    // --- FRESNEL EFFECT (Much stronger) ---
    float fresnelPower = mix(1.5, 10.0, causticStrength); // Wider range
    float fresnel = pow(1.0 - max(dot(viewDir, finalNormal), 0.0), fresnelPower);
    
    // Add pattern-based fresnel variation for surface detail
    float fresnelWithPattern = fresnel * (0.8 + combinedPattern * 0.4);

    // --- SPECULAR HIGHLIGHTS (Much more prominent) ---
    vec3 reflectDir = reflect(-lightDir, finalNormal);
    float shininess = mix(256.0, 16.0, roughness);
    float spec = pow(max(dot(viewDir, reflectDir), 0.0), shininess);
    
    // Add multiple specular lobes for more glass-like appearance
    vec3 reflectDir2 = reflect(-viewDir, finalNormal);
    float envSpec = pow(max(dot(reflectDir2, vec3(0, 1, 0)), 0.0), 32.0);
    
    vec3 specular = vec3(1.0) * (spec * causticStrength * 4.0 + envSpec * 0.5);

    // --- COMPOSITION WITH VISIBLE SURFACE DETAILS ---
    vec3 backgroundColor = waterColor;
    
    // Add surface color variation based on pattern
    vec3 surfaceColor = backgroundColor * (0.85 + combinedPattern * 0.3);
    
    // Bright reflection color for sky/environment
    vec3 reflectionColor = vec3(0.95, 0.98, 1.0) + vec3(combinedPattern * 0.1);
    
    // Mix with strong fresnel influence and pattern
    vec3 baseMix = mix(surfaceColor, reflectionColor, fresnelWithPattern * min(causticStrength * 2.5, 1.0));
    
    // Add subtle color shifts at different view angles
    vec3 angleColor = vec3(0.9, 0.95, 1.0) * fresnel * 0.2;
    
    vec3 finalRGB = baseMix + angleColor;
    
    // Add prominent specular highlights
    finalRGB += specular * (2.5 - roughness * 1.2);
    
    // Add slight surface scattering for depth
    float scattering = combinedPattern * 0.15 * (1.0 - glassOpacity);
    finalRGB += vec3(scattering) * waterColor;

    // --- ALPHA CALCULATION ---
    // Make opacity more visible at all ranges
    float fresnelContribution = fresnelWithPattern * (0.2 + roughness * 0.1) * (1.0 - glassOpacity);
    float roughnessOpacity = roughness * 0.25;
    float patternOpacity = combinedPattern * 0.08; // Surface pattern adds slight opacity
    float alpha = clamp(glassOpacity + fresnelContribution + roughnessOpacity + patternOpacity, 0.05, 1.0);
    
    FragColor = vec4(finalRGB, alpha);
}""",
    
    'shadow_volume.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
void main() {
    gl_Position = projection * view * model * vec4(aPos, 1.0);
}""",
    'shadow_volume.frag': """#version 330 core
out vec4 FragColor;
void main() {
    FragColor = vec4(0.0, 0.0, 0.0, 0.5);
}""",

    'terrain.vert': """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec3 aColor;
layout (location = 3) in vec2 aTexCoord;
layout (location = 4) in vec3 aSmoothNormal; // NEW: for splat mapping

out vec3 FragPos;
out vec3 Normal;
out vec3 VertexColor;
out vec2 TexCoords;
out vec3 SmoothNormal; // Passed to Fragment

uniform mat4 projection;
uniform mat4 view;

void main() {
    FragPos = aPos;
    Normal = aNormal;
    VertexColor = aColor;
    TexCoords = aTexCoord;
    SmoothNormal = aSmoothNormal;
    gl_Position = projection * view * vec4(aPos, 1.0);
}""",

    'terrain.frag': """#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec3 Normal;
in vec3 VertexColor;
in vec2 TexCoords;
in vec3 SmoothNormal; // Interpolated smooth normal

uniform sampler2D texGrass;    // 0
uniform sampler2D texRock;     // 1
uniform sampler2D texSand;     // 2
uniform sampler2D texSnow;     // 3
uniform vec4 biomeWeights;       // grass.r, rock.g, sand.b, snow.a
uniform float terrainHeightScale;
uniform int use_textures;

struct Light {
    vec3 position;
    vec3 color;
    float intensity;
    float radius;
};

uniform Light lights[8];
uniform int active_lights;

// Cheap 2D noise for variation
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}
float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}

// Procedural splat weights using SmoothNormal for soft blends
vec4 get_splat_weights(vec3 worldPos, vec3 smoothNorm) {
    float height = clamp(worldPos.y * terrainHeightScale, 0.0, 1.0);
    // Use SmoothNormal for slope calculation -> Smooth transitions!
    float slope = 1.0 - max(smoothNorm.y, 0.0);  
    float n = noise(worldPos.xz * 0.02 + height * 5.0) * 0.5 + 0.5;

    float grass_w = (1.0 - slope * 1.5) * (1.0 - height * 0.6) * biomeWeights.r;
    float rock_w = slope * 0.8 + n * 0.4 * biomeWeights.g;
    float sand_w = (1.0 - height * 0.4) * (1.0 - slope * 0.5) * biomeWeights.b;
    float snow_w = smoothstep(0.6, 1.0, height) * biomeWeights.a;

    vec4 weights = vec4(grass_w, rock_w, sand_w, snow_w);
    return weights / (dot(weights, vec4(1.0)) + 0.001);
}

void main() {
    // Normal used for Lighting (Flat/Faceted)
    vec3 norm = normalize(Normal);
    vec3 texColor;
    
    if (use_textures == 1) {
        // SmoothNormal used for Texturing (Smooth gradients)
        vec3 smoothNorm = normalize(SmoothNormal);
        
        // Splat blend
        vec4 splat = get_splat_weights(FragPos, smoothNorm);
        
        // Sample with reduced tiling to fix "grid" look (4.0 -> 1.0)
        vec4 grass_col = texture(texGrass, TexCoords * 1.0);
        vec4 rock_col = texture(texRock, TexCoords * 0.5 + vec2(splat.g * 0.5, 0.0));
        vec4 sand_col = texture(texSand, TexCoords * 1.5 + vec2(splat.b * 0.3, splat.b * 0.2));
        vec4 snow_col = texture(texSnow, TexCoords * 0.8);
        
        vec3 splatColor = (
            grass_col.rgb * splat.r +
            rock_col.rgb * splat.g +
            sand_col.rgb * splat.b +
            snow_col.rgb * splat.a
        );
        texColor = splatColor * VertexColor * 1.1;
    } else {
        texColor = VertexColor;
    }
    
    // Lighting uses Faceted 'norm' to keep low-poly look
    vec3 skyColor = vec3(0.6, 0.75, 0.9);
    vec3 groundColor = vec3(0.3, 0.25, 0.2);
    float skyFactor = (norm.y + 1.0) * 0.5;
    vec3 ambient = mix(groundColor, skyColor, skyFactor) * 0.3 * texColor;
    
    vec3 result = ambient;
    
    vec3 sunDir = normalize(vec3(0.4, 0.7, 0.3));
    vec3 sunColor = vec3(1.0, 0.95, 0.85);
    float sunDiff = max(dot(norm, sunDir), 0.0);
    float wrappedDiff = (sunDiff + 0.3) / 1.3;
    result += wrappedDiff * sunColor * 0.7 * texColor;
    
    vec3 fillDir = normalize(vec3(-0.3, 0.2, -0.4));
    float fillDiff = max(dot(norm, fillDir), 0.0) * 0.2;
    result += fillDiff * skyColor * texColor;
    
    for (int i = 0; i < active_lights; i++) {
        float distance = length(lights[i].position - FragPos);
        if (distance < lights[i].radius) {
            vec3 lightDir = normalize(lights[i].position - FragPos);
            float diff = max(dot(norm, lightDir), 0.0);
            float attenuation = 1.0 - smoothstep(0.0, lights[i].radius, distance);
            attenuation = attenuation * attenuation;
            result += diff * lights[i].color * lights[i].intensity * attenuation * texColor;
        }
    }
    
    float gray = dot(result, vec3(0.299, 0.587, 0.114));
    result = mix(vec3(gray), result, 1.15);
    
    FragColor = vec4(result, 1.0);
}"""
}

# Central Registry: (Shader Name) -> (Vertex Filename, Fragment Filename)
SHADER_MAP = {
    'simple':        ('simple.vert', 'simple.frag'),
    'lit':           ('lit.vert', 'lit.frag'),
    'textured':      ('textured.vert', 'textured.frag'),
    'sprite':        ('sprite.vert', 'sprite.frag'),
    'shadow_volume': ('shadow_volume.vert', 'shadow_volume.frag'),
    'fog':           ('fog.vert', 'fog.frag'),
    'water':         ('water.vert', 'water.frag'),
    'glass':         ('glass.vert', 'glass.frag'),
    'terrain':       ('terrain.vert', 'terrain.frag'),
    # Procedural is available but not yet integrated into the main render loop
    'procedural':    ('procedural_vert.glsl', 'procedural_frag.glsl'),
}

def load_shader_source(filename):
    """Loads a shader source string from the shader directory."""
    filepath = os.path.join(SHADER_DIR, filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"FATAL: Shader file not found: {filepath}")
        return ""
    except Exception as e:
        print(f"FATAL: Error reading shader file {filepath}: {e}")
        return ""