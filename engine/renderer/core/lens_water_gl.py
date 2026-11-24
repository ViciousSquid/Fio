"""Water on the lens: the drops of :mod:`engine.lens_water`, drawn.

One pass over the finished frame. Each drop is a little lens with no colour
of its own: the picture behind it is seen through it upside-down, mirrored
and much smaller (nearly the whole view fits in a drop), blurred because it
is out of focus, darker at its top and lit underneath (the bright sky above
the camera, turned over by the drop), with a faint glint. No outline: a drop
fades out from its middle.

The frame is copied (resolving any multisampling) into a texture the size of
the view, then one full-screen triangle draws it back through the drops.
Nothing is drawn, and nothing allocated, while the glass is dry.

MiniWind's camera-lens water pass (game/ui/magic_gl.py), unchanged in what
it draws.
"""

from __future__ import annotations

import numpy as np

from ...lens_water import MAX_DROPS

#: Share of a drop's radius its edge fades over.
WET_SOFT = 0.65
#: How much of the view a drop's middle replaces.
WET_OPACITY = 0.9
#: A drop is drawn this much bigger than its wet core (the soft fringe).
WET_SIZE = 1.7
#: Blur of the view through a drop (share of the view's height).
WET_BLUR = 0.03

_VERT = """#version 330 core
out vec2 uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}"""

_FRAG = """#version 330 core
in vec2 uv;
uniform sampler2D scene;
uniform vec4 drops[%(max)d];      // xy centre (uv, y up), z radius (uv height), w wetness
uniform float stretch[%(max)d];   // 1 round .. longer as it runs
uniform int count;
uniform float aspect;
uniform float soft;          // out of focus: share of the radius the edge fades over
uniform float opacity;       // how much of the view a drop's middle replaces
uniform float size;          // drawn this much bigger than its wet core
uniform float blur;          // and how blurred the view through it
out vec4 frag;
// A disc of taps for the defocus blur (golden-angle spiral).
const vec2 TAPS[12] = vec2[](
    vec2(0.20, 0.00), vec2(-0.27, 0.25), vec2(0.04, -0.44), vec2(0.35, 0.39),
    vec2(-0.58, -0.10), vec2(0.55, -0.35), vec2(-0.20, 0.68), vec2(-0.37, -0.65),
    vec2(0.80, 0.18), vec2(-0.78, 0.38), vec2(0.30, -0.85), vec2(0.38, 0.89));
void main() {
    vec2 at = uv;
    float shade = 1.0;
    float shine = 0.0;
    float cover = 0.0;
    for (int i = 0; i < %(max)d; i++) {
        if (i >= count) break;
        vec2 c = drops[i].xy;
        float R = max(drops[i].z, 1e-4) * size;
        float s = stretch[i];
        vec2 d = uv - c;
        d.x *= aspect;
        // Long while it runs: the body trails up behind its rounded front.
        d.y = (d.y - R * (s - 1.0) * 0.5) / s;
        // Egg-shaped: narrower above, wider at the bottom (more so running).
        float k = clamp(d.y / R, -1.0, 1.0);
        d.x *= 1.0 + (0.16 + 0.5 * (s - 1.0)) * k;
        float r = length(d) / R;
        if (r >= 1.0 + soft * 0.3) continue;
        // No outline: it fades out from its middle.
        float a = drops[i].w * opacity * (1.0 - smoothstep(1.0 - soft, 1.0 + soft * 0.3, r));
        float rr = min(r, 1.0);
        float h = sqrt(1.0 - rr * rr);     // the drop's dome
        // Through the drop the world is seen turned over and mirrored, and
        // much smaller: across a drop, most of the view.
        vec2 look = -(d / R) * (0.30 - 0.12 * h);
        look.x /= aspect;
        at = mix(at, c + look, a);
        cover = max(cover, a);
        // No colour of its own, but the bright sky above the camera, turned
        // over by the drop, lights its lower part and leaves its top a
        // little darker, with a faint blurred glint.
        float below = clamp(-d.y / R, -1.0, 1.0);
        shade *= mix(1.0, 1.0 - 0.32 * smoothstep(0.0, 1.0, -below), a);
        shine += a * (0.08 + 0.30 * smoothstep(-0.3, 0.9, below));
        vec2 hl = d / R - vec2(-0.30, 0.42);
        shine += a * 0.12 * exp(-dot(hl, hl) * 8.0);
    }
    vec3 sharp = texture(scene, uv).rgb;
    if (cover <= 0.0) {
        frag = vec4(sharp, 1.0);
        return;
    }
    // Out of focus: the view through the drops, blurred.
    vec3 seen = texture(scene, clamp(at, vec2(0.001), vec2(0.999))).rgb;
    for (int t = 0; t < 12; t++) {
        vec2 o = TAPS[t] * vec2(blur / aspect, blur);
        seen += texture(scene, clamp(at + o, vec2(0.001), vec2(0.999))).rgb;
    }
    seen /= 13.0;
    frag = vec4(mix(sharp, seen * shade + vec3(shine), cover), 1.0);
}""" % {'max': MAX_DROPS}


class LensWaterPass:
    """The pass's GL resources, made on first use in the current context."""

    def __init__(self):
        self.ok = None              # None: not tried; False: failed (off)
        self.program = 0
        self.locations = {}
        self.vao = 0
        self.texture = 0
        self.fbo = 0
        self.size = (0, 0)
        self._drops = np.zeros((MAX_DROPS, 4), dtype=np.float32)
        self._stretch = np.ones(MAX_DROPS, dtype=np.float32)

    def _init(self, gl):
        if self.ok is not None:
            return self.ok
        try:
            from OpenGL.GL import shaders
            self.program = shaders.compileProgram(
                shaders.compileShader(_VERT, gl.GL_VERTEX_SHADER),
                shaders.compileShader(_FRAG, gl.GL_FRAGMENT_SHADER),
                validate=False)
            self.locations = {name: gl.glGetUniformLocation(self.program, name)
                              for name in ("scene", "drops", "stretch", "count",
                                           "aspect", "soft", "opacity", "size", "blur")}
            self.vao = gl.glGenVertexArrays(1)
            self.ok = True
        except Exception as exc:                          # pragma: no cover
            print(f"[LensWater] the lens pass could not be made, so it is off: {exc}")
            self.ok = False
        return self.ok

    def _grab(self, gl, x0, y0, w, h):
        """Copy the finished frame (resolving any multisampling) into the
        pass's texture, sized to the view."""
        from engine.renderer.core import gl_state
        prev_draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        prev_read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        if self.size != (w, h):
            self._free_target(gl)
            self.texture = gl.glGenTextures(1)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0, gl.GL_RGBA,
                            gl.GL_UNSIGNED_BYTE, None)
            for param, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
            self.fbo = gl.glGenFramebuffers(1)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self.texture, 0)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, prev_draw)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, prev_read)
            self.size = (w, h)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, prev_draw)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.fbo)
        gl.glBlitFramebuffer(x0, y0, x0 + w, y0 + h, 0, 0, w, h,
                             gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, prev_draw)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, prev_read)

    def draw(self, drops, viewport=None) -> int:
        """Draw *drops* ``[(x, y, radius, wetness[, stretch])]`` (screen
        fractions, y down; :meth:`engine.lens_water.LensWater.frame`) over
        the finished frame in *viewport* ``(x, y, w, h)`` (default: the
        current one). Returns how many were drawn. GL state is put back."""
        from engine.renderer.core import gl_state
        if not drops:
            return 0
        import OpenGL.GL as gl
        if not self._init(gl):
            return 0
        try:
            if viewport is None:
                viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
            x0, y0, w, h = (int(v) for v in viewport)
            if w <= 0 or h <= 0:
                return 0
            prev_viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
            self._grab(gl, x0, y0, w, h)
            prev_program = int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
            prev_texture_unit = int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE))
            depth = gl.glIsEnabled(gl.GL_DEPTH_TEST)
            blend = gl.glIsEnabled(gl.GL_BLEND)
            cull = gl.glIsEnabled(gl.GL_CULL_FACE)
            gl_state.glDisable(gl.GL_DEPTH_TEST)
            gl_state.glDisable(gl.GL_BLEND)
            gl_state.glDisable(gl.GL_CULL_FACE)
            gl.glViewport(x0, y0, w, h)
            gl_state.glUseProgram(self.program)
            gl_state.glActiveTexture(gl.GL_TEXTURE0)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
            loc = self.locations
            n = min(MAX_DROPS, len(drops))
            data, shape = self._drops, self._stretch
            data.fill(0.0)
            shape.fill(1.0)
            for i, drop in enumerate(drops[:n]):
                x, y, r, wet = drop[:4]
                data[i] = (x, 1.0 - y, r, wet)          # uv has y up
                if len(drop) > 4:
                    shape[i] = drop[4]
            gl_state.glUniform1i(loc["scene"], 0)
            gl_state.glUniform4fv(loc["drops"], MAX_DROPS, data)
            gl_state.glUniform1fv(loc["stretch"], MAX_DROPS, shape)
            gl_state.glUniform1f(loc["soft"], WET_SOFT)
            gl_state.glUniform1f(loc["opacity"], WET_OPACITY)
            gl_state.glUniform1f(loc["size"], WET_SIZE)
            gl_state.glUniform1f(loc["blur"], WET_BLUR)
            gl_state.glUniform1i(loc["count"], n)
            gl_state.glUniform1f(loc["aspect"], w / float(h))
            gl_state.glBindVertexArray(self.vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            gl_state.glBindVertexArray(0)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl_state.glActiveTexture(prev_texture_unit)
            gl_state.glUseProgram(prev_program)
            gl.glViewport(*(int(v) for v in prev_viewport))
            (gl.glEnable if depth else gl.glDisable)(gl.GL_DEPTH_TEST)
            (gl.glEnable if blend else gl.glDisable)(gl.GL_BLEND)
            (gl.glEnable if cull else gl.glDisable)(gl.GL_CULL_FACE)
            return n
        except Exception as exc:                          # pragma: no cover
            print(f"[LensWater] the lens pass failed, so it is off: {exc}")
            self.ok = False
            return 0

    def _free_target(self, gl):
        from engine.renderer.core import gl_state
        if self.fbo:
            gl.glDeleteFramebuffers(1, [self.fbo])
            self.fbo = 0
        if self.texture:
            gl_state.glDeleteTextures([self.texture])
            self.texture = 0
        self.size = (0, 0)

    def cleanup(self):
        """Release the pass's GL objects (with its context current)."""
        from engine.renderer.core import gl_state
        try:
            import OpenGL.GL as gl
            self._free_target(gl)
            if self.vao:
                gl_state.glDeleteVertexArrays(1, [self.vao])
            if self.program:
                gl_state.glDeleteProgram(self.program)
        except Exception:
            pass
        self.__init__()
