import threading
import glm
import time
from typing import List, Any, Dict, Optional

class RenderState:
    """
    A snapshot of the game state specifically for the renderer.
    """
    def __init__(self):
        # Camera / View
        self.camera_view_matrix = glm.mat4(1.0)
        self.projection_matrix = glm.mat4(1.0)
        self.is_play_mode = False
        
        # Editor Camera
        self.editor_camera_pos = glm.vec3(0, 0, 0)
        self.editor_camera_yaw = 0.0
        self.editor_camera_pitch = 0.0
        self.editor_camera_fov = 90.0
        
        # Player
        self.player_pos = glm.vec3(0, 0, 0)
        self.player_angle = 0.0
        self.player_pitch = 0.0
        self.player_health = 100
        self.player_max_health = 100
        self.active_weapon = None
        
        # Scene Data
        self.visible_brushes = []
        self.all_brushes = []
        self.visible_things = []
        
        # HUD / Gameplay
        self.collected_keys = set()
        self.hud_message = ""
        
        # Visual FX
        self.bullet_marks = [] # List of {'pos': [x,y,z], 'alpha': float}
        
        # Debug / Stats
        self.total_brushes = 0
        self.culled_brushes = 0
        self.timestamp = 0.0


class ThreadedGameState:
    """
    Thread-safe container for communication between UI/Input and Logic threads.
    """
    def __init__(self):
        self._render_state_lock = threading.Lock()
        
        # Double Buffering: One state for reading (Render), one for writing (Logic)
        self._read_state = RenderState()
        self._write_state = RenderState()
        self._has_new_frame = False
        
        # Input state
        self._keys_lock = threading.Lock()
        self._keys = set()
        self._mouse_lock = threading.Lock()
        self._mouse_delta = (0.0, 0.0)
        
        # Shot Queue
        self._shot_lock = threading.Lock()
        self._shot_queue = []
        self._use_key_pressed = False
        
    def get_render_state(self) -> RenderState:
        """Called by RenderThread (Qt) to get the latest frame data."""
        with self._render_state_lock:
            return self._read_state

    def get_write_state(self) -> RenderState:
        """Called by LogicThread to get the object to write to."""
        return self._write_state

    def request_swap(self):
        """Called by LogicThread when a frame is completely written."""
        with self._render_state_lock:
            # Swap: write becomes read
            self._read_state = self._write_state
            # Create fresh state for next write to avoid race conditions
            self._write_state = RenderState()
            self._has_new_frame = True

    def try_swap(self) -> bool:
        """Called by QtGameView to check if a new frame is available."""
        with self._render_state_lock:
            if self._has_new_frame:
                self._has_new_frame = False
                return True
            return False

    # --- Input Handling ---

    def set_keys(self, keys: set):
        with self._keys_lock:
            self._keys = keys.copy()
            
    def get_keys(self) -> set:
        with self._keys_lock:
            return self._keys.copy()
            
    def set_mouse_delta(self, dx, dy):
        with self._mouse_lock:
            self._mouse_delta = (self._mouse_delta[0] + dx, self._mouse_delta[1] + dy)
            
    def consume_mouse_delta(self):
        with self._mouse_lock:
            delta = self._mouse_delta
            self._mouse_delta = (0.0, 0.0)
            return delta

    def set_use_key(self, pressed: bool):
        """Sets the state of the use key explicitly (True/False)."""
        self._use_key_pressed = pressed
    
    def set_use_key_pressed(self):
        """Convenience method called by main_window.py to trigger the use key."""
        self._use_key_pressed = True
        
    def consume_use_key(self) -> bool:
        if self._use_key_pressed:
            self._use_key_pressed = False
            return True
        return False

    # --- Shooting Handling ---

    def queue_shot(self):
        with self._shot_lock:
            self._shot_queue.append(True)

    def consume_shot(self):
        with self._shot_lock:
            if self._shot_queue:
                self._shot_queue.pop(0)
                return True
            return False