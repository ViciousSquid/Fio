"""The public renderer contract.

Fio draws through whichever renderer is active. A renderer is any object that
satisfies :class:`Renderer`; it does not inherit from anything in Fio. The
built-in implementation is :class:`engine.renderer.forward.ForwardRenderer`
(registered as ``"Forward"``); :class:`engine.renderer.core.RendererCore` is
optional infrastructure an implementation may reuse.

Data reaches a renderer along one path::

    EditorState -> LogicThread -> RenderTable / EntityTable -> Renderer -> OpenGL

A renderer reads the dense tables and the per-frame settings in the frame
input (:data:`FRAME_INPUT`); it never needs editor objects, the logic thread,
or another renderer's internals. Everything here is GL-free so the contract
can be imported, implemented and tested without a GL context.

Rendering runs on the thread that owns the host's OpenGL 3.3 core context.
Texture handles crossing the contract (``load_texture``,
``set_sprite_textures``, ``draw_billboards``) are GL texture names in that
context, which the host's Qt painter shares.

GL state ownership: a renderer sets whatever state it needs at the start of
each call and may leave any state behind. It must not rely on state persisting
between its calls, and the host never manages state on a renderer's behalf.
The host restores only what its own Qt painter relies on (viewport, scissor,
depth/stencil/blend/cull enables and the default pixel-store alignment)
before painting the 2D overlay.
"""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

Vec3 = Tuple[float, float, float]

#: Water cost tiers a renderer honours as a session cap over each water
#: brush's own "High quality" flag (``RenderTable.water_high_quality``):
#: ``'expensive'`` lets every brush choose; ``'cheap'`` forces all of them to
#: the cheap path. A renderer without such a distinction treats both alike.
WATER_QUALITIES = ('cheap', 'expensive')


#: The canonical frame input: the keys of the ``config`` mapping passed to
#: :meth:`Renderer.render_scene`, as published for every frame by the host.
#: A renderer may ignore any key it has no use for; it must not require keys
#: outside this set.
FRAME_INPUT = {
    # Dense world projections (engine.render_table / engine.entity_table)
    'render_table': 'RenderTable: dense brush projection published by LogicRender',
    'all_brush_slots': 'int32 slots of every live (not hidden) brush in render_table',
    'entity_table': 'EntityTable: dense entity projection (lights, sprites, models, '
                    'effects, portals, path nodes)',
    'visible_thing_slots': 'int32 entity slots the logic thread published as visible',
    'thing_hidden': 'bool per entity row: hidden or collected in the running world',
    'terrain': 'engine.terrain.Terrain or None; the renderer prepares whatever '
               'GPU state it needs from it when it first sees it',
    # Selection (same object as render_scene's primary_selection argument)
    'primary_selection': 'SelectionOverlay or None',
    # View settings
    'play_mode': 'bool: running game rather than editing',
    'render_mode': 'one of engine.constants.RENDER_MODE_* (lit, unlit, wireframe, vertex)',
    'brush_display_mode': "editor brush display: 'Textured', 'Solid Lit', ...",
    'show_triggers_as_solid': 'bool: draw trigger volumes filled rather than outlined',
    'show_sprites_in_play_mode': 'bool: keep editor entity sprites visible in play',
    'grid_visible': 'bool: draw the editor grid',
    'camera_distance_cull': 'optional bool: override the main-view distance cull '
                            '(defaults to play_mode)',
    'time': 'seconds since the view started, for editor-time animation',
    # Player presentation
    'show_glasses': 'bool: draw player glasses (portals mirror them)',
    'player_glasses_positions': 'tuple of (x, y, z) per visible player',
    'player_glasses_sprites': 'tuple of sprite-texture keys, one per position',
}


class RenderStats:
    """Per-frame counters a renderer resets and fills; read by the host.

    The system monitor reads ``draw_calls`` and ``visible_tris``; the rest are
    diagnostics a renderer fills where they apply to its technique.
    """
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


@dataclass(frozen=True, slots=True)
class EffectBillboard:
    """A selected Effect whose editor preview box is shown.

    The renderer derives the box from the camera's billboard basis, so it is
    given the inputs rather than the box.
    """
    pos: Vec3
    width: float
    height: float
    #: EXPLOSION previews grow and anchor at the base rather than the centre.
    explosion: bool


@dataclass(frozen=True, slots=True)
class SelectionOverlay:
    """What the renderer draws for the editor's primary selection.

    Built by the editor (``editor.selection_overlay.describe_selection``);
    a renderer draws whichever parts are present and needs nothing else
    about the selected object.
    """
    #: RenderTable id of a selected brush: angled brushes outline their real
    #: edges, and lit brush passes tint the brush as selected.
    brush_id: Any = None
    #: ``(pos, size)`` box outlined when the brush has no angled-mesh edges.
    outline: Optional[Tuple[Vec3, Vec3]] = None
    #: ``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)`` drawn dashed (trigger bounds).
    dashed_bounds: Optional[Tuple[float, float, float, float, float, float]] = None
    effect_billboard: Optional[EffectBillboard] = None
    #: Where the transform gizmo is drawn; None draws none.
    gizmo_pos: Optional[Vec3] = None


@runtime_checkable
class Renderer(Protocol):
    """The renderer contract the host (QtGameView) drives.

    A renderer is created by a factory registered with
    :func:`engine.renderer.register_renderer`, called as ``factory(config)``
    with the host's GL context current; *config* is the application
    ConfigParser or None.

    Matrices are column-major ``glm.mat4``; positions are ``(x, y, z)``
    sequences or ``glm.vec3``.
    """

    # -- renderer-independent settings ---------------------------------------
    #: The camera's :class:`engine.view_distance.ViewDistance` (far plane and
    #: distance fog). The host assigns its shared instance.
    view_distance: Any
    #: Whether lights may cast shadows, by whatever technique the renderer uses.
    shadows_enabled: bool
    #: One of :data:`WATER_QUALITIES`.
    water_quality: str

    # -- diagnostics -----------------------------------------------------------
    render_stats: RenderStats

    # -- lifecycle -------------------------------------------------------------
    @property
    def ready(self) -> bool:
        """False when start-up failed and the renderer cannot draw."""

    def cleanup(self) -> None:
        """Release GL resources; the renderer is not used afterwards."""

    # -- the frame -------------------------------------------------------------
    def render_scene(self, projection, view, camera_pos,
                     primary_selection: Optional[SelectionOverlay],
                     config: Mapping[str, Any], clear: bool = True,
                     brush_slots=None) -> None:
        """Draw one view of the world into the current framebuffer/viewport.

        *config* is the frame input (:data:`FRAME_INPUT`); *brush_slots* are
        the RenderTable slots the logic thread found visible for this camera.
        *clear* is False when the host shares one framebuffer between views
        (split-screen) and has cleared it itself. Called once per view; the
        host draws its post-scene operations (below) afterwards.
        """

    def set_grid(self, world_size: int, grid_size: int) -> None:
        """Size the editor grid (drawn when ``grid_visible``); 0 removes it."""

    # -- resources -------------------------------------------------------------
    def load_texture(self, texture_name: str, subfolder: str) -> int:
        """Load (or return the cached) texture; returns its GL name, 0 on failure."""

    def set_sprite_textures(self, textures: Mapping[str, int]) -> None:
        """The host's sprite texture table (GL names by sprite key), used for
        entity sprites, player glasses and similar billboards."""

    def get_loaded_model(self, filename: str):
        """The already-loaded OBJ/GLB model for *filename*, or None.

        Never loads. The 2D editor view reads ``is_loaded``, ``cpu_vertices``
        and ``cpu_triangles`` from it; None makes it fall back to a box.
        """

    # -- host drawing operations (after render_scene, same view) ---------------
    def draw_billboards(self, projection, view, positions, size, tex_id) -> int:
        """Camera-facing billboards sharing one texture and size (projectiles).
        Returns how many were drawn."""

    def draw_player_glasses(self, projection, view, positions,
                            width=40.0, height=18.0, lift=0.0, sprites=()) -> None:
        """Players drawn as glasses billboards, one sprite key per position."""

    def draw_bullet_marks(self, projection, view, marks: Sequence[Mapping]) -> None:
        """Impact marks: ``{'pos': (x, y, z), 'alpha': float}`` each."""

    def draw_connection_lines(self, projection, view, connections: Sequence[Mapping]) -> None:
        """Editor I/O links: ``{'src', 'dst', 'color'}`` each."""

    def draw_face_highlight(self, projection, view, brush: Mapping, face_name: str) -> None:
        """Highlight one face of a brush (a brush dict in the world format)."""

    def draw_component_overlay(self, projection, view, overlay, version=None) -> None:
        """Vertex/edge/face handles from the editor's component controller."""

    def draw_collision_visualization(self, projection, view, brushes: Sequence[Mapping]) -> None:
        """Debug wireframes of collision boxes (``pos``/``size`` dicts)."""
