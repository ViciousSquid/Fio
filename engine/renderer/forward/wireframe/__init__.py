"""The Wireframe display mode: brush edges coloured by distance."""

from .distance import DistanceMixin
from .edges import EdgesMixin


class WireframeMixin(DistanceMixin, EdgesMixin):
    """Brush edges, then the distance-colour pass over the frame."""


__all__ = ["WireframeMixin", "DistanceMixin", "EdgesMixin"]
