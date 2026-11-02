"""
Hardware occlusion culling over the cell groups -- the GPU half of Fio's
visibility pipeline.

The logic thread's frustum cull answers *is it in the pyramid?* and the
distance cull answers *is it in range?* -- neither knows that a wall stands in
front. This mixin asks the GPU the one question only it can answer cheaply:
*did any pixel of this cell's bounding box survive the depth test?* One probe
per visible 512-unit cell group, drawn after the frame's opaque geometry and
answered a frame or two later.

The shape of the feature follows from that latency:

* **Cell granularity, not brushes.** The instanced brush passes exist to
  collapse thousands of brushes into a handful of texture-run draws; a query
  per brush would hand those draw calls back. A cell's group box (the union
  of the brushes centred in it, from :mod:`engine.cell_cull`) contains every
  brush it covers, so a probe box that fails the depth test at every pixel
  means each brush inside it -- being inside the box along the eye ray --
  fails it too. One query then tells the truth about an entire cell, and the
  probe count is a few dozen rather than thousands.

* **Chained, never blocking.** This frame's probes are issued at the end of
  it, against its finished opaque depth, and polled (``GL_QUERY_RESULT_
  AVAILABLE`` only, never a forcing read) at the start of the next; a cell is
  skipped only once :data:`engine.cell_cull.OCCLUDE_AFTER` consecutive
  completed probes say "zero samples" and its newest answer is still fresh.
  A slow GPU therefore means *no* culling, never stale culling, and no frame
  ever waits on another.

* **Fail-open.** Play mode's main view only -- never the editor camera, a
  portal's virtual view, a shadow map or splitscreen. Cells within
  :data:`NEAR_EXEMPT` of the eye are never culled, a fast camera turn
  suspends the *use* of results for a frame while new probes catch up, and
  only the :data:`MAX_PROBES` nearest groups are even asked. When anything
  is in doubt the answer is *draw it*: the feature's failure direction is a
  wasted draw, never a hole in the world.

Nothing is baked or stored in the map. The groups are this frame's surviving
slots re-derived per call, so an edit, a mover or a cell streamed in by the
Big World plugin is simply part of the next frame's probe set; the probe
bookkeeping (``OcclusionProbeBook``) is plain per-cell integers with no GL,
so its rules are unit-testable headlessly in ``tests/renderer/``.

The probes change no pixel: colour writes, depth writes and the stencil test
are all off, both cube faces are drawn so the cull state a brush pass left
behind cannot matter, and the depth test is the whole point. The renderer
contract lets a pass set and restore its own state, and this one restores
exactly what the tail of ``render_scene`` expects to find.
"""

from __future__ import annotations

import ctypes

import glm
import numpy as np
import OpenGL.GL as gl

from engine import cell_cull
from engine.cell_cull import OcclusionProbeBook

# 36 vertices of a unit cube centred on the origin. Winding is irrelevant --
# face culling is disabled for the probe pass -- so this is simply the plain
# two-triangles-per-face cube, scaled to each cell's group box at draw time.
_PROBE_VERTS = np.array([
    # +x face
    0.5, -0.5, -0.5,   0.5, 0.5, -0.5,   0.5, 0.5, 0.5,
    0.5, -0.5, -0.5,   0.5, 0.5, 0.5,    0.5, -0.5, 0.5,
    # -x face
    -0.5, -0.5, -0.5,  -0.5, 0.5, 0.5,   -0.5, 0.5, -0.5,
    -0.5, -0.5, -0.5,  -0.5, -0.5, 0.5,  -0.5, 0.5, 0.5,
    # +y face
    -0.5, 0.5, -0.5,   0.5, 0.5, 0.5,    -0.5, 0.5, 0.5,
    -0.5, 0.5, -0.5,   0.5, 0.5, -0.5,   0.5, 0.5, 0.5,
    # -y face
    -0.5, -0.5, -0.5,  -0.5, -0.5, 0.5,  0.5, -0.5, 0.5,
    -0.5, -0.5, -0.5,  0.5, -0.5, 0.5,   0.5, -0.5, -0.5,
    # +z face
    -0.5, -0.5, 0.5,   0.5, 0.5, 0.5,    -0.5, 0.5, 0.5,
    -0.5, -0.5, 0.5,   0.5, -0.5, 0.5,   0.5, 0.5, 0.5,
    # -z face
    -0.5, -0.5, -0.5,  0.5, 0.5, -0.5,   -0.5, 0.5, -0.5,
    -0.5, -0.5, -0.5,  0.5, -0.5, -0.5,  0.5, 0.5, -0.5,
], dtype=np.float32).reshape(-1, 3)

_PROBE_VERTEX = """#version 330 core
// One box, one matrix: the probe's whole job is to be rasterised against the
// frame's existing depth, and its colour is discarded by the colour mask.
uniform mat4 uProjView;
uniform mat4 uModel;
layout(location = 0) in vec3 aPos;
void main()
{
    gl_Position = uProjView * (uModel * vec4(aPos, 1.0));
}
"""

_PROBE_FRAGMENT = """#version 330 core
// The fragment shader exists only because OpenGL 3.3 core wants one; the
// query counter, not the framebuffer, is the probe's output.
out vec4 FragColor;
void main()
{
    FragColor = vec4(0.0);
}
"""


class OcclusionMixin:
    """ForwardRenderer's chained occlusion queries over the cell groups.

    Hooked into ``render_scene`` twice: once after the distance cull, where
    cell groups the GPU has verified hidden are dropped from the slot list
    before anything classifies, sorts or packs them, and once after the fog
    volumes, where the frame's finished opaque depth is used to issue the
    next frame's probes. Nothing outside this file needs to know either hook
    is there: with the feature off (or unavailable on the host's GL), both
    hooks return the slots untouched.
    """

    #: At most this many cell groups are probed per frame, nearest first --
    #: a hard ceiling on the extra draw work the feature itself adds.
    MAX_PROBES = 48
    #: Groups whose box comes this close to the eye (squared 3D distance)
    #: are never culled: probe latency behind a distant wall is harmless,
    #: the same latency at the player's feet would show a hole.
    NEAR_EXEMPT_SQ = 1024.0 ** 2
    #: A frame-to-frame view turn whose forward vectors differ by more than
    #: this (cosine; ~37 degrees) suspends the *use* of results for a frame.
    MAX_TURN_COS = 0.8
    #: Below this many surviving slots the world on screen is small enough
    #: that grouping it costs more than hiding part of it could save.
    MIN_PROBED_ROWS = 512

    # -- lifetime -----------------------------------------------------

    def _init_occlusion(self, config=None):
        """Compile the probe pass; the feature is off whenever this fails.

        Every attribute is set before any GL call, so a failure here (or a
        ``cleanup`` racing one) finds a consistent, inert feature rather than
        a half-built one. The query objects themselves are created lazily,
        on the render thread, when probes are first issued.
        """
        self.occlusion_culling = True
        if config is not None:
            try:
                self.occlusion_culling = bool(config.getboolean(
                    'Renderer', 'occlusion_culling', fallback=True))
            except Exception:
                pass
        self._occlusion_ready = False
        self._occlusion_frame = 0
        self._occlusion_query_pool = []
        self._occlusion_probes = {}
        self._occlusion_groups = None
        self._occlusion_last_forward = None
        self._occlusion_skipped = 0
        self._occlusion_drawn = 0
        self._occlusion_program = 0
        self._occlusion_vbo = 0
        self.occlusion_book = OcclusionProbeBook()
        self.render_stats.details['occlusion'] = self._describe_occlusion
        try:
            program = self.shader_loader.compile_from_source(
                _PROBE_VERTEX, _PROBE_FRAGMENT)
            if not program:
                return
            self._occlusion_program = program
            # Registered like every other pass's program: RendererCore's
            # cleanup deletes whatever is in ``shaders``.
            self.shaders['occlusion_probe'] = program
            self._occlusion_projview_loc = gl.glGetUniformLocation(
                program, 'uProjView')
            self._occlusion_model_loc = gl.glGetUniformLocation(
                program, 'uModel')

            self._occlusion_vbo = int(gl.glGenBuffers(1))
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._occlusion_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, _PROBE_VERTS.nbytes,
                            _PROBE_VERTS, gl.GL_STATIC_DRAW)
            vao = int(gl.glGenVertexArrays(1))
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._occlusion_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12,
                                     ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
            self.vaos['occlusion_probe'] = vao

            # Conservative when OpenGL 3.3 offers it (it does, in core):
            # the query may report samples that barely failed, never the
            # reverse, which is exactly the fail-open direction wanted here.
            self._occlusion_target = getattr(
                gl, 'GL_ANY_SAMPLES_PASSED_CONSERVATIVE', gl.GL_SAMPLES_PASSED)
            self._occlusion_ready = True
        except Exception as exc:
            self._occlusion_ready = False
            print(f"[Renderer] occlusion culling unavailable: {exc}")

    def _cleanup_occlusion(self):
        """Release the probe pass's query objects and vertex buffer.

        The program and VAO go through ``shaders`` and ``vaos`` like every
        other pass's resources; the queries and the VBO are this mixin's
        alone. Written to cope with a cleanup that runs before
        ``_init_occlusion`` finished: every attribute is reached through a
        default rather than assumed.
        """
        for query in list(getattr(self, '_occlusion_probes', {}).values()):
            try:
                gl.glDeleteQueries(1, [int(query)])
            except Exception:
                pass
        self._occlusion_probes = {}
        for query in list(getattr(self, '_occlusion_query_pool', ())):
            try:
                gl.glDeleteQueries(1, [int(query)])
            except Exception:
                pass
        self._occlusion_query_pool = []
        vbo = int(getattr(self, '_occlusion_vbo', 0) or 0)
        if vbo:
            try:
                gl.glDeleteBuffers(1, [vbo])
            except Exception:
                pass
        self._occlusion_vbo = 0
        self._occlusion_ready = False

    # -- bookkeeping hooks --------------------------------------------

    def _occlusion_active(self, config):
        """The feature runs only where a wrong answer is recoverable.

        Play mode's main view: not the editor camera, not a portal's virtual
        view, not a shadow map pass, not splitscreen. Those paths draw
        different frusta against different state and none of them reaches
        the hooks, but the guard keeps a future call site honest too.
        """
        return (bool(self.occlusion_culling) and self._occlusion_ready
                and bool(config.get('play_mode', False))
                and not config.get('splitscreen_active', False))

    def _describe_occlusion(self):
        """One line for Debug Tables, via ``RenderStats.details``."""
        if not self._occlusion_ready:
            return "unavailable"
        if not self.occlusion_culling:
            return "off"
        total = self._occlusion_skipped + self._occlusion_drawn
        return (f"{self._occlusion_skipped} of {total} brushes skipped; "
                f"{len(self._occlusion_probes)} probes in flight")

    # -- the two render_scene hooks ------------------------------------

    def _occlusion_filter_slots(self, table, slots, config, camera_pos, view):
        """Drop the cell groups the GPU has verified occluded.

        Runs after the distance cull and before pass classification, so
        every brush pass -- opaque, water, glass -- submits the same
        narrowed slot list, and the classification, sorting, packing and
        buffer uploads behind it are skipped wholesale for a hidden cell.
        The grouping is this frame's own answer, derived from the slots that
        survived the earlier culls and never cached, so edits, movers and
        cells streamed in by the Big World plugin are simply part of it.
        """
        self._occlusion_groups = None
        self._occlusion_skipped = 0
        self._occlusion_drawn = 0
        if (camera_pos is None or table.bounds is None
                or len(slots) < self.MIN_PROBED_ROWS
                or not self._occlusion_active(config)):
            return slots

        slots = np.asarray(slots, dtype=np.intp)
        groups = cell_cull.group_rows_by_cell(table.bounds[slots])
        group_bounds, group_of_row, group_cells = groups
        if not len(group_bounds):
            return slots
        # Kept for the end-of-frame hook: the probes are issued against the
        # same grouping the culling used, one frame later.
        self._occlusion_groups = groups

        # A fast turn invalidates every pending answer at once -- they were
        # measured from yesterday's view. Suspend the *use* of results for
        # this frame; the probes below keep running, so the book stays warm.
        forward = (-float(view[0][2]), -float(view[1][2]),
                   -float(view[2][2]))
        last = self._occlusion_last_forward
        self._occlusion_last_forward = forward
        if (last is not None
                and (forward[0] * last[0] + forward[1] * last[1]
                     + forward[2] * last[2]) < self.MAX_TURN_COS):
            return slots

        book = self.occlusion_book
        frame = self._occlusion_frame
        cam = np.asarray(self._camera_xyz(camera_pos), dtype=np.float64)
        # Squared clamped distance from the eye to each group box: how far
        # the box is from the eye when it is not already around it.
        delta = np.maximum(
            np.abs(group_bounds[:, :3] - cam) - group_bounds[:, 3:], 0.0)
        near_sq = np.einsum('ij,ij->i', delta, delta)

        culled_groups = np.zeros(len(group_bounds), dtype=bool)
        for i in range(len(group_bounds)):
            if near_sq[i] < self.NEAR_EXEMPT_SQ:
                continue
            if book.culled(group_cells[i], frame):
                culled_groups[i] = True
        if not culled_groups.any():
            self._occlusion_drawn = len(slots)
            return slots

        keep = ~culled_groups[group_of_row]
        self._occlusion_skipped = int(np.count_nonzero(~keep))
        self._occlusion_drawn = len(slots) - self._occlusion_skipped
        return slots[keep]

    def _occlusion_end_of_frame(self, projection, view, camera_pos, config):
        """Poll last frame's probes, then issue this frame's.

        Called where the frame's opaque depth is finished (after the fog
        volumes, before the render's tail state changes), so a probe box is
        depth-tested against exactly the geometry the brushes were hidden
        behind. Results are read only when the GPU says they are ready --
        never forced -- so this is O(probes) of tiny GL calls, not a stall.
        """
        if not self._occlusion_ready:
            return
        self._occlusion_frame += 1
        frame = self._occlusion_frame

        # The cells the camera can currently reach: this frame's grouping,
        # or nothing when the feature did not run (out of play, an empty
        # world, splitscreen) -- in which case retiring everything is the
        # correct reset, not a loss.
        groups = self._occlusion_groups
        candidates = set()
        if groups is not None:
            candidates = {(int(c[0]), int(c[1])) for c in groups[2]}

        # Poll the outstanding probes. An answer for a cell that has since
        # left the candidate set is discarded unread -- it describes a view
        # the camera can no longer have.
        for coord in list(self._occlusion_probes):
            query = self._occlusion_probes[coord]
            if not self._query_object_uiv(query, gl.GL_QUERY_RESULT_AVAILABLE):
                continue
            samples = self._query_object_uiv(query, gl.GL_QUERY_RESULT)
            del self._occlusion_probes[coord]
            self._occlusion_query_pool.append(query)
            if coord in candidates:
                self.occlusion_book.record(coord, samples, frame)
        self.occlusion_book.retire(candidates)

        if groups is None or not self._occlusion_active(config):
            return
        group_bounds, _group_of_row, group_cells = groups
        if not len(group_bounds) or camera_pos is None:
            return

        # Probe budget, nearest groups first: a distant cell hidden behind a
        # wall saves the same submission work as a near one, and the near
        # ones are the first to become visible, so they are probed first.
        cam = np.asarray(self._camera_xyz(camera_pos), dtype=np.float64)
        flat = group_bounds[:, :2] - cam[:2]
        dist_sq = np.einsum('ij,ij->i', flat, flat)
        order = np.argsort(dist_sq)[: self.MAX_PROBES]

        proj_view = projection * view
        gl.glUseProgram(self._occlusion_program)
        gl.glUniformMatrix4fv(self._occlusion_projview_loc, 1, gl.GL_FALSE,
                             glm.value_ptr(proj_view))
        # The probes change no pixel. Colour writes, depth writes and the
        # stencil test are off; both cube faces are drawn so the cull state
        # a brush pass left behind cannot matter; the depth test -- against
        # the frame's finished opaque depth -- is the whole point.
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glBindVertexArray(self.vaos['occlusion_probe'])
        for i in order:
            coord = (int(group_cells[i, 0]), int(group_cells[i, 1]))
            if coord in self._occlusion_probes:
                continue  # one outstanding query per cell
            query = (self._occlusion_query_pool.pop()
                     if self._occlusion_query_pool
                     else int(gl.glGenQueries(1)))
            gl.glBeginQuery(self._occlusion_target, query)
            cx, cy, cz = group_bounds[i, :3]
            hx, hy, hz = group_bounds[i, 3:]
            model = glm.translate(glm.mat4(1.0), glm.vec3(cx, cy, cz))
            model = glm.scale(model, glm.vec3(hx, hy, hz))
            gl.glUniformMatrix4fv(self._occlusion_model_loc, 1, gl.GL_FALSE,
                                 glm.value_ptr(model))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            gl.glEndQuery(self._occlusion_target)
            self._occlusion_probes[coord] = query
        # Restore what the tail of render_scene expects to find: the very
        # next lines re-enable the depth mask and disable the depth test.
        gl.glBindVertexArray(0)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glUseProgram(0)

    @staticmethod
    def _query_object_uiv(query, pname):
        """Read a query object's uint value, whichever shape PyOpenGL chose.

        PyOpenGL returns a bare int for these getters on some paths and a
        one-element array on others (notably under the GEOMETRIC size hints
        the engine enables); both mean the same number here.
        """
        value = gl.glGetQueryObjectuiv(int(query), pname)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(np.asarray(value).reshape(-1)[0])
