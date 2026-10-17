"""Camera visibility and environment values shared with the desktop engine."""
import glm
import numpy as np

from engine.logic_render import LogicRender
from engine.render_cull import within_xz_sq


def environment(view_distance, eye):
    start, end = view_distance.resolve()
    return {
        'uFogEnabled': int(view_distance.fog_enabled),
        'uFogColor': view_distance.fog_color,
        'uFogStart': float(start),
        'uFogEnd': float(end),
        'uFogDensity': float(view_distance.fog_density),
        'uFogCamPos': tuple(float(x) for x in eye),
        'uAmbient': view_distance.ambient,
    }


class CameraVisibility:
    def __init__(self, runtime, view, projection, eye):
        self.runtime = runtime
        self.eye = eye
        self.planes = runtime.extract_frustum_planes(
            glm.mat4(*(projection @ view).T.flatten()))

    def visible(self, bounds, model, position):
        """Desktop XZ centre-distance cull, then conservative transformed AABB."""
        if not within_xz_sq(position, float(self.eye[0]), float(self.eye[2]),
                            self.runtime.view_distance.distance_sq):
            return False
        if not self.runtime.culling_enabled:
            return True
        center, half = bounds
        center = model[:3, :3] @ center + model[:3, 3]
        half = np.abs(model[:3, :3]) @ half
        return LogicRender.aabb_in_frustum(self.planes, center, half)
