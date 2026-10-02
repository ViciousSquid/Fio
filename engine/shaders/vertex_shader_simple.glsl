#version 330
layout(location = 0) in vec3 a_position;
uniform mat4 projection;
uniform mat4 view;
uniform mat4 model;
uniform vec3 color;
out vec4 v_color;
void main() {
    gl_Position = projection * view * model * vec4(a_position, 1.0);
    v_color = vec4(color, 1.0);
}