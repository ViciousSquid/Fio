"""Fio's renderer: one OpenGL renderer for the editor and play mode.

Data reaches it along one path::

    EditorState -> publication -> LogicThread -> RenderTable / EntityTable
                -> engine.renderer -> OpenGL

The package splits that one :class:`Renderer` by responsibility:

``core``        shared renderer state, frame orchestration (``render_scene``), cleanup
``tables``      RenderTable/EntityTable -> GPU preparation (instance buffers, runs)
``visibility``  culling, pass classification, LOD bands, portals
``lighting``    light UBO, fog/ambient block, point-light shadow maps
``geometry``    shared VAOs, angled-brush meshes, model loading
``materials``   shaders, textures, effect frames, terrain shader binding
``passes``      opaque/transparent/water/glass/fog/model/sprite/effect/terrain passes
``debug``       pass timing and statistics, editor overlays

Each non-core module contributes one mixin; only :class:`Renderer` is ever
instantiated.
"""

from .core import Renderer, restore_default_pixel_store
from .visibility import RenderView

__all__ = ['Renderer', 'RenderView', 'restore_default_pixel_store']
