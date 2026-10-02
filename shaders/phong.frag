// phong.frag
varying vec3 v_normal;
varying vec3 v_world_pos;

uniform int u_num_lights; // New uniform to specify the number of active lights
uniform vec3 u_light_pos[10]; // Changed to an array, e.g., for up to 10 lights
uniform vec3 u_view_pos;
uniform vec3 u_light_color[10]; // Changed to an array

void main()
{
    vec3 total_light_contribution = vec3(0.0);
    float ambient_strength = 0.2; // Ambient strength applies to all lights or a global ambient

    for (int i = 0; i < u_num_lights; ++i) {
        // Ambient component for each light (optional, can be global)
        vec3 ambient = ambient_strength * u_light_color[i];

        // Diffuse component
        vec3 norm = normalize(v_normal);
        vec3 light_dir = normalize(u_light_pos[i] - v_world_pos);
        float diff = max(dot(norm, light_dir), 0.0);
        vec3 diffuse = diff * u_light_color[i];

        // Specular component
        float specular_strength = 0.8;
        vec3 view_dir = normalize(u_view_pos - v_world_pos);
        vec3 reflect_dir = reflect(-light_dir, norm);
        float spec = pow(max(dot(view_dir, reflect_dir), 0.0), 32.0);
        vec3 specular = specular_strength * spec * u_light_color[i];

        total_light_contribution += (ambient + diffuse + specular);
    }

    // Combine and multiply by object color
    vec3 result = total_light_contribution * gl_Color.rgb;
    gl_FragColor = vec4(result, 1.0);
}