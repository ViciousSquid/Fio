"""HD_High: Fio's physically based renderer.

A second, separate renderer beside the classic one (``engine.renderer.forward``,
"Classic"). It does not derive from the classic renderer or import anything
of it: like every renderer it is created through the registry
(``create_renderer("HD_High", config)``), is handed the same frame input, and
satisfies the same contract (``engine.renderer.api.Renderer``). The host swaps
the two live (``QtGameView.switch_renderer``, the ``r_renderer`` console
command, Settings > Display > Renderer).

It began as a copy of the classic renderer's passes, so that everything Fio
draws -- sprites, water, glass, portals, decals, terrain, the editor's
overlays -- is drawn here too, and shares only ``engine.renderer.core`` (the
infrastructure any renderer may reuse). What it adds is its own:

* ``environment.py`` -- sun and filtered shadow map, the sky light volume, sky modes
  (none / panorama / procedural), exposure, the reflection fallback,
  material companion maps;
* ``surface.py`` -- physically based shading of the lit programs;
* ``postfx.py`` -- the HDR target, ambient occlusion, screen-space
  reflections, bloom, sun shafts, tone mapping, grading, FXAA;
* ``hdr.py`` -- how the programs that are not physically shaded (sprites,
  particles, water, glass) keep their colours through the tone curve.

Ported from Levitate (a fork of Fio 3.5.1.0), and extended.
"""

from .renderer import HDHighRenderer

__all__ = ['HDHighRenderer']
