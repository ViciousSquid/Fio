"""The Overlay display mode: the Textured frame with every brush triangle
drawn over it in white.
"""


import glm
import OpenGL.GL as gl
from engine.renderer.core import gl_state

from engine import render_table
from ...core.diagnostics import timed_pass
from ...core.uniform_rows import uniform_rows
from ...core.raw_calls import uniform_matrix4


class OverlayMixin:
    """The Overlay display mode: the Textured frame with every brush triangle"""

    @timed_pass('triangle overlay')
    def draw_triangle_overlay(self, projection, view, brushes, table):
        """Overlay display: every brush triangle as a white line on the frame.

        Drawn over the finished textured, lit brushes, depth-tested and pulled
        slightly toward the eye so a line wins against its own surface and
        loses to anything in front of it.
        """
        if len(brushes) == 0 or 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl_state.glUseProgram(shader)
        self._current_shader = shader
        uniform_matrix4(uniforms['projection'], glm.value_ptr(projection))
        uniform_matrix4(uniforms['view'], glm.value_ptr(view))
        gl_state.glUniform3f(uniforms['color'], 1.0, 1.0, 1.0)
        gl_state.glUniform1f(uniforms['alpha'], 1.0)
        model_loc = uniforms['model']
        models, _normals = self._frame_transforms(table, brushes)
        geometry = (table.class_bits[brushes] & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = (self._prepare_geo_meshes(table, brushes[geometry])
                      if geometry.any() else {})
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        gl_state.glEnable(gl.GL_POLYGON_OFFSET_LINE)
        gl.glPolygonOffset(-1.0, -1.0)
        gl_state.glDepthFunc(gl.GL_LEQUAL)
        gl_state.glDepthMask(gl.GL_FALSE)
        cube_vao = self.vaos['cube']
        bound_vao = None
        upload_model = uniform_rows('mat4', models)
        for index, slot in enumerate(brushes):
            mesh = (geo_meshes.get(int(table.geometry_id[int(slot)]))
                    if geometry[index] else None)
            vao, count = (mesh.vao, mesh.count) if mesh is not None else (cube_vao, 36)
            if vao != bound_vao:
                gl_state.glBindVertexArray(vao)
                bound_vao = vao
            upload_model(model_loc, index)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, count)
            self.render_stats.draw_calls += 1
        gl_state.glBindVertexArray(0)
        gl_state.glDepthMask(gl.GL_TRUE)
        gl_state.glDepthFunc(gl.GL_LESS)
        gl_state.glDisable(gl.GL_POLYGON_OFFSET_LINE)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
