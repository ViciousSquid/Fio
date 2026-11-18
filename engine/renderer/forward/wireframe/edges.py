"""The Wireframe display mode's brush edges: each brush's true edges.

The distance colouring that follows is in ``distance.py``.
"""


import glm
import OpenGL.GL as gl

from engine import render_table
from ...core.diagnostics import timed_pass
from ...core.uniform_rows import uniform_rows
from ..instancing import _OVERRIDE_COLOURS, _SELECTED_ROW


class EdgesMixin:
    """The Wireframe display mode's brush edges: each brush's true edges."""

    @timed_pass('brush edges')
    def draw_brush_edges(self, projection, view, brushes, config, table):
        """Each brush as its true edges in its own colour: no fill, no texture.

        A box brush is its 12 edges and an angled brush its convex mesh's
        edges -- never the triangles a filled mesh is cut into.
        """
        if len(brushes) == 0 or 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        self._current_shader = shader
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['alpha'], 1.0)
        model_loc, color_loc = uniforms['model'], uniforms['color']
        models, _normals = self._frame_transforms(table, brushes)
        bits = table.class_bits[brushes]
        colours = table.colour[brushes]
        selected_slot = self._selected_slot(table, config)
        geometry = (bits & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = (self._prepare_geo_meshes(table, brushes[geometry])
                      if geometry.any() else {})
        edge_vao = self._ensure_edge_vao()
        bound_vao = None
        upload_model = uniform_rows('mat4', models)
        upload_colour = uniform_rows('vec3', colours)
        upload_override = uniform_rows('vec3', _OVERRIDE_COLOURS)
        for index, slot in enumerate(brushes):
            slot = int(slot)
            upload_model(model_loc, index)
            if slot == selected_slot:
                upload_override(color_loc, _SELECTED_ROW)
            else:
                upload_colour(color_loc, index)
            mesh = (geo_meshes.get(int(table.geometry_id[slot]))
                    if geometry[index] else None)
            vao, count = ((mesh.edge_vao, mesh.edge_count)
                          if mesh is not None and mesh.edge_count else (edge_vao, 24))
            if vao != bound_vao:
                gl.glBindVertexArray(vao)
                bound_vao = vao
            gl.glDrawArrays(gl.GL_LINES, 0, count)
            self.render_stats.draw_calls += 1
        self.render_stats.visible_brushes += len(brushes)
        gl.glBindVertexArray(0)
