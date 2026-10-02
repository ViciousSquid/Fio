"""
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
    
    target_id is the stable UUID of the target entity. The runtime prefers
    target_id for lookup and falls back to target_name for legacy maps.
    """
    output_name: str          # Which output triggers this connection
    target_name: str          # Name of target entity (display / legacy fallback)
    input_name: str           # Which input to call on target
    parameter: str = ""       # Optional parameter to pass
    delay: float = 0.0        # Delay in seconds before firing
    fire_once: bool = False   # If True, connection is removed after firing
    target_id: str = ""       # Stable UUID of target entity
    _fired: bool = field(default=False, repr=False)  # Internal tracking
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for saving."""
        d = {
            'output': self.output_name,
            'target': self.target_name,
            'input': self.input_name,
            'parameter': self.parameter,
            'delay': self.delay,
            'fire_once': self.fire_once
        }
        if self.target_id:
            d['target_id'] = self.target_id
        return d
    
    @staticmethod
    def from_dict(data: dict) -> 'OutputConnection':
        """Deserialize from dictionary."""
        return OutputConnection(
            output_name=data.get('output', ''),
            target_name=data.get('target', ''),
            input_name=data.get('input', ''),
            parameter=data.get('parameter', ''),
            delay=float(data.get('delay', 0.0)),
            fire_once=bool(data.get('fire_once', False)),
            target_id=data.get('target_id', '')
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
    target_id: str = ""       # Stable UUID of target entity


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
        self._find_entity_by_id: Optional[Callable] = None
        
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

    def set_entity_finder_by_id(self, finder: Callable):
        """
        Set the function used to find entities by stable ID.
        finder(entity_id: str) -> entity or None
        """
        self._find_entity_by_id = finder
    
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
    
    def fire_output(self, source_entity, output_name: str, value: str = None):
        """
        Fire an output from an entity (thing), triggering all connected inputs.

        If 'value' is given, it is passed to any connection whose editor-authored
        parameter is blank (Source-engine style parameter pass-through). A
        connection with an explicit parameter always keeps its own parameter.
        """
        connections = self._get_connections(source_entity)
        source_name = self._get_entity_name(source_entity)

        matching_count = 0
        for conn in connections:
            if conn.output_name.lower() != output_name.lower():
                continue
            
            matching_count += 1
            
            if conn.fire_once and conn._fired:
                io_log(f"{source_name}.{output_name} -> {conn.target_name}.{conn.input_name} SKIPPED (fire_once)")
                continue
            
            conn._fired = True
            
            # Parameter pass-through: blank editor parameter inherits the
            # dynamic value fired with this output (if any).
            effective_param = conn.parameter
            if not effective_param and value is not None:
                effective_param = value
            
            delay_str = f" (delay {conn.delay}s)" if conn.delay > 0 else ""
            io_log(f"{source_name}.{output_name} -> {conn.target_name}.{conn.input_name}{delay_str}")
            
            if conn.delay > 0:
                event = PendingEvent(
                    fire_time=self.current_time + conn.delay,
                    target_name=conn.target_name,
                    input_name=conn.input_name,
                    parameter=effective_param,
                    source_name=source_name,
                    connection=conn,
                    target_id=conn.target_id
                )
                self.pending_events.append(event)
            else:
                self._execute_input(conn.target_name, conn.input_name, 
                                effective_param, source_name,
                                target_id=conn.target_id)
        
        if matching_count == 0:
            io_log(f"{source_name}.{output_name} (no connections)")
    
    def update(self, delta: float):
        self.current_time += delta
        
        still_pending = []
        for event in self.pending_events:
            if self.current_time >= event.fire_time:
                io_log(f"[Delayed] {event.target_name}.{event.input_name} (from {event.source_name})")
                self._execute_input(event.target_name, event.input_name,
                                event.parameter, event.source_name,
                                target_id=event.target_id)
            else:
                still_pending.append(event)
        
        self.pending_events = still_pending
    
    def _execute_input(self, target_name: str, input_name: str, 
                   parameter: str, source_name: str, target_id: str = ""):
        """Execute an input on a target entity.  Prefers ID lookup, falls back to name."""
        if not self._find_entity:
            debug_log("Error", "I/O: No entity finder set!")
            return
        
        target = None
        # Prefer stable-ID lookup when available
        if target_id and self._find_entity_by_id:
            target = self._find_entity_by_id(target_id)
        # Fallback to name lookup (legacy maps or missing ID)
        if target is None:
            target = self._find_entity(target_name)
        if target is None:
            debug_log("Error", f"I/O: Target '{target_name}' (id={target_id}) not found!")
            return
        
        entity_type = self._get_entity_type(target)
        
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

        # ---- Generic Hide / Show / ToggleVisibility --------------------------
        elif input_lower == 'hide':
            if isinstance(entity, dict):
                entity['hidden'] = True
            elif hasattr(entity, 'properties'):
                entity.properties['hidden'] = True

        elif input_lower == 'show':
            if isinstance(entity, dict):
                entity['hidden'] = False
            elif hasattr(entity, 'properties'):
                entity.properties['hidden'] = False

        elif input_lower == 'togglevisibility':
            if isinstance(entity, dict):
                entity['hidden'] = not entity.get('hidden', False)
            elif hasattr(entity, 'properties'):
                entity.properties['hidden'] = not entity.properties.get('hidden', False)

        # ---- Generic SetTint / ClearTint ------------------------------------
        elif input_lower == 'settint':
            self._apply_tint(entity, parameter)

        elif input_lower == 'cleartint':
            if isinstance(entity, dict):
                entity.pop('tint', None)
            elif hasattr(entity, 'properties'):
                entity.properties.pop('tint', None)

    # ------------------------------------------------------------------
    @staticmethod
    def _apply_tint(entity, parameter: str):
        """Parse 'R G B' (0-255) and store as tint list."""
        try:
            parts = parameter.split()
            if len(parts) >= 3:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                tint = [max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))]
            else:
                return
        except (ValueError, IndexError):
            return
        if isinstance(entity, dict):
            entity['tint'] = tint
        elif hasattr(entity, 'properties'):
            entity.properties['tint'] = tint
    # ------------------------------------------------------------------
    
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


    def query_keyvalue(self, store_name: str, key: str, default: str = "<missing>") -> str:
        """
        Query a value from a LogicKeyValueStore by store_name.
        This is a convenience method for other systems (not I/O handlers)
        to read persistent state without going through the entity system.
        """
        # First try to find the actual entity
        if self._find_entity:
            entity = self._find_entity(store_name)
            if entity and hasattr(entity, 'get_value'):
                return entity.get_value(key, default)

        # Fallback to the class-level persistent registry
        try:
            from editor.things import LogicKeyValueStore
            if store_name in LogicKeyValueStore._persistent_registry:
                return LogicKeyValueStore._persistent_registry[store_name].get(key, default)
        except ImportError:
            pass

        return default

    def set_keyvalue(self, store_name: str, key: str, value: str) -> bool:
        """
        Set a value in a LogicKeyValueStore by store_name.
        Returns True on success, False if store is full or not found.
        """
        if self._find_entity:
            entity = self._find_entity(store_name)
            if entity and hasattr(entity, 'set_value'):
                return entity.set_value(key, value)
        return False


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
            IODef('Teleport', 'Teleport the touching player to target_node'),
            IODef('SetTargetNode', 'Change the target PathNode name', 'string'),
            IODef('Hide', 'Hide this trigger'),
            IODef('Show', 'Show this trigger'),
            IODef('ToggleVisibility', 'Toggle visibility'),
            IODef('SetTint', 'Set tint colour (R G B, 0-255)', 'color'),
            IODef('ClearTint', 'Remove tint override'),
        ],
        outputs=[
            IODef('OnTrigger', 'Fired when activated'),
            IODef('OnStartTouch', 'Fired when player enters'),
            IODef('OnEndTouch', 'Fired when player exits'),
            IODef('OnTeleport', 'Fired after a player is teleported'),
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
            IODef('Hide', 'Hide this door'),
            IODef('Show', 'Show this door'),
            IODef('ToggleVisibility', 'Toggle visibility'),
            IODef('SetTint', 'Set tint colour (R G B, 0-255)', 'color'),
            IODef('ClearTint', 'Remove tint override'),
        ],
        outputs=[
            IODef('OnOpen', 'Fired when door starts opening'),
            IODef('OnClose', 'Fired when door starts closing'),
            IODef('OnFullyOpen', 'Fired when door is fully open'),
            IODef('OnFullyClosed', 'Fired when door is fully closed'),
            IODef('OnLockedUse', 'Fired when player tries locked door'),
        ]
    )
    
    # === MOVER ===
    register_io('mover',
        inputs=[
            IODef('Open', 'Move to end position'),
            IODef('Close', 'Move to start position'),
            IODef('Toggle', 'Toggle movement direction'),
            IODef('SetPosition', 'Set position (0-1)', 'float'),
            IODef('SetSpeed', 'Set movement speed', 'float'),
            IODef('Enable', 'Enable movement'),
            IODef('Disable', 'Disable movement'),
            IODef('FollowPath', 'Start following a PathNode chain (param = node name)', 'string'),
            IODef('StopPath', 'Stop PathNode following and hold position'),
            IODef('SetPathTarget', 'Set PathNode name to follow', 'string'),
            IODef('Hide', 'Hide this mover'),
            IODef('Show', 'Show this mover'),
            IODef('ToggleVisibility', 'Toggle visibility'),
            IODef('SetTint', 'Set tint colour (R G B, 0-255)', 'color'),
            IODef('ClearTint', 'Remove tint override'),
        ],
        outputs=[
            IODef('OnFullyOpen', 'Fired when reaching end position'),
            IODef('OnFullyClosed', 'Fired when reaching start position'),
            IODef('OnPathNodeReached', 'Fired each time mover arrives at a PathNode'),
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
            IODef('EnableShadows', 'Start casting depth cube-map shadows'),
            IODef('DisableShadows', 'Stop casting shadows'),
            IODef('ToggleShadows', 'Toggle shadow casting on/off'),
            IODef('FadeIn', 'Fade in over time', 'float'),
            IODef('FadeOut', 'Fade out over time', 'float'),
            IODef('Hide', 'Hide this light entity'),
            IODef('Show', 'Show this light entity'),
            IODef('ToggleVisibility', 'Toggle visibility'),
        ],
        outputs=[
            IODef('OnTurnedOn', 'Fired when light turns on'),
            IODef('OnTurnedOff', 'Fired when light turns off'),
            IODef('OnShadowsEnabled', 'Fired when shadow casting is enabled'),
            IODef('OnShadowsDisabled', 'Fired when shadow casting is disabled'),
        ]
    )
    
    # === SPEAKER ===
    register_io('speaker',
        inputs=[
            IODef('PlaySound', 'Start playing sound'),
            IODef('StopSound', 'Stop playing sound'),
            IODef('Toggle', 'Toggle playback'),
            IODef('SetVolume', 'Set volume (0-1)', 'float'),
            IODef('Hide', 'Hide this speaker entity'),
            IODef('Show', 'Show this speaker entity'),
            IODef('ToggleVisibility', 'Toggle visibility'),
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
            IODef('Hide', 'Hide this pickup'),
            IODef('Show', 'Show this pickup'),
            IODef('ToggleVisibility', 'Toggle visibility'),
        ],
        outputs=[
            IODef('OnPickedUp', 'Fired when collected'),
            IODef('OnRespawn', 'Fired when respawned'),
        ]
    )
    
    # === LOGIC_RELAY ===
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
            IODef('OnPlayerDeath', 'Fired when the player dies'),
        ]
    )
    
    # === MONSTER ===
    register_io('monster',
        inputs=[
            IODef('Enable',    'Enable AI'),
            IODef('Disable',   'Disable AI'),
            IODef('Kill',      'Kill this monster'),
            IODef('SetTarget', 'Set pursuit target (entity name, blank = player)', 'string'),
            IODef('Wake',      'Wake from dormant state'),
            IODef('Hide',      'Hide this monster'),
            IODef('Show',      'Show this monster'),
            IODef('ToggleVisibility', 'Toggle visibility'),
        ],
        outputs=[
            IODef('OnDeath',      'Fired when killed'),
            IODef('OnDamaged',    'Fired when taking damage'),
            IODef('OnSeePlayer',  'Fired on first sight of the player'),
            IODef('OnLostPlayer', 'Fired when player leaves sight range'),
            IODef('OnAttack',     'Fired each time the monster attacks'),
        ]
    )
    
    # === BRUSH (generic solid) ===
    register_io('brush',
        inputs=[
            IODef('Enable', 'Enable (make solid)'),
            IODef('Disable', 'Disable (make non-solid)'),
            IODef('Toggle', 'Toggle solid state'),
            IODef('Kill', 'Remove from world'),
            IODef('Hide', 'Hide this brush'),
            IODef('Show', 'Show this brush'),
            IODef('ToggleVisibility', 'Toggle visibility'),
            IODef('SetTint', 'Set tint colour (R G B, 0-255)', 'color'),
            IODef('ClearTint', 'Remove tint override'),
        ],
        outputs=[]
    )
    
    # === LOGIC_GATE ===
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
            IODef('Hide', 'Hide this model'),
            IODef('Show', 'Show this model'),
            IODef('ToggleVisibility', 'Toggle visibility'),
        ],
        outputs=[]
    )

    # === LEVEL CHANGER ===
    register_io('levelchanger',
        inputs=[
            IODef('Trigger', 'Trigger level change'),
            IODef('ChangeLevel', 'Change to the target map (optional parameter overrides map name)'),
        ],
        outputs=[
            IODef('OnUse', 'Fired when the player uses this level changer'),
        ]
    )

    # === PATH NODE ===
    # Navigation waypoint for monster patrol. Can be enabled/disabled so
    # designers can dynamically re-route patrols from a trigger/relay.
    register_io('path_node',
        inputs=[
            IODef('Enable',  'Allow monsters to patrol to this node'),
            IODef('Disable', 'Prevent monsters from patrolling to this node'),
            IODef('Toggle',  'Toggle whether this node accepts patrolling monsters'),
        ],
        outputs=[
            IODef('OnMonsterArrived', 'Fires when a patrolling monster enters this node\'s radius'),
            IODef('OnMonsterLeft',    'Fires when a patrolling monster leaves this node\'s radius'),
            IODef('OnWaitStart',      'Fires when a monster begins waiting at this node'),
            IODef('OnWaitEnd',        'Fires when a monster finishes waiting and advances to next node'),
        ]
    )

    # === LOGIC CAMERA ===
    # Cinematic camera that lerps along a PathNode chain.
    register_io('logic_camera',
        inputs=[
            IODef('Start',    'Begin the cinematic camera sequence'),
            IODef('Stop',     'Abort and return camera to the player'),
            IODef('Pause',    'Freeze camera at current chain position'),
            IODef('Resume',   'Continue a paused sequence'),
            IODef('SetSpeed', 'Override travel speed', 'float'),
        ],
        outputs=[
            IODef('OnStart',       'Fired when sequence begins'),
            IODef('OnReachNode',   'Fired each time the camera arrives at a PathNode'),
            IODef('OnFinished',    'Fired when the camera reaches the last node'),
        ]
    )

    # === LOGIC COMMAND ===
    # Runs a console command when fired (e.g. a trigger brush -> "cam 2").
    register_io('logic_command',
        inputs=[
            IODef('RunCommand', 'Run a console command (param: the command line, '
                                'e.g. "cam 2"); blank uses the command property', 'string'),
            IODef('SetCommand', 'Set the default command string', 'string'),
            IODef('Enable',     'Allow this entity to run commands'),
            IODef('Disable',    'Prevent this entity from running commands'),
        ],
        outputs=[
            IODef('OnCommand', 'Fired after a command is queued (param: the command line)'),
        ]
    )

    # === PORTAL ===
    # Prey 2006-style portal that links two named portal entities.
    register_io('portal',
        inputs=[
            IODef('Enable',      'Activate the portal (renders and teleports)'),
            IODef('Disable',     'Deactivate the portal'),
            IODef('Toggle',      'Toggle active state'),
            IODef('SetColor',    'Set rim/glow color (R G B, 0-255)', 'color'),
            IODef('SetTarget',   'Change the paired portal target name', 'string'),
            IODef('ShowRim',     'Show the rim glow border'),
            IODef('HideRim',     'Hide the rim glow border'),
            IODef('SetWidth',    'Set portal width in units', 'float'),
            IODef('SetHeight',   'Set portal height in units', 'float'),
        ],
        outputs=[
            IODef('OnEnabled',    'Fired when portal is activated'),
            IODef('OnDisabled',   'Fired when portal is deactivated'),
            IODef('OnToggled',    'Fired when portal is toggled'),
            IODef('OnTeleport',   'Fired when an entity passes through'),
            IODef('OnPlayerEnter','Fired once per transit when the player passes through'),
        ]
    )

    # === LOGIC SPAWNER ===
    # Instantiates entities at a PathNode when triggered.
    register_io('logic_spawner',
        inputs=[
            IODef('Spawn',         'Spawn one entity at the target PathNode'),
            IODef('Enable',        'Allow spawning'),
            IODef('Disable',       'Prevent spawning'),
            IODef('SetTargetNode', 'Change spawn location to a different PathNode', 'string'),
        ],
        outputs=[
            IODef('OnSpawn',       'Fired each time an entity is spawned'),
            IODef('OnMaxReached',  'Fired when max_spawn limit is hit'),
        ]
    )

    # === LOGIC KEYVALUE STORE ===
    # Persistent key/value store that survives level transitions.
    register_io('logic_keyvalue',
        inputs=[
            IODef('SetValue',      'Set a key/value pair (param: "key=value")', 'string'),
            IODef('GetValue',      'Read a key and fire OnValueRead (param: key name)', 'string'),
            IODef('ClearKey',      'Remove a single key (param: key name)', 'string'),
            IODef('ClearAll',      'Remove all keys'),
            IODef('CopyFrom',      'Copy all keys from another store by name', 'string'),
            IODef('Increment',     'Increment integer value (param: "key,amount")', 'string'),
            IODef('Decrement',     'Decrement integer value (param: "key,amount")', 'string'),
            IODef('TestValue',     'Compare a key against a value (param: "key==val", also != > < >= <=)', 'string'),
        ],
        outputs=[
            IODef('OnValueSet',    'Fired when any key is set (param: "key=value")'),
            IODef('OnValueRead',   'Fired by GetValue (param: value)'),
            IODef('OnKeyCleared',  'Fired when a key is removed (param: key name)'),
            IODef('OnStoreFull',   'Fired when trying to add beyond 25 keys'),
            IODef('OnKeyNotFound', 'Fired when GetValue/TestValue targets a missing key'),
            IODef('OnCompareTrue', 'Fired when TestValue comparison passes (param: actual value)'),
            IODef('OnCompareFalse','Fired when TestValue comparison fails (param: actual value)'),
        ]
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
        # Check for Portal type first
        etype = entity.properties.get('type', 'thing')
        if etype == 'portal':
            return 'portal'
        return etype
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
