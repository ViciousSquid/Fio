"""Editor-mode runtime delegated from LogicThread.

Owns editor viewport input and camera navigation. Play-mode simulation
orchestration remains entirely in LogicThread.
"""

from __future__ import annotations

import math

import glm


Key_W = 0x57
Key_S = 0x53
Key_A = 0x41
Key_D = 0x44
Key_Space = 0x20
Key_C = 0x43
Key_Shift = 0x01000020


class LogicEditor:
    """Runtime input/navigation for the editor viewport."""

    EDITOR_CAMERA_SPEED = 300.0
    EDITOR_CAMERA_FAST_MULT = 2.5
    EDITOR_MOUSE_SENSITIVITY = 0.15

    def __init__(self, logic):
        self.logic = logic

    def tick(self, delta: float):
        logic = self.logic
        camera = logic.camera.get_editor_camera()

        dx, dy = logic.game_state.consume_mouse_delta()
        if dx != 0 or dy != 0:
            camera.yaw += dx * self.EDITOR_MOUSE_SENSITIVITY
            camera.pitch -= dy * logic.EDITOR_MOUSE_SENSITIVITY
            camera.pitch = max(
                -89.0,
                min(89.0, camera.pitch),
            )

        keys = logic.game_state.get_keys()
        yaw_rad = math.radians(camera.yaw)
        forward = glm.vec3(math.cos(yaw_rad), 0, math.sin(yaw_rad))
        forward = glm.normalize(forward)
        right = glm.normalize(
            glm.cross(forward, glm.vec3(0, 1, 0))
        )
        up = glm.vec3(0, 1, 0)

        move_dir = glm.vec3(0, 0, 0)
        if Key_W in keys:
            move_dir += forward
        if Key_S in keys:
            move_dir -= forward
        if Key_A in keys:
            move_dir -= right
        if Key_D in keys:
            move_dir += right
        if Key_Space in keys:
            move_dir += up
        if Key_C in keys:
            move_dir -= up

        if glm.length(move_dir) > 0.001:
            move_dir = glm.normalize(move_dir)
            speed = self.EDITOR_CAMERA_SPEED
            if Key_Shift in keys:
                speed *= self.EDITOR_CAMERA_FAST_MULT
            camera.pos += move_dir * speed * delta
