"""DeferredRenderer -- a complete Fio renderer that shares nothing with Fio's own.

This is the worked example from the wiki's "Renderer Development Guide". It
implements the :class:`engine.renderer.Renderer` protocol directly: it does
not inherit from, import, or need to understand ``ForwardRenderer`` or
``RendererCore``. Everything it draws comes from the frame input -- the dense
``RenderTable`` and ``EntityTable`` the logic thread publishes.

The technique, in two passes:

1. **Geometry.** Every visible brush is drawn into a G-buffer: three colour
   attachments holding world position, normal and albedo, plus depth.
2. **Lighting.** One full-screen pass reads the G-buffer and accumulates every
   enabled light from the ``EntityTable`` light columns into the host's
   framebuffer.

It is deliberately small: brushes are drawn as their boxes in their flat
colour (no textures, angled brushes as their bounding box), and models,
sprites and effects are left out. Those are additions to this file, not to
Fio.

Resource lifetime -- part of the contract: every GL name this renderer makes
(programs, buffers, vertex arrays, the G-buffer, loaded textures) is deleted in
:meth:`cleanup`. The host drops every name it was handed before calling it,
and a renderer that replaces this one loads its own.
"""

import ctypes
import os

import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader

from engine import render_table
from engine.renderer import RenderStats

#: Lights the lighting pass accumulates per frame.
MAX_LIGHTS = 64
#: Light that reaches every surface regardless of the lights.
AMBIENT = (0.12, 0.12, 0.14)
#: Albedo tint for the editor's selected brush.
SELECTED_TINT = (1.0, 0.85, 0.3)

_GEOMETRY_VS = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
uniform mat4 uProjection;
uniform mat4 uView;
uniform mat4 uModel;
uniform mat3 uNormalMatrix;
out vec3 vWorld;
out vec3 vNormal;
void main() {
    vec4 world = uModel * vec4(aPos, 1.0);
    vWorld = world.xyz;
    vNormal = normalize(uNormalMatrix * aNormal);
    gl_Position = uProjection * uView * world;
}
"""

_GEOMETRY_FS = """#version 330 core
in vec3 vWorld;
in vec3 vNormal;
uniform vec3 uAlbedo;
layout (location = 0) out vec4 gPosition;
layout (location = 1) out vec4 gNormal;
layout (location = 2) out vec4 gAlbedo;
void main() {
    gPosition = vec4(vWorld, 1.0);           // w = 1: something was drawn here
    gNormal = vec4(normalize(vNormal), 0.0);
    gAlbedo = vec4(uAlbedo, 1.0);
}
"""

_LIGHTING_VS = """#version 330 core
out vec2 vUV;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);   // one big triangle
    vUV = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_LIGHTING_FS = """#version 330 core
in vec2 vUV;
out vec4 FragColor;
uniform sampler2D gPosition;
uniform sampler2D gNormal;
uniform sampler2D gAlbedo;
uniform int uLightCount;
uniform vec3 uLightPos[%(max)d];
uniform vec3 uLightColor[%(max)d];
uniform vec2 uLightParams[%(max)d];           // intensity, radius
uniform vec3 uAmbient;
void main() {
    vec4 position = texture(gPosition, vUV);
    if (position.w == 0.0) discard;           // background: keep the host's clear
    vec3 normal = texture(gNormal, vUV).xyz;
    vec3 albedo = texture(gAlbedo, vUV).rgb;
    vec3 light = uAmbient;
    for (int i = 0; i < uLightCount; ++i) {
        vec3 to_light = uLightPos[i] - position.xyz;
        float distance = length(to_light);
        float reach = clamp(1.0 - distance / max(uLightParams[i].y, 1.0), 0.0, 1.0);
        float diffuse = max(dot(normal, to_light / max(distance, 1e-4)), 0.0);
        light += uLightColor[i] * uLightParams[i].x * reach * reach * diffuse;
    }
    FragColor = vec4(albedo * light, 1.0);
}
""" % {"max": MAX_LIGHTS}

#: Unit cube centred on the origin, 36 vertices of (position, normal) -- the
#: shape ``render_table.model_matrices`` scales and places for each brush.
_CUBE = np.array([
    # -Z
    -.5, -.5, -.5, 0, 0, -1,  .5, .5, -.5, 0, 0, -1,  .5, -.5, -.5, 0, 0, -1,
    .5, .5, -.5, 0, 0, -1,  -.5, -.5, -.5, 0, 0, -1,  -.5, .5, -.5, 0, 0, -1,
    # +Z
    -.5, -.5, .5, 0, 0, 1,  .5, -.5, .5, 0, 0, 1,  .5, .5, .5, 0, 0, 1,
    .5, .5, .5, 0, 0, 1,  -.5, .5, .5, 0, 0, 1,  -.5, -.5, .5, 0, 0, 1,
    # -X
    -.5, .5, .5, -1, 0, 0,  -.5, .5, -.5, -1, 0, 0,  -.5, -.5, -.5, -1, 0, 0,
    -.5, -.5, -.5, -1, 0, 0,  -.5, -.5, .5, -1, 0, 0,  -.5, .5, .5, -1, 0, 0,
    # +X
    .5, .5, .5, 1, 0, 0,  .5, -.5, -.5, 1, 0, 0,  .5, .5, -.5, 1, 0, 0,
    .5, -.5, -.5, 1, 0, 0,  .5, .5, .5, 1, 0, 0,  .5, -.5, .5, 1, 0, 0,
    # -Y
    -.5, -.5, -.5, 0, -1, 0,  .5, -.5, -.5, 0, -1, 0,  .5, -.5, .5, 0, -1, 0,
    .5, -.5, .5, 0, -1, 0,  -.5, -.5, .5, 0, -1, 0,  -.5, -.5, -.5, 0, -1, 0,
    # +Y
    -.5, .5, -.5, 0, 1, 0,  .5, .5, .5, 0, 1, 0,  .5, .5, -.5, 0, 1, 0,
    .5, .5, .5, 0, 1, 0,  -.5, .5, -.5, 0, 1, 0,  -.5, .5, .5, 0, 1, 0,
], dtype=np.float32)

#: Brushes that are volumes rather than surfaces: not drawn.
_NOT_SURFACES = render_table.CLASS_TRIGGER | render_table.CLASS_FOG


def _program(vertex, fragment):
    return int(compileProgram(compileShader(vertex, gl.GL_VERTEX_SHADER),
                              compileShader(fragment, gl.GL_FRAGMENT_SHADER),
                              validate=False))


class DeferredRenderer:
    """A deferred renderer for Fio, registered as ``"Deferred"``.

    Created by the host as ``DeferredRenderer(config)`` with its GL context
    current; *config* is the application ConfigParser (or None).
    """

    def __init__(self, config=None):
        # -- the settings the host sets; renderer-independent -------------------
        self.view_distance = None
        self.shadows_enabled = True          # this example casts no shadows
        self.water_quality = 'expensive'     # nor distinguishes water cost
        self.render_stats = RenderStats()
        # Shown by Debug Tables under the frame counters.
        self.render_stats.details['G-buffer'] = self._describe_gbuffer

        # -- GL resources, every one deleted in cleanup() ----------------------
        self._textures = {}                  # (subfolder, name) -> GL name
        self._sprite_textures = {}
        self._gbuffer = None                 # (fbo, [position, normal, albedo], depth)
        self._gbuffer_size = (0, 0)
        self._ready = False
        try:
            self._geometry = _program(_GEOMETRY_VS, _GEOMETRY_FS)
            self._lighting = _program(_LIGHTING_VS, _LIGHTING_FS)
        except Exception as exc:              # a driver that refuses the shaders
            print(f"[DeferredRenderer] shaders failed: {exc}")
            self._geometry = self._lighting = 0
            return
        self._cube_vao = int(gl.glGenVertexArrays(1))
        self._cube_vbo = int(gl.glGenBuffers(1))
        gl.glBindVertexArray(self._cube_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._cube_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, _CUBE.nbytes, _CUBE, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 24, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 24, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        # A core-profile draw needs a bound VAO even when it reads no attribute.
        self._fullscreen_vao = int(gl.glGenVertexArrays(1))
        gl.glBindVertexArray(0)
        self._ready = True

    # -- lifecycle -----------------------------------------------------------------

    @property
    def ready(self):
        return self._ready

    def cleanup(self):
        """Delete every GL name this renderer made. It is not used again."""
        self._delete_gbuffer()
        if self._textures:
            names = [t for t in self._textures.values() if t]
            if names:
                gl.glDeleteTextures(names)
            self._textures.clear()
        self._sprite_textures = {}           # the host's table of our names
        for attr in ('_cube_vao', '_fullscreen_vao'):
            if getattr(self, attr, 0):
                gl.glDeleteVertexArrays(1, [getattr(self, attr)])
                setattr(self, attr, 0)
        if getattr(self, '_cube_vbo', 0):
            gl.glDeleteBuffers(1, [self._cube_vbo])
            self._cube_vbo = 0
        for attr in ('_geometry', '_lighting'):
            if getattr(self, attr, 0):
                gl.glDeleteProgram(getattr(self, attr))
                setattr(self, attr, 0)
        self._ready = False

    # -- the frame -------------------------------------------------------------------

    def render_scene(self, projection, view, camera_pos, primary_selection,
                     config, clear=True, brush_slots=None):
        stats = self.render_stats
        stats.reset()
        table = config['render_table']
        slots = np.asarray(brush_slots if brush_slots is not None else (), dtype=np.int32)
        stats.total_brushes = len(config.get('all_brush_slots', ()))

        # Where the host wants the picture: its framebuffer (not necessarily
        # 0 -- Qt renders into its own) and its viewport (split-screen views
        # share one framebuffer).
        target = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        x, y, width, height = (int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT))
        if clear:
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        if width <= 0 or height <= 0:
            return
        self._ensure_gbuffer(width, height)

        # 1. Geometry pass: brushes into the G-buffer.
        fbo = self._gbuffer[0]
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glUseProgram(self._geometry)
        uniform = lambda name: gl.glGetUniformLocation(self._geometry, name)  # noqa: E731
        gl.glUniformMatrix4fv(uniform('uProjection'), 1, gl.GL_FALSE, np.asarray(projection, np.float32))
        gl.glUniformMatrix4fv(uniform('uView'), 1, gl.GL_FALSE, np.asarray(view, np.float32))
        model_loc, normal_loc, albedo_loc = (
            uniform('uModel'), uniform('uNormalMatrix'), uniform('uAlbedo'))

        surfaces = slots[(table.class_bits[slots] & _NOT_SURFACES) == 0] if len(slots) else slots
        models, normals = render_table.model_matrices(table, surfaces)
        selected = (table.slot_of_id.get(primary_selection.brush_id)
                    if primary_selection is not None and primary_selection.brush_id else None)
        gl.glBindVertexArray(self._cube_vao)
        for row, slot in enumerate(surfaces):
            colour = SELECTED_TINT if slot == selected else table.colour[slot]
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[row])
            gl.glUniformMatrix3fv(normal_loc, 1, gl.GL_FALSE, normals[row])
            gl.glUniform3f(albedo_loc, *(float(c) for c in colour))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            stats.draw_calls += 1
        stats.visible_brushes = len(surfaces)
        stats.visible_tris = 12 * len(surfaces)

        # 2. Lighting pass: the G-buffer, lit, into the host's framebuffer.
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glViewport(x, y, width, height)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(self._lighting)
        uniform = lambda name: gl.glGetUniformLocation(self._lighting, name)  # noqa: E731
        for unit, (name, texture) in enumerate(zip(
                ('gPosition', 'gNormal', 'gAlbedo'), self._gbuffer[1])):
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glUniform1i(uniform(name), unit)
        positions, colours, params = self._lights(config)
        gl.glUniform1i(uniform('uLightCount'), len(positions))
        if len(positions):
            gl.glUniform3fv(uniform('uLightPos'), len(positions), positions)
            gl.glUniform3fv(uniform('uLightColor'), len(colours), colours)
            gl.glUniform2fv(uniform('uLightParams'), len(params), params)
        gl.glUniform3f(uniform('uAmbient'), *AMBIENT)
        gl.glBindVertexArray(self._fullscreen_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        stats.draw_calls += 1
        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(0)

    @staticmethod
    def _lights(config):
        """Enabled lights from the EntityTable's light columns, as float32 arrays."""
        entities = config['entity_table']
        slots = entities.light_slots
        if len(slots):
            keep = entities.light_enabled[slots]
            hidden = config.get('thing_hidden')
            if config.get('play_mode') and hidden is not None:
                keep = keep & ~np.asarray(hidden)[slots]   # out of the running world
            slots = slots[keep][:MAX_LIGHTS]
        return (np.ascontiguousarray(entities.pos[slots], dtype=np.float32),
                np.ascontiguousarray(entities.light_color[slots], dtype=np.float32),
                np.ascontiguousarray(entities.light_params[slots], dtype=np.float32))

    def set_grid(self, world_size, grid_size):
        pass                                  # this example draws no editor grid

    # -- resources -------------------------------------------------------------------

    def load_texture(self, texture_name, subfolder):
        """Load ``assets/<subfolder>/<texture_name>``; its GL name, or 0."""
        key = (subfolder, texture_name)
        if key not in self._textures:
            self._textures[key] = self._upload_image(
                os.path.join('assets', subfolder, texture_name))
        return self._textures[key]

    @staticmethod
    def _upload_image(path):
        from PyQt5.QtGui import QImage
        image = QImage(path)
        if image.isNull():
            return 0
        image = image.convertToFormat(QImage.Format_RGBA8888)
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, image.width(), image.height(),
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, bytes(bits))
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return texture

    def set_sprite_textures(self, textures):
        self._sprite_textures = dict(textures)

    def get_loaded_model(self, filename):
        return None                           # loads no models; the 2D view draws a box

    # -- the host's post-scene drawing: this example draws none of it --------------

    def draw_billboards(self, projection, view, positions, size, tex_id):
        return 0

    def draw_player_glasses(self, projection, view, positions,
                            width=40.0, height=18.0, lift=0.0, sprites=()):
        pass

    def draw_bullet_marks(self, projection, view, marks):
        pass

    def draw_connection_lines(self, projection, view, connections):
        pass

    def draw_face_highlight(self, projection, view, brush, face_name):
        pass

    def draw_component_overlay(self, projection, view, overlay, version=None):
        pass

    def draw_collision_visualization(self, projection, view, brushes):
        pass

    # -- the G-buffer ----------------------------------------------------------------

    def _ensure_gbuffer(self, width, height):
        if self._gbuffer is not None and self._gbuffer_size == (width, height):
            return
        self._delete_gbuffer()
        fbo = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        textures = []
        for attachment, internal in enumerate((gl.GL_RGBA32F, gl.GL_RGBA16F, gl.GL_RGBA8)):
            texture = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, width, height, 0,
                            gl.GL_RGBA, gl.GL_FLOAT, None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0 + attachment,
                                      gl.GL_TEXTURE_2D, texture, 0)
            textures.append(texture)
        depth = int(gl.glGenRenderbuffers(1))
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH_COMPONENT24, width, height)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                     gl.GL_RENDERBUFFER, depth)
        gl.glDrawBuffers(3, [gl.GL_COLOR_ATTACHMENT0 + i for i in range(3)])
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        self._gbuffer = (fbo, textures, depth)
        self._gbuffer_size = (width, height)

    def _delete_gbuffer(self):
        if self._gbuffer is None:
            return
        fbo, textures, depth = self._gbuffer
        gl.glDeleteFramebuffers(1, [fbo])
        gl.glDeleteTextures(textures)
        gl.glDeleteRenderbuffers(1, [depth])
        self._gbuffer = None
        self._gbuffer_size = (0, 0)

    def _describe_gbuffer(self):
        width, height = self._gbuffer_size
        return (f"{width}x{height}: position RGBA32F, normal RGBA16F, albedo RGBA8"
                if self._gbuffer else "not created")
