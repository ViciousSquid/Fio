"""The Wireframe display mode: the world's lines coloured by distance.

The host only publishes ``brush_display_mode``; this is how the built-in
renderer draws 'Wireframe'. The frame's opaque geometry -- brushes (water and
glass included), models, terrain -- is drawn as lines for depth alone, on
black, then coloured in one full-screen pass that rebuilds each pixel's world
position from that depth. The colour runs with distance from the eye: red and
orange up close, then yellow, green and cyan, to blue far away.
"""

import glm
import OpenGL.GL as gl
from engine.renderer.core import gl_state

from engine.constants import RENDER_MODE_LIT

from ...core.diagnostics import timed_pass
from ...core.resources import UniformCache
from ...core.raw_calls import uniform_matrix4

#: Distance, world units, over which the colour runs from red to blue.
DISTANCE_RANGE = 1500.0

_VERTEX = """#version 330 core
out vec2 vUV;
void main() {
    // One triangle that covers the viewport; no vertex buffer.
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    vUV = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_FRAGMENT = """#version 330 core
precision highp float;
in vec2 vUV;
out vec4 FragColor;
uniform sampler2D uDepth;
uniform mat4 uInvViewProj;
uniform vec3 uEye;
uniform float uRange;

// Near to far: red, orange, yellow, green, cyan, blue.
vec3 ramp(float t) {
    vec3 c[6] = vec3[](vec3(1.00, 0.12, 0.04), vec3(1.00, 0.45, 0.05),
                       vec3(1.00, 0.85, 0.10), vec3(0.30, 1.00, 0.20),
                       vec3(0.10, 0.85, 0.90), vec3(0.12, 0.25, 1.00));
    float x = clamp(t, 0.0, 1.0) * 5.0;
    int i = int(min(floor(x), 4.0));
    return mix(c[i], c[i + 1], x - float(i));
}

void main() {
    float depth = texture(uDepth, vUV).r;
    if (depth >= 1.0) discard;               // nothing was hit here
    vec4 w = uInvViewProj * vec4(vec3(vUV, depth) * 2.0 - 1.0, 1.0);
    vec3 P = w.xyz / w.w;
    float t = length(P - uEye) / uRange;
    float fade = 1.0 - 0.75 * smoothstep(0.85, 1.6, t);
    FragColor = vec4(ramp(t) * fade, 1.0);
}
"""


class DistanceMixin:
    """The distance-colour pass and the GL objects it owns."""

    def _init_distance(self):
        """Compile the distance program; without it Wireframe draws brush
        edges in the brushes' own colours."""
        try:
            program = self.shader_loader.compile_from_source(_VERTEX, _FRAGMENT)
        except Exception as exc:
            print(f"[Renderer] distance-colour shader unavailable: {exc}")
            return
        if not program:
            return
        self.shaders['distance'] = program
        self.uniforms['distance'] = UniformCache(program)
        # A core-profile draw needs a bound VAO even when it reads no
        # attribute. Registered in ``vaos`` so cleanup() releases it.
        self.vaos['distance'] = int(gl.glGenVertexArrays(1))

    def _distance_look(self, config, current_mode):
        """'Wireframe' when this frame is coloured by distance, else None."""
        look = config.get('brush_display_mode')
        if (look == 'Wireframe' and current_mode == RENDER_MODE_LIT
                and not self._portal_scene_pass):
            return look
        return None

    @timed_pass('distance colour')
    def draw_distance_pass(self, projection, view, camera_pos):
        """Colour the depth drawn so far by distance; False if it cannot."""
        if 'distance' not in self.shaders:
            return False
        if not self._capture_scene_depth():
            return False
        program, u = self.shaders['distance'], self.uniforms['distance']
        gl_state.glUseProgram(program)
        self._current_shader = program
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + self._water_depth_texture_unit)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._water_depth_texture)
        gl_state.glUniform1i(u['uDepth'], self._water_depth_texture_unit)
        # Held in a name: value_ptr does not keep its matrix alive.
        inverse = glm.inverse(projection * view)
        uniform_matrix4(u['uInvViewProj'], glm.value_ptr(inverse))
        gl_state.glUniform3f(u['uEye'], *self._camera_xyz(camera_pos))
        gl_state.glUniform1f(u['uRange'], DISTANCE_RANGE)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl_state.glDisable(gl.GL_DEPTH_TEST)
        gl_state.glDisable(gl.GL_BLEND)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl_state.glBindVertexArray(self.vaos['distance'])
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        self.render_stats.draw_calls += 1
        gl_state.glBindVertexArray(0)
        gl_state.glEnable(gl.GL_DEPTH_TEST)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        return True
