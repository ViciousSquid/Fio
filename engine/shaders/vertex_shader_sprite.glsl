#version 330 core
layout (location = 0) in vec2 a_pos;
uniform mat4 projection;
uniform mat4 view;
uniform vec3 sprite_pos_world;
uniform vec2 sprite_size;
out vec2 TexCoord;
void main() {
    vec4 pos_world = vec4(sprite_pos_world, 1.0);
    vec4 pos_view = view * pos_world;
    pos_view.xy += a_pos * sprite_size;
    gl_Position = projection * pos_view;
    TexCoord = a_pos + vec2(0.5, 0.5);
    TexCoord.y = 1.0 - TexCoord.y;
}