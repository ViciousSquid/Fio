"""Fio's renderer boundary.

The host (``QtGameView``) depends only on what this package exports:

* :class:`Renderer` -- the contract every renderer implements (``api``),
  versioned by :data:`RENDERER_CONTRACT_VERSION` -- and
  :class:`RendererOptions`, what one may offer beyond it
* the frame input (:data:`FRAME_INPUT`), :class:`RenderStats` and the
  selection descriptor (:class:`SelectionOverlay`, :class:`EffectBillboard`)
* the registry: :func:`register_renderer`, :func:`create_renderer`,
  :func:`available_renderers`, :data:`DEFAULT_RENDERER`

Implementations live in subpackages and are reached through the registry:
``forward`` is Fio's built-in renderer; ``core`` is optional infrastructure a
renderer may reuse. Importing this package needs no OpenGL.
"""

from .api import (
    FRAME_INPUT, RENDERER_CONTRACT_VERSION, RENDERER_CONTRACT_VERSION_INFO,
    WATER_QUALITIES, EffectBillboard, Renderer, RendererOptions, RenderStats,
    SelectionOverlay,
    contract_compatible, contract_violations, missing_members)
from .registry import (
    RendererContractError, RendererInitError, DEFAULT_RENDERER, available_renderers, create_renderer, register_renderer,
    renderer_factory, canonical_name)

__all__ = [
    'Renderer', 'RendererOptions', 'RENDERER_CONTRACT_VERSION', 'RENDERER_CONTRACT_VERSION_INFO',
    'contract_violations', 'missing_members', 'contract_compatible',
    'RendererContractError',
    'RendererInitError', 'RenderStats', 'SelectionOverlay', 'EffectBillboard',
    'FRAME_INPUT', 'WATER_QUALITIES',
    'DEFAULT_RENDERER', 'register_renderer', 'create_renderer',
    'available_renderers', 'renderer_factory', 'canonical_name',
]
