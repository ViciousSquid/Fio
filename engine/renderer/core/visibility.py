"""Renderer-independent visibility over the dense tables.

Distance and frustum culling, brush pass classification and depth ordering,
active-light and shadow-caster selection, the fog/ambient values, and portal
camera maths (virtual views, oblique clipping, apertures). How portals are
composited is technique-specific and lives with each renderer.
"""

import math
from dataclasses import dataclass

import glm
import numpy as np

from engine import render_table
from engine import entity_table as entity_projection
from engine.portal_transform import (
    map_point as _portal_map_point,
    map_direction as _portal_map_direction,
    corners as _portal_corners,
    contains_point as _portal_contains_point,
    mirror_point as _portal_mirror_point,
)


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


class VisibilityMixin:
    """Culling, classification, light selection and portal maths."""

    PORTAL_RENDER_DISTANCE = 2048.0

    # Render-only inset for the portal aperture.  Physical portal size and
    # transit geometry remain authored; this keeps coplanar floor/wall edges
    # from bleeding into the portal image at its boundary.
    PORTAL_APERTURE_INSET = 4.0

    #: How close (world units) a published glasses position must be to the
    #: frame camera to count as the viewer's own body. Both are the player's
    #: eye in first person, so they normally coincide exactly.
    PORTAL_SELF_GLASSES_RADIUS = 64.0

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

    def _get_active_lights(self, config):
        """Return active lights directly from the dense EntityTable."""
        table = config.get('entity_table')
        if table is None or not hasattr(table, 'light_color'):
            raise RuntimeError("dense EntityTable is required for light rendering")
        slots = table.light_slots
        if len(slots):
            keep = table.light_enabled[slots]
            hidden = config.get('thing_hidden')
            if config.get('play_mode', False) and hidden is not None:
                # A hidden light is out of the running world -- Big World
                # parks out-of-range lights exactly this way, and they must
                # not keep lighting (or take light and shadow slots).
                keep = keep & ~np.asarray(hidden)[slots]
            slots = slots[keep]
        return (table, slots)

    def env_uniform_values(self):
        """The fog/ambient block as a ``{uniform_name: value}`` dict.

        For subsystems that own their GL program and uniform table rather than
        going through :attr:`uniforms` — the terrain is the one that does.
        Types are meaningful: ``int`` uploads as ``glUniform1i``, ``float`` as
        ``glUniform1f``, a 3-sequence as ``glUniform3f``.
        """
        vd = self.view_distance
        start, end = vd.resolve()
        return {
            'uFogEnabled': 1 if vd.fog_enabled else 0,
            'uFogColor': tuple(vd.fog_color),
            'uFogStart': float(start),
            'uFogEnd': float(end),
            'uFogDensity': float(vd.fog_density),
            'uFogCamPos': tuple(self._frame_camera_pos),
            'uAmbient': tuple(vd.ambient),
        }

    @staticmethod
    def _shadow_caster_slots(table, all_slots):
        """The brushes eligible to cast a shadow, as slots.

        This filter used to be a Python walk over every brush in the level --
        five dict lookups and ``is_water_brush``'s six-string search each --
        run every frame, before anything had checked whether a single cube-map
        actually needed re-rendering.  The verdict changes only when a brush is
        edited, so the projection resolved it at edit time; here it is one mask.
        """
        if not len(all_slots):
            return all_slots
        bits = table.class_bits[all_slots]
        return all_slots[(bits & render_table.CLASS_SHADOW_CASTER) != 0]

    @staticmethod
    def _casters_in_reach(table, slots, lx, ly, lz, reach):
        """Caster slots within *reach* of a light, and a signature of them.

        Replaces rebuilding ``np.asarray([b['pos'] for b in brushes])`` and
        ``[b['size'] ...]`` from the brush dicts every frame: the projection
        already holds both, so the whole per-light test is
        ``center[slots]`` and one comparison.

        The signature is the caster geometry itself rather than a tuple
        reconstructed per brush.  A light's cube-map is valid exactly while the
        casters in reach of it have not moved or changed shape, which is what
        these bytes say -- and comparing them is a memcmp over a few kilobytes
        instead of building thousands of Python tuples.
        """
        if not len(slots):
            return slots, ()
        center = table.center[slots]
        dx = center[:, 0] - lx
        dy = center[:, 1] - ly
        dz = center[:, 2] - lz
        # A brush counts when the light reaches its bounding sphere, whose
        # radius is the largest half-extent -- the same test as before.
        limit = reach + table.half[slots].max(axis=1)
        sel = slots[(dx * dx + dy * dy + dz * dz) <= limit * limit]
        if not len(sel):
            return sel, ()
        geometry = np.concatenate((table.center[sel].ravel(),
                                   table.half[sel].ravel(),
                                   table.rot[sel].ravel().astype(np.float64)))
        return sel, (sel.tobytes(), geometry.tobytes())

    @staticmethod
    def _dense_shadow_model_slots(table, hidden=None):
        """Return model-entity slots eligible to cast shadows.

        This is the entity-table equivalent of the old Thing model scan.
        Model identity, representation and transforms have already been
        resolved at the dense projection boundary; the shadow pass only needs
        slot masks here.
        """
        if table is None or not table.count:
            return np.empty(0, dtype=np.int32)
        bits = table.class_bits[:table.count]
        mask = (
            ((bits & entity_projection.ENT_SKIP) == 0)
            & ((bits & entity_projection.ENT_ALWAYS_SPRITE) == 0)
            & ((bits & entity_projection.ENT_HAS_MODEL) != 0)
            & ((bits & entity_projection.ENT_MODE_MODEL) != 0)
        )
        if hidden is not None:
            mask &= ~np.asarray(hidden[:table.count], dtype=bool)
        return np.flatnonzero(mask).astype(np.int32)

    @staticmethod
    def _collect_dense_shadow_models(table, model_slots, lx, ly, lz, reach):
        """Return dense model caster slots in light range plus a cache key.

        The key contains only numerical projection state: slots, model recipe
        identity, translation and the precomputed rotation/scale matrix. No
        Thing or authored property dictionary is touched.
        """
        if not len(model_slots):
            return model_slots, ()
        positions = table.pos[model_slots]
        dx = positions[:, 0] - lx
        dy = positions[:, 1] - ly
        dz = positions[:, 2] - lz
        # Preserve the existing model-caster broad phase: models were treated
        # as having a radius of 2 * light reach.
        visible = (dx * dx + dy * dy + dz * dz) <= (reach * reach * 4.0)
        indices = np.flatnonzero(visible)
        slots = model_slots[indices]
        if not len(slots):
            return slots, ()
        recipe_ids = table.model_recipe_id[slots]
        positions = table.pos[slots]
        transforms = table.model_base_matrix[slots]
        signature = (
            slots.tobytes(),
            recipe_ids.tobytes(),
            positions.tobytes(),
            transforms.tobytes(),
        )
        return slots, signature

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
