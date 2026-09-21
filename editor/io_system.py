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


# The authored-flag helpers.  ``hidden`` and ``disabled`` are the two flags a
# streaming layer parks, and the engine is where the question "what is this
# object's *authored* value?" is answered — see `engine.spatial`.  Imported
# defensively so the editor still runs if the engine package is unavailable, in
# which case the flags behave exactly as they did before streaming existed.
def _property_dict(obj):
    """The mutable property mapping of a brush dict or a Thing, or None."""
    if isinstance(obj, dict):
        return obj
    props = getattr(obj, 'properties', None)
    return props if isinstance(props, dict) else None


def _plain_authored_flag(obj, flag, default=False):
    props = _property_dict(obj)
    return bool(props.get(flag, default)) if props is not None else default

def io_enabled(obj):
    """Whether this entity participates in the runtime I/O graph."""
    props = _property_dict(obj)
    return bool(props.get('io_enabled', True)) if props is not None else True


def _plain_set_authored_flag(obj, flag, value):
    props = _property_dict(obj)
    if props is None:
        return False
    props[flag] = bool(value)
    return True


try:
    from engine.spatial import authored_flag, set_authored_flag
except Exception:  # pragma: no cover - engine-less editor/tooling contexts
    authored_flag = _plain_authored_flag
    set_authored_flag = _plain_set_authored_flag


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
#
# This is the *declaration*: what the editor offers a designer. The
# *implementation* lives elsewhere — in an IOManager's handler table, which is
# per-session and which plugins contribute to at runtime. The two cannot be one
# structure, because they have different lifetimes: definitions are registered
# at import and are global, handlers are registered against a live session.
#
# Two structures means they can drift, and a declaration that has gone stale is
# invisible: the editor offers the input, a designer wires it, and nothing runs.
# `audit_io_coverage` below is the reconciliation — it is what the conformance
# tests assert on, and what a plugin author can call to check their own entity.
IO_REGISTRY: Dict[str, Dict[str, List[IODef]]] = {}

#: Lowercased declared output names per type, for the emission check in
#: :meth:`IOManager.fire_output`. Memoised because that check runs per event;
#: invalidated by :func:`register_io`, the only thing that changes the answer.
_declared_outputs_cache: Dict[str, Set[str]] = {}


def register_io(entity_type: str, inputs: List[IODef], outputs: List[IODef]):
    """Register I/O definitions for an entity type."""
    IO_REGISTRY[entity_type] = {
        'inputs': inputs,
        'outputs': outputs
    }
    _declared_outputs_cache.pop(entity_type, None)


#: Declared I/O that deliberately has no runtime implementation, as
#: ``(entity_type, io_name) -> why``.
#:
#: There is no good reason to add to this lightly.  A declared input that runs
#: nothing, or a declared output that nothing fires, is a promise the editor
#: makes to a designer and the runtime does not keep — the designer wires it,
#: nothing happens, and there is no error anywhere.  An entry here is a claim
#: that the entry is *meant* to be inert (an editor-only affordance, a slot a
#: game layer is expected to fill), and the conformance tests read this table
#: rather than a list of their own, so the exception is stated once, here,
#: where anyone reading the declaration will see it.
ABSTRACT_IO: Dict[tuple, str] = {}


def register_io_alias(alias: str, entity_type: str):
    """Make *alias* resolve to the same I/O definitions as *entity_type*.

    Used when an entity is renamed: the old type token keeps answering, so a
    map saved by an older Fio still shows its inputs and outputs in the editor
    instead of an empty list.  The two names share one definition object, so
    they cannot drift apart.
    """
    if entity_type in IO_REGISTRY:
        IO_REGISTRY[alias] = IO_REGISTRY[entity_type]


def is_registered_type(entity_type: str) -> bool:
    """Whether Fio has I/O definitions for this entity type at all.

    The question :func:`get_inputs` cannot answer: it returns an empty list both
    for a type that declares no inputs (``playerstart``) and for one Fio has
    never heard of (a plugin entity that registered none, a type from a newer
    map).  Those are opposites — the first means "any input here is wrong", the
    second means "no opinion" — and validation needs to tell them apart.
    """
    return entity_type in IO_REGISTRY


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
    source_id: str = ""       # Stable UUID of the entity that fired this
    activator_id: str = ""    # Stable UUID of the entity that began the chain


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

        # The entity whose output is being delivered right now, and its UUID.
        # Set for the duration of one input call and cleared afterwards, so a
        # handler that needs to know *who* fired it (object-local state, most
        # of all) can ask without every handler signature having to carry it.
        # This is not a queue and not a bus: it is the argument the dispatcher
        # already has, made readable by the handler it is dispatching to.
        self._source_entity = None
        self._source_id: str = ""

        # The entity that *began* the chain this delivery belongs to, as
        # distinct from the one immediately firing it.  A trigger brush the
        # player walks into fires a relay, which fires a state write: the source
        # of that last hop is the relay, but the thing the chain is *about* is
        # still the trigger.  Carried forward across every hop so a handler at
        # the far end can still tell what set it off; a chain that starts with no
        # activator simply has none.
        self._activator_entity = None
        self._activator_id: str = ""
    
    def set_logic_thread(self, logic_thread):
        """Set reference to logic thread."""
        self._logic_thread = logic_thread

    def set_game_state(self, game_state):
        """Set reference to game state for sound queue access."""
        self._game_state = game_state
    
    def get_game_state(self):
        """Get the game state reference."""
        return self._game_state

    def current_source(self):
        """The entity whose output is being delivered, or None.

        Only meaningful inside an input handler.  Outside one it is None,
        which is what a handler invoked from a console command or a test
        should see.
        """
        return self._source_entity

    def current_source_id(self) -> str:
        """The stable UUID of :meth:`current_source`, or ``""``."""
        return self._source_id

    def current_activator(self):
        """The entity that began this chain, or None.

        Differs from :meth:`current_source` only once a chain is more than one
        hop long, which is exactly when it matters.
        """
        return self._activator_entity

    def current_activator_id(self) -> str:
        """The stable UUID of :meth:`current_activator`, or ``""``."""
        return self._activator_id
    
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
        self._source_entity = None
        self._source_id = ""
        self._activator_entity = None
        self._activator_id = ""
    
    def fire_output(
        self,
        source_entity,
        output_name: str,
        value: str = None,
        activator_entity=None,
    ):
        """
        Fire an output from an entity (thing), triggering all connected inputs.

        If 'value' is given, it is passed to any connection whose editor-authored
        parameter is blank (Source-engine style parameter pass-through). A
        connection with an explicit parameter always keeps its own parameter.
        """
        if not io_enabled(source_entity):
            return

        connections = self._get_connections(source_entity)
        source_name = self._get_entity_name(source_entity)
        source_id = self._get_entity_id(source_entity)
        # An explicit activator starts a new chain context at this output. This
        # preserves the actual entity that touched a trigger through all I/O hops.
        explicit_activator_id = (
            self._get_entity_id(activator_entity)
            if activator_entity is not None else ""
        )
        activator_id = explicit_activator_id or self._activator_id or source_id

        # The mirror of the stale-declaration problem: an output the code fires
        # but no type declares is undiscoverable — it works perfectly for anyone
        # who knows the name and does not exist for anyone reading the editor's
        # dropdown.  Checked under the existing debug gate, so it costs a memoised
        # set lookup on a path that is already logging, and nothing at all when
        # I/O debugging is off.
        if IO_DEBUG_ENABLED:
            source_type = self._get_entity_type(source_entity)
            if (is_registered_type(source_type)
                    and output_name.lower() not in declared_outputs(source_type)):
                debug_log("Error",
                          "I/O: %s fired '%s', which %s entities do not declare — "
                          "no designer can wire it"
                          % (source_name or '<unnamed>', output_name, source_type))

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
                    target_id=conn.target_id,
                    source_id=source_id,
                    activator_id=activator_id,
                )
                self.pending_events.append(event)
            else:
                self._execute_input(conn.target_name, conn.input_name, 
                                effective_param, source_name,
                                target_id=conn.target_id,
                                source_entity=source_entity,
                                source_id=source_id,
                                activator_id=activator_id)
        
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
                                target_id=event.target_id,
                                source_id=event.source_id,
                                activator_id=event.activator_id)
            else:
                still_pending.append(event)
        
        self.pending_events = still_pending
    
    def _execute_input(self, target_name: str, input_name: str, 
                   parameter: str, source_name: str, target_id: str = "",
                   source_entity=None, source_id: str = "",
                   activator_id: str = ""):
        """Execute an input on a target entity.  Prefers ID lookup, falls back to name.

        Targets are resolved *here*, when the event fires, never when it was
        queued.  That is what makes a delayed event and a streamed world agree:
        the entity a delayed event names may have been parked by Big World in
        the meantime, and it is still the same object with the same UUID, so it
        still receives the input and its state still changes.  See
        :meth:`_try_generic_input` for why that change then survives.
        """
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

        if not io_enabled(target):
            return
        
        entity_type = self._get_entity_type(target)
        
        handler_key = (entity_type.lower(), input_name.lower())
        handler = self._input_handlers.get(handler_key)

        # A delayed event outlives the call that queued it, so the source object
        # may be gone by now; the id it was queued with still identifies it, and
        # the live object is looked up only if one is still there.
        if source_entity is None and source_id and self._find_entity_by_id:
            source_entity = self._find_entity_by_id(source_id)
        previous = (self._source_entity, self._source_id,
                    self._activator_entity, self._activator_id)
        self._source_entity = source_entity
        self._source_id = source_id or self._get_entity_id(source_entity)
        self._activator_id = activator_id or self._source_id
        self._activator_entity = source_entity
        if (self._activator_id and self._activator_id != self._source_id
                and self._find_entity_by_id):
            self._activator_entity = self._find_entity_by_id(self._activator_id)

        try:
            if handler:
                try:
                    handler(target, parameter, self._logic_thread)
                except Exception as e:
                    debug_log("Error", f"{entity_type}.{input_name} handler failed: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                self._try_generic_input(target, input_name, parameter)
        finally:
            (self._source_entity, self._source_id,
             self._activator_entity, self._activator_id) = previous
    
    #: Inputs every entity accepts, whatever its type, because
    #: :meth:`_try_generic_input` implements them for anything with properties.
    #: They are not in any type's declared list, so validation has to know about
    #: them separately — and it reads them from here, so the two cannot drift.
    GENERIC_INPUTS = frozenset({
        'enable', 'disable', 'kill',
        'hide', 'show', 'togglevisibility',
        'settint', 'cleartint',
    })

    def _try_generic_input(self, entity, input_name: str, parameter: str):
        """Try generic input handling for common patterns.

        ``disabled`` and ``hidden`` are written through
        :func:`engine.spatial.set_authored_flag` rather than straight onto the
        object.  Those two flags are the pair a streaming layer parks: Big World
        stashes the authored value, forces both on, and restores the stash when
        the cell comes back.  A plain write lands on the *parked* value and is
        thrown away at the next unpark, so an event delivered to a dormant
        entity would silently do nothing — the one way an event-driven world can
        lose a change without anything reporting an error.  Writing through the
        helper updates whichever of the two the object is actually using, so the
        event lands whether the entity is live or dormant.
        """
        input_lower = input_name.lower()
        
        # Generic enable/disable
        if input_lower == 'enable':
            set_authored_flag(entity, 'disabled', False)

        elif input_lower == 'disable':
            set_authored_flag(entity, 'disabled', True)

        elif input_lower == 'kill':
            # Mark for removal (handled by logic thread)
            if isinstance(entity, dict):
                entity['_kill'] = True
            elif hasattr(entity, 'properties'):
                entity.properties['_kill'] = True

        # ---- Generic Hide / Show / ToggleVisibility --------------------------
        elif input_lower == 'hide':
            set_authored_flag(entity, 'hidden', True)

        elif input_lower == 'show':
            set_authored_flag(entity, 'hidden', False)

        elif input_lower == 'togglevisibility':
            set_authored_flag(entity, 'hidden', not authored_flag(entity, 'hidden'))

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
    
    def _get_entity_id(self, entity) -> str:
        """The stable UUID of an entity, or ``""`` when it has none."""
        if entity is None:
            return ""
        if isinstance(entity, dict):
            return entity.get('id', '') or ""
        props = getattr(entity, 'properties', None)
        if isinstance(props, dict):
            return props.get('id', '') or ""
        return ""

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


    def query_keyvalue(self, store_name: str, key: str, default: str = "<missing>"):
        """Read one value out of a :class:`~editor.things.LogicState` store.

        A convenience for systems that are not I/O handlers and just want to
        look at persistent state.  The entity is preferred when one exists in
        the scene, so a store whose live values differ from the registry is
        read correctly; otherwise the registry answers, which is what a store
        belonging to a level that is not loaded needs.

        The value comes back with its type, so a stored integer is an integer.
        """
        # First try to find the actual entity
        if self._find_entity:
            entity = self._find_entity(store_name)
            if entity is not None and hasattr(entity, 'get_value'):
                return entity.get_value(key, default)

        # Fallback to the class-level persistent registry
        try:
            from editor.things import LogicState
            store = LogicState._persistent_registry.get(store_name)
            if store is not None:
                return store.get(key, default)
        except ImportError:
            pass

        return default

    def set_keyvalue(self, store_name: str, key: str, value) -> bool:
        """Write one value into a :class:`~editor.things.LogicState` store.

        True on success, False when no such store is in the scene or the write
        was refused (a blank key, or a full store).
        """
        if self._find_entity:
            entity = self._find_entity(store_name)
            if entity is not None and hasattr(entity, 'set_value'):
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
            IODef('Stop', 'Halt the door where it is'),
            IODef('Reverse', 'Send the door back the way it came'),
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
            IODef('Stop', 'Halt the mover where it is'),
            IODef('Reverse', 'Reverse the direction of travel'),
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
            IODef('Reset', 'Re-arm a fire_once relay so it can fire again'),
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
            IODef('Start', 'Start the timer (same as Enable)'),
            IODef('Stop', 'Stop the timer (same as Disable)'),
            IODef('SetTime', 'Set interval in seconds', 'float'),
            IODef('ResetTimer', 'Reset to initial time'),
        ],
        outputs=[
            IODef('OnTimer', 'Fired when timer elapses'),
            IODef('OnFinished', 'Fired when a one-shot timer stops itself'),
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
            IODef('Sleep',     'Return to dormant state'),
            IODef('SetHealth', 'Set current health', 'int'),
            IODef('Respawn',   'Revive at full health (param overrides the health)', 'int'),
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
            IODef('OnRespawn',    'Fired when revived by the Respawn input'),
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
            IODef('Trigger',     'Assert one input signal (param: its name; '
                                 'blank means "anonymous")', 'string'),
            IODef('ClearInput',  'De-assert one input signal (param: its name)', 'string'),
            IODef('ToggleInput', 'Flip one input signal between asserted and not', 'string'),
            IODef('Reset',       'De-assert every input signal'),
            IODef('Evaluate',    'Re-test the gate without changing any input'),
            IODef('Enable',      'Enable gate'),
            IODef('Disable',     'Disable gate'),
            IODef('Toggle',      'Toggle enabled state'),
        ],
        outputs=[
            IODef('OnTrigger', 'Fired when the gate condition is met'),
            IODef('OnFalse',   'Fired when the gate is evaluated and the condition fails'),
        ]
    )
    
    # === PROP ===
    register_io('prop',
        inputs=[
            IODef('Enable', 'Make this prop pickable'),
            IODef('Disable', 'Make this prop unavailable'),
            IODef('Drop', 'Release this prop if it is being carried'),
            IODef('Wake', 'Resume physics simulation'),
        ],
        outputs=[
            IODef('OnPickedUp', 'Fired when the player picks up this prop'),
            IODef('OnDropped', 'Fired when the player drops this prop'),
            IODef('OnRest', 'Fired when this prop comes to rest'),
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
            IODef('Trigger',    'Run a console command (alias of RunCommand, so a '
                                'generic trigger chain can drive one)', 'string'),
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

    # === LOGIC STATE ===
    # Fio's persistent state primitive.  Every operation is parameterised
    # ("Increment  killed,1") rather than given its own input, so the set stays
    # small and a map can express something the engine was never told about.
    register_io('logic_state',
        inputs=[
            IODef('SetValue',      'Set a value (param: "key=value", or "key:type=value" '
                                   'to state the type outright)', 'string'),
            IODef('GetValue',      'Read a key and fire OnValueRead (param: key name)', 'string'),
            IODef('ClearKey',      'Remove a single key (param: key name)', 'string'),
            IODef('ClearAll',      'Remove all keys'),
            IODef('CopyFrom',      'Copy all keys from another store by name', 'string'),
            IODef('CopyValue',     'Copy one key to another (param: "from,to" or '
                                   '"store.from,to")', 'string'),
            IODef('Increment',     'Add to a numeric value (param: "key,amount"; '
                                   'amount defaults to 1)', 'string'),
            IODef('Decrement',     'Subtract from a numeric value (param: "key,amount")', 'string'),
            IODef('Add',           'Add to a numeric value (param: "key,amount")', 'string'),
            IODef('Subtract',      'Subtract from a numeric value (param: "key,amount")', 'string'),
            IODef('Multiply',      'Multiply a numeric value (param: "key,factor")', 'string'),
            IODef('Divide',        'Divide a numeric value (param: "key,divisor")', 'string'),
            IODef('Min',           'Keep the smaller of the value and the operand '
                                   '(param: "key,value")', 'string'),
            IODef('Max',           'Keep the larger of the value and the operand '
                                   '(param: "key,value")', 'string'),
            IODef('Clamp',         'Confine a value to a range (param: "key,low,high")', 'string'),
            IODef('Toggle',        'Invert a value as a boolean (param: key name)', 'string'),
            IODef('Compare',       'Test a key and fire OnTrue/OnFalse '
                                   '(param: "key>=value"; also == != > < <=)', 'string'),
            IODef('Exists',        'Fire OnTrue if a key is present, OnFalse if not '
                                   '(param: key name)', 'string'),
            IODef('Missing',       'Fire OnTrue if a key is absent, OnFalse if present '
                                   '(param: key name)', 'string'),
            IODef('TestValue',     'Compare a key against a value (legacy name for '
                                   'Compare)', 'string'),
            IODef('SetObjectValue', "Set a value against the firing entity's UUID "
                                    '(param: "key=value")', 'string'),
            IODef('GetObjectValue', "Read a value held against the firing entity's UUID "
                                    '(param: key name)', 'string'),
            IODef('ClearObjectState', "Forget every value held against the firing "
                                      "entity's UUID"),
        ],
        outputs=[
            IODef('OnValueSet',    'Fired for every accepted write (param: "key=value")'),
            IODef('OnValueChanged','Fired only when a write actually changed the value '
                                   '(param: "key=value")'),
            IODef('OnValueRead',   'Fired by GetValue (param: the value)'),
            IODef('OnValueCleared','Fired when a key is removed (param: key name)'),
            IODef('OnKeyCleared',  'Legacy name for OnValueCleared (param: key name)'),
            IODef('OnStoreFull',   'Fired when a new key would exceed the store capacity'),
            IODef('OnKeyNotFound', 'Fired when a read or comparison targets a missing key'),
            IODef('OnTrue',        'Fired when a Compare/Exists/Missing test passes '
                                   '(param: the value)'),
            IODef('OnFalse',       'Fired when a Compare/Exists/Missing test fails '
                                   '(param: the value)'),
            IODef('OnCompareTrue', 'Legacy name for OnTrue (param: the value)'),
            IODef('OnCompareFalse','Legacy name for OnFalse (param: the value)'),
        ]
    )

    # Pre-2.4 type token for the same entity: a map saved before the rename
    # still resolves its inputs and outputs in the editor.
    register_io_alias('logic_keyvalue', 'logic_state')



# =============================================================================
# REVERSE LOOKUP: who targets whom
# =============================================================================
#
# Answering "what points at this entity?" by walking every brush and entity is
# fine once, but the Property Editor asks it every time a panel is built, which
# on a large map is a scan of the whole scene per selection.  The index below
# answers it from a dict instead, and is rebuilt only when connections actually
# change — tracked by a revision counter every mutator here bumps.

_io_revision = 0
_target_index_cache = None          # (revision, scene key) -> index
_target_index_key = None


def io_revision() -> int:
    """Counter that changes whenever any connection is added or removed.

    Callers cache derived data against it; comparing two integers is cheap
    enough to do on every panel build.
    """
    return _io_revision


def bump_io_revision():
    """Mark every cached connection lookup stale.

    Call after mutating connections through anything other than the helpers
    below — loading a map, an undo, or editing ``_io_connections`` directly.
    """
    global _io_revision
    _io_revision += 1


def _connection_target(conn):
    """``(target_id, target_name)`` of a connection, object or dict alike."""
    if isinstance(conn, dict):
        return conn.get('target_id'), (conn.get('target_name') or conn.get('target'))
    return getattr(conn, 'target_id', None), getattr(conn, 'target_name', None)


def _connection_output(conn):
    if isinstance(conn, dict):
        return conn.get('output') or conn.get('output_name') or '?'
    return getattr(conn, 'output_name', None) or '?'


def _entity_name(entity):
    if isinstance(entity, dict):
        return entity.get('name', 'unnamed')
    return entity.properties.get('name', 'unnamed')


def build_target_index(brushes, things):
    """Map every targeted name and id to the sources pointing at it.

    Returns ``{key: [(source entity, label, connection), ...]}`` where ``key``
    is either a target name or a target id — a connection is filed under both,
    so a lookup finds it whether the entity is addressed by identity or by name.
    Also files the legacy ``brush['target']`` property that predates
    connections, whose entry carries ``None`` for the connection because there
    is not one.

    The connection is carried so a caller can count *connections* rather than
    sources: two wires from one entity into one target are two inputs, and a
    listing keyed by source alone cannot see the difference.

    The *entity* is filed, never its name.  An index that stored the name would
    stop being an index and start being a stale copy of one: renaming a source
    changes nothing about the connections, so nothing invalidates the index, and
    the Property Editor's "Targeted by" list would go on quoting a name no
    entity in the scene answers to.  :func:`find_targeting_sources` reads the
    name off the live object at lookup time instead, which cannot go stale.
    """
    index = {}

    def _add(key, value):
        if key:
            index.setdefault(key, []).append(value)

    for brush in brushes:
        legacy = brush.get('target')
        if legacy:
            src_type = ('trigger' if brush.get('is_trigger')
                        else 'mover' if brush.get('is_mover') else None)
            if src_type:
                _add(legacy, (brush, src_type, None))
        for conn in brush.get('_io_connections', []) or []:
            tid, tname = _connection_target(conn)
            entry = (brush, "I/O: %s" % _connection_output(conn), conn)
            _add(tid, entry)
            if tname and tname != tid:
                _add(tname, entry)

    for thing in things:
        for conn in thing.properties.get('_io_connections', []) or []:
            tid, tname = _connection_target(conn)
            entry = (thing, "I/O: %s" % _connection_output(conn), conn)
            _add(tid, entry)
            if tname and tname != tid:
                _add(tname, entry)

    return index


def target_index(brushes, things):
    """Cached :func:`build_target_index`, rebuilt when connections change.

    The cache key is the revision counter plus the scene's object counts, so a
    map load or an undo that swaps the lists wholesale is picked up even though
    it never went through the mutators — those bump the revision as well (see
    ``EditorState._invalidate_entity_caches``), which is what makes the key
    safe: object counts and list identities can repeat, revisions cannot.
    """
    global _target_index_cache, _target_index_key
    key = (_io_revision, len(brushes), len(things), id(brushes), id(things))
    if _target_index_cache is not None and _target_index_key == key:
        return _target_index_cache
    _target_index_cache = build_target_index(brushes, things)
    _target_index_key = key
    return _target_index_cache


def find_targeting_sources(brushes, things, target_name="", target_id=""):
    """Sources pointing at one entity, as ``[(source name, label), ...]``.

    Names are read off the live source entities here rather than out of the
    index, so a rename shows up immediately without the index having to be
    rebuilt for something that did not change any connection.

    Deduplicated by connection, because a connection addressed by both id and
    name is filed under each and would otherwise be reported twice.
    """
    return [(_entity_name(entity), label)
            for entity, label, _conn in _incoming(brushes, things,
                                                  target_name, target_id)]


def _incoming(brushes, things, target_name="", target_id=""):
    """Every index entry pointing at one entity, each counted once.

    Deduplication is by connection identity rather than by ``(source, label)``:
    a connection filed under both a target id and a target name is one wire and
    must be counted once, but two separate wires from the same entity's same
    output are two and must be counted twice.
    """
    if not target_name and not target_id:
        return []
    index = target_index(brushes, things)
    out = []
    seen = set()
    for key in (target_id, target_name):
        for entry in (index.get(key, ()) if key else ()):
            entity, label, conn = entry
            ident = id(conn) if conn is not None else (id(entity), label)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(entry)
    return out


def count_incoming_connections(brushes, things, target_name="", target_id="",
                               input_name=""):
    """How many connections call one input on one entity.

    What a :class:`~editor.things.LogicGate` needs to know: how many signals is
    it waiting for?  Answered from the cached reverse index rather than by
    walking the level, and filtered by input name so a gate that also has a
    ``Reset`` wired into it is still waiting for the right number of
    ``Trigger`` signals.
    """
    wanted = input_name.lower() if input_name else ""
    total = 0
    for _entity, _label, conn in _incoming(brushes, things, target_name, target_id):
        if conn is None:
            continue
        name = (conn.get('input') or conn.get('input_name') or ''
                ) if isinstance(conn, dict) else getattr(conn, 'input_name', '')
        if not wanted or (name or '').lower() == wanted:
            total += 1
    return total


def entity_names(brushes, things):
    """Every name in the scene, for existence checks without a scan."""
    names = {b.get('name') for b in brushes if b.get('name')}
    names.update(t.properties.get('name') for t in things
                 if t.properties.get('name'))
    return names


# =============================================================================
# CONNECTION VALIDATION
# =============================================================================
#
# When the map is the program, a connection pointing at nothing is a broken
# reference — and until now it failed at run time, as a line in the debug
# console, long after the mistake was made.  These functions answer "what is
# wrong with this network?" from the data the editor already has.
#
# They are pure: no state, no cache, no manager, no scheduling.  Nothing calls
# them per frame; the editor calls them when it draws a panel, and a test calls
# them to assert a map is sound.

#: What :func:`validate_connections` can report.  Codes rather than sentences so
#: a caller can decide which ones it cares about.
PROBLEM_MISSING_TARGET = 'missing_target'
PROBLEM_UNKNOWN_INPUT = 'unknown_input'
PROBLEM_UNKNOWN_OUTPUT = 'unknown_output'


def _io_names_lower(names):
    return {n.lower() for n in names}


def validate_connection(conn, entity, target, source_type=None):
    """Problems with one connection, as ``[(code, message), ...]``.

    *target* is the entity the connection resolves to, or None.  An unresolved
    target is reported; so is an input the target's type does not accept, which
    is the failure that used to be completely silent — a connection naming
    ``Opne`` instead of ``Open`` resolved its target perfectly well and then did
    nothing, with no error anywhere.

    Two cases are carefully *not* conflated.  A type Fio has no definitions for
    (a plugin entity, a type from a newer map) is not judged at all, because
    reporting every input on it would train people to ignore the report.  A type
    that *is* registered and declares no inputs is judged — every input on it is
    wrong, and saying so is the whole point.  :func:`is_registered_type` is what
    tells the two apart; the length of the declared list cannot.

    The inputs every entity accepts regardless of type
    (:data:`IOManager.GENERIC_INPUTS` — Enable, Hide, SetTint and the rest) are
    accepted here too, because the dispatcher really does implement them for
    anything.
    """
    problems = []
    target_name = getattr(conn, 'target_name', '') or ''
    input_name = getattr(conn, 'input_name', '') or ''
    output_name = getattr(conn, 'output_name', '') or ''

    if target is None:
        problems.append((
            PROBLEM_MISSING_TARGET,
            "targets '%s', which is not in this map" % (target_name or '<unnamed>',)))
    else:
        target_type = get_entity_type_for_io(target)
        if is_registered_type(target_type):
            accepted = _io_names_lower(get_input_names(target_type))
            accepted |= IOManager.GENERIC_INPUTS
            if input_name.lower() not in accepted:
                problems.append((
                    PROBLEM_UNKNOWN_INPUT,
                    "calls '%s', which %s entities do not accept"
                    % (input_name, target_type)))

    if source_type is None and entity is not None:
        source_type = get_entity_type_for_io(entity)
    if source_type and is_registered_type(source_type):
        declared = _io_names_lower(get_output_names(source_type))
        if output_name.lower() not in declared:
            problems.append((
                PROBLEM_UNKNOWN_OUTPUT,
                "fires '%s', which %s entities do not have"
                % (output_name, source_type)))

    return problems


def declared_outputs(entity_type: str) -> Set[str]:
    """Lowercased declared output names for a type, memoised."""
    cached = _declared_outputs_cache.get(entity_type)
    if cached is None:
        cached = {io.name.lower() for io in get_outputs(entity_type)}
        _declared_outputs_cache[entity_type] = cached
    return cached


def audit_io_coverage(io_manager):
    """Reconcile what the editor declares with what the runtime implements.

    The registry and an ``IOManager``'s handler table are two separate things
    and always will be — one is global and built at import, the other is
    per-session and partly supplied by plugins. So the only way to know they
    agree is to ask, which is what this does. Pass an ``IOManager`` that has had
    every handler registered on it (the core ones *and* any plugin runtime's).

    Returns a dict of three lists, each of ``(entity_type, io_name)``:

    ``unimplemented_inputs``
        Declared, but no handler is registered and the generic dispatcher does
        not serve it either. The editor offers it; nothing runs. This is the
        failure mode the split makes possible, and the reason this function
        exists.
    ``undeclared_inputs``
        A handler exists for an input no type declares, so it works but no
        designer can find it in the editor.
    ``unknown_types``
        A handler registered against a type the registry has never heard of —
        usually a typo in the type token, which silently makes the handler
        unreachable.

    Entries listed in :data:`ABSTRACT_IO` are excluded, having been declared
    inert on purpose. Outputs are not covered here: whether an output is ever
    fired is a property of the code that fires it, not of any table, so it is
    checked by reading the source (see the conformance tests).
    """
    handlers = set(getattr(io_manager, '_input_handlers', {}))
    generic = set(IOManager.GENERIC_INPUTS)

    declared = set()
    unimplemented = []
    for entity_type in IO_REGISTRY:
        for name in get_input_names(entity_type):
            key = (entity_type.lower(), name.lower())
            declared.add(key)
            if key in handlers or key[1] in generic:
                continue
            if (entity_type, name) in ABSTRACT_IO:
                continue
            unimplemented.append((entity_type, name))

    known_types = {t.lower() for t in IO_REGISTRY}
    undeclared, unknown = [], []
    for entity_type, name in handlers:
        if entity_type not in known_types:
            unknown.append((entity_type, name))
        elif (entity_type, name) not in declared:
            undeclared.append((entity_type, name))

    return {
        'unimplemented_inputs': sorted(unimplemented),
        'undeclared_inputs': sorted(undeclared),
        'unknown_types': sorted(unknown),
    }


def validate_scene_connections(brushes, things, find_by_id=None, find_by_name=None):
    """Every problem in a whole map, as ``[(entity, connection, code, message)]``.

    Resolution follows the runtime's own rule — id first, name second — so what
    this reports is what would actually happen, not an approximation of it.
    Callers that already have the editor's lookups pass them in; otherwise a
    pair of dicts is built from the scene once, for this call only.
    """
    if find_by_id is None or find_by_name is None:
        by_id, by_name = {}, {}
        for brush in brushes:
            if brush.get('id'):
                by_id[brush['id']] = brush
            if brush.get('name'):
                by_name[brush['name']] = brush
        for thing in things:
            props = thing.properties
            if props.get('id'):
                by_id[props['id']] = thing
            if props.get('name'):
                by_name[props['name']] = thing
        find_by_id = find_by_id or by_id.get
        find_by_name = find_by_name or by_name.get

    def _resolve(conn):
        target_id = getattr(conn, 'target_id', '')
        if target_id:
            found = find_by_id(target_id)
            if found is not None:
                return found
        return find_by_name(getattr(conn, 'target_name', '') or '')

    report = []
    for entity in list(brushes) + list(things):
        source_type = get_entity_type_for_io(entity)
        for conn in get_connections(entity):
            target = _resolve(conn)
            for code, message in validate_connection(conn, entity, target, source_type):
                report.append((entity, conn, code, message))
    return report

def validate_all_scene_connections(
        brushes, things, find_by_id=None, find_by_name=None):
    """Validate every authored graph connection in the scene.

    This covers both graph systems currently used by Fio:

    - I/O connections stored as OutputConnection objects.
    - PathNode navigation links stored in ``next_node``.

    PathNode links are deliberately validated separately because they are
    navigation-graph edges, not I/O edges.
    """

    all_entities = list(brushes) + list(things)

    # ------------------------------------------------------------------
    # I/O connections
    # ------------------------------------------------------------------
    io_problems = validate_scene_connections(
        brushes,
        things,
        find_by_id=find_by_id,
        find_by_name=find_by_name,
    )

    io_count = sum(
        len(get_connections(entity))
        for entity in all_entities
    )

    # ------------------------------------------------------------------
    # Build a name lookup for PathNode links.
    #
    # PathNode.next_node stores the target's name, not an OutputConnection
    # and not a target UUID.
    # ------------------------------------------------------------------
    if find_by_name is None:
        by_name = {}

        for entity in all_entities:
            if isinstance(entity, dict):
                properties = entity.get("properties", entity)
            else:
                properties = getattr(entity, "properties", {})

            if not isinstance(properties, dict):
                continue

            name = properties.get("name", "")
            if name:
                by_name[name] = entity

        def find_by_name(name):
            return by_name.get(name)

    # ------------------------------------------------------------------
    # PathNode navigation links
    # ------------------------------------------------------------------
    pathnode_count = 0
    pathnode_problems = []

    for entity in things:
        if isinstance(entity, dict):
            properties = entity.get("properties", entity)
        else:
            properties = getattr(entity, "properties", {})

        if not isinstance(properties, dict):
            continue

        entity_type = str(properties.get("type", "")).replace("_", "").lower()

        if entity_type != "pathnode":
            continue

        next_node = properties.get("next_node", "")
        if not next_node:
            # Empty next_node is a legitimate dead-end.
            continue

        pathnode_count += 1

        target = find_by_name(next_node)

        if target is None:
            source_name = properties.get("name", "<unnamed>")

            pathnode_problems.append((
                entity,
                next_node,
                "missing_pathnode_target",
                "PathNode '%s' points to missing PathNode '%s'."
                % (source_name, next_node),
            ))
            continue

        if isinstance(target, dict):
            target_properties = target.get("properties", target)
        else:
            target_properties = getattr(target, "properties", {})

        if not isinstance(target_properties, dict):
            target_properties = {}

        target_type = (
            str(target_properties.get("type", ""))
            .replace("_", "")
            .lower()
        )

        if target_type != "pathnode":
            source_name = properties.get("name", "<unnamed>")

            pathnode_problems.append((
                entity,
                next_node,
                "invalid_pathnode_target",
                "PathNode '%s' points to '%s', which is not a PathNode."
                % (source_name, next_node),
            ))

    # ------------------------------------------------------------------
    # Combined result
    # ------------------------------------------------------------------
    problems = list(io_problems) + list(pathnode_problems)

    return {
        "io_count": io_count,
        "pathnode_count": pathnode_count,
        "total": io_count + pathnode_count,
        "problems": problems,
        "io_problems": io_problems,
        "pathnode_problems": pathnode_problems,
    }

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
    bump_io_revision()


def remove_connection(entity, connection: OutputConnection):
    """Remove an output connection from an entity."""
    connections = get_connections(entity)
    if connection in connections:
        connections.remove(connection)
        bump_io_revision()


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
    bump_io_revision()


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
