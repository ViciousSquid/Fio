#version 330 core
layout (location = 0) in vec3 a_pos;
layout (location = 1) in vec3 a_normal;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform vec3 light_pos;
uniform float extrude_amount = 1000.0;
void main()
{
    vec3 world_pos = vec3(model * vec4(a_pos, 1.0));
    vec3 light_dir = normalize(world_pos - light_pos);
    if (dot(a_normal, light_dir) < 0.0) {
        gl_Position = projection * view * vec4(world_pos + light_dir * extrude_amount, 1.0);
    } else {
        gl_Position = projection * view * model * vec4(a_pos, 1.0);
    }
}