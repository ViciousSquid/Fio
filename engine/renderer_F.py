"""
engine/renderer_F.py  –  Forward renderer, inherits shared logic from BaseRenderer
"""

import glm
import OpenGL.GL as gl
import numpy as np
from collections import defaultdict
import math
import os

from .renderer_core import BaseRenderer, normalize_color
from engine.brush_geometry import brush_has_geometry, geometry_signature
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.things import Thing, Light, PathNode, Portal, Pickup, Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger

# Beyond this distance from the camera a portal's virtual view is not rendered
# (the aperture just shows its fade/rim). Portals are still discovered for I/O
# and transit regardless.
PORTAL_RENDER_DISTANCE = 2048.0

# Cube face order — index maps to the face's 6-vertex run in the cube VAO
# (face_idx * 6). Kept as a module constant so the per-frame texture batch
# build doesn't allocate a fresh list for every brush.
_CUBE_FACE_KEYS = ('south', 'north', 'west', 'east', 'down', 'top')


def _light_casts_shadows(light):
    """True if a light should cast depth cube-map shadows.

    Robust to the flag being stored as a real bool or as a string
    (``"true"``/``"false"``) in saved maps."""
    val = light.properties.get('casts_shadows', False)
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


class Renderer_F(BaseRenderer):
    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        super().__init__(texture_loader, initial_grid_size, initial_world_size, config)
        self._current_shader = None
        self._frame_lights_uploaded = {}

        # Texture batch cache for draw_textured_brushes_optimized.
        # Key: tuple of (brush_id, sorted_tex_items) per brush.
        # Storing None initially forces a build on the first frame.
        self._tex_batch_cache     = None   # (defaultdict(list), geo_brush_list) | None
        self._tex_batch_cache_key = None   # last key tuple | None

    # ------------------------------------------------------------------
    # Matrix helpers – cached on the brush dict itself
    # ------------------------------------------------------------------

    def _brush_model_matrix(self, brush):
        """Return the model matrix for *brush*, recomputing only when the
        brush transform actually changes.  Result is stored directly on the
        brush dict so it survives across frames with zero extra bookkeeping.
        """
        pos   = brush.get('pos',  [0, 0, 0])
        size  = brush.get('size', [64, 64, 64])
        angle = brush.get('_rot_angle')
        axis  = tuple(brush.get('rot_axis', [0, 1, 0])) if angle else None
        key   = (pos[0], pos[1], pos[2],
                 size[0], size[1], size[2],
                 angle, axis)

        if brush.get('_mat_cache_key') == key:
            return brush['_mat_cache']

        mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
        if angle:
            av = glm.vec3(*axis)
            if glm.length(av) > 0.001:
                mat = glm.rotate(mat, glm.radians(float(angle)), glm.normalize(av))
        mat = glm.scale(mat, glm.vec3(*size))
        brush['_mat_cache_key'] = key
        brush['_mat_cache']     = mat
        return mat

    def _tex_cache_path(self, tex_name):
        """Return the ``textures/<name>`` cache key for *tex_name*, memoizing the
        os.path.join. Called for every drawn face every frame in play mode, so
        the join is done once per unique texture name and reused thereafter."""
        path = self._tex_path_cache.get(tex_name)
        if path is None:
            path = os.path.join('textures', tex_name)
            self._tex_path_cache[tex_name] = path
        return path

    def set_sprite_textures(self, textures):
        self.sprite_textures = textures

    def set_instance_textures(self, textures):
        self.instance_textures = textures

    def _compute_normal_matrix(self, model_matrix, brush=None):
        """Compute the normal matrix.

        If *brush* is provided the result is cached under the same cache
        key as the model matrix, so it is only recomputed when the brush
        transform changes.  Falls back to uncached behaviour when brush is
        None (e.g. calls from base-class code that don't have a brush ref).
        """
        if brush is not None:
            mk = brush.get('_mat_cache_key')
            if mk is not None and brush.get('_nmat_cache_key') == mk:
                return brush['_nmat_cache']
            try:
                nmat = glm.transpose(glm.inverse(glm.mat3(model_matrix)))
            except Exception:
                nmat = self._identity_mat3
            brush['_nmat_cache_key'] = mk
            brush['_nmat_cache']     = nmat
            return nmat
        # No brush supplied – uncached path (should be rare)
        try:
            return glm.transpose(glm.inverse(glm.mat3(model_matrix)))
        except Exception:
            return self._identity_mat3

    # ------------------------------------------------------------------

    def draw_lit_brushes_optimized(self, projection, view, camera_pos, brushes, lights, config, is_transparent_pass=False):
        if not brushes or 'lit' not in self.shaders:
            return
        visible = brushes
        self.render_stats.visible_brushes += len(visible)
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        # Cache value_ptr results – avoids redundant ctypes work per draw call
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.vaos['cube'])
        display_mode        = config.get('brush_display_mode', 'Textured')
        show_triggers_solid = config.get('show_triggers_as_solid', False)
        selected            = config.get('selected_object')
        model_loc      = uniforms['model']
        color_loc      = uniforms['object_color']
        alpha_loc      = uniforms['alpha']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1

        if is_transparent_pass:
            fill_mode = gl.GL_FILL if show_triggers_solid else gl.GL_LINE
        else:
            fill_mode = gl.GL_FILL if display_mode != "Wireframe" else gl.GL_LINE
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)

        cube_vao = self.vaos['cube']
        bound_vao = cube_vao
        # Portal virtual scene: cull each brush's interior faces so the oblique
        # clip can't expose their dark back-faces. Cube and convex-geometry
        # meshes wind oppositely, so the culled face is switched alongside the
        # VAO below. No-op in the main pass.
        self._portal_begin_cull(is_geo=False)
        for brush in visible:
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            if normal_mat_loc > 0:
                nmat = self._compute_normal_matrix(model_matrix, brush)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(nmat))
            if brush.get('is_trigger'):
                color, alpha = [0.0, 1.0, 1.0], 0.3
            elif brush is selected:
                color, alpha = [1.0, 1.0, 0.0], 1.0
            elif brush.get('operation') == 'subtract':
                color, alpha = [1.0, 0.0, 0.0], 1.0
            else:
                brush_tint   = brush.get('tint')
                brush_colour = brush.get('colour')
                color = normalize_color(brush_tint) if brush_tint else normalize_color(brush_colour)
                alpha = 1.0
            gl.glUniform3fv(color_loc, 1, color)
            gl.glUniform1f(alpha_loc, alpha)
            mesh = self._get_geo_mesh(brush)
            if mesh is not None:
                if bound_vao != mesh.vao:
                    gl.glBindVertexArray(mesh.vao)
                    bound_vao = mesh.vao
                    self._portal_set_cull(is_geo=True)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.visible_tris += mesh.count // 3
            else:
                if bound_vao != cube_vao:
                    gl.glBindVertexArray(cube_vao)
                    bound_vao = cube_vao
                    self._portal_set_cull(is_geo=False)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                self.render_stats.visible_tris += 12
            self.render_stats.draw_calls += 1

        self._portal_end_cull()
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_textured_brushes_optimized(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'textured' not in self.shaders:
            return
        visible = brushes
        self.render_stats.visible_brushes += len(visible)
        shader, uniforms = self.shaders['textured'], self.uniforms['textured']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('textured', lights)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['texture_diffuse'], 0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(self.vaos['cube'])
        model_loc = uniforms['model']

        # Ensure tex_scale_loc is permanently stored in the UniformCache so
        # we never call glGetUniformLocation on the hot path again.
        tex_scale_loc = uniforms.get('tex_scale', -1)
        if tex_scale_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_scale")
            uniforms._cache['tex_scale'] = loc   # write straight into the cache
            tex_scale_loc = loc

        tex_angle_loc = uniforms.get('tex_angle', -1)
        if tex_angle_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_angle")
            uniforms._cache['tex_angle'] = loc
            tex_angle_loc = loc

        tex_shift_loc = uniforms.get('tex_shift', -1)
        if tex_shift_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_shift")
            uniforms._cache['tex_shift'] = loc
            tex_shift_loc = loc

        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1

        is_play = config.get('play_mode', False)

        # ---- Texture batch cache -----------------------------------------
        # Angled (convex-geometry) brushes carry per-plane faces instead of
        # the six cube faces, so they are pulled out of the cube batches and
        # drawn per-face below.  Their geometry signature is part of the key
        # so clipping a brush invalidates the cached batches.
        cache_key = None if is_play else tuple(
            (id(b), tuple(sorted(b.get('textures', {}).items())), geometry_signature(b))
            for b in visible
        )

        if not is_play and cache_key == self._tex_batch_cache_key and self._tex_batch_cache is not None:
            batches, geo_brushes = self._tex_batch_cache
        else:
            batches = defaultdict(list)
            geo_brushes = []
            for brush in visible:
                if brush_has_geometry(brush):
                    geo_brushes.append(brush)
                    continue
                brush_textures = brush.get('textures', {})
                for i, face_key in enumerate(_CUBE_FACE_KEYS):
                    tex_name = brush_textures.get(face_key, 'default.png')
                    if tex_name == 'caulk.jpg':
                        continue
                    if is_play and tex_name == 'nodraw.jpg':
                        continue
                    tex_id = self.texture_manager.get(self._tex_cache_path(tex_name)) or \
                             self.load_texture_callback(tex_name, 'textures')
                    batches[tex_id].append((brush, i, face_key))
            if not is_play:
                self._tex_batch_cache     = (batches, geo_brushes)
                self._tex_batch_cache_key = cache_key
        # ------------------------------------------------------------------

        # Portal virtual scene: cull cube-brush interiors so the oblique clip
        # can't reveal their (dark) back-faces. Cube batches first (GL_FRONT).
        self._portal_begin_cull(is_geo=False)

        current_tex = None
        for tex_id, items in batches.items():
            if tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            for brush, face_idx, face_key in items:
                self.render_stats.visible_tris += 2
                model_matrix = self._brush_model_matrix(brush)
                gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
                if normal_mat_loc > 0:
                    nmat = self._compute_normal_matrix(model_matrix, brush)
                    gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(nmat))
                # Per-face surface-inspector transform: free rotation + shift.
                if tex_angle_loc != -1:
                    angle = brush.get('uv_angle', {}).get(face_key, 0.0)
                    gl.glUniform1f(tex_angle_loc, math.radians(angle))
                if tex_shift_loc != -1:
                    shift = brush.get('uv_shift', {}).get(face_key, (0.0, 0.0))
                    gl.glUniform2f(tex_shift_loc, shift[0], shift[1])
                if tex_scale_loc != -1:
                    size = brush.get('size', [64, 64, 64])
                    # --- PRIORITY 1: Use pre-computed uv_scale from editor ---
                    uv_scale = brush.get('uv_scale', {}).get(face_key)
                    if uv_scale is not None:
                        scale_x, scale_y = uv_scale[0], uv_scale[1]
                    # --- PRIORITY 2: Fallback to texture_tiling with actual dimensions ---
                    elif brush.get('texture_tiling', False):
                        tex_name = brush.get('textures', {}).get(face_key, 'default.png')
                        tex_cache_name = self._tex_cache_path(tex_name)
                        tex_w, tex_h = getattr(self, '_texture_dimensions', {}).get(tex_cache_name, (128, 128))
                        tex_w = max(tex_w, 1)
                        tex_h = max(tex_h, 1)

                        fi = face_idx
                        if fi == 0 or fi == 1:   # south, north
                            scale_x, scale_y = size[0] / tex_w, size[1] / tex_h
                        elif fi == 2 or fi == 3:  # west, east
                            scale_x, scale_y = size[2] / tex_w, size[1] / tex_h
                        else:                      # down, top
                            scale_x, scale_y = size[0] / tex_w, size[2] / tex_h
                    # --- PRIORITY 3: FIT mode (stretch 0→1) ---
                    else:
                        scale_x, scale_y = 1.0, 1.0
                    gl.glUniform2f(tex_scale_loc, scale_x, scale_y)
                gl.glDrawArrays(gl.GL_TRIANGLES, face_idx * 6, 6)
                self.render_stats.draw_calls += 1

        # ---- Angled brushes: one draw per convex face --------------------
        if tex_angle_loc != -1:
            gl.glUniform1f(tex_angle_loc, 0.0)  # angled faces use raw UVs; reset
        if tex_shift_loc != -1:
            gl.glUniform2f(tex_shift_loc, 0.0, 0.0)
        # Convex-geometry meshes wind the opposite way to the cube (GL_BACK).
        self._portal_set_cull(is_geo=True)
        for brush in geo_brushes:
            mesh = self._get_geo_mesh(brush)
            if mesh is None:
                continue  # degenerate plane set — nothing to draw
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            if normal_mat_loc > 0:
                nmat = self._compute_normal_matrix(model_matrix, brush)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(nmat))
            gl.glBindVertexArray(mesh.vao)
            for run in mesh.runs:
                tex_name = self._geo_run_texture(brush, run)
                if tex_name == 'caulk.jpg':
                    continue
                if is_play and tex_name == 'nodraw.jpg':
                    continue
                tex_id = self.texture_manager.get(self._tex_cache_path(tex_name)) or \
                         self.load_texture_callback(tex_name, 'textures')
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                if tex_scale_loc != -1:
                    su, sv = self._geo_run_tex_scale(brush, run, tex_name)
                    gl.glUniform2f(tex_scale_loc, su, sv)
                gl.glDrawArrays(gl.GL_TRIANGLES, run['first'], run['count'])
                self.render_stats.visible_tris += run['count'] // 3
                self.render_stats.draw_calls += 1
        self._portal_end_cull()
        gl.glBindVertexArray(0)

    def draw_glow_brushes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'lit' not in self.shaders:
            return
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
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
        for brush in brushes:
            self.render_stats.visible_tris += 12
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            if normal_mat_loc > 0:
                nmat = self._compute_normal_matrix(model_matrix, brush)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(nmat))
            tint = brush.get('tint') or brush.get('colour')
            base_color = normalize_color(tint, default=[1.0, 1.0, 1.0])
            intensity  = float(brush.get('glow_intensity', 10.0))
            overbright = [min(c * intensity, 10.0) for c in base_color]
            gl.glUniform3fv(color_loc, 1, overbright)
            gl.glUniform1f(alpha_loc, 1.0)
            mesh = self._get_geo_mesh(brush)
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
        gl.glBindVertexArray(0)

    def render_scene(self, projection, view, camera_pos, brushes, things, selected_object, config, clear=True):
        current_mode = config.get('render_mode', RENDER_MODE_LIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        if clear:
            # FIX: Don't clear color when rendering a portal virtual view
            if getattr(self, '_portal_virtual_view', None) is not None:
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
            else:
                gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        self._proj_ptr = glm.value_ptr(projection)
        self._view_ptr = glm.value_ptr(view)
        self.render_stats.reset()
        self.render_stats.total_brushes = len(brushes)
        self._begin_geo_frame()
        self._frame_lights_uploaded.clear()
        self._current_shader = None
        if current_mode == RENDER_MODE_WIREFRAME:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        elif current_mode == RENDER_MODE_VERTEX:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_POINT)
            gl.glPointSize(4.0)
        else:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        self.draw_grid(projection, view, self.grid_indices_count,
                      config.get('play_mode', False), config.get('grid_visible', True))
        opaque_brushes, transparent_brushes, sprite_things, fog_volumes, water_brushes, glass_brushes, glow_brushes = \
            self._sort_objects(brushes, things, config)
        textured_opaque, solid_opaque = self._split_opaque(opaque_brushes)
        models_to_render, final_sprites = [], []
        for thing in sprite_things:
            if isinstance(thing, Thing) and thing.properties.get('model_path'):
                models_to_render.append(thing)
            else:
                final_sprites.append(thing)
        lights = [t for t in things if isinstance(t, Light) and t.properties.get('state', 'on') == 'on']
        self._frame_lights = lights

        # --- Depth cube-map shadow pass -------------------------------------
        # Render shadow-casting point lights into their cube-maps *before* any
        # scene geometry so every lit/textured/terrain draw can sample them.
        self._light_shadow_index = {}
        if current_mode == RENDER_MODE_LIT and self.shadows_enabled:
            shadow_lights = [l for l in lights if _light_casts_shadows(l)]
            if shadow_lights:
                shadow_brushes = config.get('all_brushes', brushes)
                shadow_things = config.get('all_things', things)
                self.render_shadow_maps(shadow_lights, shadow_brushes, shadow_things, config, camera_pos)

        terrain = config.get('terrain', None)
        if terrain and terrain.enabled:
            self.render_terrain(projection, view, camera_pos, terrain, lights)
        if config.get('play_mode', False) and Portal is not None and self._portal_gl_ready:
            # Use ALL things for portal discovery, not just frustum-visible ones.
            # But only render portal cameras when player is within 2048 units.
            all_things = config.get('all_things', things)
            portal_things = []
            for t in all_things:
                if not isinstance(t, Portal):
                    continue
                # Include if active OR still mid-fade (fading out but not yet hidden)
                if not t.is_active() and getattr(t, '_fade_alpha', 0.0) <= 0.01:
                    continue
                # Distance check: only render virtual camera if player is close enough
                portal_pos = glm.vec3(*t.pos)
                dist_sq = glm.distance2(portal_pos, camera_pos)
                if dist_sq <= (PORTAL_RENDER_DISTANCE * PORTAL_RENDER_DISTANCE):
                    portal_things.append(t)
            if portal_things:
                try:
                    def _portal_draw_scene(proj, vw, cam, br, th, sel, cfg):
                        # Re-sort from the FULL unculled brush set, but cull it
                        # against the VIRTUAL camera frustum first — otherwise
                        # every portal re-shades the entire level. Sphere-based
                        # test is conservative, so nothing visible is dropped.
                        all_br = cfg.get('all_brushes', br)
                        all_th = cfg.get('all_things', th)
                        try:
                            planes = self._frustum_planes(proj * vw)
                            all_br = [b for b in all_br
                                      if self._brush_visible_in_frustum(planes, b)]
                        except Exception:
                            pass  # never let culling break the portal view
                        _opaque, _transparent, _sprites, _fog, _water, _glass, _glow = \
                            self._sort_objects(all_br, all_th, cfg)

                        _t_opaque, _solid = self._split_opaque(_opaque)
                        _t_brush_mode = cfg.get('brush_display_mode', 'Textured')
                        _lights = [t for t in all_th if isinstance(t, Light) and t.properties.get('state', 'on') == 'on']
                        if _t_brush_mode in ('Textured', 'Solid Lit'):
                            self.draw_textured_brushes_optimized(proj, vw, cam, _t_opaque, _lights, cfg)
                            self.draw_lit_brushes_optimized(proj, vw, cam, _solid, _lights, cfg)
                        else:
                            self.draw_lit_brushes_optimized(proj, vw, cam, _opaque, _lights, cfg)

                        self.draw_sprites(proj, vw, _sprites, self.sprite_textures, self.instance_textures)
                    self.draw_portals(
                        portal_things,
                        projection, view, camera_pos,
                        brushes, things, lights, config,
                        _portal_draw_scene,
                    )
                    self._proj_ptr = glm.value_ptr(projection)
                    self._view_ptr = glm.value_ptr(view)
                except Exception as _pe:
                    print(f"[Portal] render error: {_pe}")
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        brush_display_mode = config.get('brush_display_mode', 'Textured')
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config)
            self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config)
        elif current_mode == RENDER_MODE_LIT:
            if brush_display_mode == 'Textured' or brush_display_mode == 'Solid Lit':
                self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config)
                self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config)
            else:
                self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config)
        if glow_brushes:
            self.draw_glow_brushes(projection, view, camera_pos, glow_brushes, lights, config)
        if models_to_render:
            self.draw_models(projection, view, camera_pos, models_to_render, lights, config)
        if transparent_brushes:
            transparent_brushes.sort(key=lambda b: -self._distance_sq(b.get('pos', [0,0,0]), camera_pos))
        if water_brushes:
            water_brushes.sort(key=lambda b: -self._distance_sq(b.get('pos', [0,0,0]), camera_pos))
        if glass_brushes:
            glass_brushes.sort(key=lambda b: -self._distance_sq(b.get('pos', [0,0,0]), camera_pos))
        if final_sprites:
            final_sprites.sort(key=lambda s: -self._distance_sq(s['pos'] if isinstance(s, dict) else s.pos, camera_pos))
        if not config.get('play_mode', False):
            self.draw_path_node_cubes(projection, view, things)
        self.draw_portal_wireframes(projection, view, things, config.get('play_mode', False))
        gl.glEnable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_FALSE)
        self.draw_sprites(projection, view, final_sprites, self.sprite_textures, self.instance_textures)
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config)
        elif current_mode == RENDER_MODE_LIT:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True)
        if current_mode == RENDER_MODE_LIT:
            self.draw_water_brushes(projection, view, camera_pos, water_brushes, lights, config)
            self.draw_glass_brushes(projection, view, camera_pos, glass_brushes, lights, config)
            self.draw_fog_volumes(projection, view, camera_pos, fog_volumes, lights, config)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        if selected_object:
            if isinstance(selected_object, dict):
                self.draw_selected_brush_outline(projection, view, selected_object)
                pos = selected_object.get('pos')
                if pos is not None and not selected_object.get('lock', False):
                    self.render_gizmo(projection, view, pos)
            elif isinstance(selected_object, Thing):
                self.render_gizmo(projection, view, selected_object.pos)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)