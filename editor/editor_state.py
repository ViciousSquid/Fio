"""
RStudio Editor State Manager

Manages all the data for the current level being edited, including:
- Brushes (solid geometry)
- Things (entities)
- Undo/redo history
- Serialization/deserialization with I/O connections
"""

import json
import copy
from .things import Thing, Model

# Import I/O system for serialization
try:
    from .io_system import OutputConnection, get_connections, set_connections
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False


class EditorState:
    """Manages all the data for the current level being edited."""

    def __init__(self):
        self.brushes = []
        self.things = []
        self.selected_object = None
        self.terrain_data = None
        self.undo_stack = []
        self.redo_stack = []
        
        # Initial empty state for the undo stack
        self.save_state()

    def set_selected_object(self, obj):
        """Sets the currently selected object."""
        self.selected_object = obj

    def clear_scene(self):
        """Resets the scene to an empty state."""
        self.brushes.clear()
        self.things.clear()
        self.selected_object = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.save_state()

    def get_level_data(self):
        """Serializes the current scene state into a dictionary with a fingerprint."""
        data = {
            'fingerprint': 'RStudio',
            'version': 2,  # Version 2 includes I/O system
            'brushes': self._serialize_brushes(),
            'things': [t.to_dict() for t in self.things]
        }
        
        # Include terrain data if present
        if hasattr(self, 'terrain_data') and self.terrain_data:
            data['terrain_data'] = self.terrain_data
            
        return data
    
    def _serialize_brushes(self):
        """Serialize brushes with I/O connections."""
        serialized = []
        
        for brush in self.brushes:
            brush_copy = brush.copy()
            
            # Handle I/O connections
            if IO_AVAILABLE and '_io_connections' in brush:
                connections = brush['_io_connections']
                serialized_conns = []
                
                for conn in connections:
                    if hasattr(conn, 'to_dict'):
                        serialized_conns.append(conn.to_dict())
                    elif isinstance(conn, dict):
                        serialized_conns.append(conn)
                
                if serialized_conns:
                    brush_copy['io_connections'] = serialized_conns
                
                # Remove internal _io_connections from serialized data
                if '_io_connections' in brush_copy:
                    del brush_copy['_io_connections']
            
            serialized.append(brush_copy)
        
        return serialized
    
    def _deserialize_brushes(self, brushes_data):
        """Deserialize brushes with I/O connections."""
        result = []
        
        for brush_data in brushes_data:
            brush = brush_data.copy()
            
            # Restore I/O connections
            if IO_AVAILABLE and 'io_connections' in brush:
                io_data = brush.pop('io_connections')
                connections = [OutputConnection.from_dict(d) for d in io_data]
                brush['_io_connections'] = connections
            elif 'io_connections' in brush:
                # Without I/O system, store as raw data
                brush['_io_connections'] = brush.pop('io_connections')
            else:
                # Ensure _io_connections exists
                brush['_io_connections'] = []
            
            result.append(brush)
        
        return result

    def load_from_data(self, level_data):
        """Populates the scene from a dictionary after validating the fingerprint."""
        if level_data.get('fingerprint') != 'RStudio':
            raise ValueError("Not a valid save file: Missing 'RStudio' fingerprint.")
        
        # Handle both old and new format
        version = level_data.get('version', 1)
        
        if version >= 2:
            # New format with I/O connections stored separately
            self.brushes = self._deserialize_brushes(level_data.get('brushes', []))
        else:
            # Old format - brushes are plain dicts
            self.brushes = level_data.get('brushes', [])
            # Initialize _io_connections for old brushes
            for brush in self.brushes:
                if '_io_connections' not in brush:
                    brush['_io_connections'] = []
                    
                    # Migrate old 'target' property to I/O connection
                    if brush.get('target') and IO_AVAILABLE:
                        self._migrate_legacy_target(brush)
        
        self.terrain_data = level_data.get('terrain_data', None)
        
        # Load things
        things_data = level_data.get('things', [])
        new_things = []
        for t_data in things_data:
            if t_data.get('type') == 'Model':
                model_kwargs = {k: v for k, v in t_data.items() if k != 'type'}
                new_things.append(Model(**model_kwargs))
            else:
                thing = Thing.from_dict(t_data)
                if thing:
                    # Migrate legacy 'target' property
                    if thing.properties.get('target') and IO_AVAILABLE:
                        self._migrate_legacy_thing_target(thing)
                    new_things.append(thing)
        
        self.things = new_things
        
        self.selected_object = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.save_state()
    
    def _migrate_legacy_target(self, brush):
        """Migrate old 'target' property to I/O connection for brushes."""
        target = brush.get('target', '')
        if not target:
            return
        
        # Determine what output to use
        if brush.get('is_trigger'):
            output = 'OnTrigger'
        elif brush.get('is_mover'):
            output = 'OnFullyOpen'
        elif brush.get('is_door'):
            output = 'OnOpen'
        else:
            return
        
        # Create connection
        conn = OutputConnection(
            output_name=output,
            target_name=target,
            input_name='Toggle',  # Generic default
            parameter='',
            delay=0.0,
            fire_once=False
        )
        
        if '_io_connections' not in brush:
            brush['_io_connections'] = []
        brush['_io_connections'].append(conn)
    
    def _migrate_legacy_thing_target(self, thing):
        """Migrate old 'target' property to I/O connection for things."""
        target = thing.properties.get('target', '')
        if not target:
            return
        
        entity_type = thing.properties.get('type', '')
        
        # Determine output based on type
        if entity_type == 'logic_gate':
            output = 'OnTrigger'
        else:
            return
        
        thing.add_output_connection(
            output_name=output,
            target_name=target,
            input_name='Toggle'
        )

    def _get_selected_object_identifier(self):
        """Gets a stable identifier for the selected object for state restoration."""
        if not self.selected_object:
            return None, -1
        try:
            if isinstance(self.selected_object, dict):
                return 'brush', self.brushes.index(self.selected_object)
            else:
                return 'thing', self.things.index(self.selected_object)
        except ValueError:
            return None, -1

    def _restore_selection_from_identifier(self, selected_type, selected_index):
        """Restores the selected object using its identifier."""
        if selected_type and selected_index != -1:
            if selected_type == 'brush' and selected_index < len(self.brushes):
                self.selected_object = self.brushes[selected_index]
            elif selected_type == 'thing' and selected_index < len(self.things):
                self.selected_object = self.things[selected_index]
            else:
                self.selected_object = None
        else:
            self.selected_object = None

    def save_state(self):
        """Saves the current state of brushes and things to the undo stack."""
        selected_type, selected_index = self._get_selected_object_identifier()
        
        # Serialize brushes with I/O connections
        serialized_brushes = self._serialize_brushes_for_undo()
        
        state = {
            'brushes': serialized_brushes,
            'things': [t.to_dict() for t in self.things],
            'selected_type': selected_type,
            'selected_index': selected_index,
        }
        self.undo_stack.append(json.dumps(state))
        self.redo_stack.clear()
        
        if len(self.undo_stack) > 50:
            self.undo_stack.pop(0)
    
    def _serialize_brushes_for_undo(self):
        """Serialize brushes for undo stack (deep copy with I/O)."""
        result = []
        for brush in self.brushes:
            brush_copy = copy.deepcopy(brush)
            
            # Convert OutputConnection objects to dicts for JSON
            if '_io_connections' in brush_copy:
                connections = brush_copy['_io_connections']
                serialized = []
                for conn in connections:
                    if hasattr(conn, 'to_dict'):
                        serialized.append(conn.to_dict())
                    elif isinstance(conn, dict):
                        serialized.append(conn)
                brush_copy['_io_connections'] = serialized
            
            result.append(brush_copy)
        return result

    def restore_state(self, state_json):
        """Restores the scene from a JSON state string."""
        state = json.loads(state_json)
        
        # Restore brushes with I/O connections
        raw_brushes = state.get('brushes', [])
        self.brushes = []
        for brush_data in raw_brushes:
            brush = brush_data.copy()
            
            # Convert I/O connection dicts back to objects
            if IO_AVAILABLE and '_io_connections' in brush:
                io_data = brush['_io_connections']
                if io_data and isinstance(io_data[0], dict):
                    brush['_io_connections'] = [
                        OutputConnection.from_dict(d) for d in io_data
                    ]
            
            self.brushes.append(brush)
        
        # Restore things
        things_data = state.get('things', [])
        new_things = []
        for t_data in things_data:
            if t_data.get('type') == 'Model':
                model_kwargs = {k: v for k, v in t_data.items() if k != 'type'}
                new_things.append(Model(**model_kwargs))
            else:
                thing = Thing.from_dict(t_data)
                if thing:
                    new_things.append(thing)
        self.things = new_things
        
        self._restore_selection_from_identifier(
            state.get('selected_type'), state.get('selected_index', -1)
        )

    def undo(self):
        """Reverts to the previous state in the undo stack."""
        if len(self.undo_stack) > 1:
            current_state_json = self.undo_stack.pop()
            self.redo_stack.append(current_state_json)
            self.restore_state(self.undo_stack[-1])
            return True
        return False

    def redo(self):
        """Re-applies a state from the redo stack."""
        if self.redo_stack:
            state_json = self.redo_stack.pop()
            self.undo_stack.append(state_json)
            self.restore_state(state_json)
            return True
        return False
    
    # =========================================================================
    # I/O HELPER METHODS
    # =========================================================================
    
    def find_entity_by_name(self, name: str):
        """Find an entity (brush or thing) by name."""
        if not name:
            return None
        
        for brush in self.brushes:
            if brush.get('name') == name:
                return brush
        
        for thing in self.things:
            if thing.properties.get('name') == name:
                return thing
        
        return None
    
    def get_all_entity_names(self):
        """Get a list of all entity names in the scene."""
        names = []
        
        for brush in self.brushes:
            name = brush.get('name', '')
            if name:
                names.append(name)
        
        for thing in self.things:
            name = thing.properties.get('name', '')
            if name:
                names.append(name)
        
        return names
    
    def find_entities_targeting(self, target_name: str):
        """Find all entities that have I/O connections to the target."""
        sources = []
        
        if not IO_AVAILABLE:
            return sources
        
        for brush in self.brushes:
            for conn in get_connections(brush):
                if conn.target_name == target_name:
                    sources.append({
                        'entity': brush,
                        'connection': conn,
                        'type': 'brush'
                    })
        
        for thing in self.things:
            for conn in get_connections(thing):
                if conn.target_name == target_name:
                    sources.append({
                        'entity': thing,
                        'connection': conn,
                        'type': 'thing'
                    })
        
        return sources
