"""Camera state and camera math for the LogicThread.

LogicThread owns the simulation loop; this object owns the camera domain:
editor camera state, play camera mode, overhead projection, frustum parameters,
and first-person/overhead transitions. The thin forwarding methods left on
LogicThread preserve its existing call surface while keeping the implementation
out of the simulation orchestrator.
"""

import math

import glm

from .camera import Camera


class LogicCamera:
    """State and calculations for the editor/play camera."""

    def __init__(self, host):
        # Resolve the player through LogicPlayer at use time. The player runtime
        # owns the live actor reference and may replace it across play sessions.
        self._host = host

        # Frustum parameters are shared with the render projection. They live
        # here so camera math and the values handed to GL cannot drift apart.
        self.frustum_aspect = 16.0 / 9.0
        self.frustum_fov = 90.0

        # Play-mode camera mode: First Person or Overhead.
        self.camera_mode = "First Person"
        self.overhead_height = 800.0
        self.overhead_height_limit = None
        self.overhead_tilt = 0.0
        self.overhead_orientation = "north"

        # Avoid normalising the mode string on every render-state build.
        self._camera_mode_raw = None
        self._camera_mode_overhead = False

        # Active First Person <-> Overhead tween, or None.
        self.camera_transition = None

        # Editor camera.
        self.editor_camera = Camera()
        self.editor_camera.pos = glm.vec3(0, 150, 400)

    # ------------------------------------------------------------------
    # Editor/frustum configuration
    # ------------------------------------------------------------------

    def set_editor_camera(self, pos: glm.vec3, yaw: float, pitch: float, fov: float):
        self.editor_camera.pos = glm.vec3(pos)
        self.editor_camera.yaw = yaw
        self.editor_camera.pitch = pitch
        self.editor_camera.fov = fov

    def get_editor_camera(self) -> Camera:
        return self.editor_camera

    def set_frustum_aspect(self, aspect: float):
        self.frustum_aspect = aspect

    def set_frustum_fov(self, fov: float):
        """Set the play view's vertical FOV in degrees."""
        try:
            fov = float(fov)
        except (TypeError, ValueError):
            return
        if 1.0 <= fov <= 179.0:
            self.frustum_fov = fov

    # ------------------------------------------------------------------
    # Camera mode
    # ------------------------------------------------------------------

    def set_camera_mode(self, mode: str):
        """Select the play-mode camera: First Person or Overhead."""
        self.camera_mode = str(mode)

    def is_overhead(self) -> bool:
        """Return whether the active play camera is overhead."""
        cm = self.camera_mode
        if cm != self._camera_mode_raw:
            self._camera_mode_raw = cm
            self._camera_mode_overhead = str(cm).strip().lower() in (
                "overhead", "top-down", "topdown"
            )
        return self._camera_mode_overhead

    def effective_overhead_height(self) -> float:
        """Return the camera height after applying an optional ceiling."""
        height = float(self.overhead_height)
        limit = self.overhead_height_limit
        if limit is not None and float(limit) > 0.0:
            height = min(height, float(limit))
        return height

    # ------------------------------------------------------------------
    # Overhead camera geometry
    # ------------------------------------------------------------------

    def _overhead_camera(self, player_pos, angle):
        """Compute (cam_pos, direction, up) for the overhead camera."""
        px = float(player_pos.x)
        py = float(player_pos.y)
        pz = float(player_pos.z)

        if str(self.overhead_orientation).strip().lower() == "player":
            head_x, head_z = math.sin(angle), math.cos(angle)
        else:
            # Fixed north: world -Z is at the top of the screen.
            head_x, head_z = 0.0, -1.0

        tilt = math.radians(max(0.0, min(89.0, float(self.overhead_tilt))))
        sin_t, cos_t = math.sin(tilt), math.cos(tilt)
        dir_x = head_x * sin_t
        dir_y = -cos_t
        dir_z = head_z * sin_t
        dlen = math.sqrt(dir_x * dir_x + dir_y * dir_y + dir_z * dir_z) or 1.0
        direction = glm.vec3(dir_x / dlen, dir_y / dlen, dir_z / dlen)

        dist = self.effective_overhead_height() / max(1e-3, cos_t)
        cam_pos = glm.vec3(
            px - direction.x * dist,
            py - direction.y * dist,
            pz - direction.z * dist,
        )
        up = self._safe_up(direction, glm.vec3(head_x, 0.0, head_z))
        return cam_pos, direction, up

    def overhead_ground_footprint(self):
        """Return the ground footprint as (half_x, half_z, reach).

        The footprint is derived from the actual overhead camera and current
        player pose, so streaming/culling consumers see exactly the ground the
        view can reach rather than an independent approximation.
        """
        player = self._host.player_runtime.player
        if not self.is_overhead() or player is None:
            return None

        pos = player.pos
        cam, direction, up = self._overhead_camera(
            pos, getattr(player, "angle", 0.0)
        )
        d = glm.normalize(glm.vec3(direction))
        right = glm.normalize(glm.cross(d, glm.vec3(up)))
        true_up = glm.cross(right, d)

        tan_v = math.tan(math.radians(float(self.frustum_fov)) / 2.0)
        tan_h = tan_v * max(0.1, float(self.frustum_aspect))

        ground = float(pos.y)
        hx = hz = reach = 0.0
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                ray = d + true_up * (sy * tan_v) + right * (sx * tan_h)
                if ray.y >= -1e-3:
                    return None
                t = (ground - cam.y) / ray.y
                dx = cam.x + ray.x * t - pos.x
                dz = cam.z + ray.z * t - pos.z
                hx = max(hx, abs(dx))
                hz = max(hz, abs(dz))
                reach = max(reach, math.hypot(dx, dz))
        return hx, hz, reach

    @staticmethod
    def _safe_up(direction, up):
        """Return a non-degenerate up vector suitable for glm.lookAt."""
        d = glm.vec3(direction)
        if glm.length(d) < 1e-8:
            return glm.vec3(0, 1, 0)
        d = glm.normalize(d)
        u = glm.vec3(up)
        u = glm.normalize(u) if glm.length(u) > 1e-8 else glm.vec3(0, 1, 0)
        if abs(glm.dot(d, u)) > 0.999:
            u = glm.vec3(0, 0, 1) if abs(d.y) > 0.9 else glm.vec3(0, 1, 0)
        return u

    def _camera_for_mode(
        self, overhead, player_pos, player_angle, player_pitch, camera_height
    ):
        """Return (cam_pos, direction, up, fov) for one camera mode."""
        if overhead:
            cam_pos, direction, up = self._overhead_camera(
                player_pos, player_angle
            )
            return cam_pos, direction, up, self.frustum_fov

        cam_pos = player_pos + glm.vec3(0, camera_height, 0)
        direction = glm.vec3(
            math.sin(player_angle) * math.cos(player_pitch),
            math.sin(player_pitch),
            math.cos(player_angle) * math.cos(player_pitch),
        )
        return cam_pos, direction, glm.vec3(0, 1, 0), self.frustum_fov

    # ------------------------------------------------------------------
    # Camera transitions
    # ------------------------------------------------------------------

    def start_camera_transition(self, target_mode=None, duration=1.0):
        """Begin a smooth tween between First Person and Overhead cameras."""
        current_overhead = self.is_overhead()
        if target_mode is None:
            to_overhead = not current_overhead
        else:
            to_overhead = str(target_mode).strip().lower() in (
                "overhead", "top-down", "topdown", "top", "td"
            )
        new_mode = "Overhead" if to_overhead else "First Person"

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 1.0

        ct = self.camera_transition

        if to_overhead == current_overhead and not ct:
            self.camera_mode = new_mode
            return new_mode

        if duration <= 0.0:
            self.camera_transition = None
            self.camera_mode = new_mode
            return new_mode

        if ct and to_overhead == ct["from_overhead"]:
            progressed = min(ct["elapsed"], ct["duration"])
            remaining_frac = 1.0 - (
                progressed / ct["duration"] if ct["duration"] > 0 else 1.0
            )
            self.camera_transition = {
                "from_overhead": ct["to_overhead"],
                "to_overhead": to_overhead,
                "elapsed": remaining_frac * duration,
                "duration": duration,
            }
            self.camera_mode = new_mode
            return new_mode

        self.camera_transition = {
            "from_overhead": current_overhead,
            "to_overhead": to_overhead,
            "elapsed": 0.0,
            "duration": duration,
        }
        self.camera_mode = new_mode
        return new_mode

    def update_camera_transition(self, delta):
        """Advance the active camera tween and clear it when complete."""
        ct = self.camera_transition
        if not ct:
            return
        ct["elapsed"] += delta
        if ct["elapsed"] >= ct["duration"]:
            self.camera_transition = None
