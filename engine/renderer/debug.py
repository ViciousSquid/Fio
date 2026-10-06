"""Renderer diagnostics and editor overlays.

Per-pass CPU timing (:func:`timed_pass`, :class:`RenderStats`), driver-clamped
line/point sizes, the opt-in GL state dump, and everything the editor draws on
top of the world: grid, gizmo, selection outline, AABBs, face highlight,
component-edit handles, PathNode cubes, portal wireframes, connection lines and
collision visualisation.
"""

import ctypes
import functools
import time

import glm
import numpy as np
import OpenGL.GL as gl

from engine.constants import brush_aabb_bounds
from engine import brush_geometry
from editor.things import Thing, Effect


class RenderStats:
    __slots__ = ('total_brushes', 'culled_brushes', 'visible_brushes', 'draw_calls',
                 'shadow_draw_calls', 'total_tris', 'visible_tris', 'batched_draws',
                 'entity_candidates', 'culled_entities', 'pass_ms')
    def __init__(self):
        #: CPU milliseconds spent submitting each pass this frame, measured by
        #: :func:`timed_pass`. Inclusive: a pass that draws others (portals,
        #: shadow maps) counts theirs too.
        self.pass_ms = {}
        self.reset()
    def reset(self):
        self.total_brushes = self.culled_brushes = self.visible_brushes = 0
        self.draw_calls = self.shadow_draw_calls = self.batched_draws = 0
        self.total_tris = self.visible_tris = 0
        #: Sprite and model rows offered to the main view's entity passes,
        #: and how many of them its frustum rejected.
        self.entity_candidates = self.culled_entities = 0
        self.pass_ms.clear()


def timed_pass(name):
    """Accumulate a renderer pass's CPU time into ``render_stats.pass_ms``.

    Two clock reads per call, a handful of calls per frame: what the Debug
    Tables instrument shows as the per-pass submission cost.
    """
    def decorate(method):
        @functools.wraps(method)
        def timed(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                return method(self, *args, **kwargs)
            finally:
                ms = self.render_stats.pass_ms
                ms[name] = ms.get(name, 0.0) + (time.perf_counter() - started) * 1000.0
        return timed
    return decorate


class DebugMixin:
    """Diagnostics and editor overlays of :class:`engine.renderer.Renderer`."""

    # --------------------------------------------------------------------------
    # Editor helpers (outlines, gizmo, etc.)
    # --------------------------------------------------------------------------
    def _set_line_width(self, width):
        """Set the line width, clamped to what this driver actually supports.

        A core profile only has to support a width of 1.0, and plenty of
        hardware reports exactly ``[1, 1]`` for ``GL_ALIASED_LINE_WIDTH_RANGE``
        — asking for 2.0 there raises ``GL_INVALID_VALUE`` and takes the frame
        with it.  The range is queried once and cached, since ``glGetFloatv``
        stalls the pipeline and the limit never changes for a context.

        Returns the width actually set, so callers can tell when they did not
        get the emphasis they asked for.
        """
        if self._line_width_range is None:
            try:
                values = (gl.GLfloat * 2)()
                gl.glGetFloatv(gl.GL_ALIASED_LINE_WIDTH_RANGE, values)
                low, high = float(values[0]), float(values[1])
                if not (high >= low > 0.0):
                    low = high = 1.0
            except Exception:
                low = high = 1.0
            self._line_width_range = (low, high)
        low, high = self._line_width_range
        clamped = max(low, min(high, float(width)))
        try:
            gl.glLineWidth(clamped)
        except Exception:
            # A driver that refuses even the clamped value: keep drawing at
            # whatever width it is already using rather than losing the frame.
            return 1.0
        return clamped

    def _set_point_size(self, size):
        """Set the point size, clamped to the driver's supported range.

        Same story as :meth:`_set_line_width`: the guaranteed range is narrow
        and an out-of-range value is a GL error, not a silent clamp.
        """
        if self._point_size_range is None:
            try:
                values = (gl.GLfloat * 2)()
                gl.glGetFloatv(gl.GL_ALIASED_POINT_SIZE_RANGE, values)
                low, high = float(values[0]), float(values[1])
                if not (high >= low > 0.0):
                    low = high = 1.0
            except Exception:
                low = high = 1.0
            self._point_size_range = (low, high)
        low, high = self._point_size_range
        clamped = max(low, min(high, float(size)))
        try:
            gl.glPointSize(clamped)
        except Exception:
            return 1.0
        return clamped

    def _debug_textured_brush_gl_state(self):
        """Print the VAO/program state used by the textured-brush pass.

        This is an opt-in diagnostic path only. It is intentionally called once
        before the face submission loop rather than from the per-face hot path.
        """
        print(
            '[Renderer] textured-brush GL state: '
            f'program={int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))}, '
            f'vao={int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING))}, '
            f'array_buffer={int(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))}, '
            f'element_buffer={int(gl.glGetIntegerv(gl.GL_ELEMENT_ARRAY_BUFFER_BINDING))}, '
            f'tf_active={bool(int(gl.glGetBooleanv(gl.GL_TRANSFORM_FEEDBACK_ACTIVE)))}, '
            f'tf_paused={bool(int(gl.glGetBooleanv(gl.GL_TRANSFORM_FEEDBACK_PAUSED)))}, '
            f'rasterizer_discard={bool(int(gl.glGetBooleanv(gl.GL_RASTERIZER_DISCARD)))}'
        )

        def _scalar(value):
            return int(np.asarray(value).reshape(-1)[0])

        for attrib in (0, 1, 2):
            enabled = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED
                )
            )
            buffer = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING
                )
            )
            stride = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE
                )
            )
            attr_type = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_TYPE
                )
            )
            print(
                f'[Renderer] attrib{attrib}: '
                f'enabled={bool(enabled)}, '
                f'buffer={buffer}, '
                f'stride={stride}, '
                f'type=0x{attr_type:x}'
            )

    # --------------------------------------------------------------------------
    # Grid
    # --------------------------------------------------------------------------
    def update_grid_buffers(self, world_size, grid_size):
        if self.vaos.get('grid') is None and self.vaos.get('cube') is None:
            if grid_size <= 0 or self._shader_init_failed:
                return
        if grid_size <= 0:
            if self.vaos['grid']:
                gl.glDeleteVertexArrays(1, [self.vaos['grid']])
                if hasattr(self, '_grid_vbo') and self._grid_vbo:
                    gl.glDeleteBuffers(1, [self._grid_vbo])
                    self._grid_vbo = None
                self.vaos['grid'] = None
            return
        s, g = world_size, grid_size
        lines = [[-s, 0, i, s, 0, i, i, 0, -s, i, 0, s] for i in range(-s, s+1, g)]
        grid_vertices = np.array(lines, dtype=np.float32).flatten()
        self.grid_indices_count = len(grid_vertices) // 3
        if self.vaos['grid']:
            gl.glDeleteVertexArrays(1, [self.vaos['grid']])
        if hasattr(self, '_grid_vbo') and self._grid_vbo:
            gl.glDeleteBuffers(1, [self._grid_vbo])
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, grid_vertices.nbytes, grid_vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self._grid_vbo = vbo
        self.vaos['grid'] = vao

    @timed_pass('grid')
    def draw_grid(self, projection, view, grid_indices_count, play_mode=False, grid_visible=True):
        if not self.vaos['grid'] or play_mode or not grid_visible or 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform3f(uniforms['color'], 0.2, 0.2, 0.2)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self.vaos['grid'])
        gl.glDrawArrays(gl.GL_LINES, 0, grid_indices_count)
        gl.glBindVertexArray(0)

    def _create_gizmo_buffers(self):
        axis_verts = np.array([0,0,0, 1,0,0, 0,0,0, 0,1,0, 0,0,0, 0,0,1], dtype=np.float32)
        self.vao_gizmo_lines = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        self._gizmo_lines_vbo = vbo
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, axis_verts.nbytes, axis_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        cone_verts = []
        for i in range(12):
            t1, t2 = (i/12)*2*np.pi, ((i+1)/12)*2*np.pi
            cone_verts.extend([0,0,0, np.cos(t2)*0.05,0,np.sin(t2)*0.05, np.cos(t1)*0.05,0,np.sin(t1)*0.05])
            cone_verts.extend([0,0.2,0, np.cos(t1)*0.05,0,np.sin(t1)*0.05, np.cos(t2)*0.05,0,np.sin(t2)*0.05])
        self.gizmo_cone_v_count = len(cone_verts)//3
        cone_verts = np.array(cone_verts, dtype=np.float32)
        self.vao_gizmo_cone = gl.glGenVertexArrays(1)
        vbo2 = gl.glGenBuffers(1)
        self._gizmo_cone_vbo = vbo2
        gl.glBindVertexArray(self.vao_gizmo_cone)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo2)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, cone_verts.nbytes, cone_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    def render_gizmo(self, projection, view, position):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        pos_vec = glm.vec3(*position) if isinstance(position, (list, tuple)) else position
        base = glm.scale(glm.translate(self._identity_mat4, pos_vec), glm.vec3(32.0))
        model_loc, color_loc = uniforms['model'], uniforms['color']
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(base))
        for i,c in enumerate([(1,0,0), (0,1,0), (0,0,1)]):
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_LINES, i*2, 2)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        for axis, c, rot in [((1,0,0), (1,0,0), glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1))),
                             ((0,1,0), (0,1,0), base),
                             ((0,0,1), (0,0,1), glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)))]:
            m = glm.translate(rot if axis[1] else glm.translate(base, glm.vec3(*axis)), glm.vec3(0,1,0) if axis[1] else glm.vec3(0,0,0))
            if axis[0]: m = glm.translate(glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1)), glm.vec3(0,1,0))
            if axis[2]: m = glm.translate(glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)), glm.vec3(0,1,0))
            if axis[1]: m = glm.translate(base, glm.vec3(0,1,0))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(m))
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)
        gl.glBindVertexArray(0)

    def draw_selected_brush_outline(self, projection, view, brush, table=None):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        pos = brush.get('pos', [0, 0, 0])
        size = brush.get('size', [64, 64, 64])
        model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*pos)), glm.vec3(*size))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        if not hasattr(self, '_edge_vao') or self._edge_vao is None:
            edge_vertices = np.array([
                -0.5,-0.5,-0.5,  0.5,-0.5,-0.5,  0.5,-0.5,-0.5,  0.5,-0.5, 0.5,
                 0.5,-0.5, 0.5, -0.5,-0.5, 0.5, -0.5,-0.5, 0.5, -0.5,-0.5,-0.5,
                -0.5, 0.5,-0.5,  0.5, 0.5,-0.5,  0.5, 0.5,-0.5,  0.5, 0.5, 0.5,
                 0.5, 0.5, 0.5, -0.5, 0.5, 0.5, -0.5, 0.5, 0.5, -0.5, 0.5,-0.5,
                -0.5,-0.5,-0.5, -0.5, 0.5,-0.5,  0.5,-0.5,-0.5,  0.5, 0.5,-0.5,
                 0.5,-0.5, 0.5,  0.5, 0.5, 0.5, -0.5,-0.5, 0.5, -0.5, 0.5, 0.5,
            ], dtype=np.float32)
            self._edge_vao = gl.glGenVertexArrays(1)
            gl.glBindVertexArray(self._edge_vao)
            vbo = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, edge_vertices.nbytes, edge_vertices, gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)
            self._edge_vbo = vbo
        gl.glLineWidth(1.0)
        # Editor selection is still represented by the authored selection dict,
        # but convex geometry comes from the dense RenderTable geometry records.
        # Do not reintroduce the removed object-based mesh lookup here.
        mesh = None
        if table is not None and isinstance(brush, dict):
            slot = table.slot_of_id.get(brush.get('id'))
            if slot is not None:
                slot = int(slot)
                gid = int(table.geometry_id[slot])
                if gid >= 0 and gid < len(table.geometry_records):
                    record = table.geometry_records[gid]
                    mesh = self._get_geo_mesh_record(
                        record, geometry_id=gid,
                        geometry_generation=table.generation)
        if mesh is not None and mesh.edge_count:
            # Angled brush: outline its real convex edges instead of the AABB.
            gl.glBindVertexArray(mesh.edge_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, mesh.edge_count)
        else:
            gl.glBindVertexArray(self._edge_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, 24)
        gl.glBindVertexArray(0)

    def draw_effect_billboard_aabb(
        self, projection, view, effect, explosion=False
    ):
        """Draw the selected Effect billboard's editor-only world AABB.

        The bounds are derived from the same width/height and billboard basis
        used by the Effect shaders. EXPLOSION frame 10 uses its current shader
        growth factor so the preview box remains visually accurate.
        """
        props = getattr(effect, 'properties', {}) or {}
        try:
            width = max(0.01, float(props.get('width', 32.0)))
        except (TypeError, ValueError):
            width = 32.0
        try:
            height = max(0.01, float(props.get('height', 24.0)))
        except (TypeError, ValueError):
            height = 24.0

        right = np.asarray(
            (float(view[0][0]), float(view[1][0]), float(view[2][0])),
            dtype=np.float32,
        )
        right_norm = float(np.linalg.norm(right))
        if right_norm <= 1e-6:
            right = np.asarray((1.0, 0.0, 0.0), dtype=np.float32)
        else:
            right /= right_norm

        if explosion:
            up = np.asarray((0.0, 1.0, 0.0), dtype=np.float32)
            t = (10.0 - 0.5) / 16.0
            smooth = t * t * (3.0 - 2.0 * t)
            growth = 1.0 + 2.0 * smooth
        else:
            up = np.asarray(
                (float(view[0][1]), float(view[1][1]), float(view[2][1])),
                dtype=np.float32,
            )
            up_norm = float(np.linalg.norm(up))
            if up_norm <= 1e-6:
                up = np.asarray((0.0, 1.0, 0.0), dtype=np.float32)
            else:
                up /= up_norm
            growth = 1.0

        half_width = width * growth * 0.5
        half_height = height * growth * 0.5
        extents = (
            np.abs(right) * half_width
            + np.abs(up) * half_height
        )

        pos = np.asarray(
            getattr(effect, 'pos', [0.0, 0.0, 0.0]),
            dtype=np.float32,
        )
        center = pos.copy()
        if explosion:
            center += up * half_height

        self.draw_aabb_bounds(
            projection,
            view,
            {
                'pos': center.tolist(),
                'size': (extents * 2.0).tolist(),
            },
        )

    def draw_aabb_bounds(self, projection, view, brush):
        """Draw the exact world-space trigger AABB as orange dashed lines."""
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))

        lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = brush_aabb_bounds(brush)
        corners = np.array([
            [lo_x, lo_y, lo_z], [hi_x, lo_y, lo_z],
            [hi_x, hi_y, lo_z], [lo_x, hi_y, lo_z],
            [lo_x, lo_y, hi_z], [hi_x, lo_y, hi_z],
            [hi_x, hi_y, hi_z], [lo_x, hi_y, hi_z],
        ], dtype=np.float32)
        edges = ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7))
        dash, gap = 8.0, 5.0
        vertices = []
        for i0, i1 in edges:
            a, b = corners[i0], corners[i1]
            delta = b - a
            length = float(np.linalg.norm(delta))
            if length <= 1e-6:
                continue
            direction = delta / length
            cursor = 0.0
            while cursor < length:
                end = min(cursor + dash, length)
                p0, p1 = a + direction * cursor, a + direction * end
                vertices.extend((float(p0[0]), float(p0[1]), float(p0[2]),
                                 float(p1[0]), float(p1[1]), float(p1[2])))
                cursor += dash + gap
        if not vertices:
            return
        data = np.asarray(vertices, dtype=np.float32)
        vao = getattr(self, '_aabb_vao', None)
        vbo = getattr(self, '_aabb_vbo', None)
        if vao is None:
            vao = self._aabb_vao = gl.glGenVertexArrays(1)
            vbo = self._aabb_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
        gl.glUniform3f(uniforms['color'], 1.0, 140.0 / 255.0, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        self._set_line_width(1.0)
        gl.glDrawArrays(gl.GL_LINES, 0, len(vertices) // 3)
        gl.glBindVertexArray(0)

    def draw_face_highlight(self, projection, view, brush, face_name):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUniform3f(uniforms['color'], 0.8, 0.2, 0.9)
        gl.glUniform1f(uniforms['alpha'], 0.4)

        # Angled (clipped) brushes: highlight the real convex face polygon so
        # the sloped cut face lights up under the cursor, not an AABB side.
        if brush_geometry.brush_has_geometry(brush):
            verts = self._geo_face_highlight_verts(brush, face_name)
            if not verts:
                gl.glUseProgram(0)
                return
            self._draw_face_highlight_verts(verts, uniforms)
            gl.glDisable(gl.GL_BLEND)
            gl.glUseProgram(0)
            return

        pos, size = brush['pos'], brush['size']
        hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
        cx, cy, cz = pos[0], pos[1], pos[2]
        bias = 0.5
        verts = []
        if face_name == 'north':
            z = cz + hz + bias
            verts = [cx-hx, cy-hy, z, cx+hx, cy-hy, z, cx+hx, cy+hy, z,
                     cx-hx, cy-hy, z, cx+hx, cy+hy, z, cx-hx, cy+hy, z]
        elif face_name == 'south':
            z = cz - hz - bias
            verts = [cx+hx, cy-hy, z, cx-hx, cy-hy, z, cx-hx, cy+hy, z,
                     cx+hx, cy-hy, z, cx-hx, cy+hy, z, cx+hx, cy+hy, z]
        elif face_name == 'east':
            x = cx + hx + bias
            verts = [x, cy-hy, cz+hz, x, cy-hy, cz-hz, x, cy+hy, cz-hz,
                     x, cy-hy, cz+hz, x, cy+hy, cz-hz, x, cy+hy, cz+hz]
        elif face_name == 'west':
            x = cx - hx - bias
            verts = [x, cy-hy, cz-hz, x, cy-hy, cz+hz, x, cy+hy, cz+hz,
                     x, cy-hy, cz-hz, x, cy+hy, cz+hz, x, cy+hy, cz-hz]
        elif face_name == 'top':
            y = cy + hy + bias
            verts = [cx-hx, y, cz+hz, cx+hx, y, cz+hz, cx+hx, y, cz-hz,
                     cx-hx, y, cz+hz, cx+hx, y, cz-hz, cx-hx, y, cz-hz]
        elif face_name == 'down':
            y = cy - hy - bias
            verts = [cx-hx, y, cz-hz, cx+hx, y, cz-hz, cx+hx, y, cz+hz,
                     cx-hx, y, cz-hz, cx+hx, y, cz+hz, cx-hx, y, cz+hz]
        if not verts:
            return

        self._draw_face_highlight_verts(verts, uniforms)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    @staticmethod
    def _geo_face_highlight_verts(brush, face_name):
        """Triangle-fan positions (world space, nudged outward) for one convex
        face of an angled brush, or ``None`` when the face can't be resolved."""
        convex = brush_geometry.get_convex(brush)
        if convex is None or not convex.is_valid:
            return None
        face = None
        for f in convex.faces:
            if brush_geometry.face_key(f) == face_name:
                face = f
                break
        if face is None:
            return None
        idx = face['indices']
        if len(idx) < 3:
            return None
        ring = convex.verts[idx] + np.array(face['normal']) * 0.5   # bias off surface
        out = []
        v0 = ring[0]
        for k in range(1, len(idx) - 1):
            for p in (v0, ring[k], ring[k + 1]):
                out.extend((float(p[0]), float(p[1]), float(p[2])))
        return out

    def _draw_face_highlight_verts(self, verts, uniforms):
        """Upload and draw a fill+wire face highlight for arbitrary geometry."""
        v_data = np.asarray(verts, dtype=np.float32)
        n = len(v_data) // 3
        if self.face_highlight_vao is None:
            self.face_highlight_vao = gl.glGenVertexArrays(1)
            self.face_highlight_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self.face_highlight_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
        gl.glBindVertexArray(self.face_highlight_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
        # Orphan-and-refill so the buffer resizes for any face vertex count.
        gl.glBufferData(gl.GL_ARRAY_BUFFER, v_data.nbytes, v_data, gl.GL_DYNAMIC_DRAW)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, n)
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 1.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, n)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_component_overlay(self, projection, view, overlay, version=None):
        """Draw vertex/edge/face handles for the brushes being component-edited.

        ``overlay`` is the dict the editor's ComponentController hands over:
        ``points`` / ``hot_points`` (N, 3) and ``lines`` / ``hot_lines``
        (M, 2, 3), already ``float32``.  Nothing is computed here — the arrays
        are built by the editor when the selection or geometry changes and are
        uploaded again only when ``version`` moves, so hovering costs one
        integer comparison and a draw call rather than a geometry rebuild.
        """
        if 'simple' not in self.shaders or not overlay:
            return
        points = overlay.get('points')
        hot_points = overlay.get('hot_points')
        lines = overlay.get('lines')
        hot_lines = overlay.get('hot_lines')
        if not (len(points) or len(hot_points) or len(lines) or len(hot_lines)):
            return

        if version is None or self._component_overlay_version != version:
            # One interleaved buffer for the whole overlay: four contiguous
            # runs (cold lines, hot lines, cold points, hot points) so the
            # draw below is four glDrawArrays with no per-handle work.
            def _flat(arr):
                return (np.asarray(arr, dtype=np.float32).reshape(-1)
                        if len(arr) else np.zeros(0, dtype=np.float32))
            cold_l, hot_l = _flat(lines), _flat(hot_lines)
            cold_p, hot_p = _flat(points), _flat(hot_points)
            data = np.concatenate((cold_l, hot_l, cold_p, hot_p)) \
                if (len(cold_l) or len(hot_l) or len(cold_p) or len(hot_p)) \
                else np.zeros(0, dtype=np.float32)
            self._component_overlay_data = data
            self._component_overlay_counts = (
                len(cold_l) // 3, len(hot_l) // 3,
                len(cold_p) // 3, len(hot_p) // 3)
            self._component_overlay_version = version
            self._component_overlay_dirty = True

        counts = self._component_overlay_counts
        if not counts or not any(counts):
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)

        if self._component_overlay_vao is None:
            self._component_overlay_vao = gl.glGenVertexArrays(1)
            self._component_overlay_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._component_overlay_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._component_overlay_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        gl.glBindVertexArray(self._component_overlay_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._component_overlay_vbo)
        if self._component_overlay_dirty:
            data = self._component_overlay_data
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data,
                            gl.GL_DYNAMIC_DRAW)
            self._component_overlay_dirty = False

        cold_lines, hot_lines_n, cold_points, hot_points_n = counts
        # Handles are editor furniture: they must stay visible through the
        # geometry they belong to, so the depth test is off for this pass.
        depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        offset = 0
        if cold_lines:
            gl.glUniform3f(uniforms['color'], 0.45, 0.78, 1.0)
            self._set_line_width(1.0)
            gl.glDrawArrays(gl.GL_LINES, offset, cold_lines)
        offset += cold_lines
        if hot_lines_n:
            gl.glUniform3f(uniforms['color'], 1.0, 0.66, 0.16)
            # Hardware that cannot draw a wide line reports a range of [1, 1],
            # and the highlight then reads by colour alone — which is why the
            # hot and cold colours are far apart rather than two shades of one.
            self._set_line_width(2.0)
            gl.glDrawArrays(gl.GL_LINES, offset, hot_lines_n)
        offset += hot_lines_n
        if cold_points:
            gl.glUniform3f(uniforms['color'], 0.45, 0.78, 1.0)
            self._set_point_size(6.0)
            gl.glDrawArrays(gl.GL_POINTS, offset, cold_points)
        offset += cold_points
        if hot_points_n:
            gl.glUniform3f(uniforms['color'], 1.0, 0.66, 0.16)
            self._set_point_size(9.0)
            gl.glDrawArrays(gl.GL_POINTS, offset, hot_points_n)
        self._set_line_width(1.0)
        self._set_point_size(1.0)
        if depth_was_on:
            gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_path_node_cubes(self, projection, view, table):
        """The editor's PathNode markers, from the entity table's node rows."""
        if 'simple' not in self.shaders or table is None:
            return
        slots = table.path_node_slots
        if not len(slots):
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3f(uniforms['color'], 1.0, 0.5, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        cube_size = 16.0
        gl.glBindVertexArray(self.vaos['cube'])
        for x, y, z in table.pos[slots].tolist():
            model_matrix = glm.scale(glm.translate(self._identity_mat4,
                                                   glm.vec3(x, y, z)),
                                     glm.vec3(cube_size, cube_size, cube_size))
            gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_portal_wireframes(self, projection, view, portal_table,
                               portal_slots, play_mode=False):
        """Draw portal editor wireframes directly from EntityTable columns.

        Portal rendering has one numerical source of truth: topology, aperture
        geometry, active/fade state, colour and rim visibility all live in the
        dense entity projection. This overlay deliberately does not accept a
        Thing collection, so the renderer cannot fall back to the old
        Portal-object walk.
        """
        if 'simple' not in self.shaders or portal_table is None:
            return
        if portal_slots is None:
            return
        portal_slots = np.asarray(portal_slots, dtype=np.int32)
        if not len(portal_slots):
            return

        show = portal_table.portal_show_rim[portal_slots]
        slots = portal_slots[show]
        if not len(slots):
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['alpha'], 1.0)

        if self._portal_outline_vao is None:
            self._portal_outline_vao = gl.glGenVertexArrays(1)
            self._portal_outline_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 4 * 3 * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(
                0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        if self._portal_normal_vao is None:
            self._portal_normal_vao = gl.glGenVertexArrays(1)
            self._portal_normal_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_normal_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 2 * 3 * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(
                0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        gl.glLineWidth(1.0)
        model_loc = uniforms['model']
        color_loc = uniforms['color']
        gl.glUniformMatrix4fv(
            model_loc, 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))

        for slot_value in slots:
            slot = int(slot_value)
            r, g, b = portal_table.portal_color[slot]
            if not bool(portal_table.portal_active[slot]):
                r, g, b = r * 0.4, g * 0.4, b * 0.4

            # The target link is already resolved to an integer slot. A missing
            # target is one scalar comparison, not a second Portal object scan.
            if int(portal_table.portal_target_slot[slot]) < 0:
                r, g, b = 0.86, 0.24, 0.24

            corners = self._portal_slot_corners(portal_table, slot)
            vdata = np.asarray(corners, dtype=np.float32).reshape(-1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vdata.nbytes, vdata)
            gl.glUniform3f(color_loc, float(r), float(g), float(b))
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glDrawArrays(gl.GL_LINE_LOOP, 0, 4)

            basis = portal_table.portal_basis[slot]
            normal = basis[2]
            pos = portal_table.pos[slot]
            arrow_len = float(portal_table.portal_width_height[slot, 0]) * 0.4
            nline = np.asarray(
                (
                    float(pos[0]), float(pos[1]), float(pos[2]),
                    float(pos[0] + normal[0] * arrow_len),
                    float(pos[1] + normal[1] * arrow_len),
                    float(pos[2] + normal[2] * arrow_len),
                ),
                dtype=np.float32,
            )
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, nline.nbytes, nline)
            gl.glUniform3f(
                color_loc,
                min(1.0, float(r) * 1.6),
                min(1.0, float(g) * 1.6),
                min(1.0, float(b) * 1.6),
            )
            gl.glBindVertexArray(self._portal_normal_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, 2)

        gl.glLineWidth(1.0)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_connection_lines(self, projection, view, connections):
        if not connections or 'simple' not in self.shaders:
            return
        line_data = []
        line_colors = []
        for conn in connections:
            sx,sy,sz = conn['src']
            dx,dy,dz = conn['dst']
            line_data.extend([float(sx), float(sy), float(sz), float(dx), float(dy), float(dz)])
            line_colors.append(conn.get('color', (0.0,1.0,1.0)))
        if not line_data:
            return
        vertices = np.array(line_data, dtype=np.float32)
        if self._conn_line_vao is None:
            self._conn_line_vao = gl.glGenVertexArrays(1)
            self._conn_line_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._conn_line_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._conn_line_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 1024*1024, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self._conn_line_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._conn_line_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        color_loc = uniforms['color']
        for i, (r,g,b) in enumerate(line_colors):
            gl.glUniform3f(color_loc, r, g, b)
            gl.glDrawArrays(gl.GL_LINES, i*2, 2)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_collision_visualization(self, projection, view, brushes):
        """
        Draw wireframe overlays for collision meshes and AABB boxes.
        Helps debug why collision doesn't match the visual model.
        """
        if 'simple' not in self.shaders:
            return
        
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)
        
        # Clamp the line width to what the driver supports (a core profile is
        # only required to offer 1.0).  The helper caches the queried range,
        # so this no longer stalls the pipeline with a glGetFloatv per call.
        self._set_line_width(2.0)
        
        for brush in brushes:
            if not brush.get('_model_collision'):
                continue
            
            mode = brush.get('_collision_mode', 'aabb')
            
            if mode == 'aabb':
                # Draw yellow wireframe box
                pos = brush.get('pos', [0, 0, 0])
                size = brush.get('size', [64, 64, 64])
                self._draw_wireframe_box(pos, size, (1.0, 1.0, 0.0), uniforms)
                
            elif mode == 'mesh':
                # Draw cyan wireframe triangles
                mesh_tris = brush.get('_mesh_triangles', [])
                gl.glUniform3f(uniforms['color'], 0.0, 1.0, 1.0)  # Cyan
                
                # Build line data from triangles
                line_verts = []
                for (v0, v1, v2), normal in mesh_tris:
                    line_verts.extend([v0[0], v0[1], v0[2], v1[0], v1[1], v1[2]])
                    line_verts.extend([v1[0], v1[1], v1[2], v2[0], v2[1], v2[2]])
                    line_verts.extend([v2[0], v2[1], v2[2], v0[0], v0[1], v0[2]])
                
                if line_verts:
                    verts = np.array(line_verts, dtype=np.float32)
                    # Use dynamic VAO
                    vao = gl.glGenVertexArrays(1)
                    vbo = gl.glGenBuffers(1)
                    gl.glBindVertexArray(vao)
                    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
                    gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_DYNAMIC_DRAW)
                    gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
                    gl.glEnableVertexAttribArray(0)
                    
                    gl.glDrawArrays(gl.GL_LINES, 0, len(line_verts) // 3)
                    
                    gl.glBindVertexArray(0)
                    gl.glDeleteVertexArrays(1, [vao])
                    gl.glDeleteBuffers(1, [vbo])
        
        gl.glLineWidth(1.0)
        gl.glUseProgram(0)

    def _draw_wireframe_box(self, pos, size, color, uniforms):
        """Draw a wireframe AABB box at position with size."""
        hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
        cx, cy, cz = pos[0], pos[1], pos[2]
        
        # 12 edges of a box
        edges = [
            # Bottom face
            (cx-hx, cy-hy, cz-hz, cx+hx, cy-hy, cz-hz),
            (cx+hx, cy-hy, cz-hz, cx+hx, cy-hy, cz+hz),
            (cx+hx, cy-hy, cz+hz, cx-hx, cy-hy, cz+hz),
            (cx-hx, cy-hy, cz+hz, cx-hx, cy-hy, cz-hz),
            # Top face
            (cx-hx, cy+hy, cz-hz, cx+hx, cy+hy, cz-hz),
            (cx+hx, cy+hy, cz-hz, cx+hx, cy+hy, cz+hz),
            (cx+hx, cy+hy, cz+hz, cx-hx, cy+hy, cz+hz),
            (cx-hx, cy+hy, cz+hz, cx-hx, cy+hy, cz-hz),
            # Vertical edges
            (cx-hx, cy-hy, cz-hz, cx-hx, cy+hy, cz-hz),
            (cx+hx, cy-hy, cz-hz, cx+hx, cy+hy, cz-hz),
            (cx+hx, cy-hy, cz+hz, cx+hx, cy+hy, cz+hz),
            (cx-hx, cy-hy, cz+hz, cx-hx, cy+hy, cz+hz),
        ]
        
        verts = []
        for e in edges:
            verts.extend(e)
        
        if not verts:
            return
        
        v_data = np.array(verts, dtype=np.float32)
        gl.glUniform3f(uniforms['color'], *color)
        
        vao = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, v_data.nbytes, v_data, gl.GL_DYNAMIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        
        gl.glDrawArrays(gl.GL_LINES, 0, len(verts) // 3)
        
        gl.glBindVertexArray(0)
        gl.glDeleteVertexArrays(1, [vao])
        gl.glDeleteBuffers(1, [vbo])

    def _draw_selection_overlays(self, projection, view, primary_selection, table):
        """Outline, trigger AABB, Effect bounds and gizmo of the selection."""
        if isinstance(primary_selection, dict):
            self.draw_selected_brush_outline(
                projection, view, primary_selection, table=table)
            if primary_selection.get('is_trigger', False) and primary_selection.get('show_aabb_bounds', False):
                self.draw_aabb_bounds(projection, view, primary_selection)
            pos = primary_selection.get('pos')
            if pos is not None and not primary_selection.get('lock', False):
                self.render_gizmo(projection, view, pos)
        elif isinstance(primary_selection, Thing):
            if (isinstance(primary_selection, Effect)
                    and primary_selection.properties.get('preview', False)):
                self.draw_effect_billboard_aabb(
                    projection,
                    view,
                    primary_selection,
                    explosion=(
                        str(primary_selection.properties.get(
                            'effect_type', 'FIRE'
                        )).upper() == 'EXPLOSION'
                    ),
                )
            self.render_gizmo(projection, view, primary_selection.pos)