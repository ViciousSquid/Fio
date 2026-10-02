"""
Input Handlers for RStudio I/O System

This module registers all input handlers that define what happens when
an input is called on an entity.

Handler signature: (entity, parameter: str, logic_thread) -> None
"""

from .io_system import IOManager
import glm
import os

# Import debug logger - with fallback to print if not available
try:
    from .debug_console import debug_log
except ImportError:
    try:
        from editor.debug_console import debug_log
    except ImportError:
        def debug_log(category, message):
            print(f"[{category}] {message}")


def register_all_input_handlers(io_manager: IOManager):
    """Register all input handlers with the I/O manager."""
    
    # ==========================================================================
    # LIGHT INPUTS
    # ==========================================================================
    
    def light_turn_on(entity, param, logic):
        entity.properties['state'] = 'on'
        logic.io_manager.fire_output(entity, 'OnTurnedOn')
    
    def light_turn_off(entity, param, logic):
        entity.properties['state'] = 'off'
        logic.io_manager.fire_output(entity, 'OnTurnedOff')
    
    def light_toggle(entity, param, logic):
        current = entity.properties.get('state', 'on')
        if current == 'on':
            light_turn_off(entity, param, logic)
        else:
            light_turn_on(entity, param, logic)
    
    def light_set_brightness(entity, param, logic):
        try:
            value = float(param) if param else 1.0
            entity.properties['intensity'] = max(0.0, min(10.0, value))
        except ValueError:
            pass
    
    def light_set_color(entity, param, logic):
        """Set color from 'R G B' string (0-255)."""
        try:
            parts = param.split()
            if len(parts) >= 3:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                entity.properties['colour'] = [r, g, b]
        except (ValueError, IndexError):
            pass
    
    io_manager.register_input_handler('light', 'turnon', light_turn_on)
    io_manager.register_input_handler('light', 'turnoff', light_turn_off)
    io_manager.register_input_handler('light', 'toggle', light_toggle)
    io_manager.register_input_handler('light', 'setbrightness', light_set_brightness)
    io_manager.register_input_handler('light', 'setcolor', light_set_color)
    
    # ==========================================================================
    # DOOR INPUTS
    # ==========================================================================
    
    def door_open(entity, param, logic):
        """Open a door brush."""
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        
        if idx not in logic.door_states:
            if 'original_pos' not in entity:
                entity['original_pos'] = list(entity['pos'])
            logic.door_states[idx] = {
                'progress': 0.0,
                'state': 'closed',
                'open_timer': 0.0
            }
        
        state = logic.door_states[idx]
        if state['state'] == 'closed':
            state['state'] = 'opening'
            logic.io_manager.fire_output(entity, 'OnOpen')
    
    def door_close(entity, param, logic):
        """Close a door brush."""
        idx = _get_brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        
        state = logic.door_states[idx]
        if state['state'] == 'open':
            state['state'] = 'closing'
            logic.io_manager.fire_output(entity, 'OnClose')
    
    def door_toggle(entity, param, logic):
        """Toggle door open/closed."""
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        
        if idx in logic.door_states:
            state = logic.door_states[idx]
            if state['state'] == 'closed':
                door_open(entity, param, logic)
            elif state['state'] == 'open':
                door_close(entity, param, logic)
        else:
            door_open(entity, param, logic)
    
    def door_lock(entity, param, logic):
        entity['door_locked'] = True
    
    def door_unlock(entity, param, logic):
        entity['door_locked'] = False
    
    def door_set_speed(entity, param, logic):
        try:
            entity['speed'] = float(param) if param else 128.0
        except ValueError:
            pass
    
    io_manager.register_input_handler('door', 'open', door_open)
    io_manager.register_input_handler('door', 'close', door_close)
    io_manager.register_input_handler('door', 'toggle', door_toggle)
    io_manager.register_input_handler('door', 'lock', door_lock)
    io_manager.register_input_handler('door', 'unlock', door_unlock)
    io_manager.register_input_handler('door', 'setspeed', door_set_speed)
    
    # ==========================================================================
    # MOVER INPUTS
    # ==========================================================================
    
    def mover_open(entity, param, logic):
        entity['start_on'] = True
        idx = _get_brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]['forward'] = True
    
    def mover_close(entity, param, logic):
        entity['start_on'] = True
        idx = _get_brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]['forward'] = False
    
    def mover_toggle(entity, param, logic):
        entity['start_on'] = not entity.get('start_on', False)
    
    def mover_set_position(entity, param, logic):
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        try:
            value = max(0.0, min(1.0, float(param)))
            if idx in logic.mover_states:
                logic.mover_states[idx]['progress'] = value
        except ValueError:
            pass
    
    def mover_enable(entity, param, logic):
        entity['start_on'] = True
    
    def mover_disable(entity, param, logic):
        entity['start_on'] = False
    
    io_manager.register_input_handler('mover', 'open', mover_open)
    io_manager.register_input_handler('mover', 'close', mover_close)
    io_manager.register_input_handler('mover', 'toggle', mover_toggle)
    io_manager.register_input_handler('mover', 'setposition', mover_set_position)
    io_manager.register_input_handler('mover', 'enable', mover_enable)
    io_manager.register_input_handler('mover', 'disable', mover_disable)
    
    # ==========================================================================
    # TRIGGER INPUTS
    # ==========================================================================
    
    def trigger_enable(entity, param, logic):
        entity['disabled'] = False
        debug_log('Trigger', f"Enabled trigger '{entity.get('name', 'unnamed')}'")
    
    def trigger_disable(entity, param, logic):
        entity['disabled'] = True
        debug_log('Trigger', f"Disabled trigger '{entity.get('name', 'unnamed')}'")
    
    def trigger_toggle(entity, param, logic):
        entity['disabled'] = not entity.get('disabled', False)
    
    def trigger_touch_test(entity, param, logic):
        if not logic.player:
            return
        
        pos = glm.vec3(entity['pos'])
        size = glm.vec3(entity['size'])
        half_size = size / 2.0
        min_b = pos - half_size
        max_b = pos + half_size
        
        player_pos = logic.player.pos
        if (min_b.x <= player_pos.x <= max_b.x and
            min_b.y <= player_pos.y <= max_b.y and
            min_b.z <= player_pos.z <= max_b.z):
            logic.io_manager.fire_output(entity, 'OnTrigger')
    
    io_manager.register_input_handler('trigger', 'enable', trigger_enable)
    io_manager.register_input_handler('trigger', 'disable', trigger_disable)
    io_manager.register_input_handler('trigger', 'toggle', trigger_toggle)
    io_manager.register_input_handler('trigger', 'touchtest', trigger_touch_test)
    
    # ==========================================================================
    # SPEAKER INPUTS
    # ==========================================================================
    
    def speaker_play(entity, param, logic):
        """Start playing sound - queues to main thread via game_state."""
        entity_name = entity.properties.get('name', 'unnamed')
        sound_file = entity.properties.get('sound_file', '')
        volume = float(entity.properties.get('volume', 1.0))
        
        debug_log('Speaker', f"PlaySound called on '{entity_name}'")
        debug_log('Speaker', f"  sound_file='{sound_file}', volume={volume}")
        
        entity.properties['state'] = 'on'
        speaker_id = id(entity)
        logic.active_speakers.add(speaker_id)
        
        if not sound_file:
            debug_log('Error', f"No sound file configured for speaker '{entity_name}'!")
            return
        
        # Get game_state - try multiple paths for robustness
        game_state = None
        
        # Path 1: From logic thread directly
        if hasattr(logic, 'game_state') and logic.game_state is not None:
            game_state = logic.game_state
            debug_log('Speaker', f"  Found game_state via logic.game_state")
        
        # Path 2: From io_manager
        if game_state is None and hasattr(logic, 'io_manager'):
            gs = logic.io_manager.get_game_state()
            if gs is not None:
                game_state = gs
                debug_log('Speaker', f"  Found game_state via io_manager")
        
        if game_state is None:
            debug_log('Error', f"Could not find game_state for speaker '{entity_name}'!")
            return
        
        # Ensure sound_queue exists
        if not hasattr(game_state, 'sound_queue'):
            debug_log('Speaker', f"  Creating sound_queue on game_state")
            game_state.sound_queue = []
        
        # Queue the sound for the main thread to play
        game_state.sound_queue.append({
            'file': sound_file,
            'volume': volume,
            'entity_id': speaker_id
        })
        debug_log('Speaker', f"  Queued '{sound_file}' (queue size: {len(game_state.sound_queue)})")
        
        # Fire output event
        logic.io_manager.fire_output(entity, 'OnSoundStarted')
    
    def speaker_stop(entity, param, logic):
        entity.properties['state'] = 'off'
        speaker_id = id(entity)
        logic.active_speakers.discard(speaker_id)
        debug_log('Speaker', f"Stopped speaker '{entity.properties.get('name', 'unnamed')}'")
        logic.io_manager.fire_output(entity, 'OnSoundFinished')
    
    def speaker_toggle(entity, param, logic):
        current = entity.properties.get('state', 'off')
        if current == 'on':
            speaker_stop(entity, param, logic)
        else:
            speaker_play(entity, param, logic)
    
    def speaker_set_volume(entity, param, logic):
        try:
            entity.properties['volume'] = max(0.0, min(1.0, float(param)))
        except ValueError:
            pass
    
    io_manager.register_input_handler('speaker', 'playsound', speaker_play)
    io_manager.register_input_handler('speaker', 'stopsound', speaker_stop)
    io_manager.register_input_handler('speaker', 'toggle', speaker_toggle)
    io_manager.register_input_handler('speaker', 'setvolume', speaker_set_volume)
    
    # ==========================================================================
    # PICKUP INPUTS
    # ==========================================================================
    
    def pickup_enable(entity, param, logic):
        entity.properties['disabled'] = False
    
    def pickup_disable(entity, param, logic):
        entity.properties['disabled'] = True
    
    def pickup_respawn(entity, param, logic):
        entity.properties['collected'] = False
        for i, thing in enumerate(logic.things):
            if thing is entity:
                logic.collected_pickups.discard(i)
                break
        logic.io_manager.fire_output(entity, 'OnRespawn')
    
    def pickup_set_value(entity, param, logic):
        try:
            entity.properties['value'] = int(param)
        except ValueError:
            pass
    
    io_manager.register_input_handler('pickup', 'enable', pickup_enable)
    io_manager.register_input_handler('pickup', 'disable', pickup_disable)
    io_manager.register_input_handler('pickup', 'respawn', pickup_respawn)
    io_manager.register_input_handler('pickup', 'setvalue', pickup_set_value)
    
    # ==========================================================================
    # LOGIC_RELAY INPUTS
    # ==========================================================================
    
    def relay_trigger(entity, param, logic):
        if entity.properties.get('disabled', False):
            return
        logic.io_manager.fire_output(entity, 'OnTrigger')
    
    def relay_enable(entity, param, logic):
        entity.properties['disabled'] = False
    
    def relay_disable(entity, param, logic):
        entity.properties['disabled'] = True
    
    def relay_toggle(entity, param, logic):
        entity.properties['disabled'] = not entity.properties.get('disabled', False)
    
    io_manager.register_input_handler('logic_relay', 'trigger', relay_trigger)
    io_manager.register_input_handler('logic_relay', 'enable', relay_enable)
    io_manager.register_input_handler('logic_relay', 'disable', relay_disable)
    io_manager.register_input_handler('logic_relay', 'toggle', relay_toggle)
    
    # ==========================================================================
    # LOGIC_GATE INPUTS
    # ==========================================================================
    
    def gate_trigger(entity, param, logic):
        if entity.properties.get('disabled', False):
            return
        
        gate_name = entity.name
        logic_type = entity.properties.get('logic_type', 'AND')
        
        if gate_name not in logic.gate_inputs:
            logic.gate_inputs[gate_name] = set()
        
        source_name = param if param else 'anonymous'
        if source_name in logic.gate_inputs[gate_name]:
            logic.gate_inputs[gate_name].remove(source_name)
        else:
            logic.gate_inputs[gate_name].add(source_name)
        
        active_inputs = len(logic.gate_inputs[gate_name])
        
        total_possible = 0
        from .io_system import get_connections
        for b in logic.brushes:
            for conn in get_connections(b):
                if conn.target_name == gate_name:
                    total_possible += 1
        for t in logic.things:
            if t is not entity:
                for conn in get_connections(t):
                    if conn.target_name == gate_name:
                        total_possible += 1
        
        if total_possible == 0:
            total_possible = 1
        
        should_fire = False
        if logic_type == 'AND':
            should_fire = (active_inputs >= total_possible)
        elif logic_type == 'OR':
            should_fire = (active_inputs > 0)
        elif logic_type == 'XOR':
            should_fire = (active_inputs == 1)
        elif logic_type == 'NAND':
            should_fire = (active_inputs < total_possible)
        elif logic_type == 'NOR':
            should_fire = (active_inputs == 0)
        
        if should_fire:
            logic.io_manager.fire_output(entity, 'OnTrigger')
    
    def gate_reset(entity, param, logic):
        gate_name = entity.name
        if gate_name in logic.gate_inputs:
            logic.gate_inputs[gate_name].clear()
    
    io_manager.register_input_handler('logic_gate', 'trigger', gate_trigger)
    io_manager.register_input_handler('logic_gate', 'reset', gate_reset)
    io_manager.register_input_handler('logic_gate', 'enable', relay_enable)
    io_manager.register_input_handler('logic_gate', 'disable', relay_disable)
    
    # ==========================================================================
    # LOGIC_TIMER INPUTS
    # ==========================================================================
    
    def timer_enable(entity, param, logic):
        entity.properties['timer_enabled'] = True
        if not hasattr(logic, 'timer_states'):
            logic.timer_states = {}
        entity_id = id(entity)
        interval = float(entity.properties.get('interval', 1.0))
        logic.timer_states[entity_id] = {
            'remaining': interval,
            'interval': interval
        }
    
    def timer_disable(entity, param, logic):
        entity.properties['timer_enabled'] = False
    
    def timer_toggle(entity, param, logic):
        if entity.properties.get('timer_enabled', False):
            timer_disable(entity, param, logic)
        else:
            timer_enable(entity, param, logic)
    
    def timer_fire(entity, param, logic):
        logic.io_manager.fire_output(entity, 'OnTimer')
    
    def timer_set_time(entity, param, logic):
        try:
            entity.properties['interval'] = max(0.1, float(param))
        except ValueError:
            pass
    
    io_manager.register_input_handler('logic_timer', 'enable', timer_enable)
    io_manager.register_input_handler('logic_timer', 'disable', timer_disable)
    io_manager.register_input_handler('logic_timer', 'toggle', timer_toggle)
    io_manager.register_input_handler('logic_timer', 'firetimer', timer_fire)
    io_manager.register_input_handler('logic_timer', 'settime', timer_set_time)
    
    # ==========================================================================
    # MODEL INPUTS
    # ==========================================================================
    
    def model_enable(entity, param, logic):
        entity.properties['hidden'] = False
    
    def model_disable(entity, param, logic):
        entity.properties['hidden'] = True
    
    io_manager.register_input_handler('model', 'enable', model_enable)
    io_manager.register_input_handler('model', 'disable', model_disable)
    
    # ==========================================================================
    # MONSTER INPUTS
    # ==========================================================================
    
    def monster_kill(entity, param, logic):
        entity.properties['_kill'] = True
        logic.io_manager.fire_output(entity, 'OnDeath')
    
    io_manager.register_input_handler('monster', 'kill', monster_kill)
    io_manager.register_input_handler('monster', 'enable', relay_enable)
    io_manager.register_input_handler('monster', 'disable', relay_disable)
    
    # Log summary
    # Retrieve version from version.txt in the same directory
    version_str = "Unknown"
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        version_path = os.path.join(current_dir, 'version.txt')
        if os.path.exists(version_path):
            with open(version_path, 'r') as f:
                version_str = f.read().strip()
    except (OSError, IOError):
        pass

    # Use HTML bold tags for the console title
    debug_log('Info', f"<b>RStudio {version_str}</b>")
    debug_log('Info', f"Registered {len(io_manager._input_handlers)} input handlers")


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_brush_index(brush: dict, logic) -> int:
    """Get the index of a brush in the brushes list."""
    try:
        return logic.brushes.index(brush)
    except ValueError:
        return -1