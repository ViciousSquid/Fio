"""
HL2-Style Input/Output System for RStudio

This module implements an event-driven entity communication system inspired by
Half-Life 2's Hammer Editor. Entities ("things") communicate through:

- **Inputs**: Actions an entity can receive (e.g., TurnOn, Open, Kill)
- **Outputs**: Events an entity fires (e.g., OnTrigger, OnDamaged, OnOpened)
- **Connections**: Links from outputs to target entity inputs with delay/parameters

Example: A trigger_once fires "OnTrigger" which calls "Open" on "door_main" after 0.5s
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Callable, Optional, Set
from enum import Enum
import time

# Import debug logger - with fallback to print if not available
try:
    from .debug_console import debug_log
except ImportError:
    try:
        from editor.debug_console import debug_log
    except ImportError:
        def debug_log(category, message, also_print=True):
            if also_print:  # Only print if also_print is True
                print(f"[{category}] {message}")


# =============================================================================
# DEBUG CONFIGURATION
# =============================================================================

# Set to False to disable all I/O debug logging
IO_DEBUG_ENABLED = True


def io_log(message: str):
    """Log an I/O system message to the debug console."""
    if IO_DEBUG_ENABLED:
        debug_log("IO", message)


# =============================================================================
# INPUT/OUTPUT DEFINITIONS
# =============================================================================

class IODef:
    """Definition of an input or output with metadata."""
    def __init__(self, name: str, description: str = "", param_type: str = ""):
        self.name = name
        self.description = description
        self.param_type = param_type  # "", "float", "int", "string", "bool", "color"
    
    def __repr__(self):
        return f"IODef({self.name})"


# Registry of all entity I/O definitions
# Maps entity_type -> {'inputs': [IODef, ...], 'outputs': [IODef, ...]}
IO_REGISTRY: Dict[str, Dict[str, List[IODef]]] = {}


def register_io(entity_type: str, inputs: List[IODef], outputs: List[IODef]):
    """Register I/O definitions for an entity type."""
    IO_REGISTRY[entity_type] = {
        'inputs': inputs,
        'outputs': outputs
    }


def get_inputs(entity_type: str) -> List[IODef]:
    """Get available inputs for an entity type."""
    if entity_type in IO_REGISTRY:
        return IO_REGISTRY[entity_type]['inputs']
    return []


def get_outputs(entity_type: str) -> List[IODef]:
    """Get available outputs for an entity type."""
    if entity_type in IO_REGISTRY:
        return IO_REGISTRY[entity_type]['outputs']
    return []


def get_input_names(entity_type: str) -> List[str]:
    """Get list of input names for an entity type."""
    return [io.name for io in get_inputs(entity_type)]


def get_output_names(entity_type: str) -> List[str]:
    """Get list of output names for an entity type."""
    return [io.name for io in get_outputs(entity_type)]


# =============================================================================
# OUTPUT CONNECTION
# =============================================================================

@dataclass
class OutputConnection:
    """
    Represents a single output-to-input connection.
    
    When the source entity fires 'output_name', it calls 'input_name' on 
    the entity named 'target_name' after 'delay' seconds, passing 'parameter'.
    """
    output_name: str          # Which output triggers this connection
    target_name: str          # Name of target entity
    input_name: str           # Which input to call on target
    parameter: str = ""       # Optional parameter to pass
    delay: float = 0.0        # Delay in seconds before firing
    fire_once: bool = False   # If True, connection is removed after firing
    _fired: bool = field(default=False, repr=False)  # Internal tracking
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for saving."""
        return {
            'output': self.output_name,
            'target': self.target_name,
            'input': self.input_name,
            'parameter': self.parameter,
            'delay': self.delay,
            'fire_once': self.fire_once
        }
    
    @staticmethod
    def from_dict(data: dict) -> 'OutputConnection':
        """Deserialize from dictionary."""
        return OutputConnection(
            output_name=data.get('output', ''),
            target_name=data.get('target', ''),
            input_name=data.get('input', ''),
            parameter=data.get('parameter', ''),
            delay=float(data.get('delay', 0.0)),
            fire_once=bool(data.get('fire_once', False))
        )
    
    def reset(self):
        """Reset the fired state (used when entering play mode)."""
        self._fired = False


# =============================================================================
# PENDING EVENT (for delayed firing)
# =============================================================================

@dataclass
class PendingEvent:
    """An event queued to fire after a delay."""
    fire_time: float          # Time when this should fire
    target_name: str          # Target entity name
    input_name: str           # Input to call
    parameter: str            # Parameter to pass
    source_name: str          # Who fired this (for debugging)
    connection: OutputConnection = None  # Original connection (for fire_once tracking)


# =============================================================================
# I/O MANAGER
# =============================================================================

class IOManager:
    """
    Manages all entity I/O connections and event dispatching.
    
    This is the central hub that:
    - Stores pending delayed events
    - Dispatches output fires to target inputs
    - Handles the input execution on entities
    """
    
    def __init__(self):
        self.pending_events: List[PendingEvent] = []
        self.current_time: float = 0.0
        
        # Input handlers: Maps (entity_type, input_name) -> handler function
        # Handler signature: (entity, parameter: str, logic_thread) -> None
        self._input_handlers: Dict[tuple, Callable] = {}
        
        # Entity lookup function - set by logic_thread
        self._find_entity: Optional[Callable] = None
        
        self._logic_thread = None
        self._game_state = None
    
    def set_logic_thread(self, logic_thread):
        """Set reference to logic thread."""
        self._logic_thread = logic_thread

    def set_game_state(self, game_state):
        """Set reference to game state for sound queue access."""
        self._game_state = game_state
    
    def get_game_state(self):
        """Get the game state reference."""
        return self._game_state
    
    def set_entity_finder(self, finder: Callable):
        """
        Set the function used to find entities by name.
        finder(name: str) -> entity or None
        """
        self._find_entity = finder
    
    def register_input_handler(self, entity_type: str, input_name: str, 
                                handler: Callable):
        """
        Register a handler for a specific entity type and input.
        
        handler(entity, parameter: str, logic_thread) -> None
        """
        key = (entity_type.lower(), input_name.lower())
        self._input_handlers[key] = handler
    
    def reset(self):
        """Reset for new play session."""
        self.pending_events.clear()
        self.current_time = 0.0
    
    def fire_output(self, source_entity, output_name: str):
        """
        Fire an output from an entity, triggering all connected inputs.
        
        source_entity: The entity firing the output (Thing or brush dict)
        output_name: Name of the output being fired
        """
        # Get connections from the entity
        connections = self._get_connections(source_entity)
        source_name = self._get_entity_name(source_entity)
        
        io_log(f"fire_output: {source_name}.{output_name} ({len(connections)} connections)")
        
        matching_count = 0
        for conn in connections:
            if conn.output_name.lower() != output_name.lower():
                continue
            
            matching_count += 1
            
            # Check fire_once
            if conn.fire_once and conn._fired:
                io_log(f"  -> {conn.target_name}.{conn.input_name} SKIPPED (fire_once)")
                continue
            
            # Mark as fired
            conn._fired = True
            
            delay_str = f" (delay={conn.delay}s)" if conn.delay > 0 else ""
            io_log(f"  -> {conn.target_name}.{conn.input_name}{delay_str}")
            
            if conn.delay > 0:
                # Queue for delayed execution
                event = PendingEvent(
                    fire_time=self.current_time + conn.delay,
                    target_name=conn.target_name,
                    input_name=conn.input_name,
                    parameter=conn.parameter,
                    source_name=source_name,
                    connection=conn
                )
                self.pending_events.append(event)
            else:
                # Execute immediately
                self._execute_input(conn.target_name, conn.input_name, 
                                   conn.parameter, source_name)
        
        if matching_count == 0:
            io_log(f"  (no connections for output '{output_name}')")
    
    def update(self, delta: float):
        """
        Update the I/O manager, processing delayed events.
        Call this every logic tick.
        """
        self.current_time += delta
        
        # Process pending events
        still_pending = []
        for event in self.pending_events:
            if self.current_time >= event.fire_time:
                io_log(f"Delayed event firing: {event.target_name}.{event.input_name}")
                self._execute_input(event.target_name, event.input_name,
                                   event.parameter, event.source_name)
            else:
                still_pending.append(event)
        
        self.pending_events = still_pending
    
    def _execute_input(self, target_name: str, input_name: str, 
                       parameter: str, source_name: str):
        """Execute an input on a target entity."""
        if not self._find_entity:
            io_log("ERROR: No entity finder set!")
            return
        
        target = self._find_entity(target_name)
        if target is None:
            io_log(f"ERROR: Target '{target_name}' not found!")
            return
        
        # Get entity type
        entity_type = self._get_entity_type(target)
        
        io_log(f"execute_input: {target_name} (type={entity_type}).{input_name}")
        
        # Look up handler
        handler_key = (entity_type.lower(), input_name.lower())
        handler = self._input_handlers.get(handler_key)
        
        if handler:
            try:
                handler(target, parameter, self._logic_thread)
            except Exception as e:
                debug_log("Error", f"{entity_type}.{input_name} handler failed: {e}")
                import traceback
                traceback.print_exc()
        else:
            io_log(f"  No handler for {handler_key}, trying generic...")
            # Fallback: try generic handlers
            self._try_generic_input(target, input_name, parameter)
    
    def _try_generic_input(self, entity, input_name: str, parameter: str):
        """Try generic input handling for common patterns."""
        input_lower = input_name.lower()
        
        # Generic enable/disable
        if input_lower == 'enable':
            if isinstance(entity, dict):
                entity['disabled'] = False
            elif hasattr(entity, 'properties'):
                entity.properties['disabled'] = False
                
        elif input_lower == 'disable':
            if isinstance(entity, dict):
                entity['disabled'] = True
            elif hasattr(entity, 'properties'):
                entity.properties['disabled'] = True
                
        elif input_lower == 'kill':
            # Mark for removal (handled by logic thread)
            if isinstance(entity, dict):
                entity['_kill'] = True
            elif hasattr(entity, 'properties'):
                entity.properties['_kill'] = True
    
    def _get_connections(self, entity) -> List[OutputConnection]:
        """Get output connections from an entity."""
        if isinstance(entity, dict):
            # Brush
            return entity.get('_io_connections', [])
        elif hasattr(entity, 'properties'):
            # Thing
            return entity.properties.get('_io_connections', [])
        return []
    
    def _get_entity_name(self, entity) -> str:
        """Get the name of an entity."""
        if isinstance(entity, dict):
            return entity.get('name', '')
        elif hasattr(entity, 'name'):
            return entity.name
        elif hasattr(entity, 'properties'):
            return entity.properties.get('name', '')
        return ''
    
    def _get_entity_type(self, entity) -> str:
        """Get the type of an entity."""
        if isinstance(entity, dict):
            # Brush types
            if entity.get('is_trigger'):
                return 'trigger'
            elif entity.get('is_door'):
                return 'door'
            elif entity.get('is_mover'):
                return 'mover'
            elif entity.get('is_water'):
                return 'water'
            elif entity.get('is_fog'):
                return 'fog'
            return 'brush'
        elif hasattr(entity, 'properties'):
            return entity.properties.get('type', 'thing')
        return 'unknown'


# =============================================================================
# DEFAULT I/O DEFINITIONS
# =============================================================================

def register_default_io():
    """Register default I/O definitions for all entity types."""
    
    # === TRIGGER ===
    register_io('trigger', 
        inputs=[
            IODef('Enable', 'Enable this trigger'),
            IODef('Disable', 'Disable this trigger'),
            IODef('Toggle', 'Toggle enabled state'),
            IODef('TouchTest', 'Fire OnTrigger if player is inside'),
        ],
        outputs=[
            IODef('OnTrigger', 'Fired when activated'),
            IODef('OnStartTouch', 'Fired when player enters'),
            IODef('OnEndTouch', 'Fired when player exits'),
        ]
    )
    
    # === DOOR ===
    register_io('door',
        inputs=[
            IODef('Open', 'Open the door'),
            IODef('Close', 'Close the door'),
            IODef('Toggle', 'Toggle open/closed state'),
            IODef('Lock', 'Lock the door'),
            IODef('Unlock', 'Unlock the door'),
            IODef('SetSpeed', 'Set movement speed', 'float'),
        ],
        outputs=[
            IODef('OnOpen', 'Fired when door starts opening'),
            IODef('OnClose', 'Fired when door starts closing'),
            IODef('OnFullyOpen', 'Fired when door is fully open'),
            IODef('OnFullyClosed', 'Fired when door is fully closed'),
            IODef('OnLockedUse', 'Fired when player tries locked door'),
        ]
    )
    
    # === MOVER (func_movelinear equivalent) ===
    register_io('mover',
        inputs=[
            IODef('Open', 'Move to end position'),
            IODef('Close', 'Move to start position'),
            IODef('Toggle', 'Toggle movement direction'),
            IODef('SetPosition', 'Set position (0-1)', 'float'),
            IODef('SetSpeed', 'Set movement speed', 'float'),
            IODef('Enable', 'Enable movement'),
            IODef('Disable', 'Disable movement'),
        ],
        outputs=[
            IODef('OnFullyOpen', 'Fired when reaching end position'),
            IODef('OnFullyClosed', 'Fired when reaching start position'),
        ]
    )
    
    # === LIGHT ===
    register_io('light',
        inputs=[
            IODef('TurnOn', 'Turn light on'),
            IODef('TurnOff', 'Turn light off'),
            IODef('Toggle', 'Toggle on/off state'),
            IODef('SetBrightness', 'Set intensity (0-10)', 'float'),
            IODef('SetColor', 'Set color (R G B)', 'color'),
            IODef('FadeIn', 'Fade in over time', 'float'),
            IODef('FadeOut', 'Fade out over time', 'float'),
        ],
        outputs=[
            IODef('OnTurnedOn', 'Fired when light turns on'),
            IODef('OnTurnedOff', 'Fired when light turns off'),
        ]
    )
    
    # === SPEAKER ===
    register_io('speaker',
        inputs=[
            IODef('PlaySound', 'Start playing sound'),
            IODef('StopSound', 'Stop playing sound'),
            IODef('Toggle', 'Toggle playback'),
            IODef('SetVolume', 'Set volume (0-1)', 'float'),
        ],
        outputs=[
            IODef('OnSoundStarted', 'Fired when sound starts'),
            IODef('OnSoundFinished', 'Fired when sound ends'),
        ]
    )
    
    # === PICKUP ===
    register_io('pickup',
        inputs=[
            IODef('Enable', 'Enable pickup'),
            IODef('Disable', 'Disable pickup'),
            IODef('Respawn', 'Force respawn'),
            IODef('SetValue', 'Set pickup value', 'int'),
        ],
        outputs=[
            IODef('OnPickedUp', 'Fired when collected'),
            IODef('OnRespawn', 'Fired when respawned'),
        ]
    )
    
    # === LOGIC_RELAY (replaces basic LogicGate) ===
    register_io('logic_relay',
        inputs=[
            IODef('Trigger', 'Fire the OnTrigger output'),
            IODef('Enable', 'Enable this relay'),
            IODef('Disable', 'Disable this relay'),
            IODef('Toggle', 'Toggle enabled state'),
            IODef('CancelPending', 'Cancel any pending triggers'),
        ],
        outputs=[
            IODef('OnTrigger', 'Fired when triggered'),
        ]
    )
    
    # === LOGIC_TIMER ===
    register_io('logic_timer',
        inputs=[
            IODef('Enable', 'Start the timer'),
            IODef('Disable', 'Stop the timer'),
            IODef('Toggle', 'Toggle timer state'),
            IODef('FireTimer', 'Fire immediately'),
            IODef('SetTime', 'Set interval in seconds', 'float'),
            IODef('ResetTimer', 'Reset to initial time'),
        ],
        outputs=[
            IODef('OnTimer', 'Fired when timer elapses'),
        ]
    )
    
    # === PLAYER START ===
    register_io('playerstart',
        inputs=[],
        outputs=[
            IODef('OnPlayerSpawn', 'Fired when player spawns here'),
        ]
    )
    
    # === MONSTER ===
    register_io('monster',
        inputs=[
            IODef('Enable', 'Enable AI'),
            IODef('Disable', 'Disable AI'),
            IODef('Kill', 'Kill this monster'),
            IODef('SetTarget', 'Set pursuit target', 'string'),
            IODef('Wake', 'Wake from idle'),
        ],
        outputs=[
            IODef('OnDeath', 'Fired when killed'),
            IODef('OnDamaged', 'Fired when taking damage'),
            IODef('OnSeePlayer', 'Fired when player spotted'),
            IODef('OnLostPlayer', 'Fired when player lost'),
        ]
    )
    
    # === BRUSH (generic solid) ===
    register_io('brush',
        inputs=[
            IODef('Enable', 'Enable (make solid)'),
            IODef('Disable', 'Disable (make non-solid)'),
            IODef('Toggle', 'Toggle solid state'),
            IODef('Kill', 'Remove from world'),
        ],
        outputs=[]
    )
    
    # === LOGIC_GATE (legacy, now more like multi-input relay) ===
    register_io('logic_gate',
        inputs=[
            IODef('Trigger', 'Send input signal'),
            IODef('Reset', 'Reset all input states'),
            IODef('Enable', 'Enable gate'),
            IODef('Disable', 'Disable gate'),
        ],
        outputs=[
            IODef('OnTrigger', 'Fired when gate condition is met'),
        ]
    )
    
    # === MODEL ===
    register_io('model',
        inputs=[
            IODef('Enable', 'Show model'),
            IODef('Disable', 'Hide model'),
            IODef('SetSkin', 'Set model skin', 'int'),
            IODef('SetAnimation', 'Play animation', 'string'),
        ],
        outputs=[]
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def add_connection(entity, connection: OutputConnection):
    """Add an output connection to an entity."""
    if isinstance(entity, dict):
        if '_io_connections' not in entity:
            entity['_io_connections'] = []
        entity['_io_connections'].append(connection)
    elif hasattr(entity, 'properties'):
        if '_io_connections' not in entity.properties:
            entity.properties['_io_connections'] = []
        entity.properties['_io_connections'].append(connection)


def remove_connection(entity, connection: OutputConnection):
    """Remove an output connection from an entity."""
    connections = get_connections(entity)
    if connection in connections:
        connections.remove(connection)


def get_connections(entity) -> List[OutputConnection]:
    """Get all output connections from an entity."""
    if isinstance(entity, dict):
        return entity.get('_io_connections', [])
    elif hasattr(entity, 'properties'):
        return entity.properties.get('_io_connections', [])
    return []


def set_connections(entity, connections: List[OutputConnection]):
    """Set all output connections on an entity."""
    if isinstance(entity, dict):
        entity['_io_connections'] = connections
    elif hasattr(entity, 'properties'):
        entity.properties['_io_connections'] = connections


def clear_connections(entity):
    """Clear all output connections from an entity."""
    set_connections(entity, [])


def get_entity_type_for_io(entity) -> str:
    """Get the entity type string used for I/O lookups."""
    if isinstance(entity, dict):
        if entity.get('is_trigger'):
            return 'trigger'
        elif entity.get('is_door'):
            return 'door'
        elif entity.get('is_mover'):
            return 'mover'
        return 'brush'
    elif hasattr(entity, 'properties'):
        return entity.properties.get('type', 'thing')
    return 'unknown'


def serialize_connections(entity) -> List[dict]:
    """Serialize entity's connections to list of dicts for saving."""
    return [conn.to_dict() for conn in get_connections(entity)]


def deserialize_connections(entity, data: List[dict]):
    """Deserialize and set connections from saved data."""
    connections = [OutputConnection.from_dict(d) for d in data]
    set_connections(entity, connections)


def reset_all_connections(entities):
    """Reset all connection states (fire_once tracking) for new play session."""
    for entity in entities:
        for conn in get_connections(entity):
            conn.reset()


# Initialize default I/O definitions when module is imported
register_default_io()