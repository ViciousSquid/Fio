"""ForwardRenderer's stencil portals.

Aperture masks, nested stencil levels, depth flattening and the portal
virtual scene. The camera maths comes from the core visibility helpers.
"""

import ctypes
import math
import time

import glm
import numpy as np
import OpenGL.GL as gl

from engine import entity_table as entity_projection
from engine.constants import RENDER_MODE_UNLIT
from engine.shaders import DEFAULT_SHADERS
from ..core.diagnostics import timed_pass
from ..core.visibility import RenderView


def _clip_polygon(polygon, distance):
    """Clip a convex polygon of homogeneous points to ``distance(p) >= 0``,
    a plane linear in clip space (Sutherland-Hodgman)."""
    out = []
    count = len(polygon)
    for i in range(count):
        a = polygon[i]
        b = polygon[(i + 1) % count]
        da = distance(a)
        db = distance(b)
        if da >= 0.0:
            out.append(a)
        if (da >= 0.0) != (db >= 0.0):
            t = da / (da - db)
            out.append(tuple(pa + (pb - pa) * t for pa, pb in zip(a, b)))
    return out


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
    # across the whole screen instead of as world geometry. It was 24, to stop
    # the camera's near plane clipping the mask in the last step before
    # transit -- but a full-screen mask put the destination over everything,
    # floor and walls in front of the portal included, so the world visibly
    # jumped to the other side 24 units before the player got there. The
    # aperture quads are now drawn with depth clamping (see
    # _portal_aperture_quads), which the near plane cannot clip, so this is
    # left only for an eye on the plane itself, where the quad has no area.
    PORTAL_NEAR_STRADDLE = 0.05

    # --------------------------------------------------------------------------
    # Portal rendering
    # --------------------------------------------------------------------------
    def _init_portal_gl(self):
        mask_vert = DEFAULT_SHADERS.get('portal_mask.vert', '')
        mask_frag = DEFAULT_SHADERS.get('portal_mask.frag', '')
        rim_vert  = DEFAULT_SHADERS.get('portal_rim.vert', '')
        rim_frag  = DEFAULT_SHADERS.get('portal_rim.frag', '')
        fade_frag = DEFAULT_SHADERS.get('portal_fade.frag', '')
        try:
            self._portal_mask_shader = self.shader_loader.compile_from_source(mask_vert, mask_frag)
            self._portal_rim_shader = self.shader_loader.compile_from_source(rim_vert, rim_frag)
            self._portal_fade_shader = self.shader_loader.compile_from_source(rim_vert, fade_frag)
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
        fade = self._portal_fade_shader
        self._portal_fade_locs = {name: gl.glGetUniformLocation(fade, name) for name in
                                  ('projection', 'view', 'behind', 'viewport_rect', 'alpha')}
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

    #: glPolygonOffset(factor, units) of the depth the finished portal image
    #: is flattened to: pulls it just in front of a coplanar wall.
    PORTAL_FLATTEN_OFFSET = (-1.0, -4.0)

    #: Consecutive frames a portal view may fail before portals are turned
    #: off for the rest of the session, and how often (seconds) a failure is
    #: printed meanwhile.
    PORTAL_FAILURE_LIMIT = 30
    PORTAL_FAILURE_REPORT_INTERVAL = 5.0

    @timed_pass('portals (incl. their views)')
    def draw_portals(self, portal_table, portal_slots, projection, main_view, camera_pos, config, draw_scene_fn):
        """Render portal views from dense EntityTable topology.

        A view that raises is contained here: every piece of GL state the
        portals touch is put back (it used to stay set, so the rest of the
        frame drew only inside the aperture), the failure is reported at most
        every PORTAL_FAILURE_REPORT_INTERVAL seconds rather than every frame,
        and after PORTAL_FAILURE_LIMIT failing frames in a row portals are
        switched off for the session."""
        if not self._portal_gl_ready or getattr(self, '_portals_disabled', False):
            return
        portal_slots = self._portal_candidate_slots(portal_table, portal_slots, camera_pos)
        if not len(portal_slots):
            return
        try:
            self._draw_portal_views(portal_table, portal_slots, projection, main_view,
                                    camera_pos, config, draw_scene_fn)
        except Exception as exc:
            self._portal_failed(exc)
        else:
            self._portal_failures = 0
        finally:
            self._portal_restore_state(projection, main_view)

    def _draw_portal_views(self, portal_table, portal_slots, projection, main_view, camera_pos, config, draw_scene_fn):
        pv = projection * main_view
        rendered = 0
        # Each aperture once: two portals linked both ways name each other's
        # view as well as their own, and a second draw of the same aperture
        # finds its mask already flattened to its depth -- a whole virtual
        # scene for no fragment.
        views = []
        for portal_a in portal_slots:
            portal_a = int(portal_a)
            portal_b = int(portal_table.portal_target_slot[portal_a])
            if portal_b < 0:
                continue
            direction = self._portal_direction(portal_table, portal_a)
            if direction in (entity_projection.PORTAL_DIRECTION_FORWARD, entity_projection.PORTAL_DIRECTION_BOTH):
                views.append((portal_a, portal_b))
            if direction in (entity_projection.PORTAL_DIRECTION_REVERSE, entity_projection.PORTAL_DIRECTION_BOTH):
                views.append((portal_b, portal_a))
        for aperture, target in dict.fromkeys(views):
            if rendered >= self.MAX_PORTALS:
                break
            # Only a view actually drawn counts against the budget: one
            # behind the camera no longer crowds out one in front of it.
            if self._draw_one_portal(portal_table, aperture, target, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1):
                rendered += 1

    def _portal_restore_state(self, projection, main_view):
        """Put back everything the portal pass may have left set, whether it
        finished or a view raised part-way: the main camera's matrices, and
        the scissor, stencil, colour/depth masks, depth function and range,
        culling and blending the rest of the frame expects."""
        self._portal_scene_pass = False
        self._current_shader = None
        self._proj_ptr = glm.value_ptr(projection)
        self._view_ptr = glm.value_ptr(main_view)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_DEPTH_CLAMP)
        gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glPolygonOffset(0.0, 0.0)
        gl.glStencilMask(0xFF)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDepthRange(0.0, 1.0)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_BLEND)
        gl.glBindVertexArray(0)
        gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

    def _portal_failed(self, exc):
        """Report a failed portal frame (rate-limited) and, after
        PORTAL_FAILURE_LIMIT in a row, turn portals off for the session."""
        failures = getattr(self, '_portal_failures', 0) + 1
        self._portal_failures = failures
        now = time.monotonic()
        last = getattr(self, '_portal_failure_reported', None)
        if last is None or now - last >= self.PORTAL_FAILURE_REPORT_INTERVAL:
            self._portal_failure_reported = now
            print(f"[Portal] render error ({failures} frame(s) in a row): {exc!r}")
        if failures >= self.PORTAL_FAILURE_LIMIT:
            self._portals_disabled = True
            print(f"[Portal] {failures} frames in a row failed; portals are "
                  "off for the rest of this session")

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
        # A hair nearer than the plane, so a wall the portal is set flush
        # against draws behind its image rather than z-fighting it.
        gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glPolygonOffset(*self.PORTAL_FLATTEN_OFFSET)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glPolygonOffset(0.0, 0.0)
        gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)

        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_LESS)

    def _draw_one_portal(self, portal_table, portal_a, portal_b, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1):
        """Draw one aperture and the virtual scene behind it. Returns whether
        it drew: an aperture faded out or out of view draws nothing."""
        corners_a = self._portal_slot_corners(
            portal_table, portal_a, self.PORTAL_APERTURE_INSET)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(main_view)
        fade_a = float(portal_table.portal_fade[portal_a])
        if depth == 1:
            # Fading out with distance, as a portal fades when disabled: the
            # view over what is behind the portal, at that opacity.
            fade_a *= self._portal_distance_fade(portal_table, portal_a, camera_pos)
            if fade_a <= 0.001:
                return False
        nrm = portal_table.portal_basis[portal_a, 2]
        apos = portal_table.pos[portal_a]
        cam_d = ((float(camera_pos.x)-apos[0])*nrm[0] + (float(camera_pos.y)-apos[1])*nrm[1] + (float(camera_pos.z)-apos[2])*nrm[2])
        straddle = (depth == 1 and self.PORTAL_NEAR_STRADDLE > 0.0 and abs(cam_d) < self.PORTAL_NEAR_STRADDLE and
                    self._portal_slot_contains(portal_table, portal_a,
                        (float(camera_pos.x)-cam_d*nrm[0], float(camera_pos.y)-cam_d*nrm[1], float(camera_pos.z)-cam_d*nrm[2])))
        # A camera straddling the aperture draws its mask across the screen,
        # whatever the aperture's own corners project to: they can all be
        # behind it in the step before transit. Anywhere else an aperture with
        # no pixel on screen -- behind the camera, off to one side -- is
        # skipped before anything is copied, culled or drawn for it.
        # The main view's aperture quads are drawn depth-clamped, so the near
        # plane cannot clip them: their scissor keeps what lies nearer than it.
        clamp = depth == 1 and not straddle
        rect = None if straddle else self._portal_screen_rect(
            corners_a, pv, near_clip=not clamp)
        if rect is not None and (rect[2] <= 0 or rect[3] <= 0):
            return False
        # A portal fading in or out shows its view at that opacity over what
        # is behind it: keep that picture before the view replaces it.
        behind = self._capture_behind_portal() if depth == 1 and fade_a < 0.999 else None
        if straddle:
            gl.glDisable(gl.GL_SCISSOR_TEST)
            mask_quad=[(-1.0,-1.0,0.0),(1.0,-1.0,0.0),(1.0,1.0,0.0),(-1.0,1.0,0.0)]
            id_ptr=glm.value_ptr(self._identity_mat4); mask_proj_ptr,mask_view_ptr=id_ptr,id_ptr
        else:
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glScissor(*rect)
            mask_quad=corners_a; mask_proj_ptr,mask_view_ptr=proj_ptr,view_ptr
        parent_level=depth-1
        gl.glDisable(gl.GL_CULL_FACE); gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilMask(0xFF)
        if depth==1: gl.glClear(gl.GL_STENCIL_BUFFER_BIT)
        gl.glColorMask(gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE); gl.glDepthMask(gl.GL_FALSE)
        if straddle: gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glStencilFunc(gl.GL_EQUAL,parent_level,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_INCR)
        if clamp: gl.glEnable(gl.GL_DEPTH_CLAMP)
        self._portal_upload_quad(mask_quad); gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(self._portal_mask_proj_loc,1,gl.GL_FALSE,mask_proj_ptr); gl.glUniformMatrix4fv(self._portal_mask_view_loc,1,gl.GL_FALSE,mask_view_ptr)
        gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        if straddle: gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_TRUE); gl.glDepthFunc(gl.GL_ALWAYS); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glDepthRange(1.0,1.0)
        self._portal_upload_quad(mask_quad); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        # Never during the virtual scene: its oblique near plane is a clip.
        gl.glDisable(gl.GL_DEPTH_CLAMP)
        gl.glDepthRange(0.0,1.0); gl.glDepthFunc(gl.GL_LESS); gl.glColorMask(gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE)
        virtual_view,virtual_cam=self._portal_build_virtual_view(portal_table,portal_a,portal_b,main_view,camera_pos)
        clip_proj=self._calculate_oblique_projection(projection,virtual_view,portal_table.pos[portal_b],portal_table.portal_basis[portal_b,2])
        gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00)
        old_proj_ptr,old_view_ptr=self._proj_ptr,self._view_ptr; self._proj_ptr=glm.value_ptr(clip_proj); self._view_ptr=glm.value_ptr(virtual_view); self._current_shader=None; self._portal_scene_pass=True
        # The portal's view has its own instance streams (BrushStream): it
        # draws other brushes than the main view does.
        outer_view = self._instance_view
        self._instance_view = (int(portal_a), depth)
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
            self._instance_view = outer_view
        self._proj_ptr=old_proj_ptr; self._view_ptr=old_view_ptr; self._current_shader=None; gl.glStencilMask(0xFF)
        if depth < self.MAX_PORTAL_RECURSION:
            self._draw_nested_portals(portal_table,portal_a,portal_b,clip_proj,virtual_view,virtual_cam,config,draw_scene_fn,portal_slots,depth+1)

        if clamp: gl.glEnable(gl.GL_DEPTH_CLAMP)
        if bool(portal_table.portal_show_rim[portal_a]):
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE)
            r,g,b=portal_table.portal_color[portal_a]; self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader)
            gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,float(r),float(g),float(b),0.55*fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_LINE_LOOP,0,4); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA); gl.glDisable(gl.GL_BLEND)
        if fade_a < 0.999 and behind is not None:
            self._draw_behind_portal(corners_a, proj_ptr, view_ptr, depth, behind, 1.0 - fade_a)
        elif fade_a < 0.999:
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA)
            self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader); gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,0.0,0.0,0.0,1.0-fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4); gl.glDisable(gl.GL_BLEND)

        # Replace destination-camera depth with the real source aperture depth
        # after all portal colour/rim/fade work is complete.
        self._portal_flatten_depth(corners_a, projection, main_view, depth)
        gl.glDisable(gl.GL_DEPTH_CLAMP)
        gl.glDisable(gl.GL_STENCIL_TEST); gl.glDisable(gl.GL_SCISSOR_TEST); gl.glBindVertexArray(0)
        return True

    def _capture_behind_portal(self):
        """Copy the frame drawn so far; returns the viewport, or None.

        The same plain colour copy the glass pass makes. Only a portal that
        is fading pays for it, and only while it fades.
        """
        viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
        if viewport is None or len(viewport) < 4 or not self._portal_fade_shader:
            return None
        x, y, width, height = (int(v) for v in viewport[:4])
        if width <= 0 or height <= 0:
            return None
        if not self._portal_behind_texture:
            self._portal_behind_texture = int(gl.glGenTextures(1))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._portal_behind_texture)
        if self._portal_behind_size != (width, height):
            for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, pname, value)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            self._portal_behind_size = (width, height)
        gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, x, y, width, height)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return (x, y, width, height)

    def _draw_behind_portal(self, corners, proj_ptr, view_ptr, stencil_level, viewport, alpha):
        """Lay the copied picture back over the aperture at *alpha*."""
        locs = self._portal_fade_locs
        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilFunc(gl.GL_EQUAL, stencil_level, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glStencilMask(0x00)
        depth_test = gl.glIsEnabled(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        self._portal_upload_quad(corners)
        gl.glUseProgram(self._portal_fade_shader)
        gl.glUniformMatrix4fv(locs['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(locs['view'], 1, gl.GL_FALSE, view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._portal_behind_texture)
        gl.glUniform1i(locs['behind'], 0)
        gl.glUniform4f(locs['viewport_rect'], *(float(v) for v in viewport))
        gl.glUniform1f(locs['alpha'], max(0.0, min(1.0, float(alpha))))
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glDisable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_TRUE)
        if depth_test:
            gl.glEnable(gl.GL_DEPTH_TEST)

    def _draw_nested_portals(self, portal_table, from_a, from_b, projection, view, cam, config, draw_scene_fn, portal_slots, depth):
        candidates=self._portal_candidate_slots(portal_table,portal_slots,cam); pv=projection*view
        for portal_a in candidates:
            portal_a=int(portal_a)
            if portal_a==int(from_b): continue
            portal_b=int(portal_table.portal_target_slot[portal_a])
            if portal_b<0: continue
            self._draw_one_portal(portal_table,portal_a,portal_b,projection,view,cam,config,draw_scene_fn,pv,portal_slots,depth=depth)

    def _portal_screen_rect(self, corners, pv, near_clip=True):
        """Screen-space integer AABB (x, y, w, h) of the aperture, clamped to the
        viewport, for use as a scissor rect; zero width and height when no part
        of it is in view.

        Any corner at or behind the camera used to give up on the rect
        altogether (None, so no scissor), and an aperture wholly behind the
        camera, or beside it with one corner behind, then rendered its whole
        virtual scene for a stencil that masked every fragment of it. Now the
        aperture is rejected when every corner is outside the same frustum
        plane, and otherwise clipped to the near plane, so the rect bounds only
        the part of it that can be seen.

        *near_clip* False is for an aperture drawn depth-clamped: the near
        plane does not clip it, so the rect keeps what lies nearer than the
        near plane too, and only what is behind the eye is cut away."""
        vp = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
        vx, vy, vw, vh = int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3])
        off_screen = (vx, vy, 0, 0)
        polygon = []
        for c in corners:
            clip = pv * glm.vec4(float(c[0]), float(c[1]), float(c[2]), 1.0)
            polygon.append((clip.x, clip.y, clip.z, clip.w))
        # Inside the frustum is -w <= x, y, z <= w.
        for axis in range(3):
            if ((all(p[axis] < -p[3] for p in polygon)
                    and (near_clip or axis != 2))
                    or all(p[axis] > p[3] for p in polygon)):
                return off_screen
        # The near plane (z >= -w), and w > 0 so every vertex left projects:
        # the oblique near plane of a portal's own view can pass behind its
        # camera.
        planes = ((lambda p: p[2] + p[3]),) if near_clip else ()
        for distance in planes + ((lambda p: p[3] - 1e-5),):
            polygon = _clip_polygon(polygon, distance)
            if not polygon:
                return off_screen
        minx = miny = float('inf')
        maxx = maxy = float('-inf')
        for x, y, _z, w in polygon:
            sx = vx + (x / w * 0.5 + 0.5) * vw
            sy = vy + (y / w * 0.5 + 0.5) * vh
            minx = min(minx, sx); maxx = max(maxx, sx)
            miny = min(miny, sy); maxy = max(maxy, sy)
        pad = 2.0
        minx = max(vx, minx - pad)
        miny = max(vy, miny - pad)
        maxx = min(vx + vw, maxx + pad)
        maxy = min(vy + vh, maxy + pad)
        if maxx <= minx or maxy <= miny:
            return off_screen
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
            if mode == 'Wireframe':
                self.draw_brush_edges(
                    proj, vw, portal_groups['opaque'], cfg, portal_table)
            elif mode in ('Textured', 'Overlay'):
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
            if cfg.get('show_glasses', True) or cfg.get('spectator_glasses_active', False):
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
