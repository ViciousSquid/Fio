# engine/player.py
import math
import glm
from PyQt5.QtCore import Qt
from .constants import TILE_SIZE, GRAVITY, JUMP_STRENGTH, TERMINAL_VELOCITY

class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle, self.pitch = angle, 0.0
        self.speed, self.camera_speed = 200, 0.2
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = TILE_SIZE, TILE_SIZE * 2, TILE_SIZE
        
        # Physics state
        self.on_ground = False
        self.ground_object = None  # Reference to the brush we are standing on
        self.physics_enabled = physics_enabled
        self.step_height = 18.0  # Max height the player can step up automatically

    def update_angle(self, dx, dy):
        self.angle = (self.angle - dx * self.mouse_sensitivity) % (2 * math.pi)
        self.pitch = max(-math.pi/2, min(math.pi/2, self.pitch - dy * self.mouse_sensitivity))

    def _check_overlap(self, brushes, ignore_brush=None):
        """Returns True if the player currently overlaps any brush."""
        player_min = self.pos - glm.vec3(self.width/2, self.height/2, self.depth/2)
        player_max = self.pos + glm.vec3(self.width/2, self.height/2, self.depth/2)

        for brush in brushes:
            if brush.get('is_trigger', False): continue
            
            # Ignore the brush we are currently standing on
            if ignore_brush is not None and brush is ignore_brush:
                continue

            b_pos = glm.vec3(brush['pos'])
            b_size = glm.vec3(brush['size'])
            b_min = b_pos - b_size / 2.0
            b_max = b_pos + b_size / 2.0

            if (player_max.x > b_min.x and player_min.x < b_max.x and
                player_max.y > b_min.y and player_min.y < b_max.y and
                player_max.z > b_min.z and player_min.z < b_max.z):
                return True
        return False

    def update(self, keys, brushes, delta):
        # --- 1. Input Processing ---
        forward_input = (1 if Qt.Key_W in keys or Qt.Key_Up in keys else 0) - \
                        (1 if Qt.Key_S in keys or Qt.Key_Down in keys else 0)
        strafe_input = (1 if Qt.Key_A in keys or Qt.Key_Left in keys else 0) - \
                       (1 if Qt.Key_D in keys or Qt.Key_Right in keys else 0)
        is_fast = Qt.Key_Shift in keys
        current_speed = self.speed * 3 if is_fast else self.speed

        cam_forward = glm.vec3(math.sin(self.angle), 0, math.cos(self.angle))
        cam_right = glm.vec3(math.sin(self.angle + math.pi/2), 0, math.cos(self.angle + math.pi/2))

        move_dir = cam_forward * forward_input + cam_right * strafe_input
        if glm.length(move_dir) > 0:
            move_dir = glm.normalize(move_dir)

        self.velocity.x = move_dir.x * current_speed
        self.velocity.z = move_dir.z * current_speed

        if not self.physics_enabled:
            self.pos += move_dir * current_speed * delta
            return

        # --- 2. Physics & Collision with Step Smoothing ---
        
       # A. Horizontal Movement (X Axis)
        original_pos = glm.vec3(self.pos)
        self.pos.x += self.velocity.x * delta
        
        # Pass self.ground_object to ignore the floor we are glued to
        if self._check_overlap(brushes, ignore_brush=self.ground_object):
            self.pos.x = original_pos.x 
            self.pos.y += self.step_height 
            self.pos.x += self.velocity.x * delta 
            
            if not self._check_overlap(brushes, ignore_brush=self.ground_object):
                self._resolve_collision(brushes, axis='y')
            else:
                self.pos = original_pos
                self.pos.x += self.velocity.x * delta
                self._resolve_collision(brushes, axis='x')

        # B. Horizontal Movement (Z Axis)
        original_pos = glm.vec3(self.pos)
        self.pos.z += self.velocity.z * delta
        
        if self._check_overlap(brushes, ignore_brush=self.ground_object):
            self.pos.z = original_pos.z 
            self.pos.y += self.step_height 
            self.pos.z += self.velocity.z * delta 
            
            if not self._check_overlap(brushes, ignore_brush=self.ground_object):
                self._resolve_collision(brushes, axis='y')
            else:
                self.pos = original_pos
                self.pos.z += self.velocity.z * delta
                self._resolve_collision(brushes, axis='z')

        # C. Vertical Movement (Y Axis)
        self.velocity.y += GRAVITY * delta
        if self.velocity.y < TERMINAL_VELOCITY:
            self.velocity.y = TERMINAL_VELOCITY

        if Qt.Key_Space in keys and self.on_ground:
            self.velocity.y = JUMP_STRENGTH
            self.on_ground = False
            self.ground_object = None

        self.pos.y += self.velocity.y * delta
        
        self.on_ground = False 
        self.ground_object = None
        
        self._resolve_collision(brushes, axis='y')

    def _resolve_collision(self, brushes, axis):
        """
        Axis-Aligned Bounding Box (AABB) collision resolution.
        Moves the player out of the wall/floor if they overlap.
        """
        player_min = self.pos - glm.vec3(self.width/2, self.height/2, self.depth/2)
        player_max = self.pos + glm.vec3(self.width/2, self.height/2, self.depth/2)

        for brush in brushes:
            if brush.get('is_trigger', False):
                continue

            # Calculate Brush AABB
            b_pos = glm.vec3(brush['pos'])
            b_size = glm.vec3(brush['size'])
            b_min = b_pos - b_size / 2.0
            b_max = b_pos + b_size / 2.0

            # Check for overlap
            if (player_max.x > b_min.x and player_min.x < b_max.x and
                player_max.y > b_min.y and player_min.y < b_max.y and
                player_max.z > b_min.z and player_min.z < b_max.z):

                # Resolve based on the axis we just moved on
                if axis == 'x':
                    if self.velocity.x > 0: # Moving Right -> Hit Left Wall
                        self.pos.x = b_min.x - self.width / 2 - 0.001
                    elif self.velocity.x < 0: # Moving Left -> Hit Right Wall
                        self.pos.x = b_max.x + self.width / 2 + 0.001
                    self.velocity.x = 0
                
                elif axis == 'z':
                    if self.velocity.z > 0: # Moving Forward -> Hit Back Wall
                        self.pos.z = b_min.z - self.depth / 2 - 0.001
                    elif self.velocity.z < 0: # Moving Backward -> Hit Front Wall
                        self.pos.z = b_max.z + self.depth / 2 + 0.001
                    self.velocity.z = 0
                
                elif axis == 'y':
                    if self.velocity.y < 0: # Falling -> Hit Floor
                        self.pos.y = b_max.y + self.height / 2
                        self.velocity.y = 0
                        self.on_ground = True
                        self.ground_object = brush # Store reference for movers
                    elif self.velocity.y > 0: # Jumping -> Hit Ceiling
                        self.pos.y = b_min.y - self.height / 2
                        self.velocity.y = 0

    def get_position(self):
        return self.pos

    def get_view_matrix(self):
        cam_forward = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch)
        )
        target = self.pos + cam_forward
        return glm.lookAt(self.pos, target, glm.vec3(0, 1, 0))