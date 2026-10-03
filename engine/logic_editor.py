"""Editor-mode runtime delegated from LogicThread.

Owns editor viewport input and camera navigation. Play-mode simulation
orchestration remains entirely in LogicThread.
"""

from __future__ import annotations

import math

import glm


class LogicEditor:
    """Runtime input/navigation for the editor viewport."""

    def __init__(self, logic):
        self.logic = logic

    def tick(self, delta: float):
        logic = self.logic

        dx, dy = logic.game_state.consume_mouse_delta()
        if dx != 0 or dy != 0:
            logic._editor_mouselook_active = True
            logic.editor_camera.yaw += dx * logic.EDITOR_MOUSE_SENSITIVITY
            logic.editor_camera.pitch -= dy * logic.EDITOR_MOUSE_SENSITIVITY
            logic.editor_camera.pitch = max(
                -89.0,
                min(89.0, logic.editor_camera.pitch),
            )

        keys = logic.game_state.get_keys()
        yaw_rad = math.radians(logic.editor_camera.yaw)
        forward = glm.vec3(math.cos(yaw_rad), 0, math.sin(yaw_rad))
        forward = glm.normalize(forward)
        right = glm.normalize(
            glm.cross(forward, glm.vec3(0, 1, 0))
        )
        up = glm.vec3(0, 1, 0)

        move_dir = glm.vec3(0, 0, 0)
        if logic.Key_W in keys:
            move_dir += forward
        if logic.Key_S in keys:
            move_dir -= forward
        if logic.Key_A in keys:
            move_dir -= right
        if logic.Key_D in keys:
            move_dir += right
        if logic.Key_Space in keys:
            move_dir += up
        if logic.Key_C in keys:
            move_dir -= up

        if glm.length(move_dir) > 0.001:
            move_dir = glm.normalize(move_dir)
            speed = logic.EDITOR_CAMERA_SPEED
            if logic.Key_Shift in keys:
                speed *= logic.EDITOR_CAMERA_FAST_MULT
            logic.editor_camera.pos += move_dir * speed * delta
