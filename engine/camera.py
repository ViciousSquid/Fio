import math
import numpy as np
import glm

class Camera:
    def __init__(self):
        # Initialize _pos as a private attribute to be managed by the property
        self._pos = glm.vec3(0.0, 0.0, 0.0)
        self.yaw = -90.0
        self.pitch = 0.0
        self.fov = 90.0

    @property
    def pos(self):
        """Getter for the camera's position."""
        return self._pos

    @pos.setter
    def pos(self, value):
        """
        Setter for the camera's position.
        Checks if the assigned value is a glm.vec3. If not, attempts conversion
        from list/tuple and prints a traceback
        """
        if not isinstance(value, glm.vec3):
            import traceback
            #print(f"WARNING: Camera position being set to non-glm.vec3 type: {type(value)} - Value: {value}")

            if isinstance(value, (list, tuple)):
                try:
                    self._pos = glm.vec3(*value)
                    #print(f"INFO: Converted {type(value)} to glm.vec3.")
                except TypeError as e:
                    print(f"ERROR: Could not convert {type(value)} {value} to glm.vec3: {e}")
                    # Fallback or raise, depending on desired robustness
                    raise
            else:
                # If it's not a glm.vec3, list, or tuple, it's an unexpected type
                raise TypeError(f"Camera position must be glm.vec3, list, or tuple, got {type(value)}")
        else:
            self._pos = value

    def get_view_matrix(self):
        """Calculates the view matrix for the camera's current position and orientation."""
        front = self.get_front_vector()
        return glm.lookAt(self.pos, self.pos + front, glm.vec3(0, 1, 0))

    def get_front_vector(self):
        """Calculates and returns the camera's normalized front vector."""
        yaw_rad = glm.radians(self.yaw)
        pitch_rad = glm.radians(self.pitch)

        front = glm.vec3(
            glm.cos(yaw_rad) * glm.cos(pitch_rad),
            glm.sin(pitch_rad),
            glm.sin(yaw_rad) * glm.cos(pitch_rad)
        )
        return glm.normalize(front)

    def rotate(self, dx, dy, sensitivity=0.1):
        """Rotates the camera based on mouse movement."""
        self.yaw += dx * sensitivity
        self.pitch -= dy * sensitivity
        self.pitch = max(-89.0, min(89.0, self.pitch))

    def move_forward(self, speed):
        """Moves the camera along its front vector, ignoring the pitch."""
        front = self.get_front_vector()
        move_vector = glm.normalize(glm.vec3(front.x, 0, front.z))
        self.pos += move_vector * speed

    def strafe(self, speed):
        """Moves the camera along its right vector."""
        front = self.get_front_vector()
        right = glm.normalize(glm.cross(front, glm.vec3(0, 1, 0)))
        self.pos += right * speed

    def move_up(self, speed):
        """Moves the camera along the world's up vector."""
        # This line now implicitly uses the 'pos' property setter if 'self.pos'
        # were to become a different type, but it should correctly
        # operate on the glm.vec3 if the setter is doing its job.
        self.pos.y += speed

    def zoom(self, amount):
        """Moves the camera forward or backward along its true front vector."""
        front_vector = self.get_front_vector()
        self.pos += front_vector * amount