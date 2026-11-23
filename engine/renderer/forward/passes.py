"""ForwardRenderer's draw passes shared by every display mode.

Glow brushes, terrain, models, sprites, glasses, billboards, effects, the
transparent water/glass/fog passes, and the instanced run submission the
brush passes use. Each display mode's own brush drawing is in its subfolder:
``textured/``, ``solid_lit/``, ``wireframe/`` and ``overlay/``.
"""

import ctypes
import time
from contextlib import contextmanager

import glm
import numpy as np
import OpenGL.GL as gl

from engine import render_table
from engine import shaders
from engine.effect_table import FAMILY_EXPLOSION, FAMILY_PARTICLES
from engine.entity_table import _CLOCK_ORIGIN
from engine.render_keys import runs_in_order, sort_into_runs
from engine.shaders import EXPLOSION_TIME_WRAP
from ..api import WATER_QUALITIES
from ..core.terrain_gl import TerrainGL
from ..core.diagnostics import timed_pass
from ..core.uniform_rows import uniform_rows
from .explosion_rows import as_float32, explosion_rows, same_rows


def _tinted(colour, tint):
    """A flat model colour multiplied by the Prop's tint (None: unchanged)."""
    if not tint:
        return tuple(colour)
    return (colour[0] * tint[0], colour[1] * tint[1], colour[2] * tint[2])


@contextmanager
def _model_material(uniforms, alpha):
    """Blend clear model surfaces without writing their depth over the ink.

    Opaque materials retain the caller's state, including respawn fades.
    """
    gl.glUniform1f(uniforms['alpha'], alpha)
    if alpha >= 1.0:
        yield
        return
    blend = gl.glIsEnabled(gl.GL_BLEND)
    depth_write = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
    blend_func = [int(gl.glGetIntegerv(key)) for key in (
        gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
        gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA)]
    gl.glEnable(gl.GL_BLEND)
    gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
    gl.glDepthMask(gl.GL_FALSE)
    try:
        yield
    finally:
        gl.glDepthMask(depth_write)
        gl.glBlendFuncSeparate(*blend_func)
        if not blend:
            gl.glDisable(gl.GL_BLEND)


def _set_model_tint(uniforms, tint):
    """Set a textured model shader's tint, or clear it for the next draw.

    Brushes draw with the same program and never set it, so a model that sets
    a tint clears it again once drawn.
    """
    location = uniforms['model_tint']
    if location != -1:
        if tint:
            gl.glUniform4f(location, tint[0], tint[1], tint[2], 1.0)
        else:
            gl.glUniform4f(location, 0.0, 0.0, 0.0, 0.0)


def _set_model_emissive(uniforms, emissive):
    """Set the glTF material's linear emissiveFactor for this model draw."""
    location = uniforms['emissive_factor']
    if location is not None and location != -1:
        gl.glUniform3fv(location, 1, emissive)



class PassesMixin:
    """ForwardRenderer's draw passes."""

    #: Water rendering tiers. 'cheap': refraction, waves, sky reflection and
    #: foam at the brush edges - no extra copies. 'expensive': additionally
    #: copies the depth buffer once per water pass for depth-based colour,
    #: shoreline foam, caustics and screen-space reflections.
    WATER_QUALITIES = WATER_QUALITIES

    @classmethod
    def normalize_water_quality(cls, value) -> str:
        value = str(value or '').strip().lower()
        return value if value in cls.WATER_QUALITIES else 'expensive'

    def _submit_brush_runs(self, program_name, projection, view, lights,
                           run_starts, run_texture, run_first, vertex_count,
                           fill_mode=gl.GL_FILL, stream=None):
        """Submit sorted brush runs. The one place brush geometry reaches GL.

        A *run* is a stretch of items whose render key is equal, so everything
        it contains shares the GPU state that key encodes.  That state is
        established once here -- the texture bind, and the vertex range the
        cube face occupies -- and everything that differs inside the run
        travels as instance data, already packed into the shared buffer.

        What is left between draws is exactly what changed: a texture bind when
        this run's texture is not the one already bound, and the instance
        attribute base, which stands in for the base-instance offset OpenGL 3.3
        does not have.

        *run_texture* may be zero for a pass that binds no texture; the lit
        pass is such a pass, and passing zero is how it says so rather than by
        taking a different route to the GPU.
        """
        self._begin_instanced_pass(program_name, projection, view, lights)
        # GL_LINE when the lit pass draws the Wireframe display mode.
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)
        gl.glBindVertexArray(stream.vao)
        # The run's attribute re-pointing reads the bound array buffer.
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)

        current_tex = None
        triangles = vertex_count // 3
        for run in range(len(run_starts) - 1):
            begin = int(run_starts[run])
            length = int(run_starts[run + 1]) - begin
            if length <= 0:
                continue
            tex_id = int(run_texture[run])
            if tex_id and tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            self._point_brush_instances_at(begin)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, int(run_first[run]),
                                     vertex_count, length)
            self.render_stats.draw_calls += 1
            self.render_stats.visible_tris += triangles * length
        gl.glBindVertexArray(0)

    def _begin_instanced_pass(self, name, projection, view, lights):
        """Bind an instanced brush program and its per-pass uniform state."""
        program = self.shaders[name]
        uniforms = self.uniforms[name]
        gl.glUseProgram(program)
        self._current_shader = program
        self._upload_lights_once(name, lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        return uniforms

    #: How the batched path submits a (texture, page) group of faces:
    #: ``'elements'`` (one glDrawElements over a cached 16-bit index list) or
    #: ``'multidraw'`` (one glMultiDrawArrays over the faces' ranges); ``'off'``
    #: draws every convex face through the per-mesh path. Indexed was faster
    #: on both drivers measured (llvmpipe, softpipe), multi-draw slower than
    #: the per-mesh path on softpipe, so neither is assumed: settings.ini's
    #: ``[Renderer] geo_batching`` chooses.
    GEO_BATCHING = ('elements', 'multidraw', 'off')
    geo_batching = 'elements'

    @timed_pass('glow brushes')
    def draw_glow_brushes(self, projection, view, camera_pos, brushes, lights,
                          config, table):
        """Overbright brushes.

        With *table*, ``brushes`` is an array of slots: the
        overbright colour was resolved into ``glow_colour`` when the brush was
        edited, so the per-brush ``[min(c * intensity, 10.0) for c in base]``
        list comprehension no longer runs per frame.
        """
        if len(brushes) == 0 or 'lit' not in self.shaders:
            return
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
        _set_model_emissive(uniforms, (0.0, 0.0, 0.0))
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        model_loc      = uniforms['model']
        color_loc      = uniforms['object_color']
        alpha_loc      = uniforms['alpha']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1
        cube_vao = self.vaos['cube']
        bound_vao = cube_vao

        models, normals = self._frame_transforms(table, brushes)
        colours = table.glow_colour[brushes]
        geometry = (table.class_bits[brushes]
                    & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = self._prepare_geo_meshes(table, brushes)


        # An emissive brush (neon) is a light source, not a lit surface: it
        # is drawn with no lights, so it is the same brightness end to end
        # instead of brightest next to its own light. Looked at only when the
        # map has one.
        emissive = None
        lights_loc, lights_on, lights_now = -1, 0, 0
        if len(table.emissive_slots):
            emissive = table.is_emissive(brushes)
            if emissive.any():
                # The count _upload_lights_once just set (and remembers).
                lights_loc = uniforms['active_lights']
                if lights_loc is None:
                    lights_loc = -1
                if lights_loc != -1:
                    lights_on = lights_now = int(
                        getattr(uniforms, 'active_lights', 0) or 0)
            else:
                emissive = None

        upload_model = uniform_rows('mat4', models)
        upload_normal = uniform_rows('mat3', normals)
        upload_colour = uniform_rows('vec3', colours)
        for index in range(len(brushes)):
            self.render_stats.visible_tris += 12
            upload_model(model_loc, index)
            if normal_mat_loc != -1:
                upload_normal(normal_mat_loc, index)
            upload_colour(color_loc, index)
            if emissive is not None and lights_loc != -1:
                wanted = 0 if emissive[index] else lights_on
                if wanted != lights_now:
                    gl.glUniform1i(lights_loc, wanted)
                    lights_now = wanted
            has_geometry = bool(geometry[index])
            gl.glUniform1f(alpha_loc, 1.0)
            mesh = None
            if has_geometry:
                mesh = geo_meshes.get(int(table.geometry_id[int(brushes[index])]))
            if mesh is not None:
                if bound_vao != mesh.vao:
                    gl.glBindVertexArray(mesh.vao)
                    bound_vao = mesh.vao
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
            else:
                if bound_vao != cube_vao:
                    gl.glBindVertexArray(cube_vao)
                    bound_vao = cube_vao
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        if lights_now != lights_on:
            gl.glUniform1i(lights_loc, lights_on)       # as the pass found it
        gl.glBindVertexArray(0)

    # --------------------------------------------------------------------------
    # Terrain
    # --------------------------------------------------------------------------
    @timed_pass('terrain')
    def render_terrain(self, projection, view, camera_pos, terrain, lights, frustum_planes=None):
        """Draw the published terrain: *terrain* is a ``TerrainFrame`` (the
        dense table, its products and this frame's appearance), never the
        terrain object. Every GL name it is drawn with is this renderer's,
        held in a ``TerrainGL`` under the frame's key."""
        if terrain is None or not terrain.enabled:
            return
        terrain_gl = self._terrain_gl(terrain.key)
        # 'Solid Lit': the ground shaded, not textured.
        terrain_gl.untextured = bool(getattr(self, '_solid_look', False))
        # This renderer's program and ground textures, the first time it
        # draws this terrain.
        if terrain_gl.shader_program != self.shaders.get('terrain'):
            terrain_gl.grass_tex = terrain_gl.rock_tex = 0
            terrain_gl.sand_tex = terrain_gl.snow_tex = 0
            self.setup_terrain_shader(terrain_gl)
        self._ensure_terrain_textures(terrain_gl)
        if not (isinstance(lights, tuple) and len(lights) == 2
                and hasattr(lights[0], 'light_color')):
            raise TypeError("terrain rendering requires (LightTable, slots)")
        light_table, light_slots = lights
        max_terrain_lights = shaders.MAX_LIGHTS_TERRAIN
        terrain_lights = (light_table, light_slots)
        if len(light_slots) > max_terrain_lights:
            cx, cy, cz = self._camera_xyz(camera_pos)
            dx = light_table.pos[light_slots, 0] - cx
            dy = light_table.pos[light_slots, 1] - cy
            dz = light_table.pos[light_slots, 2] - cz
            order = np.argsort(dx * dx + dy * dy + dz * dz, kind='stable')
            terrain_lights = (light_table, light_slots[order[:max_terrain_lights]])

        # _upload_lights_once() writes regular uniforms as well as the shared
        # light UBO, so the terrain program must be current before that call.
        # Without this, glUniform1i/uFogEnabled can raise GL_INVALID_OPERATION
        # when terrain follows a pass that has left another program bound.
        gl.glUseProgram(terrain_gl.shader_program)
        self._current_shader = terrain_gl.shader_program
        self._upload_lights_once('terrain', terrain_lights)
        active_lights_count = len(terrain_lights[1])
        gl.glDisable(gl.GL_CULL_FACE)
        stats = self.render_stats
        stats.visible_tris += terrain.triangle_count
        terrain_gl.render(
            terrain, projection, view, camera_pos, frustum_planes,
            active_lights_count,
            shadow_cubemaps=(self._shadow_cubemaps if self.shadows_enabled else None),
            shadow_filter=self._shadow_filter_array(),
            shadow_unit_base=self.SHADOW_TEXTURE_UNIT_BASE,
            env_uniforms=self.env_uniform_values(),
        )
        # The terrain binds its own cube-maps (or placeholders) on the shadow
        # units: the next lit pass binds the renderer's again.
        self.shadow_units_changed()
        stats.terrain_visible_chunks = terrain_gl.visible_chunks
        stats.terrain_culled_chunks = terrain_gl.culled_chunks
        stats.terrain_triangles = terrain_gl.total_triangles
        stats.terrain_drawn_slots = terrain_gl.drawn_slots

    def _terrain_gl(self, key):
        """The GL side of the terrain published under *key*. One terrain is
        drawn at a time: another key means the map's terrain was replaced,
        and what was built for the old one is freed here, with the context
        current."""
        held = self._terrain_gl_state
        if held is None or held[0] != key:
            if held is not None:
                held[1].cleanup()
            held = self._terrain_gl_state = (key, TerrainGL())
        return held[1]

    # --------------------------------------------------------------------------
    # Models
    # --------------------------------------------------------------------------
    @timed_pass('models')
    def draw_models_instanced(self, projection, view, camera_pos, table, slots,
                              lights, config=None):
        """Render model instances from dense EntityTable columns.

        Entity objects are not touched here. Model resources are resolved once
        per distinct cold recipe; instance transforms and visibility remain
        numeric all the way to the reusable GPU staging buffer.
        """
        if not len(slots):
            return 0
        if not (self.shaders.get('lit_instanced') or self.shaders.get('textured_instanced')):
            return 0

        # Model rendering has always been double-sided at the renderer level.
        # Brush passes are free to leave face culling enabled for their own
        # geometry, but OBJ winding is authored per asset and must not make a
        # valid model vanish in the entity pass.
        cull_was_enabled = gl.glIsEnabled(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_CULL_FACE)

        count = len(slots)
        if count == 1 and float(table.render_alpha[int(slots[0])]) >= 1.0:
            # Opaque singletons do not benefit from instancing. A fading row must
            # stay on the instanced path because opacity is per-instance data.
            drawn = 1 if self._draw_dense_model_single(
                projection, view, table, slots[0], lights) else 0
            if cull_was_enabled:
                gl.glEnable(gl.GL_CULL_FACE)
            else:
                gl.glDisable(gl.GL_CULL_FACE)
            return drawn
        if len(self._model_recipe_scratch) < count:
            grown = max(64, len(self._model_recipe_scratch) * 2, count)
            self._model_recipe_scratch = np.empty(grown, dtype=np.int32)
            self._model_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        recipe_ids = self._model_recipe_scratch[:count]
        np.take(table.model_recipe_id, slots, out=recipe_ids)
        order, starts = sort_into_runs(recipe_ids)
        sorted_slots = self._model_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)

        # Every run's rows in one buffer, uploaded once (or not at all when
        # unchanged); each run's draws are aimed at its first row.
        stream = self._model_instance_stream(self._stream_key('models'))
        self._fill_model_instance_stream(stream, table, sorted_slots)

        recipes = table.model_recipes()
        current_shader = None
        # 'Solid Lit': models shaded in their colours, no textures.
        solid = bool(getattr(self, '_solid_look', False))
        for start, end in zip(starts[:-1], starts[1:]):
            if start == end:
                continue
            recipe_id = int(recipe_ids[order[start]])
            if recipe_id < 0 or recipe_id >= len(recipes):
                continue
            model_path, manual_texture, override_color, tint = recipes[recipe_id]
            if solid:
                manual_texture = None
            obj = self.load_model(model_path)
            if not obj or not obj.is_loaded:
                continue

            run_slots = sorted_slots[start:end]
            self.render_stats.visible_tris += (obj.vertex_count // 3) * len(run_slots)
            self._ensure_model_instance_vao(obj.vao)
            gl.glBindVertexArray(obj.vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
            self._point_model_instances_at(start)

            groups = self._model_draw_groups(obj)
            if manual_texture:
                shader_kind = (
                    'textured' if self.shaders.get('textured_instanced')
                    else 'lit' if self.shaders.get('lit_instanced') else None)
                if not shader_kind:
                    continue
                shader_name = shader_kind + '_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                _set_model_emissive(u, (0.0, 0.0, 0.0))
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    self._manual_model_texture_id(manual_texture))
                gl.glUniform1f(u['alpha'], 1.0)
                if shader_kind != 'textured':
                    colour = _tinted(override_color or (0.8, 0.8, 0.8), tint)
                    gl.glUniform3fv(u['object_color'], 1, colour)
                elif tint:
                    _set_model_tint(u, tint)
                gl.glDrawArraysInstanced(
                    gl.GL_TRIANGLES, 0, obj.vertex_count, len(run_slots))
                if tint and shader_kind == 'textured':
                    _set_model_tint(u, None)
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1
                continue

            if not groups:
                if not self.shaders.get('lit_instanced'):
                    continue
                shader_kind = 'lit'
                shader_name = 'lit_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                _set_model_emissive(u, (0.0, 0.0, 0.0))
                if shader_kind == 'lit':
                    gl.glUniform3fv(u['object_color'], 1, _tinted((0.8, 0.8, 0.8), tint))
                    gl.glUniform1f(u['alpha'], 1.0)
                gl.glDrawArraysInstanced(
                    gl.GL_TRIANGLES, 0, obj.vertex_count, len(run_slots))
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1
                continue

            for group in groups:
                shader_kind = (
                    'textured' if (group.textured and not solid
                                   and self.shaders.get('textured_instanced'))
                    else 'lit' if self.shaders.get('lit_instanced') else None)
                if not shader_kind:
                    continue
                shader_name = shader_kind + '_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                _set_model_emissive(u, group.emissive)
                if shader_kind == 'textured':
                    gl.glBindTexture(gl.GL_TEXTURE_2D, group.texture)
                    if tint:
                        _set_model_tint(u, tint)
                else:
                    colour = _tinted(group.colour, tint)
                    gl.glUniform3fv(u['object_color'], 1, colour)
                with _model_material(u, group.alpha):
                    if group.indexed:
                        gl.glDrawElementsInstanced(
                            gl.GL_TRIANGLES, group.count, gl.GL_UNSIGNED_INT,
                            ctypes.c_void_p(group.start * 4), len(run_slots))
                    else:
                        gl.glDrawArraysInstanced(
                            gl.GL_TRIANGLES, group.start, group.count, len(run_slots))
                if tint and shader_kind == 'textured':
                    _set_model_tint(u, None)
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1

        gl.glBindVertexArray(0)
        if cull_was_enabled:
            gl.glEnable(gl.GL_CULL_FACE)
        else:
            gl.glDisable(gl.GL_CULL_FACE)
        return count

    def _draw_dense_model_single(self, projection, view, table, slot, lights):
        """Draw one dense model row through the proven uniform model path.

        A single model does not benefit from instancing. More importantly, this
        keeps the one-model case independent of instanced-attribute driver
        quirks while preserving the dense EntityTable boundary: no Thing is
        materialised just to submit the draw.
        """
        slot = int(slot)
        recipe_id = int(table.model_recipe_id[slot])
        recipes = table.model_recipes()
        if recipe_id < 0 or recipe_id >= len(recipes):
            return 0

        model_path, manual_texture, override_color, tint = recipes[recipe_id]
        solid = bool(getattr(self, '_solid_look', False))   # 'Solid Lit'
        if solid:
            manual_texture = None
        obj = self.load_model(model_path)
        if not obj or not obj.is_loaded:
            return 0

        groups = self._model_draw_groups(obj)
        if manual_texture:
            shader_kind = (
                'textured' if self.shaders.get('textured') else
                'lit' if self.shaders.get('lit') else None)
            if shader_kind is None:
                return 0
            shader_name = shader_kind
            current_shader = self._prepare_model_shader(
                shader_name, projection, view, lights, None)
            if current_shader != shader_name:
                return 0
            u = self.uniforms[shader_name]
            if shader_kind == 'textured':
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    self._manual_model_texture_id(manual_texture))
            else:
                gl.glUniform3fv(
                    u['object_color'], 1,
                    _tinted(override_color or (0.8, 0.8, 0.8), tint))
                gl.glUniform1f(u['alpha'], 1.0)
            groups_to_draw = (None,)
        elif groups:
            groups_to_draw = groups
        else:
            groups_to_draw = (None,)

        base = np.asarray(table.model_base_matrix[slot], dtype=np.float32).copy()
        base[12:15] = np.asarray(table.pos[slot], dtype=np.float32)
        normal = np.asarray(
            table.model_normal_matrix[slot].reshape(3, 4)[:, :3],
            dtype=np.float32,
        ).reshape(-1).copy()

        current_shader = None
        draws = 0
        for group in groups_to_draw:
            # A whole-model draw (no groups, or a manual texture) takes the
            # row's own colour and texture; a group, its material's.
            if group is not None:
                use_texture, colour, alpha = (group.textured and not solid,
                                              group.colour, group.alpha)
            else:
                use_texture = bool(manual_texture)
                colour, alpha = override_color or (0.8, 0.8, 0.8), 1.0
            shader_kind = (
                'textured' if use_texture and self.shaders.get('textured') else
                'lit' if self.shaders.get('lit') else None)
            if shader_kind is None:
                continue

            shader_name = shader_kind
            current_shader = self._prepare_model_shader(
                shader_name, projection, view, lights, current_shader)
            if current_shader != shader_name:
                continue

            u = self.uniforms[shader_name]
            _set_model_emissive(
                u, group.emissive if group is not None else (0.0, 0.0, 0.0))
            if shader_kind == 'textured':
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    group.texture if group is not None
                    else self._manual_model_texture_id(manual_texture))
                if tint:
                    _set_model_tint(u, tint)
            else:
                gl.glUniform3fv(u['object_color'], 1, _tinted(colour, tint))

            gl.glUniformMatrix4fv(
                u['model'], 1, gl.GL_FALSE, base)
            normal_loc = u.get('normalMatrix', -1)
            if normal_loc >= 0:
                gl.glUniformMatrix3fv(
                    normal_loc, 1, gl.GL_FALSE, normal)

            gl.glBindVertexArray(obj.vao)
            with _model_material(u, alpha):
                if group is None:
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)
                elif group.indexed:
                    gl.glDrawElements(
                        gl.GL_TRIANGLES, group.count, gl.GL_UNSIGNED_INT,
                        ctypes.c_void_p(group.start * 4))
                else:
                    gl.glDrawArrays(
                        gl.GL_TRIANGLES, group.start, group.count)
            if tint and shader_kind == 'textured':
                _set_model_tint(u, None)
            self.render_stats.draw_calls += 1
            draws += 1

        gl.glBindVertexArray(0)
        return draws

    def _prepare_model_shader(self, shader_name, projection, view, lights, current_shader):
        program = self.shaders.get(shader_name)
        if not program:
            return current_shader
        if current_shader != shader_name:
            gl.glUseProgram(program)
            u = self.uniforms[shader_name]
            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
            self._upload_lights_once(shader_name, lights)
            if shader_name.startswith('textured'):
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glUniform1i(u['texture_diffuse'], 0)
                if u.get('tex_scale', -1) != -1:
                    gl.glUniform2f(u['tex_scale'], 1.0, 1.0)
                if u.get('tex_angle', -1) != -1:
                    gl.glUniform1f(u['tex_angle'], 0.0)
                if u.get('tex_shift', -1) != -1:
                    gl.glUniform2f(u['tex_shift'], 0.0, 0.0)
            current_shader = shader_name
        return current_shader

    # --------------------------------------------------------------------------
    # Sprites, billboards and glasses
    # --------------------------------------------------------------------------
    def _water_probe_textures(self, table, brushes):
        """Per water brush, the snapshot of the nearest reflection probe whose
        radius holds the brush's centre (0: none, the sky as usual), or None
        when no water is near a probe -- the common case, which changes
        nothing about the pass."""
        frame = self._probe_frame
        if frame is None or self._probe_capture_pass or not len(brushes):
            return None
        positions, radii, textures = frame
        centres = np.asarray(table.bounds[brushes, 0:3], dtype=np.float64)
        d2 = ((centres[:, None, :] - positions[None, :, :]) ** 2).sum(axis=2)
        inside = (d2 <= (radii * radii)[None, :]) & (textures > 0)[None, :]
        if not inside.any():
            return None
        nearest = np.where(inside, d2, np.inf).argmin(axis=1)
        return np.where(inside.any(axis=1), textures[nearest], 0)

    def _warm_sprite_layers(self, table, slots, play_mode, show_sprites):
        """Put every image the sprite pass may draw for *slots* into the layer
        array (engine.sprite_layers) in one import: each sprite, each of a
        monster's eight facings and the frames a fight switches it to
        (EntityTable._intern_fight_frames), near or far, hidden or not. Done when the
        table's sprite recipes change -- the map loads, Play starts, something
        new appears -- not per frame. Imported one at a time as each sprite
        first came into view, the array grew while it filled and every growth
        re-uploaded every layer: 46 and 130 ms frames into _SHOWCASE's Play."""
        if table is None or not len(slots) or 'sprite_layered' not in self.shaders:
            return
        recipes = table.sprite_recipes()
        seen = self._sprite_warm_seen
        entry = seen.get(id(recipes))
        if entry is not None and entry[0] is recipes and entry[1] == len(recipes):
            return
        if len(seen) >= 4:              # the render buffers' tables, and a spare
            seen.clear()
        seen[id(recipes)] = (recipes, len(recipes))
        from engine.entity_table import classify_slots
        _models, sprites = classify_slots(
            table, slots, np.zeros(len(table.class_bits), dtype=bool),
            play_mode, show_sprites)
        if not len(sprites):
            return
        fight = [sid for sid, recipe in enumerate(recipes)
                 if recipe and str(recipe[0][0]).startswith('msprite_')]
        keys = np.concatenate((table.sprite_key_id[sprites],
                               table.sprite_direction_ids[sprites].ravel(),
                               np.asarray(fight, dtype=table.sprite_key_id.dtype)))
        keys = keys[keys >= 0]
        gl_ids = self._sprite_gl_ids(table)
        if not len(keys) or not len(gl_ids):
            return
        textures = np.unique(gl_ids[keys])
        textures = textures[textures > 0]
        layers = self._sprite_layer_array()
        # Never more than the array can hold: an import that does not fit
        # turns the array off for good (SpriteLayers.layers_for), whereas
        # sprites imported as they are seen may never all be on screen.
        if len(textures) and layers.count + len(textures) <= layers._layer_limit():
            layers.layers_for(textures)

    @timed_pass('sprites')
    def draw_sprites_instanced(self, projection, view, table, slots,
                               gl_ids=None, camera_pos=None):
        """The sprite pass over dense columns: one draw, back to front.

        *slots* are rows of an :class:`engine.entity_table.EntityTable`, already
        classified into the sprite pass.  Everything this needs is a column
        read: the centre from ``pos``, the size from ``sprite_size``, the
        texture from ``sprite_key_id`` through :meth:`_sprite_gl_ids`.  No
        entity is touched.

        Rows whose texture resolves to 0 are dropped, which is what the object
        path's ``if tex_id:`` did.  The rest are ordered back to front by XZ
        distance when a camera is supplied -- the pass is blended with depth
        writes off, so that order is part of the picture -- and each sprite's
        image is a layer of one texture array (:mod:`engine.sprite_layers`), so
        the ordered set is a single ``glDrawArraysInstanced``.  Grouping by
        texture instead would draw a far sprite over a near one wherever two
        different images overlap.

        Returns the number of sprites submitted, so a caller can tell an empty
        pass from a skipped one.
        """
        if 'sprite_instanced' not in self.shaders or not len(slots):
            return 0
        if gl_ids is None:
            gl_ids = self._sprite_gl_ids(table)

        slot_count = len(slots)
        if len(self._sprite_key_scratch) < slot_count:
            grown = max(64, len(self._sprite_key_scratch) * 2, slot_count)
            self._sprite_key_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_texture_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_draw_mask = np.empty(grown, dtype=bool)
            self._sprite_depth_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_depth_aux_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        key_ids = self._sprite_key_scratch[:slot_count]
        textures = self._sprite_texture_scratch[:slot_count]
        drawn = self._sprite_draw_mask[:slot_count]

        from engine.monster_facing import sprite_keys_for_view
        eye = None if camera_pos is None else self._camera_xyz(camera_pos)
        key_ids[:] = sprite_keys_for_view(table, slots, eye)
        drawn[:] = key_ids >= 0
        if len(gl_ids):
            np.maximum(key_ids, 0, out=key_ids)
            np.take(gl_ids, key_ids, out=textures)
            drawn &= textures > 0
        else:
            drawn.fill(False)

        valid_count = int(np.count_nonzero(drawn))
        if valid_count == 0:
            return 0
        if valid_count != slot_count:
            slots = slots[drawn]
            textures = textures[drawn]

        # Back-to-front, by the XZ distance the pass has always used. This is
        # the order the pass *must* draw in -- it is blended with depth writes
        # off -- so nothing below is allowed to reorder it.
        count = len(slots)
        if camera_pos is None:
            order = np.argsort(table.sprite_overlay[slots], kind='stable')
        else:
            cx, _, cz = self._camera_xyz(camera_pos)
            depth_sq = self._sprite_depth_scratch[:count]
            depth_aux = self._sprite_depth_aux_scratch[:count]
            np.take(table.pos[:, 0], slots, out=depth_sq)
            np.subtract(depth_sq, cx, out=depth_sq)
            np.square(depth_sq, out=depth_sq)
            np.take(table.pos[:, 2], slots, out=depth_aux)
            np.subtract(depth_aux, cz, out=depth_aux)
            np.square(depth_aux, out=depth_aux)
            np.add(depth_sq, depth_aux, out=depth_sq)
            np.negative(depth_sq, out=depth_aux)
            # A flag attached at its carrier's centre must paint over that
            # body regardless of map entity order. World depth still applies.
            depth_aux += table.sprite_overlay[slots] * 0.001
            order = np.argsort(depth_aux, kind='stable')
        ordered_textures = textures[order]

        # The texture leaves the draw state when every image is a layer of one
        # array: then the whole pass is a single draw in exactly that order.
        layers = None
        if 'sprite_layered' in self.shaders:
            layers = self._sprite_layer_array().layers_for(ordered_textures)

        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        sorted_slots = self._sprite_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)
        # Gather directly into the reusable GPU staging buffer. The explicit
        # out= avoids a temporary (N,3)/(N,2) array on every sprite frame.
        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.sprite_size, sorted_slots, axis=0, out=data[:, 3:5])
        np.take(table.sprite_fixed_yaw, sorted_slots, out=data[:, 5])
        np.take(table.render_alpha, sorted_slots, out=data[:, 6])
        # Blood on the sprite rides on the opacity (see EntityTable.sprite_wound).
        data[:, 6] += table.sprite_wound[sorted_slots]
        if layers is not None:
            data[:, 7] = layers
        else:
            data[:, 7] = 0.0
        self._upload_sprite_instances(data)

        program = 'sprite_layered' if layers is not None else 'sprite_instanced'
        shader, uniforms = self.shaders[program], self.uniforms[program]
        gl.glUseProgram(shader)
        self._current_shader = shader
        # Billboards are unlit, so they never reach _upload_lights_once -- they
        # still need fogging, exactly as the per-sprite path does.
        self._upload_env_uniforms(program)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['use_fixed_facing'], 1)
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())

        if layers is not None:
            gl.glUniform1i(uniforms['sprite_layers'], 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._sprite_layers.texture)
            self._point_sprite_instances_at(0)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            self.render_stats.draw_calls += 1
            self.render_stats.batched_draws += 1
        else:
            # No array (a driver without the blit path, or more sprite images
            # than it allows layers): one draw per equal-texture stretch *of
            # the depth-ordered sequence*, which costs draws but never order.
            gl.glUniform1i(uniforms['sprite_texture'], 0)
            run_starts = runs_in_order(ordered_textures)
            for run in range(len(run_starts) - 1):
                begin = int(run_starts[run])
                length = int(run_starts[run + 1]) - begin
                gl.glBindTexture(gl.GL_TEXTURE_2D, int(ordered_textures[begin]))
                self.render_stats.batched_draws += 1
                self._point_sprite_instances_at(begin)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, length)
                self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        return count

    def draw_billboards(self, projection, view, positions, size, tex_id):
        """Camera-facing billboards sharing one texture and size: one draw.

        For the dense runtime populations that are not entities -- monster
        projectiles -- which used to cost three GL calls each per frame.
        """
        positions = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
        count = len(positions)
        if not count or 'sprite_instanced' not in self.shaders or not tex_id:
            return 0
        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        data[:, 0:3] = positions
        data[:, 3] = float(size[0])
        data[:, 4] = float(size[1])
        data[:, 5] = -10000.0
        data[:, 6] = 1.0
        data[:, 7] = 0.0
        self._upload_sprite_instances(data)
        shader, uniforms = (self.shaders['sprite_instanced'],
                            self.uniforms['sprite_instanced'])
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('sprite_instanced')
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glUniform1i(uniforms['use_fixed_facing'], 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, int(tex_id))
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())
        self._point_sprite_instances_at(0)
        gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
        gl.glBindVertexArray(0)
        self.render_stats.draw_calls += 1
        return count

    def draw_player_glasses(self, projection, view, positions,
                           width=40.0, height=18.0, lift=0.0, sprites=()):
        """Draw the player as the fixed glasses billboard.

        Player bodies are deliberately not EntityTable rows, so this is the
        small non-entity billboard path used only for player representation
        (split-screen and portal virtual scenes). It reuses the existing sprite
        shader/VAO and performs at most two draws in a normal split-screen view.
        *positions* are the published ``player_glasses_positions`` and are
        already at eye height. *lift* is an optional caller-supplied offset;
        the default is zero so portal and split-screen views do not apply a
        second eye-height offset. *sprites*
        holds the ``sprite_textures`` key each position wears (player 1's
        chosen style, see :mod:`engine.glasses`); missing or unloaded keys
        fall back to the default pair.
        """
        if not positions or 'sprite' not in self.shaders:
            return 0
        default_tex = self.sprite_textures.get('Glasses')
        if not default_tex:
            return 0
        vao = self.vaos.get('sprite')
        if not vao:
            return 0

        shader = self.shaders['sprite']
        uniforms = self.uniforms['sprite']
        gl.glUseProgram(shader)
        self._current_shader = shader
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection)
        )
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view)
        )
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindVertexArray(vao)

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        pos_loc = uniforms['sprite_pos_world']
        size_loc = uniforms['sprite_size']

        count = 0
        sprites = tuple(sprites or ())
        bound = None
        try:
            for i, pos in enumerate(positions):
                key = sprites[i] if i < len(sprites) else None
                tex_id = int(self.sprite_textures.get(key) or default_tex)
                if tex_id != bound:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    bound = tex_id
                try:
                    px, py, pz = float(pos.x), float(pos.y), float(pos.z)
                except AttributeError:
                    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
                gl.glUniform3f(pos_loc, px, py + float(lift), pz)
                gl.glUniform2f(size_loc, float(width), float(height))
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
                count += 1
        finally:
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(0)
        return count

    # --------------------------------------------------------------------------
    # Effects
    # --------------------------------------------------------------------------
    @timed_pass('effects')
    def draw_effects_instanced(
        self, projection, view, table, slots, hidden=None,
        play_mode=True, editor_time=0.0, camera_pos=None,
    ):
        """Draw FIRE/ORB as animated billboards, EXPLOSION through its
        existing shader, then every Effect's particle emitter."""
        if not len(slots):
            return 0

        slots = np.asarray(slots, dtype=np.int32)
        if hidden is not None:
            live = ~np.asarray(hidden, dtype=bool)[slots]
            slots = slots[live]
        if not len(slots):
            return 0

        fire_count = self.draw_fire_effects_instanced(
            projection, view, table, slots, hidden=None, camera_pos=camera_pos
        )

        explosion_count = self._draw_explosions_instanced(
            projection, view, table, slots, camera_pos)
        particle_count = self.draw_particles_instanced(
            projection, view, table, slots, play_mode=play_mode,
            camera_pos=camera_pos)
        return fire_count + explosion_count + particle_count

    def _draw_explosions_instanced(self, projection, view, table, slots,
                                   camera_pos):
        """Draw the live EXPLOSION Effects among *slots* in one instanced
        draw; their count.

        A row is authored data only (explosion_rows.py), built by a loop for
        the usual handful and by NumPy for many, and uploaded only when the
        rows differ from the last upload; the shader takes the time from a
        uniform. Soft-edged against the opaque scene's depth, which the
        particles drawn next reuse. GL state is put back as it was found.
        """
        explosion = table.effect_type[slots] == FAMILY_EXPLOSION
        slots = slots[explosion & table.effect_alive[slots]]
        if not len(slots) or 'effect_instanced' not in self.shaders:
            return 0

        camera_xz = None
        if camera_pos is not None:
            cx, _, cz = self._camera_xyz(camera_pos)
            camera_xz = (float(cx), float(cz))
        rows = explosion_rows(table, slots, camera_xz, _CLOCK_ORIGIN)
        count = len(rows)
        self._ensure_effect_instance_buffer(count)
        vao = self._ensure_effect_instance_vao()
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._effect_instance_vbo)
        if not same_rows(rows, self._explosion_uploaded):
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, as_float32(rows))
            self._explosion_uploaded = rows

        shader = self.shaders['effect_instanced']
        uniforms = self.uniforms['effect_instanced']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('effect_instanced')
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['uTime'], float(
            (time.perf_counter() - _CLOCK_ORIGIN) % EXPLOSION_TIME_WRAP))

        prev_active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        # Explosions write no depth, so a copy made here is still the opaque
        # scene's when the particles draw: _bind_scene_depth leaves it marked
        # current for them.
        self._bind_scene_depth(uniforms, projection)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        if self.effect_explosion_texture:
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.effect_explosion_texture)
        gl.glUniform1i(uniforms['explosion_texture'], 0)

        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        depth_test_was = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        depth_mask_was = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        blend_func = tuple(int(gl.glGetIntegerv(name)) for name in (
            gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
            gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        try:
            gl.glBindVertexArray(vao)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
            self.render_stats.draw_calls += 1
            self.render_stats.batched_draws += 1
        finally:
            gl.glBindVertexArray(0)
            gl.glBlendFuncSeparate(*blend_func)
            if not blend_was:
                gl.glDisable(gl.GL_BLEND)
            if cull_was:
                gl.glEnable(gl.GL_CULL_FACE)
            if not depth_test_was:
                gl.glDisable(gl.GL_DEPTH_TEST)
            gl.glDepthMask(gl.GL_TRUE if depth_mask_was else gl.GL_FALSE)
            gl.glActiveTexture(prev_active_texture)
        return count

    def _bind_scene_depth(self, uniforms, projection):
        """Give a soft-edged pass the opaque scene's depth: set its
        ``uSoft``, ``uSceneDepth``, ``uViewport`` and ``uDepthParams``, and
        bind the copy on the scene-depth unit. The decal pass's copy when it
        made one this frame (``_scene_depth_current``), else a copy made now,
        which is then marked current for the passes after. Perspective views
        only. Returns whether the pass is soft."""
        perspective = abs(float(projection[2][3])) > 0.5
        soft = perspective and (
            getattr(self, '_scene_depth_current', False)
            or self._capture_scene_depth())
        if soft:
            self._scene_depth_current = True
        unit = self._water_depth_texture_unit
        gl.glUniform1i(uniforms['uSoft'], 1 if soft else 0)
        gl.glUniform1i(uniforms['uSceneDepth'], unit)
        if soft:
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._water_depth_texture)
            viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
            gl.glUniform4f(uniforms['uViewport'], float(viewport[0]),
                           float(viewport[1]), float(max(int(viewport[2]), 1)),
                           float(max(int(viewport[3]), 1)))
            gl.glUniform2f(uniforms['uDepthParams'], float(projection[3][2]),
                           float(projection[2][2]))
        return soft

    # --------------------------------------------------------------------------
    # Particle emitters
    # --------------------------------------------------------------------------
    #: The largest a particle may be drawn, as a fraction of the view's height.
    PARTICLE_MAX_SCREEN = 0.35
    #: View depth over which particles fade in from nothing at the camera.
    PARTICLE_NEAR_FADE = (4.0, 32.0)
    #: How far above a FIRE/ORB/CUSTOM's centre its emitter sits, as a
    #: fraction of the Effect's height (the flame card is centred there).
    PARTICLE_HOST_LIFT = 0.25
    #: The same for an EXPLOSION, whose card grows upward from its base.
    PARTICLE_BURST_LIFT = 0.6
    #: A burst is drawn for this multiple of its preset's particle life after
    #: the trigger (the shader staggers spawns by up to 15% of it).
    PARTICLE_BURST_WINDOW = 1.2

    _PARTICLE_PRESET_TABLE = shaders.particle_preset_table()
    _PARTICLE_PRESET_LIVES = shaders.particle_preset_lives()

    @timed_pass('particles')
    def draw_particles_instanced(
        self, projection, view, table, slots, play_mode=True, camera_pos=None,
    ):
        """Draw every emitter among the Effect *slots* in one instanced draw.

        No particle has CPU state: each emitter is one row of 20 floats that
        feeds ``PARTICLES_PER_EMITTER`` instances, and particle.vert works
        out each particle from (seed, index, time). The rows are built with
        numpy and uploaded only when one of them, or the back-to-front order,
        changes; the clock is a uniform. Returns the emitters drawn.
        """
        if not len(slots) or 'particle_instanced' not in self.shaders:
            return 0
        slots = slots[table.effect_emitter[slots] != 0]
        if not len(slots):
            return 0

        now = time.perf_counter()
        family = table.effect_type[slots]
        burst = family == FAMILY_EXPLOSION
        if burst.any():
            # A looping emitter draws while its Effect is alive (always, for
            # all but EXPLOSION). A burst plays from its trigger until its
            # particles have all died -- past the explosion's own lifetime --
            # in a game or played from the editor's panel; a dormant one
            # shows only as the editor's frozen Preview (alive just then).
            active = table.effect_active[slots]
            lives = self._PARTICLE_PRESET_LIVES[table.effect_emitter[slots]]
            since = now - table.effect_spawn_time[slots]
            playing = (active & (since >= 0.0)
                       & (since < lives * self.PARTICLE_BURST_WINDOW))
            burst_live = np.where(active, playing, table.effect_alive[slots])
            keep = np.where(burst, burst_live, table.effect_alive[slots])
        else:
            keep = table.effect_alive[slots]
        if not keep.all():
            slots, family, burst = slots[keep], family[keep], burst[keep]
        count = len(slots)
        if not count:
            return 0

        if camera_pos is not None and count > 1:
            # Back to front by distance across the floor, as the other
            # effect passes sort.
            cx, _, cz = self._camera_xyz(camera_pos)
            dx = table.pos[slots, 0] - cx
            dz = table.pos[slots, 2] - cz
            order = np.argsort(-(dx * dx + dz * dz), kind='stable')
            slots, family, burst = slots[order], family[order], burst[order]

        vao = self._ensure_particle_instance_vao()
        self._ensure_particle_instance_buffer(count)
        data = self._particle_instance_data[:count]
        params = table.effect_emitter_params[slots]
        wrap = shaders.PARTICLE_TIME_WRAP
        data[:, 0:3] = table.pos[slots]
        data[:, 3] = table.effect_seed[slots]
        data[:, 4] = table.effect_emitter[slots]
        data[:, 5] = np.rint(params[:, 0] * shaders.PARTICLES_PER_EMITTER)
        data[:, 6] = params[:, 1]
        # 0 loops, 1 a burst, 2 a burst frozen for the editor's Preview.
        data[:, 7] = np.where(
            burst,
            np.where(table.effect_preview[slots] & ~table.effect_active[slots],
                     2.0, 1.0),
            0.0)
        data[:, 8:10] = params[:, 2:4]
        # Playback start on the wrapped render clock (0 for a looping
        # Effect, which runs on the shared clock origin).
        data[:, 10] = np.mod(table.effect_spawn_time[slots] - _CLOCK_ORIGIN, wrap)
        data[:, 11] = np.where(
            family == FAMILY_PARTICLES, 0.0,
            np.where(burst, self.PARTICLE_BURST_LIFT, self.PARTICLE_HOST_LIFT))
        data[:, 12:15] = table.effect_emitter_tint[slots]
        data[:, 15] = table.effect_light_enabled[slots]
        data[:, 16:19] = table.effect_light_color[slots]
        data[:, 19] = 0.0

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._particle_instance_vbo)
        uploaded = self._particle_uploaded
        if (uploaded is None or uploaded.shape != data.shape
                or not np.array_equal(uploaded, data)):
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)
            self._particle_uploaded = data.copy()

        shader = self.shaders['particle_instanced']
        uniforms = self.uniforms['particle_instanced']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('particle_instanced')
        if self._particle_presets_sent != shader:
            table_rows = self._PARTICLE_PRESET_TABLE
            gl.glUniform4fv(uniforms['uPresets'], len(table_rows), table_rows)
            self._particle_presets_sent = shader
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['uTime'], float((now - _CLOCK_ORIGIN) % wrap))
        gl.glUniform1f(uniforms['uMaxScreen'], self.PARTICLE_MAX_SCREEN)
        gl.glUniform2f(uniforms['uNearFade'], *self.PARTICLE_NEAR_FADE)

        prev_active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        # Soft particles read the opaque depth: the decal pass's (or the
        # explosions') copy when there is one this frame, else a copy now.
        self._bind_scene_depth(uniforms, projection)
        # Consumed: a later frame (or a pass outside render_scene) copies
        # its own unless the decal pass has again.
        self._scene_depth_current = False

        # Blended premultiplied, tested against depth but writing none,
        # both faces; every piece of state is put back as it was.
        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        depth_test_was = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        depth_mask_was = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        blend_func = tuple(int(gl.glGetIntegerv(name)) for name in (
            gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
            gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        try:
            gl.glBindVertexArray(vao)
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLE_STRIP, 0, 4,
                count * shaders.PARTICLES_PER_EMITTER)
            self.render_stats.draw_calls += 1
            self.render_stats.batched_draws += 1
        finally:
            gl.glBindVertexArray(0)
            gl.glBlendFuncSeparate(*blend_func)
            if not blend_was:
                gl.glDisable(gl.GL_BLEND)
            if cull_was:
                gl.glEnable(gl.GL_CULL_FACE)
            if not depth_test_was:
                gl.glDisable(gl.GL_DEPTH_TEST)
            gl.glDepthMask(gl.GL_TRUE if depth_mask_was else gl.GL_FALSE)
            gl.glActiveTexture(prev_active_texture)
        return count

    @timed_pass('fire effects')
    def draw_fire_effects_instanced(
        self, projection, view, table, slots, hidden=None, camera_pos=None,
    ):
        """Draw FIRE and ORB as normal instanced billboards using decoded GIF frames."""
        if 'sprite_instanced' not in self.shaders or not len(slots):
            return 0

        slots = np.asarray(slots, dtype=np.int32)
        if hidden is not None:
            live = ~np.asarray(hidden, dtype=bool)[slots]
            slots = slots[live]
        if not len(slots):
            return 0

        effect_types = table.effect_type[slots]
        alive = table.effect_alive[slots]
        # EXPLOSION has its own shader; PARTICLES is an emitter alone.
        animated = ((effect_types != 1) & (effect_types != FAMILY_PARTICLES)
                    & alive)
        slots = slots[animated]
        effect_types = effect_types[animated]
        if not len(slots):
            return 0

        count = len(slots)
        if len(self._sprite_texture_scratch) < count:
            grown = max(64, len(self._sprite_texture_scratch) * 2, count)
            self._sprite_texture_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_draw_mask = np.empty(grown, dtype=bool)
            self._sprite_depth_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_depth_aux_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        textures = self._sprite_texture_scratch[:count]
        drawn = self._sprite_draw_mask[:count]
        textures.fill(0)

        variants = table.effect_fire_variant[slots]
        custom_ids = table.effect_custom_id[slots]
        custom_loops = table.effect_custom_loop[slots]
        # Animated GIFs are visual-time data. Sample the clock on the render
        # thread rather than consuming the logic thread's per-frame elapsed
        # snapshot. That removes visible frame quantisation when render and
        # logic rates differ, while keeping the dense EntityTable as the source
        # of the animation's spawn timestamps.
        elapsed = np.maximum(
            time.perf_counter() - table.effect_spawn_time[slots], 0.0
        ).astype(np.float32, copy=False)

        # There are only five authored variants per animated Effect family.
        # This bounded 10-way loop replaces an entity-by-entity Python loop
        # while allowing FIRE and ORB to use separate GIF sets.
        for effect_kind in (0, 2):
            kind_mask = effect_types == effect_kind
            if not np.any(kind_mask):
                continue
            kind_variants = variants[kind_mask]
            kind_elapsed = elapsed[kind_mask]
            kind_positions = np.flatnonzero(kind_mask)
            for variant in range(5):
                mask = kind_variants == variant
                if not np.any(mask):
                    continue
                # Loaded already, unless this view saw the effect first.
                frames, cumulative = self._effect_frames(effect_kind, variant)
                if not frames or cumulative is None or not len(cumulative):
                    continue

                local_elapsed = np.mod(
                    kind_elapsed[mask]
                    + table.effect_phase[slots][kind_mask][mask] * cumulative[-1],
                    cumulative[-1]
                )
                frame_indices = np.searchsorted(
                    cumulative, local_elapsed, side='right'
                )
                frame_indices = np.minimum(
                    frame_indices, len(frames) - 1
                ).astype(np.int32, copy=False)
                positions = kind_positions[np.flatnonzero(mask)]
                textures[positions] = np.asarray(
                    frames, dtype=np.int32
                )[frame_indices]

        # CUSTOM can use any GIF path. Iterate only over unique authored GIFs,
        # never over entities; each path is decoded and uploaded once per renderer.
        custom_mask = effect_types == 3
        if np.any(custom_mask):
            custom_positions = np.flatnonzero(custom_mask)
            custom_values = custom_ids[custom_mask]
            custom_elapsed = elapsed[custom_mask]
            for custom_id in np.unique(custom_values):
                custom_id = int(custom_id)
                if custom_id <= 0:
                    continue
                frames = self.effect_custom_frames.get(custom_id)
                cumulative = self.effect_custom_cumulative.get(custom_id)
                if frames is None:
                    path = table.effect_custom_path(custom_id)
                    if path:
                        frames, cumulative = self._load_fire_gif(path)
                    else:
                        frames, cumulative = (), np.empty(0, dtype=np.float32)
                    self.effect_custom_frames[custom_id] = tuple(frames)
                    self.effect_custom_cumulative[custom_id] = cumulative
                if not frames or cumulative is None or not len(cumulative):
                    continue

                mask = custom_values == custom_id
                loop_values = custom_loops[mask]
                raw_elapsed = custom_elapsed[mask]
                # CUSTOM follows its authored Loop flag. When looping is off,
                # hold the final GIF frame instead of wrapping to frame 1.
                phase_values = table.effect_phase[slots][custom_mask][mask]
                local_elapsed = np.where(
                    loop_values,
                    np.mod(raw_elapsed + phase_values * cumulative[-1], cumulative[-1]),
                    np.minimum(raw_elapsed + phase_values * cumulative[-1], cumulative[-1]),
                )
                frame_indices = np.searchsorted(
                    cumulative, local_elapsed, side='right'
                )
                frame_indices = np.minimum(
                    frame_indices, len(frames) - 1
                ).astype(np.int32, copy=False)
                positions = custom_positions[np.flatnonzero(mask)]
                textures[positions] = np.asarray(
                    frames, dtype=np.int32
                )[frame_indices]

        drawn[:] = textures > 0
        valid_count = int(np.count_nonzero(drawn))
        if valid_count == 0:
            return 0
        if valid_count != count:
            slots = slots[drawn]
            textures = textures[drawn]

        if camera_pos is None:
            order, run_starts = sort_into_runs(textures)
            ordered_textures = textures[order]
        else:
            # Blended with depth writes off, like the entity sprites: the
            # draw order is back to front and runs are only the equal-frame
            # stretches that order happens to contain.
            cx, _, cz = self._camera_xyz(camera_pos)
            fire_count = len(slots)
            depth_sq = self._sprite_depth_scratch[:fire_count]
            depth_aux = self._sprite_depth_aux_scratch[:fire_count]
            np.take(table.pos[:, 0], slots, out=depth_sq)
            np.subtract(depth_sq, cx, out=depth_sq)
            np.square(depth_sq, out=depth_sq)
            np.take(table.pos[:, 2], slots, out=depth_aux)
            np.subtract(depth_aux, cz, out=depth_aux)
            np.square(depth_aux, out=depth_aux)
            np.add(depth_sq, depth_aux, out=depth_sq)
            np.negative(depth_sq, out=depth_aux)
            order = np.argsort(depth_aux, kind='stable')
            ordered_textures = textures[order]
            run_starts = runs_in_order(ordered_textures)

        count = len(order)
        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        sorted_slots = self._sprite_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)
        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.sprite_size, sorted_slots, axis=0, out=data[:, 3:5])
        # The staging array is shared with the entity sprite pass: columns
        # this pass does not use must still be written, or each flame would
        # inherit whatever yaw and opacity the last sprite at that index had.
        data[:, 5] = -10000.0
        data[:, 6] = 1.0
        data[:, 7] = 0.0

        self._upload_sprite_instances(data)

        shader, uniforms = (
            self.shaders['sprite_instanced'],
            self.uniforms['sprite_instanced'],
        )
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('sprite_instanced')
        gl.glUniform1i(uniforms['use_fixed_facing'], 0)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection)
        )
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view)
        )
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())

        current_tex = None
        for run in range(len(run_starts) - 1):
            begin = int(run_starts[run])
            length = int(run_starts[run + 1]) - begin
            if length <= 0:
                continue
            tex_id = int(ordered_textures[begin])
            if tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            self._point_sprite_instances_at(begin)
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLE_STRIP, 0, 4, length
            )
            self.render_stats.draw_calls += 1

        if self._effects_write_depth:
            self._write_effect_depth(uniforms, ordered_textures, run_starts)
            # Centre and size of the nearest few (drawn back to front), for
            # the water pass to reflect: numbers from the instance buffer.
            self._reflected_effects = data[-self.MAX_REFLECTED_EFFECTS:, 0:5].copy()
        gl.glBindVertexArray(0)
        return count

    #: An effect's texels at least this opaque count as solid for reflections.
    EFFECT_DEPTH_ALPHA = 0.5
    #: Effects one water pass reflects (water.frag MAX_REFLECTED_EFFECTS).
    MAX_REFLECTED_EFFECTS = 4
    #: How far across the water an effect's reflection is looked for, as a
    #: multiple of its height above the surface.
    EFFECT_REFLECTION_REACH = 4.0

    def _write_effect_depth(self, uniforms, ordered_textures, run_starts):
        """Write the solid part of the effects just drawn into the depth
        buffer (colour untouched), so high-quality water finds them.

        Water reflects by marching the copied depth buffer; effects are
        blended and write no depth, so a reflected ray passed straight
        through an orb floating over a pool. Only texels at least
        EFFECT_DEPTH_ALPHA opaque are written: a flame's soft edge stays
        depthless, as before. Done only in a frame that has high-quality
        water (render_scene sets ``_effects_write_depth``)."""
        cutoff = uniforms.get('alphaCutoff', -1)
        if cutoff is None or cutoff < 0:
            return
        depth_write = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glUniform1f(cutoff, self.EFFECT_DEPTH_ALPHA)
        current_tex = None
        try:
            for run in range(len(run_starts) - 1):
                begin = int(run_starts[run])
                length = int(run_starts[run + 1]) - begin
                if length <= 0:
                    continue
                tex_id = int(ordered_textures[begin])
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                self._point_sprite_instances_at(begin)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, length)
                self.render_stats.draw_calls += 1
        finally:
            gl.glUniform1f(cutoff, 0.0)
            gl.glDepthMask(gl.GL_TRUE if depth_write else gl.GL_FALSE)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

    def _create_water_surface_vao(self, subdivisions=64):
        """Tessellated unit-square grid on the cube's top face (y = +0.5).

        The water vertex shader needs real geometry to displace with Gerstner
        waves — the 2-triangle cube top gave it nothing to work with, which is
        why water used to look like a solid slab. Same attribute layout as the
        cube VAO (pos, normal, uv) so both bind to the water shader.
        """
        n = subdivisions
        verts = np.zeros(((n + 1) * (n + 1), 8), dtype=np.float32)
        idx = 0
        for j in range(n + 1):
            z = j / n - 0.5
            for i in range(n + 1):
                x = i / n - 0.5
                verts[idx] = (x, 0.5, z, 0.0, 1.0, 0.0, i / n, j / n)
                idx += 1

        indices = np.zeros(n * n * 6, dtype=np.uint32)
        k = 0
        for j in range(n):
            row = j * (n + 1)
            for i in range(n):
                a = row + i
                c = a + (n + 1)
                indices[k:k + 6] = (a, c, a + 1, a + 1, c, c + 1)
                k += 6

        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        ebo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glBindVertexArray(0)
        self._water_surface_vbo = vbo
        self._water_surface_ebo = ebo
        self._water_surface_index_count = len(indices)
        return vao

    # --------------------------------------------------------------------------
    # Water / Glass / Fog
    # --------------------------------------------------------------------------
    @timed_pass('water')
    def draw_water_brushes(self, projection, view, camera_pos, brushes, lights, config,
                           table):
        """Draw water from dense RenderTable state.

        The pass captures the opaque scene once for screen-space transmission.
        Optional environment cubemaps are already rendered by render_scene before
        this method is called; the hot loop consumes only dense columns and GL
        texture handles.
        """
        if len(brushes) == 0 or 'water' not in self.shaders:
            return
        if table is None:
            raise RuntimeError("draw_water_brushes requires RenderTable")
        if not getattr(self, 'water_enabled', True):
            return

        shader, uniforms = self.shaders['water'], self.uniforms['water']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('water', lights)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(
            uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))

        # Water uses the same scene capture as Glass, but captures before any
        # water surface is submitted so transmission never contains the water
        # itself.
        scene_size = self._capture_glass_scene()
        if scene_size is None:
            viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
            scene_size = (
                max(int(viewport[2]), 1),
                max(int(viewport[3]), 1),
            )
        scene_width, scene_height = scene_size
        # Quality is per brush ("High quality" in the property editor). The
        # depth copy is made once, and only if some brush in the pass wants
        # it; the renderer's water_quality is a session-only debug cap
        # (r_waterquality cheap forces every brush cheap).
        high_quality = table.water_high_quality[brushes]
        if getattr(self, 'water_quality', 'expensive') != 'expensive':
            high_quality = np.zeros_like(high_quality)
        has_depth = bool(high_quality.any()) and self._capture_scene_depth()
        depth_unit = self._water_depth_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + depth_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D,
                         self._water_depth_texture if has_depth else 0)
        gl.glUniform1i(uniforms['sceneDepth'], depth_unit)
        has_depth_loc = uniforms['hasSceneDepth']
        ssr_loc = uniforms['ssrEnabled']
        # The effects this frame drew, nearest last: high-quality water
        # reflects them directly (water.frag reflectEffects).
        effects = self._reflected_effects if has_depth else None
        if effects is not None and len(effects):
            # Only those that can show in this water: above its surface and
            # near it (a reflection reaches about four times as far across
            # the water as the effect is high). Every water fragment tests
            # each one kept, so the far side of the map is not sent.
            bounds = table.bounds[brushes]
            lo = (bounds[:, 0:3] - bounds[:, 3:6]).min(axis=0)
            hi = (bounds[:, 0:3] + bounds[:, 3:6]).max(axis=0)
            height = effects[:, 1] - hi[1]
            dx = np.maximum(np.maximum(lo[0] - effects[:, 0], effects[:, 0] - hi[0]), 0.0)
            dz = np.maximum(np.maximum(lo[2] - effects[:, 2], effects[:, 2] - hi[2]), 0.0)
            reach = self.EFFECT_REFLECTION_REACH * (height + effects[:, 4])
            effects = effects[(height > -effects[:, 4]) & (dx * dx + dz * dz < reach * reach)]
        count = 0 if effects is None else len(effects)
        gl.glUniform1i(uniforms['effectCount'], count)
        if count:
            quads = np.empty((count, 4), dtype=np.float32)
            quads[:, 0:3] = effects[::-1, 0:3]
            quads[:, 3] = effects[::-1, 3] * 0.5
            heights = np.ascontiguousarray(effects[::-1, 4] * 0.5, dtype=np.float32)
            gl.glUniform4fv(uniforms['effectQuads'], count, quads)
            gl.glUniform1fv(uniforms['effectHalfHeights'], count, heights)
        brush_depth = None
        if not has_depth:
            gl.glUniform1i(has_depth_loc, 0)
            gl.glUniform1i(ssr_loc, 0)
        # Keep the inverse alive: value_ptr() only borrows its storage.
        inv_projection = glm.inverse(projection)
        gl.glUniformMatrix4fv(
            uniforms['invProjection'], 1, gl.GL_FALSE,
            glm.value_ptr(inv_projection))

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.water_normal_id)
        gl.glUniform1i(uniforms['normalMap'], 0)

        scene_unit = self._glass_scene_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + scene_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        gl.glUniform1i(uniforms['sceneColor'], scene_unit)
        gl.glUniform2f(
            uniforms['screenSize'],
            float(scene_width),
            float(scene_height),
        )

        # The map's sky panorama, when it has one, is what all its water
        # reflects (water.frag skyColor, high quality and cheap alike) -- the
        # texture the sky pass already holds, on a unit of its own. No sky:
        # the procedural sky, exactly as before. Water inside a reflection
        # probe's radius reads the probe's snapshot there instead: the same
        # kind of texture, read the same way.
        panorama = self._background_panorama_texture()
        probe_skies = self._water_probe_textures(table, brushes)
        unit = self._panorama_texture_unit
        if panorama or probe_skies is not None:
            gl.glUniform1i(uniforms['skyPanorama'], unit)
        if panorama:
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, panorama)
        sky_loc = uniforms['reflectSky']
        gl.glUniform1i(sky_loc, 1 if panorama else 0)
        bound_sky = panorama
        # High-quality box water: side-wall fragments behind the (opaque) top
        # surface are not shaded (water.frag main). The test runs in the
        # box's unit space, so the camera is taken into it per brush.
        skip_loc = uniforms['skipCoveredSides']
        local_eye_loc = uniforms['localEye']
        margin_loc = uniforms['sideSkipMargin']
        gl.glUniform1i(skip_loc, 0)
        eye = np.array((*self._camera_xyz(camera_pos), 1.0))

        opacity_loc = uniforms['waterOpacity']
        reflectivity_loc = uniforms['waterReflectivity']
        fresnel_loc = uniforms['fresnelIntensity']
        distortion_loc = uniforms['distortionStrength']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        tint_loc = uniforms['waterTint']
        model_loc = uniforms['model']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        wave_amp_loc = uniforms['waveAmp']
        brush_size_loc = uniforms['brushSize']

        models, normals = self._frame_transforms(table, brushes)
        sizes = table.half[brushes] * 2.0
        params = table.water_params[brushes]
        spreads = table.water_wave_spread[brushes].tolist()
        spread_loc = uniforms['waveSpread']
        # "Direction": how far each flowing brush's surface has moved (its
        # velocity, world units a second, times the frame's time). Still
        # water is (0, 0), what the uniform holds unless a brush flows, so a
        # map with no flowing water sets it once a frame.
        flows = table.water_flow[brushes]
        flow_loc = uniforms['flowOffset']
        flowing = bool(flows.any())
        if flowing:
            travelled = (np.asarray(flows, dtype=np.float64)
                         * float(config.get('time', 0.0))).tolist()
        flow_now = (0.0, 0.0)
        if flow_loc != -1:
            gl.glUniform2f(flow_loc, 0.0, 0.0)
        tints = table.water_tint[brushes]
        # Emissive water glows in its tint. The uniform is touched only when
        # the map has such water, or had and it must be put back.
        emissive = (table.is_emissive(brushes)
                    if len(table.emissive_slots) else None)
        emissive_loc = uniforms['waterEmissive']
        if emissive_loc is None:
            emissive_loc = -1
        glowing = bool(getattr(self, '_water_emissive_on', False))
        planes = table.water_plane[brushes]
        bits = table.class_bits[brushes]
        geo = (bits & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = self._prepare_geo_meshes(table, brushes)

        surface_vao = self.vaos.get('water_surface')
        cube_vao = self.vaos['cube']

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        upload_model = uniform_rows('mat4', models)
        upload_normal = uniform_rows('mat3', normals)
        upload_tint = uniform_rows('vec3', tints)
        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            upload_model(model_loc, i)
            if normal_mat_loc >= 0:
                upload_normal(normal_mat_loc, i)
            if probe_skies is not None:
                sky = int(probe_skies[i]) or panorama
                if sky != bound_sky:
                    gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, sky)
                    if bool(sky) != bool(bound_sky):
                        gl.glUniform1i(sky_loc, 1 if sky else 0)
                    bound_sky = sky

            opacity = float(params[i, 0])
            fresnel = float(params[i, 1])
            if has_depth:
                wants_depth = bool(high_quality[i])
                if wants_depth != brush_depth:
                    gl.glUniform1i(has_depth_loc, 1 if wants_depth else 0)
                    # 'reflections 0' turns the scene reflection off.
                    gl.glUniform1i(ssr_loc, 1 if wants_depth and getattr(
                        self, 'reflections_enabled', True) else 0)
                    brush_depth = wants_depth

            gl.glUniform1f(opacity_loc, opacity)
            gl.glUniform1f(reflectivity_loc, fresnel)
            gl.glUniform1f(fresnel_loc, fresnel)
            gl.glUniform1f(distortion_loc, float(params[i, 4]))
            gl.glUniform1f(refraction_loc, max(float(params[i, 5]), 1.0))
            gl.glUniform1f(roughness_loc, min(max(float(params[i, 6]), 0.0), 1.0))
            gl.glUniform1f(spread_loc, spreads[i])
            if flowing and flow_loc != -1:
                wanted_flow = tuple(travelled[i])
                if wanted_flow != flow_now:
                    gl.glUniform2f(flow_loc, wanted_flow[0], wanted_flow[1])
                    flow_now = wanted_flow
            upload_tint(tint_loc, i)
            if emissive_loc != -1:
                wanted = bool(emissive is not None and emissive[i])
                if wanted != glowing:
                    gl.glUniform1f(emissive_loc, 1.0 if wanted else 0.0)
                    glowing = wanted
                    self._water_emissive_on = wanted
            gl.glUniform3f(
                brush_size_loc,
                float(sizes[i, 0]),
                float(sizes[i, 1]),
                float(sizes[i, 2]),
            )

            h = float(params[i, 2])
            if h > 2.0:
                h /= 100.0
            amp = h * 30.0 if params[i, 3] != 0.0 else 1.2
            amp = min(amp, float(sizes[i, 1]) * 0.45, 30.0)
            gl.glUniform1f(wave_amp_loc, amp)

            mesh = (
                geo_meshes.get(int(table.geometry_id[slot]))
                if geo[i] else None
            )
            if mesh is not None:
                top_count = mesh.count - mesh.side_count
                gl.glBindVertexArray(mesh.vao)
                if not bool(planes[i]):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.side_count)
                if mesh.has_flat_top and surface_vao:
                    gl.glBindVertexArray(surface_vao)
                    gl.glDrawElements(
                        gl.GL_TRIANGLES,
                        self._water_surface_index_count,
                        gl.GL_UNSIGNED_INT,
                        None,
                    )
                elif top_count:
                    gl.glDrawArrays(
                        gl.GL_TRIANGLES,
                        mesh.side_count,
                        top_count,
                    )
                elif bool(planes[i]):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.draw_calls += 1
                continue

            if not bool(planes[i]):
                # The walls are drawn before the top surface; with depth and
                # high quality that surface is opaque where it covers them.
                skip = has_depth and bool(high_quality[i])
                if skip:
                    local = np.linalg.solve(
                        models[i].reshape(4, 4).T.astype(np.float64), eye)
                    gl.glUniform3f(local_eye_loc, float(local[0]),
                                   float(local[1]), float(local[2]))
                    gl.glUniform1f(margin_loc,
                                   amp / max(float(sizes[i, 1]), 1.0e-6))
                    gl.glUniform1i(skip_loc, 1)
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
                if skip:
                    gl.glUniform1i(skip_loc, 0)
            if surface_vao:
                gl.glBindVertexArray(surface_vao)
                gl.glDrawElements(
                    gl.GL_TRIANGLES,
                    self._water_surface_index_count,
                    gl.GL_UNSIGNED_INT,
                    None,
                )
            else:
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            self.render_stats.draw_calls += 1

        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return

    def _capture_glass_scene(self):
        """Copy the current framebuffer into the glass transmission texture.

        The copy happens once before the glass pass, so every glass surface
        samples the same scene behind it. GL 3.3 supports this without adding
        another permanent render target to the forward pipeline.
        """
        viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
        if viewport is None or len(viewport) < 4:
            return None
        x, y, width, height = (int(viewport[0]), int(viewport[1]),
                               int(viewport[2]), int(viewport[3]))
        if width <= 0 or height <= 0:
            return None

        created = not self._glass_scene_texture
        if created:
            self._glass_scene_texture = int(gl.glGenTextures(1))

        unit = gl.GL_TEXTURE0 + self._glass_scene_texture_unit
        gl.glActiveTexture(unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        if created:
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)

        if self._glass_scene_size != (width, height):
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            self._glass_scene_size = (width, height)

        gl.glCopyTexSubImage2D(
            gl.GL_TEXTURE_2D, 0, 0, 0, x, y, width, height)
        return width, height

    def _read_framebuffer_has_depth(self):
        """Whether the bound read framebuffer has a single-sampled depth buffer.

        Asked up front rather than by provoking a GL error, so an unrelated
        error already queued by an earlier pass is never swallowed here --
        and asked once per framebuffer and size (four queries every frame,
        before), the read binding being the one render_scene noted at the
        start of the view."""
        fbo = getattr(self, '_frame_read_fbo', None)
        viewport = getattr(self, '_frame_viewport', None)
        if fbo is None or viewport is None:
            return self._query_read_framebuffer_depth()
        key = (fbo, int(viewport[2]), int(viewport[3]))
        known = self.__dict__.setdefault('_read_fbo_depth', {})
        has = known.get(key)
        if has is None:
            if len(known) >= 8:
                known.clear()
            has = known[key] = self._query_read_framebuffer_depth()
        return has

    def _query_read_framebuffer_depth(self):
        try:
            if int(gl.glGetIntegerv(gl.GL_SAMPLE_BUFFERS)) != 0:
                return False
            fbo = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
            attachment = gl.GL_DEPTH if fbo == 0 else gl.GL_DEPTH_ATTACHMENT
            kind = gl.glGetFramebufferAttachmentParameteriv(
                gl.GL_READ_FRAMEBUFFER, attachment,
                gl.GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE)
            if int(kind) == gl.GL_NONE:
                return False
            bits = gl.glGetFramebufferAttachmentParameteriv(
                gl.GL_READ_FRAMEBUFFER, attachment,
                gl.GL_FRAMEBUFFER_ATTACHMENT_DEPTH_SIZE)
            return int(bits) > 0
        except Exception:
            return False

    def _capture_scene_depth(self):
        """Copy the current depth buffer into the water depth texture.

        Plain GL 3.3 core: one ``glCopyTexSubImage2D`` into a depth texture,
        like the colour copy in :meth:`_capture_glass_scene`. Returns False
        (and the water keeps its depth-less look) when there is no
        single-sampled depth buffer to copy from.
        """
        viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
        if viewport is None or len(viewport) < 4:
            return False
        x, y, width, height = (int(viewport[0]), int(viewport[1]),
                               int(viewport[2]), int(viewport[3]))
        if width <= 0 or height <= 0 or not self._read_framebuffer_has_depth():
            return False

        if not self._water_depth_texture:
            self._water_depth_texture = int(gl.glGenTextures(1))
        gl.glActiveTexture(gl.GL_TEXTURE0 + self._water_depth_texture_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._water_depth_texture)
        if self._water_depth_size != (width, height):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_MODE, gl.GL_NONE)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH_COMPONENT24, width, height, 0,
                gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
            self._water_depth_size = (width, height)
        gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, x, y, width, height)
        self._scene_depth_copies = getattr(self, '_scene_depth_copies', 0) + 1
        return True

    @timed_pass('glass')
    def draw_glass_brushes(self, projection, view, camera_pos, brushes, lights, config,
                           table):
        if len(brushes) == 0 or 'glass' not in self.shaders:
            return
        if table is None:
            raise RuntimeError("draw_glass_brushes requires RenderTable")
        shader, uniforms = self.shaders['glass'], self.uniforms['glass']
        gl.glUseProgram(shader)
        self._upload_env_uniforms('glass')
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))

        # Capture once, before any glass surface is drawn.
        scene_size = self._capture_glass_scene()
        if scene_size is None:
            return
        scene_width, scene_height = scene_size
        scene_tex_unit = self._glass_scene_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + scene_tex_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        gl.glUniform1i(uniforms['sceneColor'], scene_tex_unit)
        gl.glUniform2f(uniforms['screenSize'],
                       float(scene_width), float(scene_height))
        gl.glActiveTexture(gl.GL_TEXTURE0)

        model_loc = uniforms['model']
        water_color_loc = uniforms['waterColor']
        distortion_loc = uniforms['distortionStrength']
        fresnel_loc = uniforms['fresnelIntensity']
        opacity_loc = uniforms['glassOpacity']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

        models, normals = self._frame_transforms(table, brushes)
        colors = table.glass_color[brushes]
        params = table.glass_params[brushes]
        geo = ((table.class_bits[brushes] & render_table.CLASS_HAS_GEOMETRY) != 0)
        geo_meshes = self._prepare_geo_meshes(table, brushes)
        upload_model = uniform_rows('mat4', models)
        upload_normal = uniform_rows('mat3', normals)
        upload_colour = uniform_rows('vec3', colors)
        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            upload_model(model_loc, i)
            if normal_mat_loc != -1:
                upload_normal(normal_mat_loc, i)
            upload_colour(water_color_loc, i)
            gl.glUniform1f(distortion_loc, float(params[i, 1]))
            gl.glUniform1f(fresnel_loc, float(params[i, 4]))
            gl.glUniform1f(opacity_loc, float(params[i, 0]))
            gl.glUniform1f(refraction_loc, float(params[i, 2]))
            gl.glUniform1f(roughness_loc, float(params[i, 3]))
            mesh = geo_meshes.get(int(table.geometry_id[slot])) if geo[i] else None
            if mesh is not None:
                gl.glBindVertexArray(mesh.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glBindVertexArray(self.vaos['cube'])
            else:
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        return

    @timed_pass('fog')
    def draw_fog_volumes(self, projection, view, camera_pos, brushes, lights, config, *, table):
        if len(brushes) == 0 or 'fog' not in self.shaders:
            return
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        shader, uniforms = self.shaders['fog'], self.uniforms['fog']
        gl.glUseProgram(shader)
        self._upload_lights_once('fog', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self.noise_texture_id)
        gl.glUniform1i(uniforms['noiseTexture'], 1)
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_CULL_FACE)

        model_loc = uniforms['model']
        inv_model_loc = uniforms['inverseModel']
        density_loc = uniforms['density']
        fog_color_loc = uniforms['fogColor']
        noise_scale_loc = uniforms['noiseScale']
        object_color_loc = uniforms['object_color']
        alpha_loc = uniforms['alpha']

        # A fog volume's faces usually lie exactly on the floor, walls and
        # ceiling that bound it. Pulled a hair towards the camera, they win
        # that depth tie instead of fighting it pixel by pixel; a surface
        # really in front of a face still hides it.
        offset = getattr(self, 'fog_face_offset', True)
        if offset:
            gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
            gl.glPolygonOffset(-1.0, -1.0)

        models, _ = self._frame_transforms(table, brushes)
        # model_matrices is column-major for GL; transpose into conventional
        # matrices, invert the batch, then transpose back for glUniform.
        mats = models.reshape(-1, 4, 4).transpose(0, 2, 1)
        inv = np.linalg.inv(mats).transpose(0, 2, 1).reshape(-1, 16).astype(np.float32)
        colors = table.fog_color[brushes]
        params = table.fog_params[brushes]
        geo = ((table.class_bits[brushes] & render_table.CLASS_HAS_GEOMETRY) != 0)
        geo_meshes = self._prepare_geo_meshes(table, brushes)
        upload_model = uniform_rows('mat4', models)
        upload_inverse = uniform_rows('mat4', inv)
        upload_colour = uniform_rows('vec3', colors)
        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            upload_model(model_loc, i)
            upload_inverse(inv_model_loc, i)
            upload_colour(fog_color_loc, i)
            gl.glUniform1f(density_loc, float(params[i, 0]))
            gl.glUniform1f(noise_scale_loc, float(params[i, 1]))
            upload_colour(object_color_loc, i)
            gl.glUniform1f(alpha_loc, 0.4)
            mesh = geo_meshes.get(int(table.geometry_id[slot])) if geo[i] else None
            if mesh is not None:
                gl.glBindVertexArray(mesh.vao)
                gl.glCullFace(gl.GL_FRONT)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glCullFace(gl.GL_BACK)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glBindVertexArray(self.vaos['cube'])
            else:
                # All six faces. The bottom used to be left out (a pattern
                # copied from the water pass, where the floor hides it), so
                # from inside a fog volume nothing covered the floor and it
                # showed through unfogged while the walls and ceiling fogged.
                gl.glCullFace(gl.GL_FRONT)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                gl.glCullFace(gl.GL_BACK)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        if offset:
            gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return
