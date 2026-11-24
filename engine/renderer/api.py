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
or another renderer's internals. The frame input is the only thing a frame
hands a renderer: the host publishes no key that is not documented there. Everything here is GL-free so the contract
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

Resource lifetime: no GL resource id survives the lifetime of the renderer
that owns it. A renderer instance owns every GL object it makes -- programs,
buffers, vertex arrays, framebuffers, and the textures and models it loads,
including the names ``load_texture`` returns -- and :meth:`Renderer.cleanup`
deletes them all. The host discards every renderer-produced handle it holds
(its sprite table, the terrain's bindings) before calling ``cleanup()``, and a
renderer that replaces it (``QtGameView.switch_renderer``) receives fresh
resource tables: the host reloads its sprites through the new renderer.
"""

import inspect
import re
import typing
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from engine.view_distance import ViewDistance

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
    'all_brush_slots': 'int32 slots of every live brush in render_table: not '
                       'hidden, and not filtered out by the editor\'s view filters',
    'entity_table': 'EntityTable: dense entity projection (lights, sprites, models, '
                    'effects, portals, path nodes)',
    'visible_thing_slots': 'int32 entity slots to draw in this view: every entity '
                           'pass (sprites, models, effects, path nodes, portal '
                           'outlines) draws only these rows. The editor\'s view '
                           'filters leave rows out; a light left out still lights',
    'thing_hidden': 'bool per entity row: hidden or collected in the running world',
    'hide_hidden_lights': 'optional bool: while editing, a light whose thing_hidden '
                          'row is set (hidden with H) does not light. Off (the '
                          'default), hidden lights light the editor; in play a '
                          'hidden light never lights',
    'light_focus': 'optional (x, y, z): only lights within light_range of this '
                   'point light the frame, nearest first, so every shader\'s '
                   'light cap keeps the closest (the host passes the player in play)',
    'light_range': 'optional float: the reach of light_focus in world units '
                   '(default engine.renderer.core.visibility.ACTIVE_LIGHT_RANGE)',
    'light_fade_range': 'optional float: finish the intensity fade before this '
                       'world-streaming boundary; does not change light selection',
    'terrain': 'engine.terrain_frame.TerrainFrame or None: the published terrain '
               '(its dense TerrainTable, per-slot products, paint maps and '
               'this frame\'s appearance). Never the Terrain object; a '
               'renderer owns every GL resource it builds from it',
    'decals': 'projected decals, or None: (models (n, 16), params (n, 4), '
              'inverses (n, 16)) float32 from the logic thread\'s DecalTable -- '
              'each a box (the unit cube\'s model matrix, column-major), '
              '(texture layer/variant, opacity, kind, flags) and the box\'s '
              'inverse, whose bottom row (elements 3, 7, 11) holds a placed '
              'decal\'s tint less one (zero: white); drawn onto whatever '
              'lies inside the box',
    # Selection (same object as render_scene's primary_selection argument)
    'primary_selection': 'SelectionOverlay or None',
    # View settings
    'play_mode': 'bool: running game rather than editing',
    'splitscreen_active': 'bool: two player views share the viewport; '
                          'single-view GPU occlusion history must not run',
    'render_mode': 'one of engine.constants.RENDER_MODE_* (lit, unlit, wireframe, vertex)',
    'brush_display_mode': "the editor's Display box: 'Textured' (everything as "
                          "normal), 'Solid Lit' (everything shaded in its "
                          "colours, nothing textured), 'Wireframe' (the world "
                          "as lines) or 'Overlay' (Textured with the world's "
                          "wireframe drawn over it). How each looks is the "
                          "renderer's choice; one it does not know is drawn "
                          "as 'Textured'. The host publishes 'Textured' in "
                          "play unless Wireframe or Overlay is on",
    'show_triggers_as_solid': 'bool: draw trigger volumes filled rather than outlined',
    'show_sprites_in_play_mode': 'bool: keep editor entity sprites visible in play',
    'grid_visible': 'bool: draw the editor grid',
    'camera_distance_cull': 'optional bool: override the main-view distance cull '
                            '(defaults to play_mode)',
    'time': 'seconds since the view started, for editor-time animation',
    # The map's sky, for a renderer that lights from it
    'hd_sky': 'optional dict: the map\'s HD sky settings, from its primary '
              'PlayerStart (engine.hd_graphics.sky_settings), or None for the '
              'defaults. Plain data; a renderer that does not light from a sky '
              'ignores it',
    'hd_preview': 'optional bool: whether the editor view is drawn with the HD '
                  'look by a renderer that has one (View > HD preview; default '
                  'True). Play is always drawn with it',
    # Player presentation
    'show_glasses': 'bool: draw player glasses (portals mirror them)',
    'spectator_glasses_active': 'bool: spectator appearances remain visible independently of normal player glasses',
    'player_glasses_positions': 'tuple of (x, y, z) per visible player',
    'player_glasses_sprites': 'tuple of sprite-texture keys, one per position',
    'player_glasses_self_candidates': 'optional bool per pair: remote spectators cannot be mistaken for the viewer in mirrors',
}


class RenderStats:
    """Per-frame counters a renderer resets and fills; read by the host.

    The system monitor reads ``draw_calls`` and ``visible_tris``; the rest are
    diagnostics a renderer fills where they apply to its technique.
    """
    __slots__ = ('total_brushes', 'culled_brushes', 'visible_brushes', 'draw_calls',
                 'shadow_draw_calls', 'total_tris', 'visible_tris', 'batched_draws',
                 'entity_candidates', 'culled_entities', 'pass_ms', 'details',
                 'terrain_visible_chunks', 'terrain_culled_chunks',
                 'terrain_triangles', 'terrain_drawn_slots')
    def __init__(self):
        #: CPU milliseconds spent submitting each pass this frame, measured by
        #: :func:`timed_pass`. Inclusive: a pass that draws others (portals,
        #: shadow maps) counts theirs too.
        self.pass_ms = {}
        #: Renderer-specific diagnostic lines for tools such as Debug Tables:
        #: ``name -> text``, or a zero-argument callable returning the text
        #: (evaluated only when a tool shows it). Not cleared by :meth:`reset`.
        self.details = {}
        self.reset()
    def reset(self):
        self.total_brushes = self.culled_brushes = self.visible_brushes = 0
        self.draw_calls = self.shadow_draw_calls = self.batched_draws = 0
        self.total_tris = self.visible_tris = 0
        #: Sprite and model rows offered to the main view's entity passes,
        #: and how many of them its frustum rejected.
        self.entity_candidates = self.culled_entities = 0
        #: What the terrain pass drew: chunks, chunks the frustum rejected,
        #: triangles, and the table slots drawn, in draw order.
        self.terrain_visible_chunks = self.terrain_culled_chunks = 0
        self.terrain_triangles = 0
        self.terrain_drawn_slots = ()
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


#: The version of the renderer contract (:class:`Renderer`, the frame input
#: and the descriptors here), apart from the plugin API's
#: (plugins.api.API_VERSION). A renderer says which it was written against
#: with a ``contract_version`` attribute, as a physics does
#: (engine.physics.PHYSICS_CONTRACT_VERSION). The declaration is required:
#: the registry refuses a renderer that does not say, one whose declaration
#: is not a version, and one written for a newer contract or another major
#: version (contract_compatible).
RENDERER_CONTRACT_VERSION = "1.0.0"

#: What a ``contract_version`` looks like: ``MAJOR.MINOR`` or
#: ``MAJOR.MINOR.PATCH``, digits only.
_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')


def _version_info(value):
    parts = [int(piece) for piece in value.split('.')]
    return tuple(parts + [0] * (3 - len(parts)))


RENDERER_CONTRACT_VERSION_INFO = _version_info(RENDERER_CONTRACT_VERSION)


@runtime_checkable
class Renderer(Protocol):
    """The renderer contract the host (QtGameView) drives.

    A renderer is created by a factory registered with
    :func:`engine.renderer.register_renderer`, called as ``factory(config)``
    with the host's GL context current; *config* is the application
    ConfigParser or None.

    Matrices are column-major ``glm.mat4``; positions are ``(x, y, z)``
    sequences or ``glm.vec3``.

    The registry checks what a factory returns against this class before the
    host paints with it (:func:`contract_violations`): every member is
    there, each method takes the arguments declared here, and each attribute
    holds a value of its declared type.
    """

    # -- the contract ----------------------------------------------------------
    #: The contract version the renderer was written against:
    #: :data:`RENDERER_CONTRACT_VERSION` when it was written.
    contract_version: str

    # -- renderer-independent settings ---------------------------------------
    #: The camera's :class:`engine.view_distance.ViewDistance` (far plane and
    #: distance fog). The host assigns its shared instance; None until it
    #: has, in a renderer that makes none of its own.
    view_distance: Optional[ViewDistance]
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
        """Delete every GL object this renderer made; it is not used afterwards.

        Also called on a renderer whose ``ready`` is False, so it must cope
        with a partly initialised instance.
        """

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

    def set_background_image(self, path: str) -> None:
        """Set a camera-locked equirectangular panorama behind the scene.

        *path* is a project asset path or an absolute image path; an empty
        path disables the panorama and restores the renderer's clear colour.
        """

    def invalidate_reflection_probes(self) -> None:
        """Retake the snapshots of the map's reflection probes (EntityTable
        rows with ``ENT_PROBE``) on the next frame. The host calls it when a
        map has loaded and when Play starts. A renderer that does not draw
        probe reflections does nothing."""

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

    def draw_connection_lines(self, projection, view, segments, colours) -> None:
        """Editor I/O links: ``segments`` (n, 2, 3) and ``colours`` (n, 3)
        float32 (``engine.overlay_geometry.connection_segments``)."""

    def draw_face_highlight(self, projection, view, triangles) -> None:
        """Highlight one face: world-space ``triangles`` (n, 3) float32
        (``engine.overlay_geometry.face_highlight_triangles``)."""

    def draw_component_overlay(self, projection, view, overlay, version=None) -> None:
        """Vertex/edge/face handles from the editor's component controller."""

    def draw_collision_visualization(self, projection, view, vertices, runs) -> None:
        """Debug wireframes of collision shapes: GL_LINES ``vertices`` (n, 3)
        float32 and ``runs`` of ``(first, count, colour)``
        (``engine.overlay_geometry.collision_wireframes``)."""


class RendererOptions(Protocol):
    """What a renderer may offer beyond :class:`Renderer`.

    The host and the editor look each of these up by name and use the ones
    that are there; a renderer without one draws without that feature
    (``None`` under the name says the same), and the editor's control for
    it says so. One that is there is held to its declaration here, as the
    required members are. Nothing outside this class and :class:`Renderer`
    is read from a renderer by the viewport, the editor or a plugin.
    """

    # -- settings the editor shows or changes ----------------------------------
    #: Whether this renderer has an HD look the editor view can be drawn
    #: with or without (View > HD preview, the frame input's ``hd_preview``).
    hd: bool
    #: Whether this renderer was started for a low-power machine. The editor
    #: reads it to choose which renderer the settings ask for there.
    lowpower_mode: bool
    #: Whether screen-space reflections (water, wet decals) are drawn. The
    #: console's ``reflections`` sets it.
    reflections_enabled: bool
    #: How far from the camera wet decals reflect, in world units; 0 for
    #: none. The console's ``decalfade`` sets it.
    decal_reflection_fade: float

    # -- drawing the host asks for when it is there -------------------------------

    def animate_textures(self, clock: float) -> None:
        """Show each animated (GIF) texture's frame for *clock* seconds of
        unpaused Play (0 outside Play), keeping its GL name. Called before
        each frame."""

    def draw_face_hover(self, projection, view, triangles) -> None:
        """A faint highlight for the face under the cursor while another is
        selected: world-space ``triangles`` (n, 3) float32, as
        :meth:`Renderer.draw_face_highlight` takes."""

    def draw_lens_water(self, drops, viewport) -> int:
        """Water on the camera's lens over the finished frame: *drops*
        ``[(x, y, radius, wetness, stretch)]`` (screen fractions, y down) in
        *viewport* ``(x, y, w, h)``. Returns how many were drawn."""


#: The values the contract permits for an attribute, beyond its type.
ATTRIBUTE_VALUES = {'water_quality': WATER_QUALITIES}

_ABSENT = object()


def _declared_members(protocol=Renderer):
    """What *protocol* declares: ``(methods, attributes)``, a method name to
    its function and an attribute name to its declared type (a property's is
    its return type)."""
    methods, attributes = {}, dict(typing.get_type_hints(protocol))
    for name in typing.get_protocol_members(protocol):
        declared = inspect.getattr_static(protocol, name, None)
        if isinstance(declared, property):
            attributes[name] = typing.get_type_hints(declared.fget).get('return', Any)
        elif inspect.isfunction(declared):
            methods[name] = declared
    return methods, attributes


def _read(renderer, name):
    """``(value, None)``, or ``(_ABSENT, why)``: *why* is None for a member
    the renderer does not have, and the exception for one whose read raised
    (a property that failed)."""
    try:
        return getattr(renderer, name), None
    except Exception as exc:
        if (isinstance(exc, AttributeError)
                and inspect.getattr_static(renderer, name, _ABSENT) is _ABSENT):
            return _ABSENT, None
        return _ABSENT, exc


def _positional(signature):
    return [p for p in signature.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def _signature_mismatch(declared, implemented):
    """Why a method with the signature *implemented* cannot take every call
    the contract's *declared* one allows, or None when it can.

    The host passes arguments by position and by name, and leaves out the
    ones the contract gives a default. So each declared parameter must be
    there under its name, in its place, with the declared default where there
    is one; whatever else the method takes must be optional."""
    wanted, have = _positional(declared), _positional(implemented)
    for index, parameter in enumerate(wanted):
        if index >= len(have):
            return "takes no '%s' argument" % parameter.name
        own = have[index]
        if own.name != parameter.name or own.kind is not own.POSITIONAL_OR_KEYWORD:
            return "argument %d is '%s', not '%s'" % (index + 1, own.name, parameter.name)
        if parameter.default is not parameter.empty:
            if own.default is own.empty:
                return "'%s' has no default (the contract's is %r)" % (
                    parameter.name, parameter.default)
            try:
                same = bool(own.default == parameter.default)
            except Exception:
                same = False
            if not same or type(own.default) is not type(parameter.default):
                return "'%s' defaults to %r, not %r" % (
                    parameter.name, own.default, parameter.default)
    for own in have[len(wanted):]:
        if own.default is own.empty:
            return "requires '%s', which the contract does not pass" % own.name
    for own in implemented.parameters.values():
        if own.kind is own.KEYWORD_ONLY and own.default is own.empty:
            return "requires '%s', which the contract does not pass" % own.name
    return None


def _method_violation(name, declared, value):
    if not callable(value):
        return "%s is not callable" % name
    try:
        implemented = inspect.signature(value)
    except (TypeError, ValueError):
        return "%s has no signature to check" % name
    wanted = inspect.signature(declared)
    wanted = wanted.replace(parameters=list(wanted.parameters.values())[1:])   # self
    mismatch = _signature_mismatch(wanted, implemented)
    if mismatch:
        return "%s%s %s (the contract declares %s%s)" % (
            name, implemented, mismatch, name, wanted.replace(
                return_annotation=inspect.Signature.empty))
    return None


def _permitted_types(declared):
    """The classes a declared type admits; empty for ``Any``."""
    if declared is Any:
        return ()
    if typing.get_origin(declared) is typing.Union:
        return typing.get_args(declared)
    return (declared,)


def _attribute_violation(name, declared, value):
    if name == 'contract_version':
        reason = _version_reason(value)
        return reason and "%s: %s" % (name, reason)
    types = _permitted_types(declared)
    if types and not isinstance(value, types):
        return "%s must be %s, not %r (%s)" % (
            name, ' or '.join('None' if t is type(None) else t.__name__ for t in types),
            value, type(value).__name__)
    permitted = ATTRIBUTE_VALUES.get(name)
    if permitted is not None and value not in permitted:
        return "%s must be one of %s, not %r" % (
            name, ', '.join(repr(v) for v in permitted), value)
    return None


def _version_reason(declared):
    if not isinstance(declared, str) or not _VERSION.fullmatch(declared):
        return ("%r is not a contract version (declare contract_version = "
                "RENDERER_CONTRACT_VERSION, %r)" % (declared, RENDERER_CONTRACT_VERSION))
    wanted = _version_info(declared)
    have = RENDERER_CONTRACT_VERSION_INFO
    if (wanted[0] != have[0] or wanted > have
            or (have[0] == 0 and wanted[1] != have[1])):
        return ("written for renderer contract %s; this Fio has %s"
                % (declared, RENDERER_CONTRACT_VERSION))
    return None


def contract_violations(renderer: Any) -> list:
    """Every way *renderer* fails :class:`Renderer`, one line each, sorted
    by member; empty for an object that satisfies the contract.

    * a member it does not have;
    * a member whose read raised (a property that failed): the host reads
      these while it paints, so one that cannot be read is not there;
    * a method that is not callable, or that cannot take the arguments the
      contract declares (their names, order and defaults);
    * an attribute that does not hold a value of its declared type, or holds
      one the contract does not permit (:data:`ATTRIBUTE_VALUES`);
    * a ``contract_version`` that is not declared, is not a version, or
      names a contract this Fio cannot run;
    * an optional member (:class:`RendererOptions`) that is there and is not
      what its declaration says: the host calls whichever it finds.

    It reads attributes and inspects signatures; it calls no method."""
    methods, attributes = _declared_members()
    violations = []
    for name in sorted(set(methods) | set(attributes)):
        value, error = _read(renderer, name)
        if value is _ABSENT:
            if error is None:
                violations.append(
                    "%s is not declared" % name if name == 'contract_version'
                    else "%s is missing" % name)
            else:
                violations.append("%s raised %s: %s" % (name, type(error).__name__, error))
            continue
        violation = (_method_violation(name, methods[name], value) if name in methods
                     else _attribute_violation(name, attributes[name], value))
        if violation:
            violations.append(violation)
    methods, attributes = _declared_members(RendererOptions)
    for name in sorted(set(methods) | set(attributes)):
        value, error = _read(renderer, name)
        if error is not None:
            violations.append("%s raised %s: %s" % (name, type(error).__name__, error))
        elif value is not _ABSENT and value is not None:
            violation = (_method_violation(name, methods[name], value) if name in methods
                         else _attribute_violation(name, attributes[name], value))
            if violation:
                violations.append(violation)
    return violations


def missing_members(renderer: Any) -> list:
    """The members of :class:`Renderer` *renderer* lacks, sorted: an
    attribute it does not have, or a method it has that cannot be called.
    Empty for an object that has them all, which is less than satisfying
    the contract: :func:`contract_violations` is the whole check."""
    methods, attributes = _declared_members()
    missing = []
    for name in sorted(set(methods) | set(attributes)):
        value, error = _read(renderer, name)
        if (value is _ABSENT and error is None) or (
                value is not _ABSENT and name in methods and not callable(value)):
            missing.append(name)
    return missing


def contract_compatible(renderer: Any) -> Optional[str]:
    """Why *renderer* cannot run on this contract, or None when it can.

    It must say which contract it was written against: one with no
    ``contract_version``, or with one that is not a version, is refused, as
    is one written for a newer version or another major version -- or,
    while the major is 0, another minor version."""
    declared, error = _read(renderer, 'contract_version')
    if declared is _ABSENT:
        if error is None:
            return "contract_version is not declared"
        return "contract_version raised %s: %s" % (type(error).__name__, error)
    return _version_reason(declared)
