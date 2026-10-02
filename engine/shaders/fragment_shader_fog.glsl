#version 330 core
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
}