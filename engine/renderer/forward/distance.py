"""The Points and Wireframe display modes: the world coloured by distance.

The host only publishes ``brush_display_mode``; this is how the built-in
renderer draws two of its values. Both draw the frame's opaque geometry --
brushes (water and glass included), models, terrain -- for depth alone, on
black, then colour it in one full-screen pass that rebuilds each pixel's world
position from that depth. The colour runs with distance from the eye: red and
orange up close, then yellow, green and cyan, to blue far away.

* **Points** (after Scanner Sombre): the geometry is drawn as surfaces, and the
  pass lights only the pixels that fall on a scan point. Points sit on a
  jittered grid laid across every surface in world space, so they stay put as
  the camera moves, are hidden by whatever is in front of them, and shrink with
  distance.
* **Wireframe**: brushes are drawn as their true edges and models and terrain
  as line meshes; the pass colours every line pixel.
"""

import glm
import OpenGL.GL as gl

from engine.constants import RENDER_MODE_LIT

from ..core.diagnostics import timed_pass
from ..core.resources import UniformCache

#: World units between neighbouring scan points.
SCAN_SPACING = 9.0
#: A near point's core radius, world units.
SCAN_POINT_RADIUS = 2.0
#: Fraction of grid cells that hold a point; the rest are gaps in the scan.
SCAN_DENSITY = 0.5
#: Distance, world units, over which the colour runs from red to blue.
SCAN_RANGE = 1500.0

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
uniform float uPixelAngle;   // world size of one pixel per unit of distance
uniform float uSpacing;
uniform float uRadius;
uniform float uDensity;
uniform float uRange;
uniform int uPoints;          // 1: Points; 0: Wireframe (colour every pixel)

float hash13(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.zyx + 31.32);
    return fract((p.x + p.y) * p.z);
}

vec2 hash23(vec3 p) {
    vec3 q = fract(p * vec3(0.1031, 0.1030, 0.0973));
    q += dot(q, q.yzx + 33.33);
    return fract((q.xx + q.yz) * q.zy);
}

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
    float dist = length(P - uEye);
    float t = dist / uRange;
    float fade = 1.0 - 0.75 * smoothstep(0.85, 1.6, t);
    if (uPoints == 0) {
        FragColor = vec4(ramp(t) * fade, 1.0);
        return;
    }

    // Lay the point grid across the surface: the two axes the surface
    // spreads along, and which layer of the third it lies in.
    vec3 n = abs(normalize(cross(dFdx(P), dFdy(P))));
    vec2 uv; float along;
    if (n.x >= n.y && n.x >= n.z)  { uv = P.zy; along = P.x; }
    else if (n.y >= n.z)           { uv = P.xz; along = P.y; }
    else                           { uv = P.xy; along = P.z; }
    float layer = floor(along / uSpacing + 0.5);
    vec2 g = uv / uSpacing;
    vec2 cell = floor(g);

    // Grid cells per pixel at this distance: a far point stays a pixel wide
    // and dims to its coverage instead of vanishing or filling the cell.
    float px = dist * uPixelAngle / uSpacing;
    float core = max(uRadius / uSpacing, 0.6 * px);
    float dim = min(1.0, 0.42 / core);
    core = min(core, 0.42);
    float halo = core * 2.0;
    // Far away a pixel spans several cells; thin the points out so a far
    // wall reads as sparse specks, as a scan does, not as noise.
    float occupancy = uDensity * min(1.0, 0.18 / max(px, 1e-4));

    float glow = 0.0;
    for (int j = -1; j <= 1; ++j) {
        for (int i = -1; i <= 1; ++i) {
            vec3 key = vec3(cell + vec2(i, j), layer);
            float h = hash13(key);
            if (h > occupancy) continue;
            vec2 point = cell + vec2(i, j) + hash23(key);
            float d = length(g - point);
            float c = 1.0 - smoothstep(core * 0.45, core, d);
            float soft = 0.18 * exp(-(d * d) / (halo * halo));
            glow = max(glow, (c + soft) * (0.7 + 0.6 * fract(h * 7.31)));
        }
    }
    FragColor = vec4(min(ramp(t) * glow * fade * dim * 1.3, vec3(1.0)), 1.0);
}
"""


class DistanceMixin:
    """The distance-colour pass and the GL objects it owns."""

    def _init_distance(self):
        """Compile the distance program; without it Points and Wireframe draw
        brush edges in the brushes' own colours."""
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
        """'Points' or 'Wireframe' when this frame is coloured by distance."""
        look = config.get('brush_display_mode')
        if (look in ('Points', 'Wireframe') and current_mode == RENDER_MODE_LIT
                and not self._portal_scene_pass):
            return look
        return None

    @timed_pass('distance colour')
    def draw_distance_pass(self, projection, view, camera_pos, points):
        """Colour the depth drawn so far by distance; False if it cannot."""
        if 'distance' not in self.shaders:
            return False
        if not self._capture_scene_depth():
            return False
        width, height = self._water_depth_size
        program, u = self.shaders['distance'], self.uniforms['distance']
        gl.glUseProgram(program)
        self._current_shader = program
        gl.glActiveTexture(gl.GL_TEXTURE0 + self._water_depth_texture_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._water_depth_texture)
        gl.glUniform1i(u['uDepth'], self._water_depth_texture_unit)
        # Held in a name: value_ptr does not keep its matrix alive.
        inverse = glm.inverse(projection * view)
        gl.glUniformMatrix4fv(u['uInvViewProj'], 1, gl.GL_FALSE, glm.value_ptr(inverse))
        gl.glUniform3f(u['uEye'], *self._camera_xyz(camera_pos))
        gl.glUniform1f(u['uPixelAngle'], 2.0 / (float(projection[1][1]) * max(height, 1)))
        gl.glUniform1f(u['uSpacing'], SCAN_SPACING)
        gl.glUniform1f(u['uRadius'], SCAN_POINT_RADIUS)
        gl.glUniform1f(u['uDensity'], SCAN_DENSITY)
        gl.glUniform1f(u['uRange'], SCAN_RANGE)
        gl.glUniform1i(u['uPoints'], 1 if points else 0)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(self.vaos['distance'])
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return True
