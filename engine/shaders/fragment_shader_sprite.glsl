#version 330 core
out vec4 FragColor;
in vec2 TexCoord;
uniform sampler2D sprite_texture;
void main() {
    vec4 tex_color = texture(sprite_texture, TexCoord);
    if(tex_color.a < 0.1) discard;
    FragColor = tex_color;
}