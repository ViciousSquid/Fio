"""Resolve model presentation without adding model properties to spawn points."""

import math


def _properties(thing):
    return getattr(thing, 'properties', thing if isinstance(thing, dict) else {})


def model_path(thing):
    return (getattr(thing, 'editor_model_path', None)
            or _properties(thing).get('model_path'))


def model_rotation(thing):
    if getattr(thing, 'editor_model_path', None):
        # Models face +Z; spawn angles are measured from +X towards -Z.
        return (0.0, 90.0 + thing.get_angle(), 0.0)
    return _properties(thing).get('rotation', (0.0, 0.0, 0.0))


def model_scale(thing):
    if getattr(thing, 'editor_model_path', None):
        # The collision hull does not rotate with the heading. Keep the
        # square foot's world bounds equal to that hull at diagonal angles too.
        yaw = math.radians(model_rotation(thing)[1])
        fit = 1.0 / (abs(math.cos(yaw)) + abs(math.sin(yaw)))
        return (fit, 1.0, fit)
    return _properties(thing).get('scale', 1.0)


def editor_model_ray_distance(thing, origin, direction):
    """Pick the private sign's rotated bounds, including its head and base."""
    lo, hi = thing.editor_model_bounds
    yaw = math.radians(model_rotation(thing)[1])
    c, s = math.cos(yaw), math.sin(yaw)
    x, y, z = [origin[i] - thing.pos[i] for i in range(3)]
    dx, dy, dz = direction
    local_origin = (c*x - s*z, y, s*x + c*z)
    local_direction = (c*dx - s*dz, dy, s*dx + c*dz)
    scale = model_scale(thing)
    local_origin = [o / k for o, k in zip(local_origin, scale)]
    local_direction = [d / k for d, k in zip(local_direction, scale)]
    near, far = 0.0, float('inf')
    for o, d, low, high in zip(local_origin, local_direction, lo, hi):
        if abs(d) < 1e-8:
            if o < low or o > high:
                return None
            continue
        a, b = sorted(((low-o)/d, (high-o)/d))
        near, far = max(near, a), min(far, b)
        if near > far:
            return None
    return near
