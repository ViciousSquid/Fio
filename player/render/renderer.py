"""OpenGL ES 3 world renderer with ZIP-backed textures and touch overlay.

Only the passes actually used by the player are compiled. CPU scene data is
retained so buffers and textures can be rebuilt after Android context loss.
"""

from __future__ import annotations

from typing import Dict

from .gles_context import compile_program
from .overlay import OverlayRenderer
from . import glmath


class GLESRenderer:
    """Owns GL programs and draws each frame."""

    def __init__(self, *, gles=True):
        self.gles = gles
        self.programs: Dict[str, int] = {}
        self.width = 1
        self.height = 1
        self._triangle_vao = None
        self._triangle_vbo = None
        self._scene = None
        self._start_time = 0.0
        self._batches = []
        self._textures = {}
        self._lights = []
        self._embedded = {}
        self._draw_sources = {}
        self._tints = {}
        self._terrain = None
        self._context_lost = False
        self._ui_key = None
        self._ui_texture = self._ui_vao = self._ui_vbo = None
        self.overlay = OverlayRenderer()

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------
    def on_gl_ready(self) -> None:
        """(Re)build all GL resources. Safe to call again after context loss."""
        import time

        from ..gles_shaders import build_gles_shader_set
        from . import world_shaders

        gl = _gl()
        self._context_lost = True
        self._ui_key = None
        self._ui_texture = self._ui_vao = self._ui_vbo = None
        self._start_time = time.perf_counter()

        sources = build_gles_shader_set(prefer_arm=True)
        if not self.gles:
            from engine.shaders import DEFAULT_SHADERS
            sources = DEFAULT_SHADERS
        from ..gles_shaders import for_desktop
        world_vertex = world_shaders.VERTEX if self.gles else for_desktop(world_shaders.VERTEX)
        world_fragment = world_shaders.FRAGMENT if self.gles else for_desktop(world_shaders.FRAGMENT)
        # Unused desktop passes can exceed mobile sampler limits.
        self.programs = {
            "simple": compile_program(sources["simple.vert"], sources["simple.frag"]),
            "world": compile_program(world_vertex, world_fragment),
        }
        # Object names from a lost context must never be reused or deleted.
        self._batches = []
        self._textures = {}
        self._tints = {}
        print(f"[GLES] compiled {len(self.programs)} shader programs: "
              f"{sorted(self.programs)}")

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.06, 0.07, 0.09, 1.0)
        self._build_reference_triangle()
        self.overlay.on_gl_ready(self.programs)

        if self._scene is not None:
            self._upload_scene()
        self._context_lost = False

    def resize(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        _gl().glViewport(0, 0, self.width, self.height)

    # ------------------------------------------------------------------
    # Reference triangle (milestone 1)
    # ------------------------------------------------------------------
    def _build_reference_triangle(self) -> None:
        import ctypes

        import numpy as np

        gl = _gl()
        verts = np.array(
            [0.0, 0.5, 0.0, -0.5, -0.5, 0.0, 0.5, -0.5, 0.0],
            dtype=np.float32,
        )
        self._triangle_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self._triangle_vao)
        self._triangle_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._triangle_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    def _draw_reference_triangle(self) -> None:
        import time

        gl = _gl()
        prog = self.programs.get("simple")
        if prog is None or self._triangle_vao is None:
            return
        gl.glUseProgram(prog)

        t = time.perf_counter() - self._start_time
        aspect = self.width / max(1, self.height)
        model = glmath.rotate_z(t)
        view = glmath.identity()
        proj = glmath.ortho(-aspect, aspect, -1.0, 1.0, -1.0, 1.0)

        _set_mat4(gl, prog, "model", model)
        _set_mat4(gl, prog, "view", view)
        _set_mat4(gl, prog, "projection", proj)
        _set_vec3(gl, prog, "color", (0.20, 0.85, 0.70))
        _set_float(gl, prog, "alpha", 1.0)

        gl.glBindVertexArray(self._triangle_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Per-frame
    # ------------------------------------------------------------------
    def render(self, render_state=None) -> None:
        gl = _gl()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        if self._scene is not None:
            self.render_scene(render_state)
        else:
            self._draw_reference_triangle()

    def render_overlay(self, controls, inp, w: int, h: int) -> None:
        """Draw the on-screen touch controls over the scene (call last)."""
        self.overlay.render(controls, inp, w, h)

    # ------------------------------------------------------------------
    # World geometry
    # ------------------------------------------------------------------
    def load_scene(self, map_data: dict, package=None, world=None) -> None:
        """Build brush batches and upload their ZIP-backed textures."""
        if self._terrain and (world is None or self._terrain is not world.terrain):
            self._release_terrain()
        self._scene = {"map": map_data, "package": package, "world": world}
        self._upload_scene()

    def _release_terrain(self):
        if self._terrain:
            gl = _gl()
            program = self._terrain.shader_program
            self._terrain.cleanup()
            if program:
                gl.glDeleteProgram(program)
            self._terrain.release_renderer_resources()
            self._terrain = None

    def clear_scene(self):
        gl = _gl()
        self._release_terrain()
        for vao, vbo, _, _ in self._batches:
            gl.glDeleteVertexArrays(1, [vao])
            gl.glDeleteBuffers(1, [vbo])
        for texture in self._textures.values():
            gl.glDeleteTextures([texture])
        self._batches, self._textures, self._draw_sources, self._tints = [], {}, {}, {}
        self._scene = None

    def close(self):
        self.clear_scene()
        gl = _gl()
        for vao in (self._triangle_vao, self._ui_vao, self.overlay._disc_vao):
            if vao:
                gl.glDeleteVertexArrays(1, [vao])
        for vbo in (self._triangle_vbo, self._ui_vbo, self.overlay._disc_vbo):
            if vbo:
                gl.glDeleteBuffers(1, [vbo])
        if self._ui_texture:
            gl.glDeleteTextures([self._ui_texture])
        for program in self.programs.values():
            gl.glDeleteProgram(program)
        self.programs.clear()

    def _add_batches(self, batches, source=None):
        import ctypes
        gl = _gl()
        for name, vertices in batches.items():
            tint = (1, 1, 1, 1)
            if isinstance(name, tuple):
                name, tint = name
            vao = gl.glGenVertexArrays(1)
            vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
            for attribute, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
                gl.glVertexAttribPointer(attribute, size, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(offset))
                gl.glEnableVertexAttribArray(attribute)
            self._batches.append((vao, vbo, self._load_texture(name), vertices.size // 8))
            self._tints[vao] = tint
            if source:
                self._draw_sources[vao] = source

    def _upload_scene(self):
        from .scene import build_textured_batches, extract_lights
        from engine.change_journal import JOURNAL
        gl = _gl()
        for vao, vbo, texture, count in self._batches:
            gl.glDeleteVertexArrays(1, [vao])
            gl.glDeleteBuffers(1, [vbo])
        for texture in self._textures.values():
            gl.glDeleteTextures([texture])
        self._batches, self._textures, self._draw_sources, self._embedded = [], {}, {}, {}
        self._tints = {}
        world = self._scene.get('world')
        data = world.render_map() if world else self._scene['map']
        self._lights = extract_lights(data)
        static = []
        for brush in data.get('brushes', []):
            if brush.get('is_mover') or brush.get('is_door'):
                self._add_batches(build_textured_batches({'brushes': [brush]}),
                                  ('brush', brush, tuple(brush['pos'])))
            else:
                static.append(brush)
        self._add_batches(build_textured_batches({'brushes': static}))
        if world:
            from .models import batches as model_batches
            from engine.prop_entity import Prop
            for thing in world.state.things:
                if isinstance(thing, Prop) and thing.properties.get('render_mode') == 'model':
                    groups, embedded = model_batches(thing.properties['model_path'], self._scene['package'])
                    self._embedded.update(embedded)
                    self._add_batches(groups, ('model', thing, None))
                elif hasattr(thing, 'get_sprite_path'):
                    import numpy as np
                    quad = np.asarray([[x, y, 0, 0, 0, 1, x + .5, y + .5]
                                       for x, y in ((-.5, -.5), (.5, -.5), (.5, .5), (-.5, -.5), (.5, .5), (-.5, .5))], dtype=np.float32).reshape(-1)
                    self._add_batches({'': quad}, ('sprite', thing, None))
            if world.terrain:
                self._prepare_terrain(world)
            self._entity_ids = tuple(id(t) for t in world.state.things)
        JOURNAL.subscribe(self)
        JOURNAL.drain(self)
        gl.glBindVertexArray(0)

    def _prepare_terrain(self, world):
        from engine.terrain import Terrain
        from ..gles_shaders import translate, for_desktop
        # Recreate GPU ownership from retained terrain data after context loss.
        if self._terrain is world.terrain and not self._context_lost:
            self._terrain.load_terrain_textures(type('Textures', (), {'get': lambda _, name: self._load_texture(name)})())
            return
        if self._terrain is world.terrain:
            terrain = Terrain(defer_gl=True)
            terrain.from_dict(world.terrain.to_dict())
            world.terrain = world.logic.world_runtime.terrain = terrain
        self._terrain = world.terrain
        def compiler(vertex, fragment):
            if self.gles:
                vertex, fragment = translate(vertex, 'vertex'), translate(fragment, 'fragment')
            else:
                vertex, fragment = for_desktop(vertex), for_desktop(fragment)
            return compile_program(vertex, fragment)
        self._terrain.shader_compiler = compiler
        self._terrain.load_terrain_textures(type('Textures', (), {'get': lambda _, name: self._load_texture(name)})())
        self._terrain._init_shader()
        if not self._terrain.shader_program:
            raise RuntimeError('Terrain shaders could not be compiled')

    def _load_texture(self, name):
        import io
        from PIL import Image

        gl = _gl()
        if name in self._textures:
            return self._textures[name]
        image = Image.new("RGBA", (1, 1), (210, 210, 210, 255))
        package = self._scene.get("package")
        raw = self._embedded.get(name) or (package.read_asset(name) if package and name else None)
        if raw:
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                image = source.convert("RGBA")
        limit = min(2048, int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_SIZE)))
        image.thumbnail((limit, limit))
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        texture = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, image.width, image.height,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, image.tobytes())
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        self._textures[name] = texture
        return texture

    def render_scene(self, render_state) -> None:
        import math
        import numpy as np

        state = render_state or {}
        world = self._scene.get('world')
        if world:
            from engine.change_journal import JOURNAL, OVERFLOW, STATE
            changes = JOURNAL.drain(self)
            brush_ids = {id(b) for b in world.brushes if not (b.get('is_mover') or b.get('is_door'))}
            if changes is OVERFLOW or any(oid in brush_ids and flags & STATE for oid, flags in changes.items()) or tuple(id(t) for t in world.state.things) != self._entity_ids:
                self._upload_scene()
            from .scene import extract_lights
            self._lights = extract_lights(world.render_map())
        gl = _gl()
        prog = self.programs["world"]
        gl.glUseProgram(prog)
        gl.glUniform1i(gl.glGetUniformLocation(prog, 'unlit'), 0)
        eye = np.asarray(state.get("cam_pos", (0, 64, 0)), dtype=np.float32)
        front = glmath.front_from_angles(state.get("cam_yaw", -90), state.get("cam_pitch", 0))
        _set_mat4(gl, prog, "view", state.get("view", glmath.look_at(eye, eye + front)))
        _set_mat4(gl, prog, "projection", glmath.perspective(
            math.radians(state.get("fov", 70)), self.width / self.height, 0.5, 100000.0))
        gl.glUniform1i(gl.glGetUniformLocation(prog, "lightCount"), len(self._lights))
        for i, light in enumerate(self._lights):
            _set_vec3(gl, prog, f"lightPos[{i}]", light["pos"])
            _set_vec3(gl, prog, f"lightColor[{i}]", light["color"])
            _set_float(gl, prog, f"lightRadius[{i}]", light["radius"])
            _set_float(gl, prog, f"lightIntensity[{i}]", light["intensity"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(gl.glGetUniformLocation(prog, "diffuse"), 0)
        for vao, vbo, texture, count in self._batches:
            source = self._draw_sources.get(vao)
            model = glmath.identity()
            if source:
                kind, item, origin = source
                if kind == 'brush':
                    if item.get('hidden'):
                        continue
                    model = glmath.translate(*(item['pos'][i] - origin[i] for i in range(3)))
                else:
                    if item.properties.get('hidden') or getattr(item, 'collected', False):
                        continue
                    if kind == 'sprite':
                        item._asset_source = self._scene.get('package')
                        texture = self._load_texture(item.get_sprite_path())
                        width, height = item.properties.get('sprite_size', (
                            item.properties.get('sprite_width', 64), item.properties.get('sprite_height', 64)))
                        view = state.get('view', glmath.look_at(eye, eye + front))
                        model[:3, 0] = view[0, :3] * width
                        model[:3, 1] = view[1, :3] * height
                        model[:3, 2] = np.cross(view[0, :3], view[1, :3])
                        model[:3, 3] = item.pos
                    else:
                        model = glmath.entity_matrix(item.pos, item.properties)
            _set_mat4(gl, prog, 'model', model)
            gl.glUniform4f(gl.glGetUniformLocation(prog, 'tint'), *self._tints[vao])
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glBindVertexArray(vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, count)
        gl.glBindVertexArray(0)
        if world and world.terrain:
            import glm
            view = state.get('view', glmath.look_at(eye, eye + front))
            projection = glmath.perspective(math.radians(state.get('fov', 70)), self.width / self.height, .5, 100000.)
            world.terrain.update_and_render(glm.mat4(*projection.T.flatten()), glm.mat4(*view.T.flatten()), glm.vec3(*eye))

    def render_ui(self, state, width, height, has_world=True):
        import ctypes
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from ..menu import buttons

        gl = _gl()
        key = (state.get('hud', ''), state.get('paused', False), width, height, has_world, state.get('control_buttons', ()))
        if key != self._ui_key:
            self._ui_key = key
            image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=max(14, min(28, height // 28)))
            if state.get('paused'):
                draw.rectangle((0, 0, width, height), fill=(10, 14, 22, 215))
                draw.text((width / 2, height * .20), 'Fio Player — Paused' if has_world else 'Fio Player',
                          font=font, anchor='mm', fill='white')
                for _, label, (x, y, w, h) in buttons(width, height, has_world):
                    draw.rounded_rectangle((x, y, x + w, y + h), radius=8,
                                           fill=(45, 65, 90, 255), outline=(140, 160, 180, 255), width=2)
                    draw.text((x + w / 2, y + h / 2), label, font=font, anchor='mm', fill='white')
            else:
                draw.multiline_text((18, 14), state.get('hud', ''), font=font, fill='white',
                                    stroke_width=1, stroke_fill=(0, 0, 0, 240))
                for action, x, y in state.get('control_buttons', ()):
                    draw.text((x * width, y * height), action.title(), font=font, anchor='mm',
                              fill='white', stroke_width=1, stroke_fill='black')
            if self._ui_texture:
                gl.glDeleteTextures([self._ui_texture])
            self._ui_texture = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._ui_texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, width, height, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, image.tobytes())
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        if self._ui_vao is None:
            self._ui_vao = gl.glGenVertexArrays(1)
            self._ui_vbo = gl.glGenBuffers(1)
            vertices = np.array([[x, y, 0, 0, 0, 1, x, y]
                                 for x, y in ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1))], dtype=np.float32)
            gl.glBindVertexArray(self._ui_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._ui_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
            for attribute, count, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
                gl.glVertexAttribPointer(attribute, count, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(offset))
                gl.glEnableVertexAttribArray(attribute)
        prog = self.programs['world']
        gl.glUseProgram(prog)
        _set_mat4(gl, prog, 'model', glmath.scale(width, height, 1))
        _set_mat4(gl, prog, 'view', glmath.identity())
        _set_mat4(gl, prog, 'projection', glmath.ortho(0, width, height, 0))
        gl.glUniform1i(gl.glGetUniformLocation(prog, 'unlit'), 1)
        gl.glUniform4f(gl.glGetUniformLocation(prog, 'tint'), 1, 1, 1, 1)
        gl.glUniform1i(gl.glGetUniformLocation(prog, 'lightCount'), 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._ui_texture)
        gl.glUniform1i(gl.glGetUniformLocation(prog, 'diffuse'), 0)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glBindVertexArray(self._ui_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
        gl.glBindVertexArray(0)
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_DEPTH_TEST)


# ----------------------------------------------------------------------
# Small uniform helpers (kept local so the module has no hard GL import)
# ----------------------------------------------------------------------
def _gl():
    import OpenGL.GL as gl
    return gl


def _set_mat4(gl, prog, name, mat) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniformMatrix4fv(loc, 1, gl.GL_FALSE, glmath.column_major(mat))


def _set_vec3(gl, prog, name, vec) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniform3f(loc, float(vec[0]), float(vec[1]), float(vec[2]))


def _set_float(gl, prog, name, value) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniform1f(loc, float(value))
