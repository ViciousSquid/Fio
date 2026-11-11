"""The terrain's GL side, owned by the renderer (M5: tables all the way to GL).

A :class:`TerrainGL` holds every GL name the terrain is drawn with -- the
programs, the height and paint texture pages, the index arrays, the grass
and block-mesh buffers -- and draws one published
:class:`engine.terrain_frame.TerrainFrame`: the dense ``TerrainTable``, the
per-slot products and paint maps beside it, and that frame's appearance. It
never sees the ``Terrain`` object, and nothing here computes a height: a row
is uploaded when the version it holds differs from the published one.

The draw code is the terrain's own, moved here unchanged from
``engine/terrain.py``; the CPU upkeep it used to do first -- streaming, LOD
scheduling, building chunks -- is the terrain's ``advance`` step, which the
host runs before the frame.
"""

import ctypes
import sys
import time

import glm
import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader

from engine import shaders
from engine.terrain_frame import grass_vertex_count, heightfield_indices
from engine.terrain_table import GRID_BORDER, STORED_GRID


def _no_current_gl_context():
    """True only when macOS reports that no GL context is current.

    A Terrain is built before any view's context exists (map load, the terrain
    panel). Calling into GL then raises elsewhere, which _compile_program
    handles, but crashes the process on macOS, so ask CGL first. Other
    platforms may mix GLX and EGL, where the query could miss Qt's context.
    """
    if sys.platform != 'darwin':
        return False
    try:
        from OpenGL import platform as gl_platform
        return not gl_platform.PLATFORM.GetCurrentContext()
    except Exception:
        return False


class TerrainGL:
    """GL state for one published terrain (``TerrainFrame.key``)."""

    #: Every uniform the terrain programs are driven through, less the
    #: appearance uniforms a frame names itself.
    UNIFORM_NAMES = (
        'projection', 'view', 'active_lights', 'use_textures', 'lod_level',
        'texGrass', 'texRock', 'texSand', 'texSnow',
        'terrainPaintMap', 'uPaintLayer', 'uPaintEnabled',
        'uHeights', 'uChunkI', 'uChunkX', 'uChunkY', 'uTiling', 'uFlatMode',
        'uGradCount', 'uGradH', 'uGradC', 'uGradW', 'uGradD',
    )

    def __init__(self):
        #: The frame being drawn; set for the length of :meth:`render`.
        self.frame = None
        self.shader_program = 0
        self.uniforms = {}
        self.block_program = 0
        self.block_uniforms = {}
        self.grass_shader_program = 0
        self.grass_uniforms = {}
        #: Programs compiled here (a renderer may hand in its own instead).
        self._own_programs = set()
        self._failed_programs = set()
        self.grass_tex = self.rock_tex = self.sand_tex = self.snow_tex = 0
        self.grass_time = 0.0
        self._height_pages = []
        self._page_layers = 0
        self._paint_pages = []
        self._paint_page_layers = 0
        self._paint_epoch = None
        self._index_vaos = {}          # resolution -> (vertex array, index buffer)
        self._placeholder_cubemap = 0
        self._sync_gl_columns_reset()
        #: What the last frame drew, for the host (SysMon, the terrain
        #: editor, Debug Tables).
        self.visible_chunks = 0
        self.culled_chunks = 0
        self.total_triangles = 0
        self.drawn_slots = np.empty(0, dtype=np.intp)

    def _sync_gl_columns_reset(self):
        for name in ('_grass_vao', '_grass_vbo', '_grass_count', '_gpu_version',
                     '_paint_gpu_version', '_block_vao', '_block_vbo', '_block_count',
                     '_grass_version', '_block_version', '_slot_order'):
            setattr(self, name, np.zeros(0, dtype=np.int64))

    def _compile_program(self, vertex_name):
        """Compile the terrain fragment shader against *vertex_name*.

        Returns the program, or 0 when there is no GL context yet (harmless:
        the next frame recompiles, with the context current) or the
        compile genuinely failed (reported).
        """
        if gl is None or compileProgram is None or compileShader is None:
            return 0
        if _no_current_gl_context():
            return 0
        if vertex_name in self._failed_programs:
            return 0
        try:
            vertex_code = shaders.DEFAULT_SHADERS[vertex_name]
            # The heightfield draws with flat face outputs; the block mesh
            # keeps the smooth-input fragment shader (engine/shaders.py).
            fragment_name = ('terrain_flat.frag' if vertex_name == 'terrain_indexed.vert'
                             else 'terrain.frag')
            fragment_code = shaders.light_ubo_source(
                shaders.DEFAULT_SHADERS[fragment_name])
            vertex_shader = compileShader(vertex_code, gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(fragment_code, gl.GL_FRAGMENT_SHADER)
            program = compileProgram(vertex_shader, fragment_shader, validate=False)
            if not program:
                print("ERROR: Failed to compile terrain shader program!")
                self._failed_programs.add(vertex_name)
                return 0
        except Exception as e:
            msg = str(e)
            # The Terrain can be constructed before the view's GL context is
            # current (e.g. during map load), so glCreateShader isn't bound yet.
            # Stay quiet then; only a genuine compile failure is worth reporting.
            if ("glCreateShader" in msg or "undefined alternate function" in msg
                    or "context" in msg.lower()):
                return 0
            print(f"ERROR: Exception during terrain shader compilation: {e}")
            self._failed_programs.add(vertex_name)
            return 0
        block_index = gl.glGetUniformBlockIndex(program, 'FioLightBlock')
        invalid = getattr(gl, 'GL_INVALID_INDEX', 0xFFFFFFFF)
        if block_index != invalid:
            gl.glUniformBlockBinding(program, block_index, shaders.LIGHT_UBO_BINDING)
        return program

    def _init_shader(self):
        self.shader_program = self._compile_program('terrain_indexed.vert')
        if not self.shader_program:
            return
        self._own_programs.add(int(self.shader_program))
        self._init_grass_shader()
        self.uniforms = {}

    def _init_grass_shader(self):
        try:
            vertex_code = shaders.DEFAULT_SHADERS['grass.vert']
            fragment_code = shaders.DEFAULT_SHADERS['grass.frag']
            vertex_shader = compileShader(vertex_code, gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(fragment_code, gl.GL_FRAGMENT_SHADER)
            self.grass_shader_program = compileProgram(
                vertex_shader, fragment_shader, validate=False)
        except Exception as e:
            msg = str(e)
            if ("glCreateShader" in msg or "undefined alternate function" in msg
                    or "context" in msg.lower()):
                self.grass_shader_program = 0
                return
            print(f"ERROR: Exception during grass shader compilation: {e}")
            self.grass_shader_program = 0
            return

        self.grass_uniforms = {
            'projection': gl.glGetUniformLocation(self.grass_shader_program, 'projection'),
            'view': gl.glGetUniformLocation(self.grass_shader_program, 'view'),
            'time': gl.glGetUniformLocation(self.grass_shader_program, 'time'),
            'grassColor': gl.glGetUniformLocation(self.grass_shader_program, 'grassColor'),
            'cameraPos': gl.glGetUniformLocation(self.grass_shader_program, 'cameraPos'),
            'windStrength': gl.glGetUniformLocation(self.grass_shader_program, 'windStrength'),
            'uFogEnabled': gl.glGetUniformLocation(self.grass_shader_program, 'uFogEnabled'),
            'uFogColor': gl.glGetUniformLocation(self.grass_shader_program, 'uFogColor'),
            'uFogStart': gl.glGetUniformLocation(self.grass_shader_program, 'uFogStart'),
            'uFogEnd': gl.glGetUniformLocation(self.grass_shader_program, 'uFogEnd'),
            'uFogDensity': gl.glGetUniformLocation(self.grass_shader_program, 'uFogDensity'),
            'uFogCamPos': gl.glGetUniformLocation(self.grass_shader_program, 'uFogCamPos'),
            'uAmbient': gl.glGetUniformLocation(self.grass_shader_program, 'uAmbient'),
        }
        for name in ('grassTipColor', 'uMatchGround', 'uTipAuto', 'uSegments', 'uLodStart', 'uLodEnd',
                     'uFadeStart', 'uFadeEnd', 'uBladeHeight', 'uBladeWidth'):
            self.grass_uniforms[name] = gl.glGetUniformLocation(
                self.grass_shader_program, name)

    def _upload_appearance_uniforms(self, u):
        for name, value in self.frame.appearance.items():
            loc = u.get(name, -1)
            if loc is None or loc == -1:
                continue
            if name == 'uPalette':
                gl.glUniform3fv(loc, len(value), np.ascontiguousarray(value, dtype=np.float32))
            elif isinstance(value, int):
                gl.glUniform1i(loc, value)
            elif isinstance(value, float):
                gl.glUniform1f(loc, value)
            elif len(value) == 2:
                gl.glUniform2f(loc, *value)
            else:
                gl.glUniform3f(loc, *value)

    def _sync_gl_columns(self):
        """Grow the per-slot GL arrays to the table's capacity."""
        cap = self.frame.table.capacity
        if len(getattr(self, '_grass_count', ())) >= cap:
            return
        for name in ('_grass_vao', '_grass_vbo', '_grass_count', '_gpu_version',
                     '_paint_gpu_version', '_block_vao', '_block_vbo', '_block_count',
                     '_grass_version', '_block_version', '_slot_order'):
            old = getattr(self, name, np.zeros(0, dtype=np.int64))
            new = np.zeros(cap, dtype=np.int64)
            new[:len(old)] = old
            setattr(self, name, new)

    def _ensure_block_program(self) -> int:
        if not self.block_program:
            self.block_program = self._compile_program('terrain_mesh.vert')
            self.block_uniforms = {}
            if self.block_program:
                self._own_programs.add(int(self.block_program))
        return self.block_program

    def draw_block_slots(self, slots) -> int:
        """Draw built *slots* from their block meshes (the program is current)."""
        triangles = 0
        for slot in slots:
            slot = int(slot)
            if not self._block_vao[slot] or self._block_count[slot] <= 0:
                continue
            gl.glBindVertexArray(int(self._block_vao[slot]))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, int(self._block_count[slot]))
            triangles += int(self._block_count[slot]) // 3
        gl.glBindVertexArray(0)
        return triangles

    def _ensure_height_page(self, page: int) -> int:
        """The texture array holding height-grid page *page*, created on demand.

        One layer per table slot (slot = page * layers + layer), R32F, every
        layer the bordered LOD-0 grid size; a lower LOD uses the top-left
        corner.
        """
        if not self._page_layers:
            limit = int(gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS))
            self._page_layers = max(1, min(self.frame.height_page_layers, limit))
        while len(self._height_pages) <= page:
            tex = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_R32F,
                            STORED_GRID, STORED_GRID, self._page_layers, 0,
                            gl.GL_RED, gl.GL_FLOAT, None)
            for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
            self._height_pages.append(tex)
        return self._height_pages[page]

    def _upload_heightfield(self, slot: int):
        """Copy a slot's height grid into its texture layer."""
        table = self.frame.table
        self._sync_gl_columns()
        if not self._page_layers:
            self._ensure_height_page(0)
        page, layer = divmod(int(slot), self._page_layers)
        tex = self._ensure_height_page(page)
        n = int(table.grid_res[slot]) + 1 + 2 * GRID_BORDER
        # Stored row i is texel row y = i, column k is x = k; the drawn grid
        # starts GRID_BORDER texels in (terrain.vert offsets by the same).
        data = np.ascontiguousarray(table.heights[slot, :n, :n], dtype=np.float32)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layer, n, n, 1,
                           gl.GL_RED, gl.GL_FLOAT, data)
        self._gpu_version[slot] = table.version[slot]

    def chunk_uniforms(self, slots):
        """``(uChunkI, uChunkX, uChunkY)`` for each of *slots*, in order.

        Python ints and floats, the floats rounded to the float32 the shader
        receives. One set of column operations for a whole draw, rather than
        a dozen NumPy scalar reads per chunk.
        """
        table = self.frame.table
        slots = np.asarray(slots, dtype=np.intp)
        res = table.grid_res[slots].astype(np.int64)
        layer = (slots % self._page_layers if self._page_layers
                 else np.zeros(len(slots), dtype=np.intp))
        lo = table.min_y[slots]
        hi = table.max_y[slots]
        cx = np.empty((len(slots), 3), dtype=np.float32)
        cx[:, :2] = table.world[slots]
        cx[:, 2] = table.size[slots] / res
        cy = np.empty((len(slots), 2), dtype=np.float32)
        cy[:, 0] = lo
        cy[:, 1] = np.where(hi > lo, hi - lo, 1.0)
        return list(zip(zip(res.tolist(), layer.tolist()),
                        cx.tolist(), cy.tolist()))

    def set_heightfield_frame_uniforms(self, u, unit):
        """Per-frame heightfield uniforms: sampler unit, tiling, gradient."""
        count, H, C, W, D = self.frame.gradient
        limit = self.frame.max_gradient_stops
        gl.glUniform1i(u['uHeights'], unit)
        gl.glUniform1f(u['uTiling'], float(np.float32(self.frame.tiling)))
        gl.glUniform1i(u['uFlatMode'], 1 if self.frame.flat_mode else 0)
        gl.glUniform1i(u['uGradCount'], count)
        gl.glUniform1fv(u['uGradH'], limit, H)
        gl.glUniform3fv(u['uGradC'], limit, C)
        gl.glUniform1fv(u['uGradW'], limit, W)
        gl.glUniform3fv(u['uGradD'], limit, D)

    def _index_vao(self, res: int) -> int:
        """The vertex array a chunk of *res* quads a side is drawn with: no
        attributes (the vertex shader reads the height grid), only the index
        list of :func:`heightfield_indices`. One per resolution, made on
        first use, in the context the terrain draws in."""
        entry = self._index_vaos.get(res)
        if entry is None:
            vao = int(gl.glGenVertexArrays(1))
            buffer = int(gl.glGenBuffers(1))
            indices = heightfield_indices(res)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, buffer)
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices,
                            gl.GL_STATIC_DRAW)
            entry = self._index_vaos[res] = (vao, buffer)
        return entry[0]

    def draw_heightfield_slots(self, u, slots, unit, lod_level_loc=-1,
                               paint_unit=None):
        """Draw built terrain chunks with their baked material paint."""
        table = self.frame.table
        stale = slots[self._gpu_version[slots] != table.version[slots]]
        for slot in stale:
            self._upload_heightfield(int(slot))
        order = slots.tolist()
        # Each slot's paint version, 0 where it has none; None when the map
        # has no paint, which is most maps. Uploads happen here, before the
        # loop binds any height page: an upload binds the paint page on the
        # active unit.
        paint = None
        versions = self.frame.paint_versions
        if paint_unit is not None and versions:
            paint = [versions.get(tuple(coord), 0)
                     for coord in table.coord[slots].tolist()]
            gpu_version = self._paint_gpu_version
            for slot, version in zip(order, paint):
                if version and gpu_version[slot] != version:
                    self._upload_paint_chunk(slot)
        bound_res = -1
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        lods = table.lod[slots].tolist() if lod_level_loc != -1 else None
        loc_chunk_i, loc_chunk_x, loc_chunk_y = (
            u['uChunkI'], u['uChunkX'], u['uChunkY'])
        page_layers = self._page_layers
        bound_page = -1
        bound_paint_page = -1
        # The program keeps a uniform's value between draws, so uPaintEnabled
        # is sent when it changes, starting with the first chunk.
        paint_enabled = None
        triangles = 0
        for i, ((res, layer), cx, cy) in enumerate(self.chunk_uniforms(slots)):
            slot = order[i]
            page = slot // page_layers
            if page != bound_page:
                gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._height_pages[page])
                bound_page = page
            if paint_unit is not None:
                if paint is not None and paint[i]:
                    paint_page = slot // self._paint_page_layers
                    if paint_page != bound_paint_page:
                        gl.glActiveTexture(gl.GL_TEXTURE0 + paint_unit)
                        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY,
                                         self._paint_pages[paint_page])
                        bound_paint_page = paint_page
                        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                    if paint_enabled != 1:
                        gl.glUniform1i(u['uPaintEnabled'], 1)
                        paint_enabled = 1
                    gl.glUniform1i(u['uPaintLayer'],
                                   slot % self._paint_page_layers)
                elif paint_enabled != 0:
                    gl.glUniform1i(u['uPaintEnabled'], 0)
                    paint_enabled = 0
            if lods is not None:
                gl.glUniform1i(lod_level_loc, lods[i])
            gl.glUniform2i(loc_chunk_i, res, layer)
            gl.glUniform3f(loc_chunk_x, *cx)
            gl.glUniform2f(loc_chunk_y, *cy)
            if res != bound_res:
                gl.glBindVertexArray(self._index_vao(res))
                bound_res = res
            gl.glDrawElements(gl.GL_TRIANGLES, 6 * res * res,
                              gl.GL_UNSIGNED_INT, None)
            triangles += 2 * res * res
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindVertexArray(0)
        return triangles

    def _draw_grass(self, visible_slots, projection, view, camera_pos, env_uniforms):
        if not self.frame.grass_enabled or not self.grass_shader_program:
            return
        # A monotonic frame clock keeps wind speed independent of actual FPS.
        import time
        now = time.perf_counter()
        last = getattr(self, '_grass_last_time', now)
        self.grass_time = (self.grass_time + max(0.0, min(now - last, 0.1))) % 100000.0
        self._grass_last_time = now
        gl.glUseProgram(self.grass_shader_program)
        u = self.grass_uniforms
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(u['time'], self.grass_time)
        gl.glUniform3f(u['grassColor'], *self.frame.grass_color)
        gl.glUniform3f(u['cameraPos'], float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        gl.glUniform1f(u['windStrength'], 0.65)
        gl.glUniform3f(u['grassTipColor'], *self.frame.grass_tip_colour)
        gl.glUniform1i(u['uMatchGround'], 1 if self.frame.grass_match_ground else 0)
        gl.glUniform1i(u['uTipAuto'], 1 if self.frame.grass_tip_auto else 0)
        gl.glUniform1f(u['uLodStart'], self.frame.grass_lod_start)
        gl.glUniform1f(u['uLodEnd'], self.frame.grass_lod_distance)
        gl.glUniform1f(u['uFadeStart'], min(self.frame.grass_fade_start, self.frame.grass_max_distance))
        gl.glUniform1f(u['uFadeEnd'], self.frame.grass_max_distance)
        gl.glUniform1f(u['uBladeHeight'], self.frame.grass_height)
        gl.glUniform1f(u['uBladeWidth'], self.frame.grass_blade_width)

        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc == -1:
                    continue
                if isinstance(value, int):
                    gl.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl.glUniform1f(loc, value)
                else:
                    gl.glUniform3f(loc, *value)

        # Opaque tapered blades. Explicitly restore the depth state here:
        # grass must test against the already-rendered terrain, not inherit
        # depth state from a preceding renderer pass.
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glDepthMask(gl.GL_TRUE)
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        dist_sq = self.frame.table.nearest_dist_sq(
            visible_slots, float(camera_pos.x), float(camera_pos.z))
        max_sq = self.frame.grass_max_distance * self.frame.grass_max_distance
        for slot, d in zip(visible_slots, dist_sq):
            if self._grass_count[slot] <= 0 or not self._grass_vao[slot]:
                continue
            if d > max_sq:
                continue
            # Whole chunks swap between detailed and single-triangle blades.
            # The shader has already morphed every blade past
            # GRASS_LOD_DISTANCE onto the far shape, and a chunk's nearest
            # point is never further than any of its blades, so the swap
            # cannot be seen.
            lod_sq = self.frame.grass_lod_distance * self.frame.grass_lod_distance
            segments = self.frame.grass_segments if d < lod_sq else 1
            gl.glUniform1i(u['uSegments'], segments)
            gl.glBindVertexArray(int(self._grass_vao[slot]))
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLES, 0, grass_vertex_count(segments),
                int(self._grass_count[slot]))
        gl.glBindVertexArray(0)
        # Grass blades are double-sided. Put culling back the way it was
        # found -- the terrain pass runs with it off. Forcing it on leaked
        # into Qt's overlay painter, which then culled the SysMon panel.
        if cull_was:
            gl.glEnable(gl.GL_CULL_FACE)

    def _ensure_placeholder_cubemap(self) -> int:
        """Lazily create a 1x1 complete cube-map used for shadow sampler units
        that have no real depth cube-map (shadows off, or fewer cube-maps than
        sampler slots).  A complete texture on every unit guarantees no
        samplerCube is left referencing texture unit 0 or an incomplete texture,
        either of which can make glDrawArrays raise GL_INVALID_OPERATION."""
        if self._placeholder_cubemap:
            return self._placeholder_cubemap
        tex = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
        black = (ctypes.c_ubyte * 4)(0, 0, 0, 255)
        for face in range(6):
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0, gl.GL_RGBA,
                            1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, black)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        self._placeholder_cubemap = tex
        return self._placeholder_cubemap

    def _ensure_paint_page(self, page: int) -> int:
        if not self._paint_page_layers:
            limit = int(gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS))
            self._paint_page_layers = max(1, min(self.frame.paint_page_layers, limit))
        while len(self._paint_pages) <= page:
            tex = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
            gl.glTexImage3D(
                gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_RGBA8,
                self.frame.paint_resolution, self.frame.paint_resolution,
                self._paint_page_layers, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            for pname, value in (
                (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
            self._paint_pages.append(tex)
        return self._paint_pages[page]

    def _upload_paint_chunk(self, slot: int):
        table = self.frame.table
        self._sync_gl_columns()
        coord = (int(table.coord[slot, 0]), int(table.coord[slot, 1]))
        paint = self.frame.paint_display.get(coord)
        if paint is None:
            paint = self.frame.paint_maps.get(coord)
        if paint is None:
            self._paint_gpu_version[slot] = 0
            return
        if not self._paint_page_layers:
            self._ensure_paint_page(0)
        page, layer = divmod(int(slot), self._paint_page_layers)
        tex = self._ensure_paint_page(page)
        data = np.ascontiguousarray(paint, dtype=np.uint8)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glTexSubImage3D(
            gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layer,
            self.frame.paint_resolution, self.frame.paint_resolution, 1,
            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        self._paint_gpu_version[slot] = self.frame.paint_versions.get(coord, 0)

    # -- slots ------------------------------------------------------------

    def _adopt(self, table):
        """Match the per-slot GL columns to the published table: a slot that
        was freed, or freed and given to another chunk (its allocation
        ``order`` moved), loses the grass, block mesh and paint it held."""
        self._sync_gl_columns()
        n = min(len(self._slot_order), table.capacity)
        if not n:
            return
        current = np.where(table.live[:n], table.order[:n], -1)
        changed = np.flatnonzero(self._slot_order[:n] != current)
        if len(changed):
            self._free_slots(changed)
            self._slot_order[changed] = current[changed]

    def _free_slots(self, slots):
        for slot in slots:
            slot = int(slot)
            if self._grass_vao[slot]:
                gl.glDeleteVertexArrays(1, [int(self._grass_vao[slot])])
            if self._grass_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._grass_vbo[slot])])
            self._grass_vao[slot] = self._grass_vbo[slot] = 0
            self._grass_count[slot] = 0
            self._grass_version[slot] = 0
            if self._block_vao[slot]:
                gl.glDeleteVertexArrays(1, [int(self._block_vao[slot])])
            if self._block_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._block_vbo[slot])])
            self._block_vao[slot] = self._block_vbo[slot] = 0
            self._block_count[slot] = 0
            self._block_version[slot] = 0
            self._paint_gpu_version[slot] = 0
            self._gpu_version[slot] = 0

    def _product(self, store, slot):
        """The published product of *slot* in *store*, if it was built for
        the chunk the slot holds now: ``(version, data)`` or None."""
        entry = store.get(int(slot))
        if entry is None or entry[1] != int(self.frame.table.order[slot]):
            return None
        return entry[0], entry[2]

    def _upload_block_mesh(self, slot: int):
        entry = self._product(self.frame.products.blocks, slot)
        if entry is None:
            return
        version, data = entry
        if not self._block_vao[slot]:
            self._block_vao[slot] = int(gl.glGenVertexArrays(1))
            self._block_vbo[slot] = int(gl.glGenBuffers(1))
        gl.glBindVertexArray(int(self._block_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._block_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, max(data.nbytes, 4),
                        data if data.nbytes else None, gl.GL_STATIC_DRAW)
        stride = 14 * 4
        for loc, size, offset in ((0, 3, 0), (1, 3, 12), (2, 3, 24), (3, 2, 36), (4, 3, 44)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
        gl.glBindVertexArray(0)
        self._block_count[slot] = len(data)
        self._block_version[slot] = version

    def _upload_grass_chunk(self, slot: int):
        """Upload a slot's published grass instances (``(n, 9)`` float32)."""
        entry = self._product(self.frame.products.grass, slot)
        if entry is None:
            return
        version, data = entry
        count = len(data)
        self._grass_version[slot] = version
        if count <= 0:
            self._grass_count[slot] = 0
            return
        if not self._grass_vao[slot]:
            self._grass_vao[slot] = int(gl.glGenVertexArrays(1))
            self._grass_vbo[slot] = int(gl.glGenBuffers(1))
        gl.glBindVertexArray(int(self._grass_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._grass_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STATIC_DRAW)
        stride = 9 * 4
        for loc, size, offset in ((6, 3, 24), (2, 3, 0), (3, 1, 12), (4, 1, 16), (5, 1, 20)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
            gl.glVertexAttribDivisor(loc, 1)
        gl.glBindVertexArray(0)
        self._grass_count[slot] = count

    def _stale(self, store, held, slots):
        """Those of *slots* whose published product in *store* is not the
        version *held* records."""
        table = self.frame.table
        out = []
        for slot in slots.tolist():
            entry = store.get(slot)
            if (entry is not None and entry[0] != held[slot]
                    and entry[1] == int(table.order[slot])):
                out.append(slot)
        return out

    # -- the frame --------------------------------------------------------

    def render(self, frame, projection, view, camera_pos, frustum_planes=None,
               active_lights_count=0, shadow_cubemaps=None, shadow_unit_base=4,
               env_uniforms=None):
        """Draw the published *frame* with the current GL context."""
        self.visible_chunks = 0
        self.culled_chunks = 0
        self.total_triangles = 0
        if frame is None or not frame.enabled:
            return
        self.frame = frame
        try:
            self._render(projection, view, camera_pos, frustum_planes,
                         active_lights_count, shadow_cubemaps, shadow_unit_base,
                         env_uniforms)
        finally:
            self.frame = None

    def _render(self, projection, view, camera_pos, frustum_planes,
                active_lights_count, shadow_cubemaps, shadow_unit_base,
                env_uniforms):
        frame = self.frame
        if not self.shader_program:
            self._init_shader()
        prog = self.shader_program
        if not prog:
            return
        u = self.uniforms
        # Blocks are drawn from per-chunk meshes by their own vertex shader;
        # everything else from the heightfield.
        blocks = frame.blocks and bool(self._ensure_block_program())
        if blocks:
            prog = self.block_program
            u = self.block_uniforms

        # The grass program is compiled when a context is first current.
        if frame.grass_enabled and not self.grass_shader_program:
            self._init_grass_shader()

        # Re-resolve late-bound uniforms against the *current* program. A
        # renderer can hand in its own program whose uniform table covers a
        # subset of locations (ForwardRenderer.setup_terrain_shader), so any
        # sampler relied on must be looked up here or it stays unbound: an
        # unassigned ``samplerCube`` defaults to texture unit 0, collides
        # with the ``sampler2D`` terrain textures there, and makes the draw
        # raise GL_INVALID_OPERATION.
        names = self.UNIFORM_NAMES + tuple(frame.appearance) + tuple(env_uniforms or ())
        names += tuple(f'shadowMaps[{i}]' for i in range(shaders.MAX_SHADOW_LIGHTS))
        for name in names:
            if name not in u:
                u[name] = gl.glGetUniformLocation(prog, name)

        table = frame.table
        self._adopt(table)
        if self._paint_epoch != frame.paint_epoch:
            self._paint_gpu_version[:] = 0
            self._paint_epoch = frame.paint_epoch

        gl.glUseProgram(prog)
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.grass_tex); gl.glUniform1i(u['texGrass'], 0)
        gl.glActiveTexture(gl.GL_TEXTURE1); gl.glBindTexture(gl.GL_TEXTURE_2D, self.rock_tex);  gl.glUniform1i(u['texRock'],  1)
        gl.glActiveTexture(gl.GL_TEXTURE2); gl.glBindTexture(gl.GL_TEXTURE_2D, self.sand_tex);  gl.glUniform1i(u['texSand'],  2)
        gl.glActiveTexture(gl.GL_TEXTURE3); gl.glBindTexture(gl.GL_TEXTURE_2D, self.snow_tex);  gl.glUniform1i(u['texSnow'],  3)
        height_texture_unit = shadow_unit_base + shaders.MAX_SHADOW_LIGHTS
        paint_texture_unit = height_texture_unit + 1
        self._upload_appearance_uniforms(u)
        loc = u.get('terrainPaintMap', -1)
        if loc != -1:
            gl.glUniform1i(loc, paint_texture_unit)

        # Textures off in flat mode, or while they are not loaded.
        textures_loaded = (self.grass_tex != 0 and self.rock_tex != 0
                           and self.sand_tex != 0 and self.snow_tex != 0)
        use_tex = 0 if frame.flat_mode or not textures_loaded else (1 if frame.use_textures else 0)
        gl.glUniform1i(u['use_textures'], use_tex)

        # Distance fog + global ambient (engine.shaders.FOG_GLSL): this
        # program has its own uniform table, so the renderer hands the
        # resolved values across rather than writing them itself.
        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc is None or loc == -1:
                    continue
                if isinstance(value, int):
                    gl.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl.glUniform1f(loc, value)
                else:
                    gl.glUniform3f(loc, *value)

        # The renderer has already selected the terrain light subset and
        # packed it into the shared std140 light UBO.
        gl.glUniform1i(u['active_lights'], active_lights_count)

        # Every ``samplerCube shadowMaps[i]`` must point at its own reserved
        # unit, with a *complete* cube map bound there -- the real depth map,
        # or a 1x1 placeholder -- shadows on or off: samplers default to unit
        # 0, which holds a ``sampler2D``, and two sampler types on one unit
        # (or an incomplete texture on an active one) fail the next draw.
        shadow_cubemaps = shadow_cubemaps or []
        placeholder = self._ensure_placeholder_cubemap()
        for i in range(shaders.MAX_SHADOW_LIGHTS):
            loc = u.get(f'shadowMaps[{i}]', -1)
            if loc is None or loc == -1:
                continue
            cm = shadow_cubemaps[i] if (i < len(shadow_cubemaps) and shadow_cubemaps[i]) else placeholder
            gl.glActiveTexture(gl.GL_TEXTURE0 + shadow_unit_base + i)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
            gl.glUniform1i(loc, shadow_unit_base + i)
        gl.glActiveTexture(gl.GL_TEXTURE0)

        lod_level_loc = u.get('lod_level', -1)
        slots = table.live_slots()
        # The frustum (far plane included, so the view distance applies)
        # decides what is drawn; which chunks exist and at what detail was
        # decided by the terrain, before the frame.
        visible = table.visible(slots, frustum_planes)
        self.culled_chunks = int(len(slots) - np.count_nonzero(visible))

        if frame.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        # The height grids live in texture units above the shadow cube-maps.
        unit = height_texture_unit
        drawn = slots[visible & table.built[slots]]
        if blocks:
            for slot in self._stale(frame.products.blocks, self._block_version, drawn):
                self._upload_block_mesh(slot)
            self.total_triangles = self.draw_block_slots(drawn)
        else:
            self.set_heightfield_frame_uniforms(u, unit)
            self.total_triangles = self.draw_heightfield_slots(
                u, drawn, unit, lod_level_loc, paint_texture_unit)
        self.visible_chunks = int(len(drawn))
        #: The slots drawn this frame, in draw order (tests, Debug Tables).
        self.drawn_slots = drawn
        if frame.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        if frame.grass_enabled and self.grass_shader_program:
            built = slots[table.built[slots]]
            for slot in self._stale(frame.products.grass, self._grass_version, built):
                self._upload_grass_chunk(slot)
            candidates = built[self._grass_count[built] > 0]
            visible_grass = candidates[table.visible(candidates, frustum_planes)]
            self._draw_grass(visible_grass, projection, view, camera_pos, env_uniforms)

    # -- lifetime ---------------------------------------------------------

    def adopt_program(self, program, uniforms):
        """Draw the heightfield with a renderer's own *program* from now on."""
        self.shader_program = program
        self.uniforms = uniforms

    def cleanup(self):
        """Delete every GL name held (the owning context is current)."""
        self._free_slots(range(len(self._grass_vao)))
        for program in sorted(self._own_programs):
            try:
                gl.glDeleteProgram(program)
            except Exception:
                pass
        self._own_programs.clear()
        if self.grass_shader_program:
            try:
                gl.glDeleteProgram(self.grass_shader_program)
            except Exception:
                pass
        self.shader_program = self.block_program = self.grass_shader_program = 0
        self.uniforms, self.block_uniforms, self.grass_uniforms = {}, {}, {}
        for pages in (self._paint_pages, self._height_pages):
            if pages:
                try:
                    gl.glDeleteTextures(len(pages), pages)
                except Exception:
                    pass
        self._paint_pages, self._height_pages = [], []
        self._paint_page_layers = 0
        for vao, buffer in self._index_vaos.values():
            gl.glDeleteVertexArrays(1, [vao])
            gl.glDeleteBuffers(1, [buffer])
        self._index_vaos = {}
        if self._placeholder_cubemap:
            gl.glDeleteTextures(1, [self._placeholder_cubemap])
            self._placeholder_cubemap = 0
        self.grass_tex = self.rock_tex = self.sand_tex = self.snow_tex = 0
        self._sync_gl_columns_reset()
