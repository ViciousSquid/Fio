"""Fio's renderer boundary.

The host (``QtGameView``) depends only on what this package exports:

* :class:`Renderer` -- the contract every renderer implements (``api``)
* the frame input (:data:`FRAME_INPUT`), :class:`RenderStats` and the
  selection descriptor (:class:`SelectionOverlay`, :class:`EffectBillboard`)
* the registry: :func:`register_renderer`, :func:`create_renderer`,
  :func:`available_renderers`, :data:`DEFAULT_RENDERER`

Implementations live in subpackages and are reached through the registry:
``forward`` is Fio's built-in renderer; ``core`` is optional infrastructure a
renderer may reuse. Importing this package needs no OpenGL.
"""

from .api import (
    FRAME_INPUT, WATER_QUALITIES, EffectBillboard, Renderer, RenderStats,
    SelectionOverlay)
from .registry import (
    DEFAULT_RENDERER, available_renderers, create_renderer, register_renderer,
    renderer_factory)

__all__ = [
    'Renderer', 'RenderStats', 'SelectionOverlay', 'EffectBillboard',
    'FRAME_INPUT', 'WATER_QUALITIES',
    'DEFAULT_RENDERER', 'register_renderer', 'create_renderer',
    'available_renderers', 'renderer_factory',
]
