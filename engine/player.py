import math
import glm
from PyQt5.QtCore import Qt
from .constants import TILE_SIZE, GRAVITY, JUMP_STRENGTH, TERMINAL_VELOCITY

class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        # Initialize position (Y is set to 2 tiles high by default)
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle = angle
        self.pitch = 0.0
        
        # Physics constants
        self.speed = 200.0
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = TILE_SIZE, TILE_SIZE * 2, TILE_SIZE
        self.camera_height = 40.0 # Offset from pos.y
        
        # Physics state
        self.on_ground = False
        self.ground_object = None  # Reference to the brush we are standing on
        self.physics_enabled = physics_enabled
        self.step_height = 18.0  # Max height the player can step up automatically

    def get_view_matrix(self):
        """Calculate the view matrix for rendering."""
        # Eye position is player position + camera height offset
        cam_pos = self.pos + glm.vec3(0, self.camera_height, 0)
        
        # Calculate look direction
        direction = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch)
        )
        
        return glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))

    def update(self, delta, move_input, jump, crouch, brushes, movers=None, doors=None, terrain=None):
        """
        Update player physics.
        """
        # Combine all solid objects for collision
        colliders = brushes
        if movers: colliders = colliders + movers
        if doors: colliders = colliders + doors

        # --- 1. Movement Physics ---
        
        # Calculate forward and right vectors relative to player angle
        forward_vec = glm.vec3(math.sin(self.angle), 0, math.cos(self.angle))
        right_vec = glm.vec3(math.cos(self.angle), 0, -math.sin(self.angle))
        
        # Combine inputs to get world-space wish direction
        wish_dir = forward_vec * move_input.z + right_vec * move_input.x
        
        if glm.length(wish_dir) > 0.1:
            wish_dir = glm.normalize(wish_dir)
            
        # Determine speed
        target_speed = self.speed
        if crouch: target_speed *= 0.5
        
        # Apply Horizontal Velocity (Simple acceleration/friction)
        self.velocity.x = wish_dir.x * target_speed
        self.velocity.z = wish_dir.z * target_speed

        if not self.physics_enabled:
            self.pos += self.velocity * delta
            return

        # --- 2. Gravity & Jumping ---
        
        self.velocity.y += GRAVITY * delta
        
        # Terminal velocity clamp
        if self.velocity.y < TERMINAL_VELOCITY:
            self.velocity.y = TERMINAL_VELOCITY

        if jump and self.on_ground:
            self.velocity.y = JUMP_STRENGTH
            self.on_ground = False
            self.ground_object = None

        # --- 3. Collision Resolution ---

        # A. Horizontal X Movement
        original_pos = glm.vec3(self.pos)
        self.pos.x += self.velocity.x * delta
        
        if self._check_overlap(colliders, ignore_brush=self.ground_object):
            # Try Step Up
            self.pos.x = original_pos.x 
            self.pos.y += self.step_height 
            self.pos.x += self.velocity.x * delta 
            
            if not self._check_overlap(colliders, ignore_brush=self.ground_object):
                pass 
            else:
                self.pos = original_pos
                self.pos.x += self.velocity.x * delta
                # IMPORTANT: Pass ground_object to ignore it during X resolution
                self._resolve_collision(colliders, axis='x', ignore_brush=self.ground_object)

        # B. Horizontal Z Movement
        original_pos = glm.vec3(self.pos)
        self.pos.z += self.velocity.z * delta
        
        if self._check_overlap(colliders, ignore_brush=self.ground_object):
            # Try Step Up
            self.pos.z = original_pos.z 
            self.pos.y += self.step_height 
            self.pos.z += self.velocity.z * delta 
            
            if not self._check_overlap(colliders, ignore_brush=self.ground_object):
                pass
            else:
                self.pos = original_pos
                self.pos.z += self.velocity.z * delta
                # IMPORTANT: Pass ground_object to ignore it during Z resolution
                self._resolve_collision(colliders, axis='z', ignore_brush=self.ground_object)

        # C. Vertical Y Movement
        self.pos.y += self.velocity.y * delta
        
        # Reset ground state before checking Y collision
        self.on_ground = False 
        self.ground_object = None
        
        # 1. Check Brushes/Movers (No ignore_brush here, we want to hit the floor)
        self._resolve_collision(colliders, axis='y')

        # 2. Check Terrain
        if terrain and terrain.is_solid():
            terrain_height = terrain.get_height_at_safe(self.pos.x, self.pos.z)
            if terrain_height is not None:
                feet_y = self.pos.y - self.height / 2.0
                if feet_y < terrain_height:
                    self.pos.y = terrain_height + self.height / 2.0
                    self.velocity.y = 0
                    self.on_ground = True
                    self.ground_object = terrain
        
        # Floor clamp (safety net)
        if self.pos.y < -2000:
            self.pos = glm.vec3(0, 100, 0)
            self.velocity = glm.vec3(0, 0, 0)

    def _resolve_collision(self, brushes, axis, ignore_brush=None):
        """
        Resolve AABB collision by pushing the player out of overlapping brushes.
        """
        for brush in brushes:
            if ignore_brush and brush is ignore_brush:
                continue

            # Skip hidden, water, fog
            if brush.get('hidden') or brush.get('is_water') or brush.get('is_fog'):
                continue

            # Movers and Doors must act as solid objects even if flagged oddly.
            is_dynamic_solid = brush.get('is_mover') or brush.get('is_door')
            if brush.get('is_trigger') and not is_dynamic_solid:
                continue

            # Calculate Player AABB (Recalculate inside loop for iterative accuracy)
            player_min = self.pos - glm.vec3(self.width/2, self.height/2, self.depth/2)
            player_max = self.pos + glm.vec3(self.width/2, self.height/2, self.depth/2)

            # Calculate Brush AABB
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5

            # AABB vs AABB Check
            if (player_max.x < b_min.x or player_min.x > b_max.x or
                player_max.y < b_min.y or player_min.y > b_max.y or
                player_max.z < b_min.z or player_min.z > b_max.z):
                continue
                
            # Collision Detected - Resolve based on axis
            if axis == 'x':
                dx1 = player_max.x - b_min.x  # Hit left side of wall
                dx2 = b_max.x - player_min.x  # Hit right side of wall
                
                if dx1 < dx2:
                    self.pos.x -= dx1 + 0.001
                else:
                    self.pos.x += dx2 + 0.001
                self.velocity.x = 0
                
            elif axis == 'z':
                dz1 = player_max.z - b_min.z # Hit back of wall
                dz2 = b_max.z - player_min.z # Hit front of wall
                
                if dz1 < dz2:
                    self.pos.z -= dz1 + 0.001
                else:
                    self.pos.z += dz2 + 0.001
                self.velocity.z = 0
                
            elif axis == 'y':
                dy1 = player_max.y - b_min.y # Head hit ceiling
                dy2 = b_max.y - player_min.y # Feet hit floor
                
                if dy1 < dy2:
                    # Head hit ceiling
                    self.pos.y -= dy1 + 0.001
                    if self.velocity.y > 0: self.velocity.y = 0
                else:
                    # Feet hit floor
                    self.pos.y += dy2
                    self.velocity.y = 0
                    self.on_ground = True
                    self.ground_object = brush

    def _check_overlap(self, brushes, ignore_brush=None):
        """Returns True if the player currently overlaps any solid brush."""
        player_min = self.pos - glm.vec3(self.width/2, self.height/2, self.depth/2)
        player_max = self.pos + glm.vec3(self.width/2, self.height/2, self.depth/2)

        for brush in brushes:
            if brush.get('hidden') or brush.get('is_water') or brush.get('is_fog'):
                continue
            
            is_dynamic_solid = brush.get('is_mover') or brush.get('is_door')
            if brush.get('is_trigger') and not is_dynamic_solid:
                continue
            
            if ignore_brush is not None and brush is ignore_brush:
                continue

            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5

            if (player_max.x > b_min.x and player_min.x < b_max.x and
                player_max.y > b_min.y and player_min.y < b_max.y and
                player_max.z > b_min.z and player_min.z < b_max.z):
                return True
        return False