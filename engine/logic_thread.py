import threading
import time
import numpy as np
from typing import List, Dict, Any, Optional
import glm
import math

from .threaded_game_state import ThreadedGameState, RenderState
from .player import Player

# Import Thing subclasses for type checking
try:
    from editor.things import Speaker, Pickup, Light
except ImportError:
    Speaker = None
    Pickup = None
    Light = None

class LogicThread(threading.Thread):
    """
    Separate thread for game logic processing.
    Runs at a fixed timestep (default 60 Hz) independent of rendering.
    """
    
    TICK_RATE = 60
    TICK_DURATION = 1.0 / TICK_RATE
    
    def __init__(self, game_state: ThreadedGameState, 
                 brushes: List[Dict], things: List[Any], 
                 visibility_system: Optional[Any] = None):
        super().__init__(daemon=True)
        self.game_state = game_state
        self.brushes = brushes
        self.things = things
        self.visibility_system = visibility_system
        
        self.running = False
        self.player: Optional[Player] = None
        self.play_mode = False
        
        # Player stats
        self.player_health = 100
        self.player_max_health = 100
        
        # Trigger state
        self.player_in_triggers: set = set()
        self.fired_once_triggers: set = set()
        
        # Pickup state
        self.collected_pickups: set = set()
        
        # Speaker state (for triggered speakers)
        self.active_speakers: set = set()
        
        # Mover Animation State
        self.mover_states = {} 
        self._init_movers()
        
        # Performance Monitoring
        self.actual_tps = 0.0
        self._tick_count = 0
        self._last_tps_time = time.perf_counter()

    def set_player(self, player: Optional[Player]):
        """Set or clear the active player."""
        self.player = player
        
    def set_play_mode(self, enabled: bool):
        self.play_mode = enabled
        if enabled:
            self._init_movers()
            # Reset player stats
            self.player_health = 100
            self.player_max_health = 100
            # Reset pickup state
            self.collected_pickups.clear()
            for thing in self.things:
                if Pickup and isinstance(thing, Pickup):
                    thing.properties['collected'] = False
            # Reset speaker state
            self.active_speakers.clear()
        else:
            self.player_in_triggers.clear()
            self.fired_once_triggers.clear()
            self.collected_pickups.clear()
            self.active_speakers.clear()
            self._reset_movers()

    def _init_movers(self):
        """Initialize mover states."""
        self.mover_states = {}
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover') and not brush.get('move_once', False):
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                self.mover_states[i] = {'progress': 0.0, 'forward': True}

    def _reset_movers(self):
        """Reset movers to original positions."""
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.mover_states = {}
            
    def run(self):
        """Main logic loop with fixed timestep."""
        self.running = True
        last_time = time.perf_counter()
        accumulator = 0.0
        
        while self.running:
            current_time = time.perf_counter()
            frame_time = current_time - last_time
            last_time = current_time
            
            if frame_time > 0.25: frame_time = 0.25
                
            accumulator += frame_time
            
            while accumulator >= self.TICK_DURATION:
                self._tick(self.TICK_DURATION)
                accumulator -= self.TICK_DURATION
                self._update_tps_counter()
                
            self._prepare_render_state()
            self.game_state.request_swap()
            
            sleep_time = self.TICK_DURATION - (time.perf_counter() - current_time)
            if sleep_time > 0:
                time.sleep(sleep_time * 0.9)
                
    def stop(self):
        self.running = False

    def _update_tps_counter(self):
        self._tick_count += 1
        t = time.perf_counter()
        if t - self._last_tps_time >= 1.0:
            self.actual_tps = self._tick_count / (t - self._last_tps_time)
            self._tick_count = 0
            self._last_tps_time = t
        
    def _tick(self, delta: float):
        """Single logic tick."""
        if not self.play_mode or not self.player:
            return
            
        # 1. Update Movers (and carry player)
        self._update_movers(delta)

        # 2. Get Input
        keys = self.game_state.get_keys()
        mouse_delta = self.game_state.consume_mouse_delta()
        use_key_pressed = self.game_state.consume_use_key()
        
        # 3. Update Player Physics
        if mouse_delta != (0.0, 0.0):
            self.player.update_angle(mouse_delta[0], mouse_delta[1])
            
        self.player.update(keys, self.brushes, delta)
        
        # 4. Handle Triggers
        self._handle_triggers()
        
        # 5. Handle Pickups
        self._handle_pickups(use_key_pressed)

    def _update_movers(self, delta):
        """Calculate new mover positions and move player if standing on one."""
        for i, brush in enumerate(self.brushes):
            if not brush.get('is_mover') or brush.get('move_once', False): continue
            if not brush.get('start_on', False): continue
            
            # Initialize state if missing
            if i not in self.mover_states:
                if 'original_pos' not in brush: brush['original_pos'] = list(brush['pos'])
                self.mover_states[i] = {'progress': 0.0, 'forward': True}
            
            state = self.mover_states[i]
            speed = brush.get('speed', 64.0)
            distance = brush.get('distance', 128.0)
            direction = np.array(brush.get('direction', [0, 1, 0]), dtype=float)
            
            # Normalize direction
            dir_len = np.linalg.norm(direction)
            if dir_len > 0: direction = direction / dir_len
            
            # Animation Logic
            progress_delta = (speed * delta) / distance if distance > 0 else 0
            
            if state['forward']:
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['forward'] = False
            else:
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['forward'] = True
            
            # Calculate position
            eased_progress = self._ease_in_out(state['progress'])
            original = np.array(brush['original_pos'])
            new_pos_np = original + direction * distance * eased_progress
            new_pos_list = new_pos_np.tolist()

            # --- Physics: Apply velocity to player if standing on this brush ---
            if self.player and self.player.ground_object is brush:
                # Calculate how much the brush moved this specific tick
                current_brush_pos = glm.vec3(brush['pos'])
                next_brush_pos = glm.vec3(new_pos_list)
                brush_delta = next_brush_pos - current_brush_pos
                
                # 1. Move player by that delta
                self.player.pos += brush_delta

                # 2. GLUE FIX: If mover is going down, force player slightly deeper
                # This ensures collision detection (in player.py) registers the collision
                # and keeps 'on_ground' = True.
                if brush_delta.y < 0:
                    self.player.pos.y -= 0.1

            # Update brush position
            brush['pos'] = new_pos_list

    def _ease_in_out(self, t):
        if t < 0.5: return 4 * t * t * t
        else: return 1 - pow(-2 * t + 2, 3) / 2

    def _handle_triggers(self):
        if not self.player:
            return
            
        player_pos = self.player.pos
        currently_colliding = set()
        
        for i, brush in enumerate(self.brushes):
            if not isinstance(brush, dict) or not brush.get('is_trigger'):
                continue
                
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            half_size = size / 2.0
            min_bounds = pos - half_size
            max_bounds = pos + half_size
            
            if (min_bounds.x <= player_pos.x <= max_bounds.x and
                min_bounds.y <= player_pos.y <= max_bounds.y and
                min_bounds.z <= player_pos.z <= max_bounds.z):
                currently_colliding.add(i)
                
                trigger_type = brush.get('trigger_type', 'multiple').lower()
                
                if trigger_type == 'once':
                    # Only fire once, ever
                    if i not in self.player_in_triggers and i not in self.fired_once_triggers:
                        self._activate_trigger(brush, i)
                        self.fired_once_triggers.add(i)
                else:
                    # 'multiple' - fire on entry only (not continuously)
                    if i not in self.player_in_triggers:
                        self._activate_trigger(brush, i)
                    
        self.player_in_triggers = currently_colliding
        
    def _activate_trigger(self, brush: Dict, trigger_id: int):
        target_name = brush.get('target')
        if not target_name:
            return
        
        # Try to find target in brushes (movers)
        for b in self.brushes:
            if b.get('name') == target_name and b.get('is_mover'):
                b['start_on'] = not b.get('start_on', False)
                return
        
        # Try to find target in things (speakers, lights, etc.)
        for thing in self.things:
            if hasattr(thing, 'name') and thing.name == target_name:
                # Handle Speaker
                if Speaker and isinstance(thing, Speaker):
                    # Toggle speaker state
                    current_state = thing.properties.get('state', 'off')
                    thing.properties['state'] = 'on' if current_state == 'off' else 'off'
                    return
                # Handle Light
                if Light and isinstance(thing, Light):
                    current_state = thing.properties.get('state', 'on')
                    thing.properties['state'] = 'on' if current_state == 'off' else 'off'
                    return
            
    def _handle_pickups(self, use_key_pressed: bool):
        """Handle pickup collection based on activation type."""
        if not self.player or not Pickup:
            return
            
        player_pos = self.player.pos
        pickup_radius = 32.0  # Distance to collect a pickup
        use_radius = 64.0     # Distance to use a pickup with E key
        
        for i, thing in enumerate(self.things):
            if not isinstance(thing, Pickup):
                continue
            if thing.properties.get('collected', False):
                continue
            if i in self.collected_pickups:
                continue
                
            thing_pos = glm.vec3(thing.pos)
            distance = glm.distance(player_pos, thing_pos)
            
            activation = thing.properties.get('activation', 'walk_over')
            
            should_collect = False
            if activation == 'walk_over' and distance <= pickup_radius:
                should_collect = True
            elif activation == 'use' and use_key_pressed and distance <= use_radius:
                should_collect = True
                
            if should_collect:
                self._collect_pickup(thing, i)
    
    def _collect_pickup(self, pickup, pickup_id: int):
        """Actually collect a pickup and apply its effect."""
        item_type = pickup.properties.get('item_type', 'health')
        value = pickup.properties.get('value', 25)
        
        if item_type == 'health':
            self.player_health = min(self.player_max_health, self.player_health + value)
        elif item_type == 'armour':
            # Could add armor system later
            pass
        elif item_type == 'ammo':
            # Could add ammo system later
            pass
            
        # Mark as collected
        pickup.properties['collected'] = True
        self.collected_pickups.add(pickup_id)
            
    def _prepare_render_state(self):
        write_state = self.game_state.get_write_state()
        
        if self.player:
            write_state.player_pos = glm.vec3(self.player.pos)
            write_state.player_angle = self.player.angle
            write_state.player_pitch = self.player.pitch
            write_state.camera_view_matrix = self.player.get_view_matrix()
        
        # Player stats
        write_state.player_health = self.player_health
        write_state.player_max_health = self.player_max_health

        # Build safe display list of brushes with statistics tracking
        safe_brushes = []
        for b in self.brushes:
            if b.get('hidden', False):
                continue
            
            if b.get('is_mover', False):
                safe_brushes.append(b.copy())
            else:
                safe_brushes.append(b)

        write_state.visible_brushes = safe_brushes
        write_state.total_brushes = len(self.brushes)
        write_state.culled_brushes = write_state.total_brushes - len(safe_brushes)
        
        # Filter out collected pickups from visible things
        visible_things = []
        for i, thing in enumerate(self.things):
            if Pickup and isinstance(thing, Pickup) and i in self.collected_pickups:
                continue  # Don't render collected pickups
            visible_things.append(thing)
        
        write_state.visible_things = visible_things
        write_state.timestamp = time.perf_counter()