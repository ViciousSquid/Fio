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
* **Self-funding.** The grouping and the probes are not free -- on the
  low-power ARM target every probe is a draw call through a driver where
  draw calls cost a measurable share of the frame. A rolling window of
  what the probes actually removed shrinks the per-frame budget when the
  savings do not cover the cost, parks the feature when they cover none,
  and re-arms after a cooldown to re-sample a world that may have changed
  (:class:`engine.cell_cull.ProbeGovernor`).

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
import time
import weakref

import glm
import numpy as np
import OpenGL.GL as gl

from engine import cell_cull
from engine.cell_cull import (
    PROBE_KEEP_FRACTION, OcclusionProbeBook, ProbeGovernor,
)

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

    #: Hard ceiling on additional probe draws *and* outstanding queries.
    #: Keep this deliberately small even on fast GPUs: the extra PyOpenGL
    #: submission calls are not free, particularly on low-power hardware.
    MAX_PROBES = 8
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
    # A tiny group cannot pay for its own GL query on the Adreno driver.
    MIN_PROBE_GROUP_ROWS = 16

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
        self._occlusion_probe_meta = {}
        self._occlusion_box_by_cell = {}
        self._occlusion_groups = None
        self._occlusion_near_sq = None
        self._occlusion_group_counts = None
        self._occlusion_cpu_filter_ms = 0.0
        self._occlusion_rows_sampled = 0
        self._occlusion_cpu_index_ms = 0.0
        self._occlusion_cpu_query_ms = 0.0
        self._occlusion_cpu_probe_ms = 0.0
        self._occlusion_index_rebuilds = 0
        #: id(RenderTable) -> [weakref, generation, CellGroupIndex, the
        #: table's row_change_serial last applied].
        self._occlusion_by_table = {}
        self._occlusion_view_epoch = 0
        self._occlusion_last_eye = None
        self._occlusion_last_forward = None
        self._occlusion_skipped = 0
        self._occlusion_drawn = 0
        self._occlusion_issued = 0
        self._occlusion_program = 0
        self._occlusion_vbo = 0
        self.occlusion_book = OcclusionProbeBook()
        # The Snapdragon-class design target: there a probe is a draw call
        # through a driver where draw calls cost a measurable share of the
        # frame, so the budget starts tighter and the bar for staying on is
        # higher. Everywhere else the ceiling and the default bar apply.
        low_power = False
        try:
            from engine import shaders
            low_power = shaders.detect_low_power_arm()[0]
        except Exception:
            pass
        self.occlusion_governor = ProbeGovernor(
            max_probes=4 if low_power else self.MAX_PROBES,
            min_budget=2,
            keep_fraction=0.15 if low_power else PROBE_KEEP_FRACTION,
            # 1% of a 60fps CPU frame on SQ3; only a conservative
            # governor guardrail, real break-even is measured by --off/on.
            max_cpu_ms=0.167 if low_power else 0.25)
        self._occlusion_budget = 0
        # Larger early-out on Snapdragon-class machines: an O(N log N)
        # grouping pass is not justified for a few hundred visible rows.
        self._occlusion_min_rows = 1024 if low_power else self.MIN_PROBED_ROWS
        self._occlusion_min_group_rows = 32 if low_power else self.MIN_PROBE_GROUP_ROWS
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

            # The query target is asked of the live context, not assumed:
            # GL_ANY_SAMPLES_PASSED_CONSERVATIVE is desktop 4.3
            # (ARB_ES3_compatibility), so a 3.3 core context rejects it with
            # INVALID_ENUM -- and a query whose glBeginQuery was refused
            # never completes. The feature would sit "ready" while culling
            # nothing, with every issued query pending forever. The strongest
            # target the context accepts is used; none means the feature
            # stays off rather than keying on answers that never arrive.
            self._occlusion_target = self._pick_query_target()
            if self._occlusion_target is None:
                print("[Renderer] occlusion culling unavailable: the context "
                      "accepts no occlusion query target")
                return
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
        self._occlusion_probe_meta = {}
        self._occlusion_box_by_cell = {}
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
        governor = getattr(self, 'occlusion_governor', None)
        state = f"; {governor.state()}" if governor is not None else ''
        return (f"{self._occlusion_skipped} of {total} brushes skipped; "
                f"{self._occlusion_issued} probes issued, "
                f"{len(self._occlusion_probes)} pending; "
                f"CPU filter {self._occlusion_cpu_filter_ms:.3f}ms, "
                f"GPU query calls {self._occlusion_cpu_query_ms:.3f}ms, "
                f"probe submission {self._occlusion_cpu_probe_ms:.3f}ms; "
                f"index {self._occlusion_cpu_index_ms:.3f}ms{state}")

    # -- the two render_scene hooks ------------------------------------

    def _occlusion_filter_slots(self, table, slots, config, camera_pos, view):
        """Measure the actual cost of the cell-filter pass, including index work."""
        if not self.occlusion_culling:
            self._occlusion_cpu_filter_ms = 0.0
            self._occlusion_cpu_index_ms = 0.0
            self._occlusion_rows_sampled = 0
            return slots
        t0 = time.perf_counter_ns()
        try:
            return self._occlusion_filter_slots_impl(
                table, slots, config, camera_pos, view)
        finally:
            self._occlusion_cpu_filter_ms = (
                time.perf_counter_ns() - t0) * 1e-6
            if self._occlusion_rows_sampled:
                # Probe submission and result polling are measured by the
                # preceding completed frame. Give the governor real CPU
                # overhead, not merely the percentage of skipped brushes.
                self.occlusion_governor.record(
                    self._occlusion_rows_sampled, self._occlusion_skipped,
                    overhead_ms=(self._occlusion_cpu_filter_ms
                                 + self._occlusion_cpu_query_ms
                                 + self._occlusion_cpu_probe_ms))

    def _occlusion_filter_slots_impl(self, table, slots, config, camera_pos, view):
        """Drop the cell groups the GPU has verified occluded.

        Runs after the distance cull and before pass classification, so
        every brush pass -- opaque, water, glass -- submits the same
        narrowed slot list, and the classification, sorting, packing and
        buffer uploads behind it are skipped wholesale for a hidden cell.
        Cell membership and union boxes come from the RenderTable's
        journal-driven index. This frame only gathers already computed dense
        group IDs from surviving slots and counts them via NumPy.
        """
        self._occlusion_groups = None
        self._occlusion_near_sq = None
        self._occlusion_group_counts = None
        self._occlusion_rows_sampled = 0
        self._occlusion_cpu_index_ms = 0.0
        self._occlusion_skipped = 0
        self._occlusion_drawn = 0
        if (camera_pos is None or table.bounds is None
                or len(slots) < self._occlusion_min_rows
                or not self._occlusion_active(config)):
            return slots
        budget = self.occlusion_governor.probe_count()
        if budget <= 0:
            # Parked by the governor: the last windows measured the probes
            # saving less than they cost, so this frame pays neither the
            # grouping nor the probe draws; the cooldown re-arms a small
            # budget to re-sample a world that may have changed.
            return slots
        self._occlusion_budget = budget

        # One index per published RenderTable buffer, kept here and found by
        # the table's identity; the table only reports which rows it has
        # refreshed since (row_changes_since), so a frame applies those and
        # needs no per-frame group rebuild. Nothing is written on the table.
        t_index = time.perf_counter_ns()
        held = self._occlusion_by_table.get(id(table))
        if held is None or held[0]() is not table or held[1] != table.generation:
            index = cell_cull.CellGroupIndex(table)
            self._occlusion_by_table = {
                key: entry for key, entry in self._occlusion_by_table.items()
                if entry[0]() is not None}
            self._occlusion_by_table[id(table)] = [
                weakref.ref(table), table.generation, index, table.row_change_serial]
            self._occlusion_index_rebuilds += 1
        else:
            index = held[2]
            changed = table.row_changes_since(held[3])
            if changed is None:
                index.rebuild(table)
            elif len(changed):
                index.update(table, changed.tolist())
            held[3] = table.row_change_serial
        slots = np.asarray(slots, dtype=np.intp)
        visible = index.visible(slots)
        self._occlusion_cpu_index_ms = (
            time.perf_counter_ns() - t_index) * 1e-6
        if visible is None:
            return slots
        eligible, group_bounds, group_of_row, group_cells, counts, signatures = visible
        safe_count = int(np.count_nonzero(eligible))
        if safe_count < self._occlusion_min_rows:
            return slots
        self._occlusion_rows_sampled = safe_count
        groups = (group_bounds, group_of_row, group_cells)
        # A signature is built only when a cell's published rows change.
        # Cache-hit frames reuse these immutable tuples; no per-frame
        # Python walk over every brush to calculate bounding boxes.
        book = self.occlusion_book
        current_boxes = {
            (int(c[0]), int(c[1])): signature
            for c, signature in zip(group_cells, signatures)
        }
        previous_boxes = self._occlusion_box_by_cell
        for coord, signature in current_boxes.items():
            if previous_boxes.get(coord) != signature:
                book.forget(coord)
        self._occlusion_box_by_cell = current_boxes
        self._occlusion_groups = groups
        self._occlusion_group_counts = counts

        cam = np.asarray(self._camera_xyz(camera_pos), dtype=np.float64)
        delta = np.maximum(
            np.abs(group_bounds[:, :3] - cam) - group_bounds[:, 3:], 0.0)
        near_sq = np.einsum('ij,ij->i', delta, delta)
        self._occlusion_near_sq = near_sq

        # A teleport or fast turn makes previous depth answers unsafe.
        # Invalidate them and discard pending queries from that old view.
        forward = (-float(view[0][2]), -float(view[1][2]),
                   -float(view[2][2]))
        last_forward = self._occlusion_last_forward
        last_eye = self._occlusion_last_eye
        self._occlusion_last_forward = forward
        self._occlusion_last_eye = cam.copy()
        jumped = (last_eye is not None
                  and float(np.dot(cam - last_eye, cam - last_eye))
                  > 32.0 ** 2)
        turned = (last_forward is not None
                  and sum(a * b for a, b in zip(forward, last_forward))
                  < self.MAX_TURN_COS)
        if jumped or turned:
            self._occlusion_view_epoch += 1
            book.retire(())
            return slots

        frame = self._occlusion_frame
        culled_groups = np.zeros(len(group_bounds), dtype=bool)
        for i in range(len(group_bounds)):
            if near_sq[i] < self.NEAR_EXEMPT_SQ:
                continue
            if book.culled(group_cells[i], frame):
                culled_groups[i] = True
        if not culled_groups.any():
            self._occlusion_drawn = len(slots)
            return slots

        # The same visible set (the same immutable array), the same rows and
        # the same hidden groups: the same result, handed back as the same
        # read-only array so what follows knows it by identity (M4).
        kept = getattr(self, '_occlusion_kept', None)
        if kept is None:
            kept = self._occlusion_kept = {}
        inputs = (table.generation, table.transform_version)
        held = kept.get(self._instance_view)
        if (held is not None and held[0] is slots and held[1] == inputs
                and np.array_equal(held[2], culled_groups)):
            self._occlusion_skipped, self._occlusion_drawn = held[4]
            return held[3]
        keep = np.ones(len(slots), dtype=bool)
        keep[eligible] = ~culled_groups[group_of_row]
        self._occlusion_skipped = int(np.count_nonzero(~keep))
        self._occlusion_drawn = len(slots) - self._occlusion_skipped
        # Score savings against *eligible* rows; a map full of protected
        # convex geometry should not bury a useful small occlusion region.
        out = slots[keep]
        out.flags.writeable = False
        kept[self._instance_view] = (slots, inputs, culled_groups, out,
                                     (self._occlusion_skipped, self._occlusion_drawn))
        return out

    def _occlusion_end_of_frame(self, projection, view, camera_pos, config):
        """Measure non-blocking query polling and optional probe submission."""
        if not self._occlusion_ready:
            return
        self._occlusion_cpu_query_ms = 0.0
        self._occlusion_cpu_probe_ms = 0.0
        started = time.perf_counter_ns()
        try:
            self._occlusion_end_of_frame_impl(
                projection, view, camera_pos, config)
        finally:
            total_ms = (time.perf_counter_ns() - started) * 1e-6
            self._occlusion_cpu_probe_ms = max(
                0.0, total_ms - self._occlusion_cpu_query_ms)

    def _occlusion_end_of_frame_impl(self, projection, view, camera_pos, config):
        """Poll last frame's probes, then issue this frame's.

        Called where the frame's opaque depth is finished (after the fog
        volumes, before the render's tail state changes), so a probe box is
        depth-tested against exactly the geometry the brushes were hidden
        behind. Results are read only when the GPU says they are ready --
        never forced -- so this is O(probes) of tiny GL calls, not a stall.
        """
        if not self._occlusion_ready:
            return
        started_poll = time.perf_counter_ns()
        self._occlusion_frame += 1
        self._occlusion_issued = 0
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
            metadata = self._occlusion_probe_meta.pop(coord, None)
            if coord not in candidates or metadata is None:
                continue
            box, view_epoch, issued_frame, issued_eye = metadata
            # A query that the Adreno driver reports many frames late
            # describes an old camera. Never stamp it with today's frame.
            # This also guards the slow-motion case where no individual
            # movement was large enough to invalidate the view epoch.
            eye_now = self._occlusion_last_eye
            if (box != self._occlusion_box_by_cell.get(coord)
                    or view_epoch != self._occlusion_view_epoch
                    or frame - issued_frame > cell_cull.MAX_RESULT_AGE
                    or eye_now is None
                    or float(np.dot(eye_now - issued_eye,
                                    eye_now - issued_eye)) > 32.0 ** 2):
                continue
            self.occlusion_book.record(coord, samples, issued_frame)
        self.occlusion_book.retire(candidates)
        # This includes the GL_QUERY_RESULT_AVAILABLE calls; no blocking
        # result is ever requested on an unfinished query.
        self._occlusion_cpu_query_ms = (
            time.perf_counter_ns() - started_poll) * 1e-6

        if groups is None or not self._occlusion_active(config):
            return
        group_bounds, _group_of_row, group_cells = groups
        if not len(group_bounds) or camera_pos is None:
            return

        # The budget is a ceiling, not a quota. Avoid ever probing nearby
        # exempt boxes or cells with too few brushes to repay a draw call.
        # The squared distances were already calculated for the cull mask;
        # selection remains GL-free and is exercised by headless tests.
        order = cell_cull.select_probe_groups(
            _group_of_row, self._occlusion_near_sq,
            self._occlusion_budget, near_exempt_sq=self.NEAR_EXEMPT_SQ,
            min_group_rows=self._occlusion_min_group_rows,
            counts=self._occlusion_group_counts)
        if not len(order) or len(self._occlusion_probes) >= self.occlusion_governor.max_probes:
            return
        # If all selected groups already have pending results, no shader,
        # VAO or GL render-state change is warranted this frame.
        order = [i for i in order if (int(group_cells[i, 0]),
                                     int(group_cells[i, 1])) not in self._occlusion_probes]
        if not order:
            return

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
            if len(self._occlusion_probes) >= self.occlusion_governor.max_probes:
                break  # also bound pending GPU work on a slow driver
            query = (self._occlusion_query_pool.pop()
                     if self._occlusion_query_pool
                     else self._query_name(gl.glGenQueries(1)))
            gl.glBeginQuery(self._occlusion_target, query)
            model = self._probe_model_matrix(group_bounds[i])
            gl.glUniformMatrix4fv(self._occlusion_model_loc, 1, gl.GL_FALSE,
                                 glm.value_ptr(model))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            gl.glEndQuery(self._occlusion_target)
            self._occlusion_probes[coord] = query
            self._occlusion_issued += 1
            self._occlusion_probe_meta[coord] = (
                self._occlusion_box_by_cell[coord],
                self._occlusion_view_epoch, frame,
                self._occlusion_last_eye.copy())
        # Restore what the tail of render_scene expects to find: the very
        # next lines re-enable the depth mask and disable the depth test.
        gl.glBindVertexArray(0)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glUseProgram(0)

    def _pick_query_target(self, gl_module=None):
        """The strongest occlusion query target this context accepts.

        Ranked by latency, not truth: GL_ANY_SAMPLES_PASSED_CONSERVATIVE
        over-reports visibility (a box that barely failed the depth test can
        still count -- the fail-open direction), but it reached desktop
        OpenGL with 4.3's ARB_ES3_compatibility; GL_ANY_SAMPLES_PASSED is
        3.3 core (ARB_occlusion_query2) and GL_SAMPLES_PASSED 1.5. The
        feature keys only on *zero samples*, which all three answer exactly,
        so each candidate is tried, strongest first, against the live
        context: an empty query is begun and ended between two error-flag
        reads, after draining whatever earlier passes left behind. A target
        the driver rejects -- a 3.3 core context rejects the conservative
        one with INVALID_ENUM -- is skipped, not trusted. ``None`` means
        the context takes no occlusion query at all.
        """
        if gl_module is None:
            gl_module = gl
        candidates = []
        for name in ('GL_ANY_SAMPLES_PASSED_CONSERVATIVE',
                     'GL_ANY_SAMPLES_PASSED'):
            target = getattr(gl_module, name, None)
            if target is not None:
                candidates.append(target)
        candidates.append(gl_module.GL_SAMPLES_PASSED)
        for target in candidates:
            try:
                while gl_module.glGetError() != gl_module.GL_NO_ERROR:
                    pass  # errors left by earlier passes are not ours to read
                query = self._query_name(gl_module.glGenQueries(1))
                gl_module.glBeginQuery(target, query)
                gl_module.glEndQuery(target)
                accepted = gl_module.glGetError() == gl_module.GL_NO_ERROR
                gl_module.glDeleteQueries(1, [query])
            except Exception:
                continue
            if accepted:
                return target
        return None

    @staticmethod
    def _probe_model_matrix(bounds):
        """Map the core-profile unit cube to the ENTIRE union AABB.

        The cube's local corners are +/-0.5, whereas RenderTable holds
        half-extents. The model scale must therefore be 2*half on each
        axis. This helper is independently testable without a GL context.
        """
        cx, cy, cz, hx, hy, hz = (float(v) for v in bounds)
        model = glm.translate(glm.mat4(1.0), glm.vec3(cx, cy, cz))
        return glm.scale(model, glm.vec3(2 * hx, 2 * hy, 2 * hz))

    @staticmethod
    def _query_name(value):
        """int() of whichever shape PyOpenGL returns for a single query id.

        A bare int on most paths, a one-element array under the size hints
        the engine enables -- the same shapes _query_object_uiv already
        copes with.
        """
        try:
            return int(value)
        except TypeError:
            return int(value[0])

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
