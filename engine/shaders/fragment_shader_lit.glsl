#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
struct Light {
    vec3 position;
    vec3 color;
    float intensity;
    float radius;
};
#define MAX_LIGHTS 16
uniform Light lights[MAX_LIGHTS];
uniform int active_lights;
uniform vec3 object_color;
uniform float alpha;
void main() {
    vec3 ambient = 0.15 * object_color;
    vec3 norm = normalize(Normal);
    vec3 total_diffuse = vec3(0.0);
    for (int i = 0; i < active_lights; i++) {
        vec3 light_dir = lights[i].position - FragPos;
        float distance = length(light_dir);
        if(distance < lights[i].radius){
            light_dir = normalize(light_dir);
            float diff = max(dot(norm, light_dir), 0.0);
            float attenuation = 1.0 - (distance / lights[i].radius);
            total_diffuse += lights[i].color * diff * lights[i].intensity * attenuation;
        }
    }
    vec3 result = ambient + (total_diffuse * object_color);
    FragColor = vec4(result, alpha);
}