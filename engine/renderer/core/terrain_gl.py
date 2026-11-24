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
from . import gl_state
from OpenGL.GL.shaders import compileProgram, compileShader

from engine import shaders
from engine.terrain_frame import grass_vertex_count, heightfield_indices
from engine.terrain_table import GRID_BORDER, STORED_GRID
from .raw_calls import draw_elements, uniform_matrix4


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


#: EXT/ARB_texture_filter_anisotropic (not in PyOpenGL's core namespace).
_TEXTURE_MAX_ANISOTROPY = 0x84FE
_MAX_TEXTURE_MAX_ANISOTROPY = 0x84FF

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
        #: The strata walls at the world's edge (_draw_skirts): their
        #: program, its uniforms, and the attribute-less vertex array they
        #: are drawn from.
        self.skirt_program = 0
        self.skirt_uniforms = {}
        self._skirt_vao = 0
        #: Chunk sides drawn as strata walls the last frame (tests, SysMon).
        self.skirt_sides = 0
        #: Programs compiled here (a renderer may hand in its own instead).
        self._own_programs = set()
        self._failed_programs = set()
        self.grass_tex = self.rock_tex = self.sand_tex = self.snow_tex = 0
        self.grass_time = 0.0
        self._height_pages = []
        self._page_layers = 0
        self._paint_pages = []
        self._paint_page_layers = 0
        # Paint layers go to painted chunks only: slot -> layer number across
        # the pages, and the numbers given back by slots that lost theirs.
        self._paint_layer = {}
        self._paint_layers_free = []
        self._paint_layers_used = 0
        self._paint_anisotropy_limit = None
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
                gl_state.glUniform3fv(loc, len(value), np.ascontiguousarray(value, dtype=np.float32))
            elif isinstance(value, int):
                gl_state.glUniform1i(loc, value)
            elif isinstance(value, float):
                gl_state.glUniform1f(loc, value)
            elif len(value) == 2:
                gl_state.glUniform2f(loc, *value)
            else:
                gl_state.glUniform3f(loc, *value)

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
            gl_state.glBindVertexArray(int(self._block_vao[slot]))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, int(self._block_count[slot]))
            triangles += int(self._block_count[slot]) // 3
        gl_state.glBindVertexArray(0)
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
            gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
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
        gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
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
        gl_state.glUniform1i(u['uHeights'], unit)
        gl_state.glUniform1f(u['uTiling'], float(np.float32(self.frame.tiling)))
        gl_state.glUniform1i(u['uFlatMode'], 1 if self.frame.flat_mode else 0)
        gl_state.glUniform1i(u['uGradCount'], count)
        gl_state.glUniform1fv(u['uGradH'], limit, H)
        gl_state.glUniform3fv(u['uGradC'], limit, C)
        gl_state.glUniform1fv(u['uGradW'], limit, W)
        gl_state.glUniform3fv(u['uGradD'], limit, D)

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
            gl_state.glBindVertexArray(vao)
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
            # A version with no pixels behind it (the chunk's paint went
            # between publication and here) has no layer: draw it unpainted.
            layers = self._paint_layer
            paint = [version if slot in layers else 0
                     for slot, version in zip(order, paint)]
        bound_res = -1
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
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
                gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._height_pages[page])
                bound_page = page
            if paint_unit is not None:
                if paint is not None and paint[i]:
                    paint_page, paint_layer = divmod(
                        self._paint_layer[slot], self._paint_page_layers)
                    if paint_page != bound_paint_page:
                        gl_state.glActiveTexture(gl.GL_TEXTURE0 + paint_unit)
                        gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY,
                                         self._paint_pages[paint_page])
                        bound_paint_page = paint_page
                        gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
                    if paint_enabled != 1:
                        gl_state.glUniform1i(u['uPaintEnabled'], 1)
                        paint_enabled = 1
                    gl_state.glUniform1i(u['uPaintLayer'], paint_layer)
                elif paint_enabled != 0:
                    gl_state.glUniform1i(u['uPaintEnabled'], 0)
                    paint_enabled = 0
            if lods is not None:
                gl_state.glUniform1i(lod_level_loc, lods[i])
            gl_state.glUniform2i(loc_chunk_i, res, layer)
            gl_state.glUniform3f(loc_chunk_x, *cx)
            gl_state.glUniform2f(loc_chunk_y, *cy)
            if res != bound_res:
                gl_state.glBindVertexArray(self._index_vao(res))
                bound_res = res
            draw_elements(gl.GL_TRIANGLES, 6 * res * res, gl.GL_UNSIGNED_INT, 0)
            triangles += 2 * res * res
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glBindVertexArray(0)
        return triangles

    # -- the world's edge: strata walls -----------------------------------

    #: Beds a wall shows from foot to top when the frame does not say
    #: (``TerrainAppearance.strata_layers``), the thinnest a bed may be, and
    #: the scale of the soil horizons over them (terrain_skirt.frag).
    SKIRT_LAYERS = 24
    SKIRT_MIN_BED = 4.0
    SKIRT_SOIL_SCALE = 1.0
    #: How far the wall's face leans out per unit of drop (about 31 degrees
    #: off sheer). The play camera looks straight down from over the player:
    #: a sheer wall (0) faces away from it and is never seen in play; an
    #: escarpment this steep fills the edge of the screen with its beds.
    SKIRT_LEAN = 0.6
    SKIRT_UNIFORMS = (
        'projection', 'view', 'uHeights', 'uChunkI', 'uChunkX', 'uSide',
        'uFloor', 'uEdge', 'uLean', 'uBasement', 'uMeta', 'uBand', 'uSoil',
        'uChunkY',
    )

    def _ensure_skirt_program(self) -> int:
        """The strata-wall program, compiled on first use (0 without a
        context or after a failed compile, which is reported once)."""
        if self.skirt_program or 'skirt' in self._failed_programs:
            return self.skirt_program
        if gl is None or _no_current_gl_context():
            return 0
        try:
            vertex_shader = compileShader(
                shaders.DEFAULT_SHADERS['terrain_skirt.vert'], gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(
                shaders.DEFAULT_SHADERS['terrain_skirt.frag'], gl.GL_FRAGMENT_SHADER)
            program = compileProgram(vertex_shader, fragment_shader, validate=False)
        except Exception as e:
            msg = str(e)
            if ("glCreateShader" in msg or "undefined alternate function" in msg
                    or "context" in msg.lower()):
                return 0
            print(f"ERROR: Exception during terrain skirt shader compilation: {e}")
            self._failed_programs.add('skirt')
            return 0
        self.skirt_program = int(program)
        self._own_programs.add(self.skirt_program)
        self.skirt_uniforms = {}
        return self.skirt_program

    @staticmethod
    def edge_sides(world, size, bounds):
        """``(slot index, side)`` pairs for chunks on the terrain's edge.

        *world* is the chunks' ``(x, z)`` origins, *size* their edge length
        and *bounds* ``(min x, max x, min z, max z)``. Side 0 is the -x edge,
        1 +x, 2 -z, 3 +z; a corner chunk has two.
        """
        world = np.asarray(world, dtype=np.float64).reshape(-1, 2)
        size = np.asarray(size, dtype=np.float64).reshape(-1)
        if not len(size):
            return []
        min_x, max_x, min_z, max_z = (float(v) for v in bounds[:4])
        eps = np.maximum(size * 1e-4, 1e-3)
        x0, z0 = world[:, 0], world[:, 1]
        hits = (np.abs(x0 - min_x) <= eps,
                np.abs(x0 + size - max_x) <= eps,
                np.abs(z0 - min_z) <= eps,
                np.abs(z0 + size - max_z) <= eps)
        pairs = []
        for side, mask in enumerate(hits):
            pairs.extend((int(i), side) for i in np.flatnonzero(mask))
        return pairs

    @classmethod
    def bed_thickness(cls, floor, high, layers=None):
        """A bed's mean thickness for walls from *floor* up to *high* that
        show *layers* beds (the terrain's ``strata_layers``) from foot to top."""
        layers = cls.SKIRT_LAYERS if layers is None else max(1.0, float(layers))
        return max(cls.SKIRT_MIN_BED, (float(high) - float(floor)) / layers)

    @classmethod
    def soil_scale(cls, floor, high):
        """How deep the soil horizons run (1 is about ten units of topsoil):
        deeper on a taller wall, so a mountain edge keeps a visible soil."""
        return cls.SKIRT_SOIL_SCALE * max(1.0, (float(high) - float(floor)) / 1500.0)

    @staticmethod
    def strata_levels(floor, low, high):
        """``(basement top, metamorphic top)`` world heights for walls
        whose foot is *floor* under ground from *low* to *high*."""
        depth = max(float(low) - float(floor), 1.0)
        return (float(floor) + 0.30 * depth,
                float(floor) + 0.58 * depth)

    @staticmethod
    def skirt_visible(world, size, min_y, max_y, floor, lean, frustum_planes):
        """Which edge chunks' walls may be in the frustum (far plane, and so
        the view distance, included): each chunk's box taken down to the
        wall's foot and out by the most the wall leans, so a wall is never
        culled with its chunk while it is itself in view."""
        world = np.asarray(world, dtype=np.float64).reshape(-1, 2)
        n = len(world)
        if frustum_planes is None or not n:
            return np.ones(n, dtype=bool)
        size = np.asarray(size, dtype=np.float64)
        top = np.asarray(max_y, dtype=np.float64)
        bottom = np.minimum(np.asarray(min_y, dtype=np.float64), float(floor))
        reach = np.maximum(top - bottom, 0.0) * float(lean)
        lo = np.stack([world[:, 0] - reach, bottom, world[:, 1] - reach], axis=1)
        hi = np.stack([world[:, 0] + size + reach, top + 1.0, world[:, 1] + size + reach], axis=1)
        planes = np.asarray(frustum_planes, dtype=np.float64).reshape(-1, 4)
        inside = np.ones(n, dtype=bool)
        for a, b, c, d in planes:
            # The box corner furthest along the plane's normal.
            px = np.where(a >= 0, hi[:, 0], lo[:, 0])
            py = np.where(b >= 0, hi[:, 1], lo[:, 1])
            pz = np.where(c >= 0, hi[:, 2], lo[:, 2])
            inside &= a * px + b * py + c * pz + d >= 0
        return inside

    def _draw_skirts(self, slots, projection, view, env_uniforms, unit,
                     frustum_planes=None, paint_unit=None, use_textures=0) -> int:
        """Draw the cliff walls on the terrain's edge for every built chunk
        that lies on it, each side a triangle strip from the chunk's own
        height grid down to the wall's foot. Returns the sides drawn.

        Culled by the wall's own box (skirt_visible), never the chunk's: a
        wall reaches far below and beyond its chunk. Each strip is drawn with
        the terrain's own material uniforms, which terrain_skirt.frag runs at
        the rim for the top layer.
        """
        frame = self.frame
        table = frame.table
        if not len(slots):
            return 0
        floor, low, high = frame.skirt[4], frame.skirt[5], frame.skirt[6]
        pairs = self.edge_sides(table.world[slots], table.size[slots], frame.skirt)
        if not pairs:
            return 0
        edge_idx = np.unique(np.asarray([i for i, _ in pairs], dtype=np.intp))
        edge = slots[edge_idx]
        seen = self.skirt_visible(table.world[edge], table.size[edge], table.min_y[edge],
                                  table.max_y[edge], floor, self.SKIRT_LEAN, frustum_planes)
        keep = set(edge_idx[seen].tolist())
        pairs = [(i, side) for i, side in pairs if i in keep]
        if not pairs:
            return 0
        prog = self._ensure_skirt_program()
        if not prog:
            return 0
        u = self.skirt_uniforms
        # The wall's program is built on the terrain's (terrain_skirt.frag
        # runs terrain.frag's material on the rim), so it takes every one of
        # the terrain's uniforms as well as its own.
        names = (self.UNIFORM_NAMES + tuple(frame.appearance) + self.SKIRT_UNIFORMS
                 + tuple(env_uniforms or ()))
        for name in names:
            if name not in u:
                u[name] = gl.glGetUniformLocation(prog, name)
        edge_slots = np.unique(np.asarray([slots[i] for i, _ in pairs], dtype=np.intp))
        stale = edge_slots[self._gpu_version[edge_slots] != table.version[edge_slots]]
        for slot in stale:
            self._upload_heightfield(int(slot))
        # Paint on the rim chunks, uploaded with its own unit active (an
        # upload binds the paint page on the active unit).
        paint = {}
        versions = frame.paint_versions
        if paint_unit is not None and versions:
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + paint_unit)
            gpu_version = self._paint_gpu_version
            for slot, coord in zip(edge_slots.tolist(), table.coord[edge_slots].tolist()):
                version = versions.get(tuple(coord), 0)
                if version and gpu_version[slot] != version:
                    self._upload_paint_chunk(slot)
                if version and slot in self._paint_layer:
                    paint[slot] = self._paint_layer[slot]

        basement, meta = self.strata_levels(floor, low, high)
        layers = frame.skirt[7] if len(frame.skirt) > 7 else self.SKIRT_LAYERS
        band = self.bed_thickness(floor, high, layers)
        gl_state.glUseProgram(prog)
        uniform_matrix4(u['projection'], glm.value_ptr(projection))
        uniform_matrix4(u['view'], glm.value_ptr(view))
        # The ground's material, set as _render sets it for the heightfield.
        for i, (name, tex) in enumerate((('texGrass', self.grass_tex), ('texRock', self.rock_tex),
                                         ('texSand', self.sand_tex), ('texSnow', self.snow_tex))):
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + i)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, tex)
            if u.get(name, -1) != -1:
                gl_state.glUniform1i(u[name], i)
        if u.get('use_textures', -1) != -1:
            gl_state.glUniform1i(u['use_textures'], use_textures)
        self._upload_appearance_uniforms(u)
        self.set_heightfield_frame_uniforms(u, unit)
        if paint_unit is not None:
            if u.get('terrainPaintMap', -1) != -1:
                gl_state.glUniform1i(u['terrainPaintMap'], paint_unit)
        paint_enabled = None
        for name, value in (('uFloor', floor), ('uBasement', basement), ('uMeta', meta),
                            ('uBand', band),
                            ('uSoil', self.soil_scale(floor, high)),
                            ('uLean', self.SKIRT_LEAN)):
            if u.get(name, -1) != -1:
                gl_state.glUniform1f(u[name], float(value))
        if u.get('uEdge', -1) != -1:
            gl_state.glUniform4f(u['uEdge'], *(float(v) for v in frame.skirt[:4]))
        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc is None or loc == -1:
                    continue
                if isinstance(value, int):
                    gl_state.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl_state.glUniform1f(loc, value)
                else:
                    gl_state.glUniform3f(loc, *value)

        if not self._skirt_vao:
            self._skirt_vao = int(gl.glGenVertexArrays(1))
        gl_state.glBindVertexArray(self._skirt_vao)
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
        # Every strip is wound counter-clockwise seen from outside
        # (terrain_skirt.vert): a wall seen from behind -- from the world's
        # far side, past ground too distant to be drawn -- is culled.
        cull = gl.glIsEnabled(gl.GL_CULL_FACE)
        cull_mode = int(np.asarray(gl.glGetIntegerv(gl.GL_CULL_FACE_MODE)).ravel()[0])
        front = int(np.asarray(gl.glGetIntegerv(gl.GL_FRONT_FACE)).ravel()[0])
        gl_state.glEnable(gl.GL_CULL_FACE)
        gl_state.glCullFace(gl.GL_BACK)
        gl.glFrontFace(gl.GL_CCW)
        uniforms = {slot: entry for slot, entry in
                    zip(edge_slots.tolist(), self.chunk_uniforms(edge_slots))}
        page_layers = self._page_layers
        bound_page = -1
        bound_paint_page = -1
        drawn = 0
        for i, side in pairs:
            slot = int(slots[i])
            (res, layer), cx, cy = uniforms[slot]
            page = slot // page_layers
            if page != bound_page:
                gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._height_pages[page])
                bound_page = page
            if paint_unit is not None:
                number = paint.get(slot)
                if number is not None:
                    paint_page, paint_layer = divmod(number, self._paint_page_layers)
                    if paint_page != bound_paint_page:
                        gl_state.glActiveTexture(gl.GL_TEXTURE0 + paint_unit)
                        gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._paint_pages[paint_page])
                        bound_paint_page = paint_page
                        gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
                    if paint_enabled != 1:
                        gl_state.glUniform1i(u['uPaintEnabled'], 1)
                        paint_enabled = 1
                    gl_state.glUniform1i(u['uPaintLayer'], paint_layer)
                elif paint_enabled != 0:
                    gl_state.glUniform1i(u['uPaintEnabled'], 0)
                    paint_enabled = 0
            gl_state.glUniform2i(u['uChunkI'], res, layer)
            gl_state.glUniform3f(u['uChunkX'], *cx)
            gl_state.glUniform2f(u['uChunkY'], *cy)
            gl_state.glUniform1i(u['uSide'], side)
            gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 2 * (res + 1))
            drawn += 1
        gl_state.glCullFace(cull_mode)
        gl.glFrontFace(front)
        if not cull:
            gl_state.glDisable(gl.GL_CULL_FACE)
        gl_state.glBindVertexArray(0)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        self.total_triangles += sum(2 * int(uniforms[int(slots[i])][0][0]) for i, _ in pairs)
        return drawn

    def _draw_grass(self, visible_slots, projection, view, camera_pos, env_uniforms):
        if not self.frame.grass_enabled or not self.grass_shader_program:
            return
        # A monotonic frame clock keeps wind speed independent of actual FPS.
        import time
        now = time.perf_counter()
        last = getattr(self, '_grass_last_time', now)
        self.grass_time = (self.grass_time + max(0.0, min(now - last, 0.1))) % 100000.0
        self._grass_last_time = now
        gl_state.glUseProgram(self.grass_shader_program)
        u = self.grass_uniforms
        uniform_matrix4(u['projection'], glm.value_ptr(projection))
        uniform_matrix4(u['view'], glm.value_ptr(view))
        gl_state.glUniform1f(u['time'], self.grass_time)
        gl_state.glUniform3f(u['grassColor'], *self.frame.grass_color)
        gl_state.glUniform3f(u['cameraPos'], float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        gl_state.glUniform1f(u['windStrength'], 0.65)
        gl_state.glUniform3f(u['grassTipColor'], *self.frame.grass_tip_colour)
        gl_state.glUniform1i(u['uMatchGround'], 1 if self.frame.grass_match_ground else 0)
        gl_state.glUniform1i(u['uTipAuto'], 1 if self.frame.grass_tip_auto else 0)
        gl_state.glUniform1f(u['uLodStart'], self.frame.grass_lod_start)
        gl_state.glUniform1f(u['uLodEnd'], self.frame.grass_lod_distance)
        gl_state.glUniform1f(u['uFadeStart'], min(self.frame.grass_fade_start, self.frame.grass_max_distance))
        gl_state.glUniform1f(u['uFadeEnd'], self.frame.grass_max_distance)
        gl_state.glUniform1f(u['uBladeHeight'], self.frame.grass_height)
        gl_state.glUniform1f(u['uBladeWidth'], self.frame.grass_blade_width)

        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc == -1:
                    continue
                if isinstance(value, int):
                    gl_state.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl_state.glUniform1f(loc, value)
                else:
                    gl_state.glUniform3f(loc, *value)

        # Opaque tapered blades. Explicitly restore the depth state here:
        # grass must test against the already-rendered terrain, not inherit
        # depth state from a preceding renderer pass.
        gl_state.glDisable(gl.GL_BLEND)
        gl_state.glEnable(gl.GL_DEPTH_TEST)
        gl_state.glDepthFunc(gl.GL_LEQUAL)
        gl_state.glDepthMask(gl.GL_TRUE)
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        gl_state.glDisable(gl.GL_CULL_FACE)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
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
            gl_state.glUniform1i(u['uSegments'], segments)
            gl_state.glBindVertexArray(int(self._grass_vao[slot]))
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLES, 0, grass_vertex_count(segments),
                int(self._grass_count[slot]))
        gl_state.glBindVertexArray(0)
        # Grass blades are double-sided. Put culling back the way it was
        # found -- the terrain pass runs with it off. Forcing it on leaked
        # into Qt's overlay painter, which then culled the SysMon panel.
        if cull_was:
            gl_state.glEnable(gl.GL_CULL_FACE)

    def _ensure_placeholder_cubemap(self) -> int:
        """Lazily create a 1x1 complete cube-map used for shadow sampler units
        that have no real depth cube-map (shadows off, or fewer cube-maps than
        sampler slots).  A complete texture on every unit guarantees no
        samplerCube is left referencing texture unit 0 or an incomplete texture,
        either of which can make glDrawArrays raise GL_INVALID_OPERATION."""
        if self._placeholder_cubemap:
            return self._placeholder_cubemap
        tex = int(gl.glGenTextures(1))
        gl_state.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
        black = (ctypes.c_ubyte * 4)(0, 0, 0, 255)
        for face in range(6):
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0, gl.GL_RGBA,
                            1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, black)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl_state.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        self._placeholder_cubemap = tex
        return self._placeholder_cubemap

    #: Mip levels a paint page has beyond the first. Enough that paint in
    #: the distance does not shimmer; few enough that a chunk's edge texels
    #: are not averaged far into its neighbour's missing ones.
    PAINT_MIP_LEVELS = 3

    def _ensure_paint_page(self, page: int) -> int:
        if not self._paint_page_layers:
            limit = int(gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS))
            self._paint_page_layers = max(1, min(self.frame.paint_page_layers, limit))
        while len(self._paint_pages) <= page:
            tex = int(gl.glGenTextures(1))
            gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
            side = int(self.frame.paint_resolution)
            levels = 0
            for level in range(self.PAINT_MIP_LEVELS + 1):
                if side >> level < 1:
                    break
                gl.glTexImage3D(
                    gl.GL_TEXTURE_2D_ARRAY, level, gl.GL_RGBA8,
                    side >> level, side >> level,
                    self._paint_page_layers, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
                levels = level
            for pname, value in (
                (gl.GL_TEXTURE_BASE_LEVEL, 0),
                (gl.GL_TEXTURE_MAX_LEVEL, levels),
                (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR),
                (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
            # Seen at a grazing angle -- which the ground mostly is -- plain
            # trilinear filtering picks a mip for the squashed axis and blurs
            # the other: bias it back a level (core GL), and filter along
            # the squash where the driver can (an extension nearly all have).
            gl.glTexParameterf(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_LOD_BIAS,
                               self.PAINT_LOD_BIAS)
            anisotropy = self._paint_anisotropy()
            if anisotropy > 1.0:
                gl.glTexParameterf(gl.GL_TEXTURE_2D_ARRAY,
                                   _TEXTURE_MAX_ANISOTROPY, anisotropy)
            self._paint_pages.append(tex)
        return self._paint_pages[page]

    PAINT_LOD_BIAS = -1.0
    PAINT_ANISOTROPY = 4.0

    def _paint_anisotropy(self):
        """Anisotropic samples for paint pages, 1.0 where unsupported."""
        if self._paint_anisotropy_limit is None:
            limit = 1.0
            try:
                count = int(gl.glGetIntegerv(gl.GL_NUM_EXTENSIONS))
                names = {gl.glGetStringi(gl.GL_EXTENSIONS, i).decode('ascii', 'replace')
                         for i in range(count)}
                if ('GL_EXT_texture_filter_anisotropic' in names
                        or 'GL_ARB_texture_filter_anisotropic' in names):
                    limit = float(gl.glGetFloatv(_MAX_TEXTURE_MAX_ANISOTROPY))
            except Exception:
                limit = 1.0
            self._paint_anisotropy_limit = limit
        return max(1.0, min(self.PAINT_ANISOTROPY, self._paint_anisotropy_limit))

    def _release_paint_layer(self, slot: int):
        layer = self._paint_layer.pop(int(slot), None)
        if layer is not None:
            self._paint_layers_free.append(layer)

    def _release_paint_layers(self):
        """Every paint layer back: the paint was replaced wholesale."""
        self._paint_layer.clear()
        self._paint_layers_free = []
        self._paint_layers_used = 0

    @staticmethod
    def _paint_mip(paint):
        """*paint* (n, n, 4 uint8) at half the side, colour weighted by alpha
        (an unpainted texel is black, and must not darken its neighbours)."""
        n = paint.shape[0] // 2
        texels = paint[:n * 2, :n * 2]
        # In integers, a 2x2 block at a time: the same values the float
        # version rounded to, several times quicker (this runs three times
        # for every chunk a stamp touches, on the frame that shows it).
        alpha = texels[..., 3].astype(np.uint32)
        weight = (alpha[0::2, 0::2] + alpha[0::2, 1::2]
                  + alpha[1::2, 0::2] + alpha[1::2, 1::2])
        out = np.empty((n, n, 4), dtype=np.uint8)
        if not weight.any():
            out[:] = 0
            return out
        divisor = np.maximum(weight, 1)
        for channel in range(3):
            scaled = texels[..., channel] * alpha
            total = (scaled[0::2, 0::2] + scaled[0::2, 1::2]
                     + scaled[1::2, 0::2] + scaled[1::2, 1::2])
            out[..., channel] = (2 * total + divisor) // (2 * divisor)
        out[..., 3] = (weight + 2) >> 2
        return out

    def _upload_paint_chunk(self, slot: int):
        table = self.frame.table
        self._sync_gl_columns()
        slot = int(slot)
        coord = (int(table.coord[slot, 0]), int(table.coord[slot, 1]))
        paint = self.frame.paint_display.get(coord)
        if paint is None:
            paint = self.frame.paint_maps.get(coord)
        if paint is None:
            self._paint_gpu_version[slot] = 0
            self._release_paint_layer(slot)
            return
        if not self._paint_page_layers:
            self._ensure_paint_page(0)
        number = self._paint_layer.get(slot)
        if number is None:
            if self._paint_layers_free:
                number = self._paint_layers_free.pop()
            else:
                number = self._paint_layers_used
                self._paint_layers_used += 1
            self._paint_layer[slot] = number
        page, layer = divmod(number, self._paint_page_layers)
        tex = self._ensure_paint_page(page)
        data = np.ascontiguousarray(paint, dtype=np.uint8)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        side = int(self.frame.paint_resolution)
        for level in range(self.PAINT_MIP_LEVELS + 1):
            if side >> level < 1:
                break
            if level:
                data = np.ascontiguousarray(self._paint_mip(data))
            gl.glTexSubImage3D(
                gl.GL_TEXTURE_2D_ARRAY, level, 0, 0, layer,
                side >> level, side >> level, 1,
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
                gl_state.glDeleteVertexArrays(1, [int(self._grass_vao[slot])])
            if self._grass_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._grass_vbo[slot])])
            self._grass_vao[slot] = self._grass_vbo[slot] = 0
            self._grass_count[slot] = 0
            self._grass_version[slot] = 0
            if self._block_vao[slot]:
                gl_state.glDeleteVertexArrays(1, [int(self._block_vao[slot])])
            if self._block_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._block_vbo[slot])])
            self._block_vao[slot] = self._block_vbo[slot] = 0
            self._block_count[slot] = 0
            self._block_version[slot] = 0
            self._paint_gpu_version[slot] = 0
            self._release_paint_layer(slot)
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
        gl_state.glBindVertexArray(int(self._block_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._block_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, max(data.nbytes, 4),
                        data if data.nbytes else None, gl.GL_STATIC_DRAW)
        stride = 14 * 4
        for loc, size, offset in ((0, 3, 0), (1, 3, 12), (2, 3, 24), (3, 2, 36), (4, 3, 44)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
        gl_state.glBindVertexArray(0)
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
        gl_state.glBindVertexArray(int(self._grass_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._grass_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STATIC_DRAW)
        stride = 9 * 4
        for loc, size, offset in ((6, 3, 24), (2, 3, 0), (3, 1, 12), (4, 1, 16), (5, 1, 20)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
            gl.glVertexAttribDivisor(loc, 1)
        gl_state.glBindVertexArray(0)
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
               env_uniforms=None, shadow_filter=None):
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
                         env_uniforms, shadow_filter)
        finally:
            self.frame = None

    def _render(self, projection, view, camera_pos, frustum_planes,
                active_lights_count, shadow_cubemaps, shadow_unit_base,
                env_uniforms, shadow_filter=None):
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
        names += ('fioCookies', 'uShadowFilter[0]')
        for name in names:
            if name not in u:
                u[name] = gl.glGetUniformLocation(prog, name)

        table = frame.table
        self._adopt(table)
        if self._paint_epoch != frame.paint_epoch:
            self._paint_gpu_version[:] = 0
            self._release_paint_layers()
            self._paint_epoch = frame.paint_epoch

        gl_state.glUseProgram(prog)
        uniform_matrix4(u['projection'], glm.value_ptr(projection))
        uniform_matrix4(u['view'], glm.value_ptr(view))
        gl_state.glActiveTexture(gl.GL_TEXTURE0); gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.grass_tex); gl_state.glUniform1i(u['texGrass'], 0)
        gl_state.glActiveTexture(gl.GL_TEXTURE1); gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.rock_tex);  gl_state.glUniform1i(u['texRock'],  1)
        gl_state.glActiveTexture(gl.GL_TEXTURE2); gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.sand_tex);  gl_state.glUniform1i(u['texSand'],  2)
        gl_state.glActiveTexture(gl.GL_TEXTURE3); gl_state.glBindTexture(gl.GL_TEXTURE_2D, self.snow_tex);  gl_state.glUniform1i(u['texSnow'],  3)
        height_texture_unit = shadow_unit_base + shaders.MAX_SHADOW_LIGHTS
        paint_texture_unit = height_texture_unit + 1
        self._upload_appearance_uniforms(u)
        loc = u.get('terrainPaintMap', -1)
        if loc != -1:
            gl_state.glUniform1i(loc, paint_texture_unit)

        # Textures off in flat mode, or while they are not loaded.
        textures_loaded = (self.grass_tex != 0 and self.rock_tex != 0
                           and self.sand_tex != 0 and self.snow_tex != 0)
        use_tex = (0 if frame.flat_mode or not textures_loaded
                   or getattr(self, 'untextured', False)
                   else (1 if frame.use_textures else 0))
        gl_state.glUniform1i(u['use_textures'], use_tex)

        # Distance fog + global ambient (engine.shaders.FOG_GLSL): this
        # program has its own uniform table, so the renderer hands the
        # resolved values across rather than writing them itself.
        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc is None or loc == -1:
                    continue
                if isinstance(value, int):
                    gl_state.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl_state.glUniform1f(loc, value)
                else:
                    gl_state.glUniform3f(loc, *value)

        # The renderer has already selected the terrain light subset and
        # packed it into the shared std140 light UBO.
        gl_state.glUniform1i(u['active_lights'], active_lights_count)

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
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + shadow_unit_base + i)
            gl_state.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
            gl_state.glUniform1i(loc, shadow_unit_base + i)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        # Each shadow slot's filter, from the Light that owns it (0: the
        # shader's default), as the other lit passes have it.
        loc = u.get('uShadowFilter[0]', -1)
        if loc is not None and loc != -1:
            values = (shadow_filter if shadow_filter is not None
                      else np.zeros((shaders.MAX_SHADOW_LIGHTS, 2), dtype=np.float32))
            gl_state.glUniform2fv(loc, shaders.MAX_SHADOW_LIGHTS,
                            np.ascontiguousarray(values, dtype=np.float32))
        # The light-cookie array (engine.light_cookies) on its own unit, for
        # the same reason; the renderer binds the array there when a light
        # has a cookie, and nothing is sampled when none has.
        loc = u.get('fioCookies', -1)
        if loc is not None and loc != -1:
            gl_state.glUniform1i(loc, shaders.COOKIE_TEXTURE_UNIT)

        lod_level_loc = u.get('lod_level', -1)
        slots = table.live_slots()
        # The frustum (far plane included, so the view distance applies)
        # decides what is drawn; which chunks exist and at what detail was
        # decided by the terrain, before the frame.
        visible = table.visible(slots, frustum_planes)
        self.culled_chunks = int(len(slots) - np.count_nonzero(visible))

        if frame.wireframe: gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
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
        self.skirt_sides = 0
        if not blocks and frame.skirt is not None:
            self.skirt_sides = self._draw_skirts(
                slots[table.built[slots]], projection, view, env_uniforms, unit,
                frustum_planes, paint_texture_unit, use_tex)
        if frame.wireframe: gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

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
                gl_state.glDeleteProgram(program)
            except Exception:
                pass
        self._own_programs.clear()
        if self.grass_shader_program:
            try:
                gl_state.glDeleteProgram(self.grass_shader_program)
            except Exception:
                pass
        self.shader_program = self.block_program = self.grass_shader_program = 0
        self.skirt_program = 0
        self.uniforms, self.block_uniforms, self.grass_uniforms = {}, {}, {}
        self.skirt_uniforms = {}
        if self._skirt_vao:
            try:
                gl_state.glDeleteVertexArrays(1, [self._skirt_vao])
            except Exception:
                pass
            self._skirt_vao = 0
        for pages in (self._paint_pages, self._height_pages):
            if pages:
                try:
                    gl_state.glDeleteTextures(len(pages), pages)
                except Exception:
                    pass
        self._paint_pages, self._height_pages = [], []
        self._paint_page_layers = 0
        self._release_paint_layers()
        for vao, buffer in self._index_vaos.values():
            gl_state.glDeleteVertexArrays(1, [vao])
            gl.glDeleteBuffers(1, [buffer])
        self._index_vaos = {}
        if self._placeholder_cubemap:
            gl_state.glDeleteTextures(1, [self._placeholder_cubemap])
            self._placeholder_cubemap = 0
        self.grass_tex = self.rock_tex = self.sand_tex = self.snow_tex = 0
        self._sync_gl_columns_reset()
