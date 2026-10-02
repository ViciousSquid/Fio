import threading
import time
from dataclasses import dataclass, field
from typing import List, Dict, Set, Any, Optional
from copy import deepcopy
import glm


@dataclass
class RenderState:
    """Immutable snapshot of game state for rendering."""
    player_pos: glm.vec3 = field(default_factory=lambda: glm.vec3(0, 0, 0))
    player_angle: float = 0.0
    player_pitch: float = 0.0
    player_health: int = 100
    player_max_health: int = 100
    visible_brushes: List[Dict] = field(default_factory=list)
    visible_things: List[Any] = field(default_factory=list)
    active_lights: List[Any] = field(default_factory=list)
    camera_view_matrix: glm.mat4 = field(default_factory=lambda: glm.mat4(1.0))
    timestamp: float = 0.0
    total_brushes: int = 0
    culled_brushes: int = 0


class ThreadedGameState:
    """
    Thread-safe game state manager using double-buffering.
    
    The logic thread writes to the 'write' buffer.
    The render thread reads from the 'read' buffer.
    Buffers are swapped atomically at frame boundaries.
    """
    
    def __init__(self):
        self._lock = threading.Lock()
        self._write_state = RenderState()
        self._read_state = RenderState()
        self._swap_requested = False
        
        # Input state (written by main thread, read by logic thread)
        self._input_lock = threading.Lock()
        self._keys_pressed: Set[int] = set()
        self._mouse_delta = (0.0, 0.0)
        self._use_key_pressed = False  # Single-shot use key
        
    def request_swap(self):
        """Called by logic thread when a frame is complete."""
        with self._lock:
            self._swap_requested = True
            
    def try_swap(self) -> bool:
        """Called by render thread before rendering. Returns True if swap occurred."""
        with self._lock:
            if self._swap_requested:
                self._read_state, self._write_state = self._write_state, self._read_state
                self._swap_requested = False
                return True
            return False
    
    def get_render_state(self) -> RenderState:
        """Get the current render state (called by render thread)."""
        with self._lock:
            return self._read_state
            
    def get_write_state(self) -> RenderState:
        """Get the write state for modification (called by logic thread)."""
        return self._write_state
    
    # Input handling
    def set_keys(self, keys: Set[int]):
        with self._input_lock:
            self._keys_pressed = keys.copy()
            
    def get_keys(self) -> Set[int]:
        with self._input_lock:
            return self._keys_pressed.copy()
            
    def set_mouse_delta(self, dx: float, dy: float):
        with self._input_lock:
            self._mouse_delta = (dx, dy)
            
    def consume_mouse_delta(self) -> tuple:
        with self._input_lock:
            delta = self._mouse_delta
            self._mouse_delta = (0.0, 0.0)
            return delta

    def set_use_key_pressed(self):
        """Signal that the use key was pressed (single-shot)."""
        with self._input_lock:
            self._use_key_pressed = True

    def consume_use_key(self) -> bool:
        """Check and clear the use key state."""
        with self._input_lock:
            pressed = self._use_key_pressed
            self._use_key_pressed = False
            return pressed