#version 330 core
layout (location = 0) in vec3 a_pos;
layout (location = 1) in vec3 a_normal;
layout (location = 2) in vec2 a_tex_coord;
out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoord;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
void main() {
    FragPos = vec3(model * vec4(a_pos, 1.0));
    Normal = mat3(transpose(inverse(model))) * a_normal;
    TexCoord = a_tex_coord;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}