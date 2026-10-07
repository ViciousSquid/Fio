"""RendererCore: optional, reusable infrastructure for renderer implementations.

A renderer satisfies :class:`engine.renderer.api.Renderer` by implementing the
protocol; it never has to inherit from this class. ``RendererCore`` exists for
implementations that want Fio's renderer-independent pieces:

* texture, model, effect-frame and shader-loading resources (``resources``)
* the cube and sprite primitives and the angled-brush mesh cache (``geometry``)
* dense-table lookups: transforms, texture ids, sprite recipes (``tables``)
* culling, pass classification, light/shadow-caster selection and portal
  camera maths over the tables (``visibility``)
* editor and debug overlays (``overlays``)

None of it assumes a lighting model or pass structure. A subclass calls
``RendererCore.__init__`` first, then, with its GL context current, compiles
the overlay program (:meth:`_compile_overlay_shader`) and creates the shared
primitives (``vaos['cube']``, ``vaos['sprite']``, :meth:`_create_gizmo_buffers`)
at the point in its own start-up where it wants them.
"""

import glm
import numpy as np
import OpenGL.GL as gl

from engine.view_distance import ViewDistance
from ..api import RenderStats
from .geometry import GeometryMixin
from .overlays import OverlaysMixin
from .resources import ResourcesMixin, ShaderLoader
from .tables import TablesMixin
from .visibility import VisibilityMixin


class RendererCore(ResourcesMixin, GeometryMixin, TablesMixin, VisibilityMixin,
                   OverlaysMixin):
    """Renderer-independent state and helpers; see the module docstring."""

    def __init__(self, texture_loader=None):
        self.texture_manager = {}
        self.loaded_models = {}
        #: model path -> perf_counter() of its last failed load.
        self._failed_models = {}


        #: Resolves a brush texture name the cache has not seen. Defaults to
        #: this renderer's own loader; the GL tests substitute a fixed texture.
        self.load_texture_callback = (
            texture_loader if texture_loader is not None else self.load_texture)
        self._identity_mat4 = glm.mat4(1.0)
        self._identity_mat3 = glm.mat3(1.0)
        self.render_stats = RenderStats()

        # How far this camera draws, and the fog that hides its far plane.
        # A renderer/camera setting, never a world-streaming one: it changes
        # what is on screen and nothing about what is loaded or simulated.
        # The viewport replaces this with the instance it shares with the
        # editor spinbox and the console, so a change there reaches the next
        # frame with no rebuild -- see QtGameView._sync_view_distance.
        self.view_distance = ViewDistance()
        # Camera position for the current frame, cached by render_scene so the
        # passes that do not receive one (sprites) can still fog correctly.
        self._frame_camera_pos = (0.0, 0.0, 0.0)
        self._sprite_gl_by_id = np.zeros(0, dtype=np.int32)
        self._sprite_gl_resolved = 0
        self._sprite_recipes_seen = None
        #: Mesh bounding radius per interned model recipe (entity frustum cull).
        self._model_radius_recipes_seen = None
        self._model_radius_by_recipe = np.zeros(0, dtype=np.float64)
        # Decoded animated Effect GIF frames, indexed by dense variant.
        # FIRE and ORB share this normal-instanced billboard path.
        self.effect_fire_frames = {}
        self.effect_fire_cumulative = {}
        self.effect_orb_frames = {}
        self.effect_orb_cumulative = {}
        # Reusable model/normal matrix buffers for the batched transform build.
        self._brush_mat_buf = np.empty((0, 16), dtype=np.float32)
        self._brush_nmat_buf = np.empty((0, 9), dtype=np.float32)


        # PERF: cache of texture-name -> "textures/<name>" cache-key path.
        # draw_textured_brushes_optimized resolves this for every drawn face
        # every frame in play mode; os.path.join is comparatively expensive,
        # so memoize the join per unique texture name.
        self._tex_path_cache = {}
        # Dense texture ids are local to a RenderTable. With double-buffered
        # projections the two tables may have discovered different names first,
        # so a single name_id -> GL-id array is no longer a valid cache boundary.
        # Cache the resolved arrays per table; each array still grows only when
        # that table interns a new name.
        self._gl_tex_by_table = {}
        self._tex_size_by_table = {}

        # VAOs and buffers (initialised after shaders compile)
        self.vaos = {'cube': None, 'sprite': None, 'grid': None}
        self.grid_indices_count = 0
        self.sprite_textures = {}
        self._edge_vao = None
        self._edge_vbo = None
        self._gizmo_lines_vbo = None
        self._gizmo_cone_vbo = None
        self._portal_outline_vao = None
        self._portal_outline_vbo = None
        self._portal_normal_vao = None
        self._portal_normal_vbo = None
        self._conn_line_vao = None
        self._conn_line_vbo = None
        self.face_highlight_vao = None
        self.face_highlight_vbo = None
        # Component-edit handle overlay (editor only).  The buffer is refilled
        # only when the editor's overlay version changes, never per frame.
        self._component_overlay_vao = None
        self._component_overlay_vbo = None
        self.vao_gizmo_lines = None
        self.vao_gizmo_cone = None
        self._aabb_vao = None
        self._aabb_vbo = None
        self._component_overlay_data = None
        self._component_overlay_counts = None
        self._component_overlay_version = None
        self._component_overlay_dirty = False
        # Driver limits for wide lines / big points, queried once on first use
        # (they need a live context, and glGetFloatv stalls the pipeline).
        self._line_width_range = None
        self._point_size_range = None
        self._cube_vbo = None
        self._sprite_vbo = None
        self._grid_vbo = None

        # Convex geometry meshes, owned by geometry signature. The signature
        # carries the brush's geometry epoch, which every change to its shape
        # or face mapping bumps, so it names the mesh's content exactly -- and
        # it survives a table reconcile and is the same in both render
        # buffers' tables. Keying by (table generation, geometry id) rebuilt
        # every convex mesh, once per buffer, after any structural edit.
        # Meshes are dropped after going unused for a while (_begin_geo_frame).
        self._geo_mesh_cache = {}
        #: id(GeometryRecord) -> mesh: the per-frame lookup, which does not
        #: hash the signature. Validated against the record's signature, so a
        #: recycled id cannot alias; cleared with each stale-mesh sweep.
        self._geo_mesh_by_record = {}
        self._geo_mesh_frame = 0

        self._shader_init_failed = False

        # Shaders (will be filled by subclasses or base helpers)
        self.shaders = {}
        self.uniforms = {}
        # Cached inverse of the main projection matrix, reused across every
        # oblique-clip computation in a frame (the projection is constant, only
        # the per-portal view changes).
        self._portal_proj_inv_sig = None
        self._portal_proj_inv = None

        self.shader_loader = ShaderLoader()

    @property
    def ready(self):
        """True while the renderer initialised its programs and can draw."""
        return not self._shader_init_failed

    def cleanup(self):
        """Release the GL resources RendererCore owns, including every
        program and VAO a subclass registered in ``shaders`` / ``vaos``."""
        # Delete VAOs and VBOs
        for name, vao in self.vaos.items():
            if vao:
                gl.glDeleteVertexArrays(1, [vao])
        if self._edge_vao:
            gl.glDeleteVertexArrays(1, [self._edge_vao])
        if self._edge_vbo:
            gl.glDeleteBuffers(1, [self._edge_vbo])
        if self._gizmo_lines_vbo:
            gl.glDeleteBuffers(1, [self._gizmo_lines_vbo])
        if self._gizmo_cone_vbo:
            gl.glDeleteBuffers(1, [self._gizmo_cone_vbo])
        for vao in (self.vao_gizmo_lines, self.vao_gizmo_cone,
                    self._aabb_vao, self._component_overlay_vao):
            if vao:
                gl.glDeleteVertexArrays(1, [vao])
        for vbo in (self._aabb_vbo, self._component_overlay_vbo):
            if vbo:
                gl.glDeleteBuffers(1, [vbo])
        self.vao_gizmo_lines = self.vao_gizmo_cone = None
        self._aabb_vao = self._aabb_vbo = None
        self._component_overlay_vao = self._component_overlay_vbo = None
        if self._portal_outline_vao:
            gl.glDeleteVertexArrays(1, [self._portal_outline_vao])
        if self._portal_outline_vbo:
            gl.glDeleteBuffers(1, [self._portal_outline_vbo])
        if self._portal_normal_vao:
            gl.glDeleteVertexArrays(1, [self._portal_normal_vao])
        if self._portal_normal_vbo:
            gl.glDeleteBuffers(1, [self._portal_normal_vbo])
        if self._conn_line_vao:
            gl.glDeleteVertexArrays(1, [self._conn_line_vao])
        if self._conn_line_vbo:
            gl.glDeleteBuffers(1, [self._conn_line_vbo])
        if self.face_highlight_vao:
            gl.glDeleteVertexArrays(1, [self.face_highlight_vao])
        if self.face_highlight_vbo:
            gl.glDeleteBuffers(1, [self.face_highlight_vbo])
        for mesh in self._geo_mesh_cache.values():
            self._delete_geo_mesh(mesh)
        self._geo_mesh_cache.clear()
        self._geo_mesh_by_record.clear()

        if self._cube_vbo:
            gl.glDeleteBuffers(1, [self._cube_vbo])
        if self._sprite_vbo:
            gl.glDeleteBuffers(1, [self._sprite_vbo])
        if self._grid_vbo:
            gl.glDeleteBuffers(1, [self._grid_vbo])
        # Every texture and model this renderer loaded. A retired renderer's
        # resources are nobody's: the host reloads its sprite table through
        # the renderer that replaces this one (QtGameView.switch_renderer).
        textures = sorted({int(t) for t in self.texture_manager.values() if t})
        if textures:
            gl.glDeleteTextures(textures)
        self.texture_manager.clear()
        for model in {id(m): m for m in self.loaded_models.values()}.values():
            model.cleanup()
        self.loaded_models.clear()
        for prog in self.shaders.values():
            if prog:
                try:
                    gl.glDeleteProgram(prog)
                except Exception:
                    pass
        self.shaders.clear()
        self.uniforms.clear()
        self._shader_init_failed = True
        print("[Renderer] Cleaned up GL resources.")
