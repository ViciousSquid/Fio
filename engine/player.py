# engine/player.py
import pygame
import math
from .constants import TILE_SIZE, WALL_TILE, FLOOR_HEIGHT

class Player:
    def __init__(self, x, y, angle=math.pi, physics_enabled=True, controls=None):
        self.x, self.y, self.angle, self.height = x, y, angle, TILE_SIZE / 2.0
        self.speed, self.mouse_sensitivity = 8, 0.002
        self.z = 0  # Height from the floor
        self.radius = TILE_SIZE / 5
        
        # Physics properties
        self.physics_enabled = physics_enabled
        self.velocity_z = 0
        self.gravity = -0.5
        self.jump_power = 10
        self.on_ground = True
        self.controls = controls if controls else {'forward': pygame.K_w, 'back': pygame.K_s, 'left': pygame.K_a, 'right': pygame.K_d, 'invert_mouse': False}

    def update(self, tile_map):
        dx_mouse, dy_mouse = pygame.mouse.get_rel()
        
        # Update player's facing angle based on mouse movement
        self.angle = (self.angle - dx_mouse * self.mouse_sensitivity) % (2 * math.pi)
        
        if self.controls.get('invert_mouse'):
            self.angle = (self.angle - dy_mouse * self.mouse_sensitivity) % (2 * math.pi)

        keys = pygame.key.get_pressed()
        forward_input = keys[self.controls['forward']] - keys[self.controls['back']]
        strafe_input = keys[self.controls['right']] - keys[self.controls['left']]
        
        # Calculate movement direction relative to player's facing angle
        dx = (forward_input * math.cos(self.angle)) + (strafe_input * math.cos(self.angle + math.pi/2))
        dy = (forward_input * math.sin(self.angle)) + (strafe_input * math.sin(self.angle + math.pi/2))
        
        # Normalize the vector to prevent faster diagonal movement
        if dx != 0 or dy != 0:
            length = math.sqrt(dx*dx + dy*dy)
            dx = (dx / length) * self.speed
            dy = (dy / length) * self.speed
        
        # Physics handling
        if self.physics_enabled:
            # Jumping
            if keys[pygame.K_SPACE] and self.on_ground:
                self.velocity_z = self.jump_power
                self.on_ground = False
            
            # Apply gravity
            self.velocity_z += self.gravity
            self.z += self.velocity_z
            
            # Check for ground collision
            if self.z <= 0:
                self.z = 0
                self.velocity_z = 0
                self.on_ground = True
        else:
            # Flying controls
            if keys[pygame.K_r]: self.z += self.speed
            if keys[pygame.K_f]: self.z -= self.speed

        # Apply movement with collision detection
        if not self.is_colliding(self.x + dx, self.y, tile_map): 
            self.x += dx
        if not self.is_colliding(self.x, self.y + dy, tile_map): 
            self.y += dy

    def is_colliding(self, x, y, tile_map):
        grid_height, grid_width = tile_map.shape
        for corner_x_offset in [-self.radius, self.radius]:
            for corner_y_offset in [-self.radius, self.radius]:
                check_x, check_y = x + corner_x_offset, y + corner_y_offset
                grid_c, grid_r = int(check_x / TILE_SIZE), int(check_y / TILE_SIZE)
                if not (0 <= grid_r < grid_height and 0 <= grid_c < grid_width): 
                    return True
                if tile_map[grid_r, grid_c] == WALL_TILE: 
                    return True
        return False