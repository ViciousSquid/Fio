"""Visibility: culling, classification and portals.

Distance and frustum culling over dense slots, brush pass classification and
depth ordering, the LOD distance bands, and stencil portal rendering (virtual
views, oblique clipping, aperture masks and the portal virtual scene).
"""

import ctypes
import math
from dataclasses import dataclass

import glm
import numpy as np
import OpenGL.GL as gl

from engine import render_table
from engine import entity_table as entity_projection
from engine.constants import RENDER_MODE_UNLIT
from engine.shaders import DEFAULT_SHADERS
from engine.portal_transform import (
    map_point as _portal_map_point,
    map_direction as _portal_map_direction,
    corners as _portal_corners,
    contains_point as _portal_contains_point,
    mirror_point as _portal_mirror_point,
)
from .debug import timed_pass


@dataclass(frozen=True)
class RenderView:
    """Camera state for a secondary render view.

    The main camera can use the same shape later; portals already use it so
    aperture/clip/recursion state is carried alongside the camera instead of
    being implicit renderer globals.
    """
    projection: object
    view: object
    camera_pos: object
    aperture_slot: int = -1
    clip_slot: int = -1
    recursion_depth: int = 0


class LODManager:
    __slots__ = ('full_dist_sq', 'cull_dist_sq')
    LOD_FULL, LOD_REDUCED, LOD_CULLED = 0, 1, 2
    def __init__(self, full_dist=500.0, cull_dist=2000.0):
        self.full_dist_sq = full_dist * full_dist
        self.cull_dist_sq = cull_dist * cull_dist
    def get_lod_level(self, brush_pos, camera_pos):
        if isinstance(brush_pos, (list, tuple)):
            dx, dy, dz = brush_pos[0] - camera_pos.x, brush_pos[1] - camera_pos.y, brush_pos[2] - camera_pos.z
        else:
            dx, dy, dz = brush_pos.x - camera_pos.x, brush_pos.y - camera_pos.y, brush_pos.z - camera_pos.z
        dist_sq = dx*dx + dy*dy + dz*dz
        if dist_sq < self.full_dist_sq:
            return self.LOD_FULL
        elif dist_sq < self.cull_dist_sq:
            return self.LOD_REDUCED
        return self.LOD_CULLED


class VisibilityMixin:
    """Culling, classification and portals of :class:`engine.renderer.Renderer`."""

    def _classify_brush_slots(self, table, slots, config):
        """Split visible brush slots into the render passes, numerically.

        The array form of ``_sort_objects``' brush half.  That loop asked every
        visible brush what it was, once per frame -- ``is_water_brush`` alone is
        six ``str.lower()`` calls and six substring searches per brush -- to
        reach a verdict that only changes when the brush is edited.  The
        projection resolved it at edit time into ``class_bits``, so the same
        split is one mask per class.

        The classes are mutually exclusive in the same order the Python chain
        tried them (water, then fog, then glass, then glow, then trigger), so
        ``_brush_class_bits`` sets at most one of them and the masks cannot
        disagree with what the loop used to decide.

        Returns int32 slot arrays -- no objects are materialised here.
        """
        empty = slots[:0]
        if not len(slots):
            return {'opaque': empty, 'textured': empty, 'solid': empty,
                    'transparent': empty, 'water': empty, 'glass': empty,
                    'fog': empty, 'glow': empty}

        bits = table.class_bits[slots]
        opaque_mask = (bits & render_table.CLASS_NON_OPAQUE) == 0
        textured_mask = (bits & render_table.CLASS_TEXTURED) != 0
        # A trigger volume is drawn as a wireframe while editing and not at all
        # in play, which is what the old loop's `if not is_play` meant.
        if config.get('play_mode', False):
            trigger_mask = np.zeros(len(slots), dtype=bool)
        else:
            trigger_mask = (bits & render_table.CLASS_TRIGGER) != 0

        return {
            'opaque': slots[opaque_mask],
            'textured': slots[opaque_mask & textured_mask],
            'solid': slots[opaque_mask & ~textured_mask],
            'transparent': slots[trigger_mask],
            'water': slots[(bits & render_table.CLASS_WATER) != 0],
            'glass': slots[(bits & render_table.CLASS_GLASS) != 0],
            'fog': slots[(bits & render_table.CLASS_FOG) != 0],
            'glow': slots[(bits & render_table.CLASS_GLOW) != 0],
        }

    @staticmethod
    def _distance_cull_thing_slots(table, slots, cx, cz, limit_sq):
        """:meth:`_distance_cull_slots` with the Thing pass's exemption.

        Lights and Portals survive the dense entity cull at any distance.
        The object-path predicate is no longer part of Portal rendering;
        the dense EntityTable ENT_CULL_EXEMPT mask is authoritative.
        """
        if not len(slots):
            return slots
        dx = table.pos[slots, 0] - cx
        dz = table.pos[slots, 2] - cz
        near = (dx * dx + dz * dz) <= limit_sq
        exempt = (table.class_bits[slots]
                  & entity_projection.ENT_CULL_EXEMPT) != 0
        return slots[near | exempt]

    @staticmethod
    def _distance_cull_slots(table, slots, cx, cz, limit_sq):
        """Narrow *slots* to those within *limit_sq* on the XZ plane.

        The broad-phase distance cull, as a mask rather than a compaction.  It
        used to run over a Python list and rebuild another one an element at a
        time (``render_cull.cull_by_distance``); the surviving slots are the
        same answer with no object touched.
        """
        if not len(slots):
            return slots
        dx = table.center[slots, 0] - cx
        dz = table.center[slots, 2] - cz
        return slots[(dx * dx + dz * dz) <= limit_sq]

    @staticmethod
    def _sort_slots_by_distance(table, slots, cx, cz, reverse=True):
        """Depth-order *slots* from the projection's centres.

        Replaces reconstructing an array from a list of row views and then
        rebuilding an object list from the sort order: the positions are
        already dense, so the sort is one argsort over a gathered distance
        vector and the result is still slots.
        """
        if len(slots) < 2:
            return slots
        dx = table.center[slots, 0] - cx
        dz = table.center[slots, 2] - cz
        distances = dx * dx + dz * dz
        order = np.argsort(-distances if reverse else distances, kind="stable")
        return slots[order]

    @staticmethod
    def _camera_xyz(camera_pos):
        """``(x, y, z)`` from a glm vec, a sequence, or ``None``."""
        if camera_pos is None:
            return (0.0, 0.0, 0.0)
        if hasattr(camera_pos, 'x'):
            return (float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        return (float(camera_pos[0]), float(camera_pos[1]), float(camera_pos[2]))

    def _entity_radii(self, table, slots, models):
        """Conservative world bounding-sphere radius of each entity row.

        A billboard is a ``w x h`` rectangle centred on its position and turned
        about it, to the camera or to a locked yaw, so half its diagonal bounds
        it in every orientation. A model's mesh radius is scaled by the longest
        axis of its rotation/scale matrix.
        """
        if not models:
            sizes = table.sprite_size[slots].astype(np.float64)
            return 0.5 * np.hypot(sizes[:, 0], sizes[:, 1])
        recipe_radii = self._model_recipe_radii(table)
        recipe_ids = table.model_recipe_id[slots]
        local = np.full(len(slots), np.inf, dtype=np.float64)
        known = (recipe_ids >= 0) & (recipe_ids < len(recipe_radii))
        local[known] = recipe_radii[recipe_ids[known]]
        basis = table.model_base_matrix[slots].astype(np.float64)
        axes = np.stack([basis[:, 0:3], basis[:, 4:7], basis[:, 8:11]], axis=1)
        scale = np.sqrt((axes ** 2).sum(axis=2)).max(axis=1)
        return local * scale

    def _cull_entity_rows(self, table, slots, planes, models=False):
        """Keep the entity rows whose bounding sphere meets the frustum.

        *planes* are six normalised ``(a, b, c, d)`` rows, inside when
        ``n.p + d >= 0``. One product over the rows' centres: the entity-side
        twin of the brush frustum mask, and exact-conservative, so no row that
        could put a pixel on screen is dropped.
        """
        if not len(slots):
            return slots
        planes = np.asarray(planes, dtype=np.float64)
        centres = table.pos[slots]
        radii = self._entity_radii(table, slots, models)
        distances = centres @ planes[:, :3].T + planes[:, 3]
        inside = np.all(distances >= -radii[:, None], axis=1)
        return slots[inside]

    @staticmethod
    def _frustum_planes(m):
        """Six normalized frustum planes (a,b,c,d) from a view-projection matrix
        ``m`` (column-major, m[col][row]).  A point is inside when
        a*x+b*y+c*z+d >= 0 for every plane."""
        def norm(a, b, c, d):
            l = math.sqrt(a * a + b * b + c * c)
            if l < 1e-8:
                return (0.0, 0.0, 0.0, 0.0)
            return (a / l, b / l, c / l, d / l)
        return (
            norm(m[0][3] + m[0][0], m[1][3] + m[1][0], m[2][3] + m[2][0], m[3][3] + m[3][0]),
            norm(m[0][3] - m[0][0], m[1][3] - m[1][0], m[2][3] - m[2][0], m[3][3] - m[3][0]),
            norm(m[0][3] + m[0][1], m[1][3] + m[1][1], m[2][3] + m[2][1], m[3][3] + m[3][1]),
            norm(m[0][3] - m[0][1], m[1][3] - m[1][1], m[2][3] - m[2][1], m[3][3] - m[3][1]),
            norm(m[0][3] + m[0][2], m[1][3] + m[1][2], m[2][3] + m[2][2], m[3][3] + m[3][2]),
            norm(m[0][3] - m[0][2], m[1][3] - m[1][2], m[2][3] - m[2][2], m[3][3] - m[3][2]),
        )

    MAX_PORTALS = 4      # maximum portal apertures rendered per frame

    PORTAL_RENDER_DISTANCE = 2048.0

    # Render-only inset for the portal aperture.  Physical portal size and
    # transit geometry remain authored; this keeps coplanar floor/wall edges
    # from bleeding into the portal image at its boundary.
    PORTAL_APERTURE_INSET = 4.0

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

    def _portal_candidate_slots(self, table, slots, camera_pos):
        """Return active, linked portal slots near a camera using table columns."""
        if table is None or slots is None or not len(slots):
            return np.empty(0, dtype=np.int32)
        slots = np.asarray(slots, dtype=np.int32)
        target = table.portal_target_slot[slots]
        # Keep an enabled portal rendering from the start of its fade-in, and
        # keep a disabled portal alive until its fade-out reaches zero.  The
        # active flag controls gameplay/transit; portal_fade controls whether
        # the visual aperture is still being rendered.
        keep = (target >= 0) & (table.portal_active[slots] | (table.portal_fade[slots] > 0.01))
        if camera_pos is not None:
            delta = table.pos[slots] - np.asarray((float(camera_pos.x), float(camera_pos.y), float(camera_pos.z)), dtype=np.float64)
            keep &= np.einsum('ij,ij->i', delta, delta) <= (self.PORTAL_RENDER_DISTANCE * self.PORTAL_RENDER_DISTANCE)
        return slots[keep]

    @staticmethod
    def _portal_slot_basis(table, slot):
        basis = table.portal_basis[int(slot)]
        return basis[0], basis[1], basis[2]

    @classmethod
    def _portal_slot_corners(cls, table, slot, inset=0.0):
        width = float(table.portal_width_height[int(slot), 0])
        height = float(table.portal_width_height[int(slot), 1])
        inset = max(0.0, float(inset))
        # Keep the render aperture valid even for unusually small authored portals.
        max_inset = max(0.0, 0.5 * min(width, height) - 8.0)
        inset = min(inset, max_inset)
        return _portal_corners(
            table.pos[int(slot)],
            cls._portal_slot_basis(table, slot),
            width - inset * 2.0,
            height - inset * 2.0,
        )

    @staticmethod
    def _portal_slot_contains(table, slot, point):
        return _portal_contains_point(table.pos[int(slot)], VisibilityMixin._portal_slot_basis(table, slot),
                                     table.portal_width_height[int(slot), 0], table.portal_width_height[int(slot), 1], point)

    def _portal_direction(self, table, slot):
        return int(table.portal_direction[int(slot)])

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

    def _calculate_oblique_projection(self, projection, view, plane_pos, plane_normal):
        # 1. Define the clipping plane in world space
        # The normal faces OUT of the destination portal, keeping everything in front of it.
        normal = glm.vec3(*plane_normal)
        pos = glm.vec3(*plane_pos)

        # Nudge the plane slightly backward into the wall to prevent z-fighting with the portal itself
        pos -= normal * 0.05

        dist = -glm.dot(normal, pos)
        plane_world = glm.vec4(normal.x, normal.y, normal.z, dist)

        # 2. Transform the plane to view space
        inv_trans_view = glm.transpose(glm.inverse(view))
        plane_view = inv_trans_view * plane_world

        # 3. Modify the projection matrix using Lengyel's oblique near-plane algorithm
        q = glm.vec4(
            1.0 if plane_view.x >= 0.0 else -1.0,
            1.0 if plane_view.y >= 0.0 else -1.0,
            1.0,
            1.0
        )

        # The projection is constant for the whole frame, so cache its inverse
        # instead of recomputing it for every portal (up to MAX_PORTALS*2 times).
        proj_inv = self._get_cached_proj_inverse(projection)
        q_view = proj_inv * q
        c = plane_view * (2.0 / glm.dot(plane_view, q_view))

        oblique_proj = glm.mat4(projection)
        # Replace the third column (Z-mapping) of the projection matrix
        oblique_proj[0][2] = c.x - oblique_proj[0][3]
        oblique_proj[1][2] = c.y - oblique_proj[1][3]
        oblique_proj[2][2] = c.z - oblique_proj[2][3]
        oblique_proj[3][2] = c.w - oblique_proj[3][3]

        return oblique_proj

    def _get_cached_proj_inverse(self, projection):
        # Cheap signature from the entries that actually vary between frames.
        sig = (float(projection[0][0]), float(projection[1][1]),
               float(projection[2][2]), float(projection[2][3]),
               float(projection[3][2]))
        if sig != self._portal_proj_inv_sig:
            self._portal_proj_inv = glm.inverse(projection)
            self._portal_proj_inv_sig = sig
        return self._portal_proj_inv

    def _portal_build_virtual_view(self, portal_table, portal_a, portal_b, current_view, camera_pos):
        """Build a portal camera from dense EntityTable columns."""
        src_basis=self._portal_slot_basis(portal_table,portal_a)
        dst_basis=self._portal_slot_basis(portal_table,portal_b)
        virtual_cam=glm.vec3(*_portal_map_point(portal_table.pos[portal_a],src_basis,portal_table.pos[portal_b],dst_basis,
                                                (float(camera_pos.x),float(camera_pos.y),float(camera_pos.z))))
        fwd=(-float(current_view[0][2]),-float(current_view[1][2]),-float(current_view[2][2]))
        up=(float(current_view[0][1]),float(current_view[1][1]),float(current_view[2][1]))
        nf=_portal_map_direction(src_basis,dst_basis,fwd); nu=_portal_map_direction(src_basis,dst_basis,up)
        new_fwd=glm.normalize(glm.vec3(*nf)); new_up=glm.normalize(glm.vec3(*nu))
        return glm.lookAt(virtual_cam,virtual_cam+new_fwd,new_up),virtual_cam

    def _portal_numeric_scene_inputs(self, projection, view, config):
        """Resolve a portal virtual scene entirely from the dense projections.

        Portal topology, transforms and aperture geometry all come from
        EntityTable columns. The world seen through that camera never falls back
        to _sort_objects or reconstructs a brush or entity list.
        all_brush_slots is already the live-hidden-filtered
        world projection published by the logic thread; the virtual frustum
        is applied as a vector mask over RenderTable.center/half.
        EntityTable supplies the entity classification and live hidden
        filtering for the sprite pass.
        """
        table = config.get('render_table')
        if table is None:
            raise RuntimeError("Portal virtual view requires RenderTable")

        slots = config.get('all_brush_slots')
        if slots is None:
            slots = np.empty(0, dtype=np.int32)
        else:
            slots = np.asarray(slots, dtype=np.int32)

        planes = np.asarray(
            self._frustum_planes(projection * view),
            dtype=np.float64,
        )
        if len(slots):
            centres = table.center[slots]
            radii = np.linalg.norm(table.half[slots], axis=1)
            distances = centres @ planes[:, :3].T + planes[:, 3]
            slots = slots[np.all(distances >= -radii[:, None], axis=1)]

        groups = self._classify_brush_slots(table, slots, config)

        etable = config.get('entity_table')
        thing_hidden = config.get('thing_hidden')
        # Portal cameras see the world from a different frustum.  Their entity
        # input therefore starts from the dense world slot set, not the main
        # camera's already-published visible selection.  Hidden/collected rows
        # are filtered numerically; no Thing objects are materialised.
        if etable is not None and thing_hidden is not None:
            thing_slots = np.arange(etable.count, dtype=np.int32)
            model_slots, sprite_slots = entity_projection.classify_slots(
                etable,
                thing_slots,
                thing_hidden,
                config.get('play_mode', False),
                config.get('show_sprites_in_play_mode', False),
            )
            # The same exact-conservative bounds the main view uses: a
            # billboard's half-diagonal, a mesh's measured radius.
            sprite_slots = self._cull_entity_rows(etable, sprite_slots, planes)
            model_slots = self._cull_entity_rows(
                etable, model_slots, planes, models=True)
            effect_slots = thing_slots[
                (etable.class_bits[thing_slots] & entity_projection.ENT_EFFECT) != 0
            ]
        else:
            model_slots = np.empty(0, dtype=np.int32)
            sprite_slots = np.empty(0, dtype=np.int32)
            effect_slots = np.empty(0, dtype=np.int32)

        lights = self._get_active_lights(config)
        return table, groups, model_slots, sprite_slots, effect_slots, lights

    #: How close (world units) a published glasses position must be to the
    #: frame camera to count as the viewer's own body. Both are the player's
    #: eye in first person, so they normally coincide exactly.
    PORTAL_SELF_GLASSES_RADIUS = 64.0

    def _portal_glasses_positions(self, cfg, view_state, frame_camera_pos):
        """Glasses to draw in one portal's virtual scene, as ``(pos, key)``.

        A portal whose ``glasses`` property is on (the default) mirrors the
        viewer's own glasses when they look straight into it (only for
        portals seen directly, not portals seen inside portals); everyone
        else keeps their real position. With it off, everyone keeps their
        real position. *key* is the sprite each player wears (see
        ``player_glasses_sprites``).
        """
        positions = cfg.get('player_glasses_positions', ())
        if not positions:
            return ()
        sprites = tuple(cfg.get('player_glasses_sprites', ()))
        sprites = sprites + ('Glasses',) * (len(positions) - len(sprites))
        entries = list(zip(positions, sprites))
        aperture = int(getattr(view_state, 'aperture_slot', -1))
        clip = int(getattr(view_state, 'clip_slot', -1))
        table = cfg.get('entity_table')
        if (table is not None and 0 <= aperture < len(table.portal_glasses)
                and not bool(table.portal_glasses[aperture])):
            return tuple(entries)
        self_index = -1
        if frame_camera_pos is not None:
            cx = float(frame_camera_pos[0])
            cy = float(frame_camera_pos[1])
            cz = float(frame_camera_pos[2])
            best = self.PORTAL_SELF_GLASSES_RADIUS ** 2
            for i, pos in enumerate(positions):
                dx = float(pos[0]) - cx
                dy = float(pos[1]) - cy
                dz = float(pos[2]) - cz
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq <= best:
                    best = dist_sq
                    self_index = i
        if self_index < 0:
            return tuple(entries)
        others = [entry for i, entry in enumerate(entries) if i != self_index]
        if (int(getattr(view_state, 'recursion_depth', 1)) == 1
                and aperture >= 0 and clip >= 0 and table is not None):
            others.append((_portal_mirror_point(
                table.pos[aperture], self._portal_slot_basis(table, aperture),
                table.pos[clip], self._portal_slot_basis(table, clip),
                positions[self_index]), sprites[self_index]))
        return tuple(others)

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