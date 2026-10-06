"""ForwardRenderer's stencil portals.

Aperture masks, nested stencil levels, depth flattening and the portal
virtual scene. The camera maths comes from the core visibility helpers.
"""

import ctypes
import math

import glm
import numpy as np
import OpenGL.GL as gl

from engine import entity_table as entity_projection
from engine.constants import RENDER_MODE_UNLIT
from engine.shaders import DEFAULT_SHADERS
from ..core.diagnostics import timed_pass
from ..core.visibility import RenderView


class PortalsMixin:
    """ForwardRenderer's stencil portal rendering."""

    MAX_PORTALS = 4      # maximum portal apertures rendered per frame

    # How many times a portal may be seen recursively through another portal.
    # 1 = classic single virtual view (default; identical to the original
    # behaviour). Raise to 2-3 for a bounded "infinite corridor" effect — that
    # path is wired up but costs an extra full scene pass per level of depth, so
    # verify performance/appearance in-engine before shipping it enabled.
    MAX_PORTAL_RECURSION = 1

    # Within this many world units of a portal plane the aperture mask is drawn
    # across the screen instead of as world geometry, so the camera's near plane
    # can't clip the mask and reveal the wall behind the portal during the last
    # step before transit. Set to 0.0 to disable (exact original behaviour).
    PORTAL_NEAR_STRADDLE = 24.0

    # --------------------------------------------------------------------------
    # Portal rendering
    # --------------------------------------------------------------------------
    def _init_portal_gl(self):
        mask_vert = DEFAULT_SHADERS.get('portal_mask.vert', '')
        mask_frag = DEFAULT_SHADERS.get('portal_mask.frag', '')
        rim_vert  = DEFAULT_SHADERS.get('portal_rim.vert', '')
        rim_frag  = DEFAULT_SHADERS.get('portal_rim.frag', '')
        try:
            self._portal_mask_shader = self.shader_loader.compile_from_source(mask_vert, mask_frag)
            self._portal_rim_shader = self.shader_loader.compile_from_source(rim_vert, rim_frag)
        except Exception as e:
            print(f"[Portal] Shader compile error: {e}")
            return
        self._portal_quad_vao = gl.glGenVertexArrays(1)
        self._portal_quad_vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_quad_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, 4*3*4, None, gl.GL_DYNAMIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self._portal_mask_proj_loc = gl.glGetUniformLocation(self._portal_mask_shader, 'projection')
        self._portal_mask_view_loc = gl.glGetUniformLocation(self._portal_mask_shader, 'view')
        self._portal_rim_proj_loc  = gl.glGetUniformLocation(self._portal_rim_shader,  'projection')
        self._portal_rim_view_loc  = gl.glGetUniformLocation(self._portal_rim_shader,  'view')
        self._portal_rim_color_loc = gl.glGetUniformLocation(self._portal_rim_shader,  'rim_color')
        self._portal_gl_ready = True
        print("[Portal] GL resources initialised")

    def _portal_begin_cull(self, is_geo=False):
        """Enable back-face culling for the portal virtual scene, culling the
        interior face of the geometry type currently being drawn.

        The oblique near-plane clip slices solid brushes open at the destination
        portal; with culling off the exposed interior/underside back-faces show
        through the aperture as a dark strip along the bottom. Culling the
        interior faces keeps solids solid, exactly as they look in the main
        view. Cube brushes are wound clockwise-outward (interior == GL_FRONT)
        while generated convex-geometry meshes are counter-clockwise-outward
        (interior == GL_BACK), so the caller says which it is drawing. No-op
        outside the portal pass, so the main scene is left untouched."""
        if not self._culling_interiors():
            return
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK if is_geo else gl.GL_FRONT)

    def _portal_set_cull(self, is_geo):
        """Switch the culled face mid-pass (cube batches vs. convex-geometry
        meshes wind oppositely). No-op unless interiors are being culled."""
        if not self._culling_interiors():
            return
        gl.glCullFace(gl.GL_BACK if is_geo else gl.GL_FRONT)

    def _portal_end_cull(self):
        """Restore the default (culling off, GL_BACK) after a culled brush pass."""
        if not self._culling_interiors():
            return
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

    def _culling_interiors(self):
        """Whether the brush pass being drawn culls interior (away-facing)
        faces: always inside a portal's virtual scene, and in the main view's
        opaque passes (see :attr:`cull_opaque_back_faces`)."""
        return (getattr(self, '_portal_scene_pass', False)
                or getattr(self, '_opaque_cull_pass', False))

    @timed_pass('portals (incl. their views)')
    def draw_portals(self, portal_table, portal_slots, projection, main_view, camera_pos, config, draw_scene_fn):
        """Render portal views from dense EntityTable topology."""
        if not self._portal_gl_ready:
            return
        portal_slots = self._portal_candidate_slots(portal_table, portal_slots, camera_pos)
        if not len(portal_slots):
            return
        pv = projection * main_view
        rendered = 0
        for portal_a in portal_slots:
            portal_a = int(portal_a)
            portal_b = int(portal_table.portal_target_slot[portal_a])
            if portal_b < 0:
                continue
            direction = self._portal_direction(portal_table, portal_a)
            if direction in (entity_projection.PORTAL_DIRECTION_FORWARD, entity_projection.PORTAL_DIRECTION_BOTH) and rendered < self.MAX_PORTALS:
                self._draw_one_portal(portal_table, portal_a, portal_b, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1)
                rendered += 1
            if direction in (entity_projection.PORTAL_DIRECTION_REVERSE, entity_projection.PORTAL_DIRECTION_BOTH) and rendered < self.MAX_PORTALS:
                self._draw_one_portal(portal_table, portal_b, portal_a, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1)
                rendered += 1
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

    def _portal_flatten_depth(self, corners, projection, view, stencil_level):
        """Make the completed portal image occupy the source aperture's depth plane.

        The virtual scene needs its own depth buffer while it is rendered, but that
        depth is in the *destination* camera space.  Leaving it in the main depth
        buffer makes arbitrary destination geometry occlude main-world objects
        such as a carried prop.  Once the portal colour is complete, replace those
        virtual depths with the real source-aperture depth so the later main-scene
        passes compare against the portal plane, not the destination scene.
        """
        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilFunc(gl.GL_EQUAL, stencil_level, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glStencilMask(0x00)
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_ALWAYS)
        gl.glDepthRange(0.0, 1.0)

        self._portal_upload_quad(corners)
        gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(
            self._portal_mask_proj_loc, 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            self._portal_mask_view_loc, 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)

        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_LESS)

    def _draw_one_portal(self, portal_table, portal_a, portal_b, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1):
        corners_a = self._portal_slot_corners(
            portal_table, portal_a, self.PORTAL_APERTURE_INSET)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(main_view)
        fade_a = float(portal_table.portal_fade[portal_a])
        rect = self._portal_screen_rect(corners_a, pv)
        if rect is not None:
            if rect[2] <= 0 or rect[3] <= 0:
                return
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glScissor(*rect)
        nrm = portal_table.portal_basis[portal_a, 2]
        apos = portal_table.pos[portal_a]
        cam_d = ((float(camera_pos.x)-apos[0])*nrm[0] + (float(camera_pos.y)-apos[1])*nrm[1] + (float(camera_pos.z)-apos[2])*nrm[2])
        straddle = (depth == 1 and self.PORTAL_NEAR_STRADDLE > 0.0 and abs(cam_d) < self.PORTAL_NEAR_STRADDLE and
                    self._portal_slot_contains(portal_table, portal_a,
                        (float(camera_pos.x)-cam_d*nrm[0], float(camera_pos.y)-cam_d*nrm[1], float(camera_pos.z)-cam_d*nrm[2])))
        if straddle:
            gl.glDisable(gl.GL_SCISSOR_TEST)
            mask_quad=[(-1.0,-1.0,0.0),(1.0,-1.0,0.0),(1.0,1.0,0.0),(-1.0,1.0,0.0)]
            id_ptr=glm.value_ptr(self._identity_mat4); mask_proj_ptr,mask_view_ptr=id_ptr,id_ptr
        else:
            mask_quad=corners_a; mask_proj_ptr,mask_view_ptr=proj_ptr,view_ptr
        parent_level=depth-1
        gl.glDisable(gl.GL_CULL_FACE); gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilMask(0xFF)
        if depth==1: gl.glClear(gl.GL_STENCIL_BUFFER_BIT)
        gl.glColorMask(gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE); gl.glDepthMask(gl.GL_FALSE)
        if straddle: gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glStencilFunc(gl.GL_EQUAL,parent_level,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_INCR)
        self._portal_upload_quad(mask_quad); gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(self._portal_mask_proj_loc,1,gl.GL_FALSE,mask_proj_ptr); gl.glUniformMatrix4fv(self._portal_mask_view_loc,1,gl.GL_FALSE,mask_view_ptr)
        gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        if straddle: gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_TRUE); gl.glDepthFunc(gl.GL_ALWAYS); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glDepthRange(1.0,1.0)
        self._portal_upload_quad(mask_quad); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        gl.glDepthRange(0.0,1.0); gl.glDepthFunc(gl.GL_LESS); gl.glColorMask(gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE)
        virtual_view,virtual_cam=self._portal_build_virtual_view(portal_table,portal_a,portal_b,main_view,camera_pos)
        clip_proj=self._calculate_oblique_projection(projection,virtual_view,portal_table.pos[portal_b],portal_table.portal_basis[portal_b,2])
        gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00)
        old_proj_ptr,old_view_ptr=self._proj_ptr,self._view_ptr; self._proj_ptr=glm.value_ptr(clip_proj); self._view_ptr=glm.value_ptr(virtual_view); self._current_shader=None; self._portal_scene_pass=True
        try:
            draw_scene_fn(
                RenderView(
                    clip_proj, virtual_view, virtual_cam,
                    aperture_slot=portal_a,
                    clip_slot=portal_b,
                    recursion_depth=depth,
                ),
                config,
            )
        finally:
            self._portal_scene_pass=False
        self._proj_ptr=old_proj_ptr; self._view_ptr=old_view_ptr; self._current_shader=None; gl.glStencilMask(0xFF)
        if depth < self.MAX_PORTAL_RECURSION:
            self._draw_nested_portals(portal_table,portal_a,portal_b,clip_proj,virtual_view,virtual_cam,config,draw_scene_fn,portal_slots,depth+1)

        if bool(portal_table.portal_show_rim[portal_a]):
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE)
            r,g,b=portal_table.portal_color[portal_a]; self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader)
            gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,float(r),float(g),float(b),0.55*fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_LINE_LOOP,0,4); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA); gl.glDisable(gl.GL_BLEND)
        if fade_a < 0.999:
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA)
            self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader); gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,0.0,0.0,0.0,1.0-fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4); gl.glDisable(gl.GL_BLEND)

        # Replace destination-camera depth with the real source aperture depth
        # after all portal colour/rim/fade work is complete.
        self._portal_flatten_depth(corners_a, projection, main_view, depth)
        gl.glDisable(gl.GL_STENCIL_TEST); gl.glDisable(gl.GL_SCISSOR_TEST); gl.glBindVertexArray(0)

    def _draw_nested_portals(self, portal_table, from_a, from_b, projection, view, cam, config, draw_scene_fn, portal_slots, depth):
        candidates=self._portal_candidate_slots(portal_table,portal_slots,cam); pv=projection*view
        for portal_a in candidates:
            portal_a=int(portal_a)
            if portal_a==int(from_b): continue
            portal_b=int(portal_table.portal_target_slot[portal_a])
            if portal_b<0: continue
            self._draw_one_portal(portal_table,portal_a,portal_b,projection,view,cam,config,draw_scene_fn,pv,portal_slots,depth=depth)

    def _portal_screen_rect(self, corners, pv):
        """Screen-space integer AABB (x, y, w, h) of the aperture, clamped to the
        viewport, for use as a scissor rect.  Returns None when any corner is at
        or behind the near plane (the rect would be unreliable — caller falls
        back to no scissor / straddle handling)."""
        vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
        vx, vy, vw, vh = int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3])
        minx = miny = float('inf')
        maxx = maxy = float('-inf')
        for c in corners:
            clip = pv * glm.vec4(float(c[0]), float(c[1]), float(c[2]), 1.0)
            if clip.w <= 1e-5:
                return None
            sx = vx + (clip.x / clip.w * 0.5 + 0.5) * vw
            sy = vy + (clip.y / clip.w * 0.5 + 0.5) * vh
            minx = min(minx, sx); maxx = max(maxx, sx)
            miny = min(miny, sy); maxy = max(maxy, sy)
        pad = 2.0
        minx = max(vx, minx - pad)
        miny = max(vy, miny - pad)
        maxx = min(vx + vw, maxx + pad)
        maxy = min(vy + vh, maxy + pad)
        if maxx <= minx or maxy <= miny:
            return (vx, vy, 0, 0)  # off-screen
        return (int(minx), int(miny),
                int(math.ceil(maxx - minx)), int(math.ceil(maxy - miny)))

    def _portal_upload_quad(self, corners):
        # corners should be a list of 4 [x,y,z] points
        vdata = np.array(corners, dtype=np.float32).flatten()
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_quad_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vdata.nbytes, vdata)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

    def _draw_portal_scene(self, view_state, cfg, camera_pos):
        """Draw the world as one portal's virtual camera sees it.

        The *draw_scene_fn* :meth:`draw_portals` calls for each aperture.  It
        runs the same dense brush/entity/material passes as the main view,
        narrowed to the virtual frustum, and finishes with the player glasses
        seen through the portal.  *camera_pos* is the main view's camera.
        """
        # Fog and all scene classification remain driven by the
        # virtual camera and the same dense tables as the main view.
        proj = view_state.projection
        vw = view_state.view
        cam = view_state.camera_pos
        saved_cam = self._frame_camera_pos
        self._frame_camera_pos = self._camera_xyz(cam)
        try:
            (portal_table, portal_groups,
             portal_model_slots, portal_sprite_slots,
             portal_effect_slots, portal_lights) = self._portal_numeric_scene_inputs(
                 proj, vw, cfg)
            mode = cfg.get('brush_display_mode', 'Textured')

            # Match the main numeric scene pipeline: every brush
            # material class consumes the same projected slots,
            # narrowed only by the virtual camera frustum.
            if mode in ('Textured', 'Solid Lit'):
                self.draw_textured_brushes_optimized(
                    proj, vw, cam,
                    portal_groups['textured'], portal_lights, cfg,
                    portal_table)
                self.draw_lit_brushes_optimized(
                    proj, vw, cam,
                    portal_groups['solid'], portal_lights, cfg,
                    table=portal_table)
            else:
                self.draw_lit_brushes_optimized(
                    proj, vw, cam,
                    portal_groups['opaque'], portal_lights, cfg,
                    table=portal_table)

            # Entity models use the same dense recipe/transform
            # projection as the main camera.  No erefs[...] and no
            # Thing list are materialised for the portal scene.
            if len(portal_model_slots):
                portal_table_entities = cfg.get('entity_table')
                portal_fading_mask = (
                    portal_table_entities.render_alpha[portal_model_slots] < 1.0
                )
                portal_opaque_model_slots = portal_model_slots[~portal_fading_mask]
                portal_fading_model_slots = portal_model_slots[portal_fading_mask]
                if len(portal_opaque_model_slots):
                    self.draw_models_instanced(
                        proj, vw, cam,
                        portal_table_entities,
                        portal_opaque_model_slots,
                        portal_lights, cfg)
                if len(portal_fading_model_slots):
                    gl.glEnable(gl.GL_BLEND)
                    gl.glDepthMask(gl.GL_FALSE)
                    self.draw_models_instanced(
                        proj, vw, cam,
                        portal_table_entities,
                        portal_fading_model_slots,
                        portal_lights, cfg)
                    gl.glDepthMask(gl.GL_TRUE)
                    gl.glDisable(gl.GL_BLEND)

            # Match the remaining dense material passes.
            if len(portal_groups['glow']):
                self.draw_glow_brushes(
                    proj, vw, cam,
                    portal_groups['glow'], portal_lights, cfg,
                    table=portal_table)

            if len(portal_effect_slots):
                gl.glEnable(gl.GL_BLEND)
                gl.glDepthMask(gl.GL_FALSE)
                self.draw_effects_instanced(
                    proj, vw, cfg.get('entity_table'),
                    portal_effect_slots,
                    hidden=cfg.get('thing_hidden'),
                    play_mode=cfg.get('play_mode', False),
                    editor_time=cfg.get('time', 0.0),
                    camera_pos=cam)

            if len(portal_sprite_slots):
                self.draw_sprites_instanced(
                    proj, vw, cfg.get('entity_table'),
                    portal_sprite_slots, camera_pos=cam)

            gl.glEnable(gl.GL_BLEND)
            gl.glDepthMask(gl.GL_FALSE)
            if mode == RENDER_MODE_UNLIT:
                self.draw_textured_brushes_optimized(
                    proj, vw, cam,
                    portal_groups['transparent'], portal_lights, cfg,
                    portal_table)
            else:
                self.draw_lit_brushes_optimized(
                    proj, vw, cam,
                    portal_groups['transparent'], portal_lights, cfg,
                    is_transparent_pass=True,
                    table=portal_table)
            self.draw_water_brushes(
                proj, vw, cam, portal_groups['water'], portal_lights, cfg,
                table=portal_table)
            self.draw_glass_brushes(
                proj, vw, cam, portal_groups['glass'], portal_lights, cfg,
                table=portal_table)
            self.draw_fog_volumes(
                proj, vw, cam, portal_groups['fog'], portal_lights, cfg,
                table=portal_table)

            # Player glasses through a portal (same "show glasses"
            # setting as split-screen). A portal with its Glasses
            # property on shows your own glasses as a mirror would
            # when you look into it: your position is reflected
            # across that aperture and carried through to the
            # destination side, so it is seen straight back at you.
            # Every other player (split-screen), and you in a portal
            # with Glasses off, is drawn where they really are --
            # the virtual scene is the real world seen from the
            # destination side.
            # Drawn after every world material pass so water, glass
            # and fog cannot overwrite them, with the oblique
            # projection so anything behind the destination aperture
            # stays clipped, and inside this portal's stencil level.
            if cfg.get('show_glasses', True):
                player_glasses = self._portal_glasses_positions(
                    cfg, view_state, camera_pos)
                if player_glasses:
                    depth = int(getattr(
                        view_state, 'recursion_depth', 1))
                    gl.glEnable(gl.GL_STENCIL_TEST)
                    gl.glStencilMask(0x00)
                    gl.glStencilFunc(gl.GL_EQUAL, depth, 0xFF)
                    gl.glStencilOp(
                        gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
                    self.draw_player_glasses(
                        proj, vw,
                        [pos for pos, _ in player_glasses],
                        sprites=[key for _, key in player_glasses])

            gl.glDepthMask(gl.GL_TRUE)
        finally:
            self._frame_camera_pos = saved_cam
            self._frame_lights_uploaded.clear()
