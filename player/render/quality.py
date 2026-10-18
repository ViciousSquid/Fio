"""Player-only quality budgets; shared desktop shaders remain unchanged."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RenderQuality:
    name: str = 'reference'
    render_scale: float = 1.0
    adaptive: bool = False
    shadow_samples: int = 20
    fog_steps: int = 16
    lights_per_batch: int = 64
    static_cell_size: int = 0
    animation_mipmaps: bool = True
    view_distance: float = 4096.0


REFERENCE = RenderQuality()
MOBILE = RenderQuality(name='mobile', render_scale=.67, adaptive=True,
                       shadow_samples=8, fog_steps=8, lights_per_batch=8,
                       static_cell_size=512, animation_mipmaps=False,
                       view_distance=2048.0)


def resolve(name='reference'):
    return {'reference': REFERENCE, 'mobile': MOBILE}[name]
