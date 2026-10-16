"""Frozen CANARY track sampler before the October 2026 linear-selection pass.

Kept independent of production sampling so equivalence checks retain the original
stable-sort, timestamp normalization and interpolation behavior.
"""

import math


class FrozenCutsceneRuntime:
    @staticmethod
    def _cutscene_number(value, default=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(value):
            return float(default)
        return value

    @staticmethod
    def _cutscene_vec3(value, default=None):
        if default is None:
            default = [0.0, 0.0, 0.0]
        try:
            if len(value) != 3:
                raise ValueError
            result = [float(value[0]), float(value[1]), float(value[2])]
            if not all(math.isfinite(v) for v in result):
                raise ValueError
            return result
        except (TypeError, ValueError, IndexError):
            return list(default)

    @staticmethod
    def _cutscene_lerp_angle(a, b, t):
        delta = ((float(b) - float(a) + math.pi) % (2.0 * math.pi)) - math.pi
        return float(a) + delta * t

    @staticmethod
    def _cutscene_sample(rows, elapsed, initial_pos=None, initial_yaw=0.0):
        """Sample one actor/camera track with linear interpolation."""
        valid = [row for row in rows if isinstance(row, dict)]
        if not valid:
            return None
        valid.sort(key=lambda row: FrozenCutsceneRuntime._cutscene_number(row.get("time", 0.0)))
        first_time = FrozenCutsceneRuntime._cutscene_number(valid[0].get("time", 0.0))
        if elapsed < first_time and initial_pos is not None:
            return {"pos": list(initial_pos), "yaw": float(initial_yaw), "_before": True}
        if elapsed <= first_time:
            return valid[0]
        for index in range(1, len(valid)):
            right = valid[index]
            left = valid[index - 1]
            right_time = FrozenCutsceneRuntime._cutscene_number(right.get("time", 0.0))
            left_time = FrozenCutsceneRuntime._cutscene_number(left.get("time", 0.0))
            if elapsed <= right_time:
                span = right_time - left_time
                t = 1.0 if span <= 1e-9 else max(0.0, min(1.0, (elapsed - left_time) / span))
                lp = FrozenCutsceneRuntime._cutscene_vec3(left.get("pos", [0, 0, 0]))
                rp = FrozenCutsceneRuntime._cutscene_vec3(right.get("pos", lp), lp)

                # A teleport belongs to the destination keyframe.  Keep the
                # previous shot completely unchanged until its timestamp, then
                # cut to the new location.  This makes large cross-map jumps
                # explicit rather than turning them into a very fast move.
                if right.get("teleport", False):
                    if elapsed < right_time:
                        return {
                            "pos": list(lp),
                            "yaw": FrozenCutsceneRuntime._cutscene_number(left.get("yaw", 0.0)),
                            "pitch": FrozenCutsceneRuntime._cutscene_number(left.get("pitch", 0.0)),
                            "fov": FrozenCutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0),
                            "look_at": left.get("look_at"),
                        }
                    return {
                        "pos": list(rp),
                        "yaw": FrozenCutsceneRuntime._cutscene_number(right.get("yaw", 0.0)),
                        "pitch": FrozenCutsceneRuntime._cutscene_number(right.get("pitch", 0.0)),
                        "fov": FrozenCutsceneRuntime._cutscene_number(right.get("fov", 90.0), 90.0),
                        "look_at": right.get("look_at"),
                    }

                return {
                    "pos": [lp[i] + (rp[i] - lp[i]) * t for i in range(3)],
                    "yaw": FrozenCutsceneRuntime._cutscene_lerp_angle(
                        FrozenCutsceneRuntime._cutscene_number(left.get("yaw", 0.0)),
                        FrozenCutsceneRuntime._cutscene_number(right.get("yaw", 0.0)),
                        t,
                    ),
                    "pitch": (
                        FrozenCutsceneRuntime._cutscene_number(left.get("pitch", 0.0))
                        + (FrozenCutsceneRuntime._cutscene_number(right.get("pitch", 0.0))
                           - FrozenCutsceneRuntime._cutscene_number(left.get("pitch", 0.0))) * t
                    ),
                    "fov": (
                        FrozenCutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0)
                        + (FrozenCutsceneRuntime._cutscene_number(right.get("fov", 90.0), 90.0)
                           - FrozenCutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0)) * t
                    ),
                    "look_at": left.get("look_at"),
                }
        return valid[-1]


reference_sample = FrozenCutsceneRuntime._cutscene_sample
