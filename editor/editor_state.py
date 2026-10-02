import json
import copy
from .things import Thing, Model

class EditorState:
    """Manages all the data for the current level being edited."""

    def __init__(self):
        self.brushes = []
        self.things = []
        self.selected_object = None
        
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
        return {
            'fingerprint': 'RStudio', # The magic number
            'brushes': self.brushes, 
            'things': [t.to_dict() for t in self.things]
        }

    def load_from_data(self, level_data):
        """Populates the scene from a dictionary after validating the fingerprint."""
        if level_data.get('fingerprint') != 'RStudio':
            raise ValueError("Not a valid save file: Missing 'RStudio' fingerprint.") # Refuse to load
            
        self.brushes = level_data.get('brushes', [])
        
        things_data = level_data.get('things', [])
        new_things = []
        for t_data in things_data:
            if t_data.get('type') == 'Model':
                model_kwargs = {k: v for k, v in t_data.items() if k != 'type'}
                new_things.append(Model(**model_kwargs))
            else:
                new_things.append(Thing.from_dict(t_data))
        self.things = new_things
        
        self.selected_object = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.save_state()

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
        state = {
            'brushes': copy.deepcopy(self.brushes),
            'things': [t.to_dict() for t in self.things],
            'selected_type': selected_type,
            'selected_index': selected_index,
        }
        self.undo_stack.append(json.dumps(state))
        self.redo_stack.clear()
        
        if len(self.undo_stack) > 50:
            self.undo_stack.pop(0)

    def restore_state(self, state_json):
        """Restores the scene from a JSON state string."""
        state = json.loads(state_json)
        self.brushes = state.get('brushes', [])
        
        things_data = state.get('things', [])
        new_things = []
        for t_data in things_data:
            if t_data.get('type') == 'Model':
                model_kwargs = {k: v for k, v in t_data.items() if k != 'type'}
                new_things.append(Model(**model_kwargs))
            else:
                new_things.append(Thing.from_dict(t_data))
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