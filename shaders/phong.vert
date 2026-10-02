// phong.vert
varying vec3 v_normal;
varying vec3 v_world_pos;

void main()
{
    // Transform normal from model space to view space
    v_normal = normalize(gl_NormalMatrix * gl_Normal);

    // Transform vertex position to world space for the fragment shader
    v_world_pos = vec3(gl_ModelViewMatrix * gl_Vertex);

    // Final vertex position
    gl_Position = ftransform();
    gl_FrontColor = gl_Color;
}