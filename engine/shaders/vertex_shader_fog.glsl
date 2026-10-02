#version 330 core
layout (location = 0) in vec3 a_pos;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

out vec3 localPos;

void main() {
    // Pass the local position of the vertex
    localPos = a_pos;
    gl_Position = projection * view * model * vec4(a_pos, 1.0);
}