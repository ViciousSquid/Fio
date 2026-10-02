"""
RStudio Things Module - Entity Definitions

This module defines all placeable entities (Things) in the level editor.
Each Thing type has associated I/O definitions (inputs/outputs) for
the event-driven entity communication system.
"""

import os
from PyQt5.QtGui import QPixmap
import json
import ast

def find_subclasses(cls):
    """Recursively finds all subclasses of a given class."""
    all_subclasses = []
    for subclass in cls.__subclasses__():
        all_subclasses.append(subclass)
        all_subclasses.extend(find_subclasses(subclass))
    return all_subclasses


class Thing:
    """Base class for all placeable entities."""
    pixmap_path = None
    _pixmap_cache = {}  # Class-level cache for loaded pixmaps
    _counters = {}      # Class-level counter for unique naming

    def __init__(self, pos=None, properties=None):
        self.pos = pos if pos is not None else [0, 0, 0]
        self.properties = properties if properties is not None else {}
        self.properties.setdefault('type', self.__class__.__name__.lower())
        
        # Set a default and unique name
        if 'name' not in self.properties or not self.properties['name']:
            class_name = self.__class__.__name__
            if class_name not in Thing._counters:
                Thing._counters[class_name] = 1
            else:
                Thing._counters[class_name] += 1
            self.properties['name'] = f"{class_name}_{Thing._counters[class_name]}"
        
        # I/O Connections - stored as list of OutputConnection objects
        # Serialized separately in to_dict/from_dict
        if '_io_connections' not in self.properties:
            self.properties['_io_connections'] = []

    @property
    def name(self):
        """Gets the name from the properties dictionary."""
        return self.properties.get('name', '')

    @name.setter
    def name(self, value):
        """Sets the name in the properties dictionary."""
        self.properties['name'] = value

    @classmethod
    def get_pixmap(cls):
        """
        Gets the QPixmap for this class, loading it from disk and caching it
        the first time it's requested.
        """
        class_name = cls.__name__
        if class_name in cls._pixmap_cache:
            return cls._pixmap_cache[class_name]

        if not cls.pixmap_path:
            cls._pixmap_cache[class_name] = None
            return None

        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
        except NameError:
            project_root = os.path.abspath(os.path.join(os.getcwd()))

        absolute_path = os.path.join(project_root, cls.pixmap_path)

        pixmap = None
        if os.path.exists(absolute_path):
            loaded_pixmap = QPixmap(absolute_path)
            if not loaded_pixmap.isNull():
                pixmap = loaded_pixmap
            else:
                print(f"Error: QPixmap failed to load image for {class_name} from {absolute_path}")
        else:
            print(f"Warning: Sprite file not found for {class_name} at: {absolute_path}")

        cls._pixmap_cache[class_name] = pixmap
        return pixmap
    
    def get_instance_pixmap(self):
        """
        Gets the QPixmap for this specific instance. 
        Override in subclasses for dynamic sprite selection.
        """
        return self.__class__.get_pixmap()

    def to_dict(self):
        """Serialize to dictionary for saving."""
        # Separate I/O connections from regular properties
        io_connections = self.properties.get('_io_connections', [])
        
        # Serialize connections
        serialized_connections = []
        for conn in io_connections:
            if hasattr(conn, 'to_dict'):
                serialized_connections.append(conn.to_dict())
            elif isinstance(conn, dict):
                serialized_connections.append(conn)
        
        # Copy properties without _io_connections for cleaner serialization
        props_copy = {k: v for k, v in self.properties.items() if k != '_io_connections'}
        serializable_props = {k: str(v) for k, v in props_copy.items()}
        
        result = {
            'type': self.properties.get('type'),
            'pos': self.pos,
            'properties': serializable_props
        }
        
        # Only include connections if there are any
        if serialized_connections:
            result['io_connections'] = serialized_connections
        
        return result

    @staticmethod
    def from_dict(data):
        """Deserialize from dictionary."""
        thing_type = data.get('type')
        if not thing_type:
            return None

        properties = data.get('properties', {})
        for key, value in properties.items():
            if isinstance(value, str):
                try:
                    properties[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    pass

        # Find matching subclass
        thing = None
        for cls in find_subclasses(Thing):
            if cls.__name__.lower() == thing_type:
                thing = cls(pos=data.get('pos'), properties=properties)
                break
        
        # Fallback for base Thing
        if thing is None:
            if thing_type == 'thing':
                thing = Thing(pos=data.get('pos'), properties=properties)
            else:
                print(f"Warning: Unknown thing type '{thing_type}' found in map file.")
                return None
        
        # Restore I/O connections
        io_data = data.get('io_connections', [])
        if io_data:
            try:
                from .io_system import OutputConnection
                connections = [OutputConnection.from_dict(d) for d in io_data]
                thing.properties['_io_connections'] = connections
            except ImportError:
                # io_system not available, store raw data
                thing.properties['_io_connections'] = io_data
        
        return thing
    
    def add_output_connection(self, output_name, target_name, input_name, 
                               parameter="", delay=0.0, fire_once=False):
        """Helper method to add an output connection."""
        try:
            from .io_system import OutputConnection, add_connection
            conn = OutputConnection(
                output_name=output_name,
                target_name=target_name,
                input_name=input_name,
                parameter=parameter,
                delay=delay,
                fire_once=fire_once
            )
            add_connection(self, conn)
            return conn
        except ImportError:
            # Fallback to dict storage
            if '_io_connections' not in self.properties:
                self.properties['_io_connections'] = []
            self.properties['_io_connections'].append({
                'output': output_name,
                'target': target_name,
                'input': input_name,
                'parameter': parameter,
                'delay': delay,
                'fire_once': fire_once
            })
            return None
    
    def get_io_connections(self):
        """Get all I/O connections for this entity."""
        return self.properties.get('_io_connections', [])
    
    def clear_io_connections(self):
        """Clear all I/O connections."""
        self.properties['_io_connections'] = []


# =============================================================================
# STANDARD THING SUBCLASSES
# =============================================================================

class PlayerStart(Thing):
    """Defines where the player spawns."""
    pixmap_path = "assets/sprites/player.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'playerstart')
        self.properties.setdefault('angle', 0.0)

    def get_angle(self):
        return float(self.properties.get('angle', 0.0))


class Light(Thing):
    """Dynamic light source."""
    pixmap_path = "assets/sprites/light.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'light')
        self.properties.setdefault('colour', [255, 255, 255])
        self.properties.setdefault('intensity', 1.0)
        self.properties.setdefault('radius', 512.0)
        self.properties.setdefault('state', 'on')
        self.properties.setdefault('show_radius', False)
        self.properties.setdefault('casts_shadows', False)

    def get_color(self):
        color = self.properties.get('colour', [255, 255, 255])
        return [c / 255.0 for c in color]

    def get_intensity(self):
        return float(self.properties.get('intensity', 1.0))

    def get_radius(self):
        return float(self.properties.get('radius', 512.0))


class Speaker(Thing):
    """Sound emitter entity."""
    pixmap_path = "assets/sprites/speaker.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'speaker')
        self.properties.setdefault('sound_file', "")
        self.properties.setdefault('radius', 512.0)
        self.properties.setdefault('global', False)
        self.properties.setdefault('show_radius', False)
        self.properties.setdefault('volume', 1.0)
        self.properties.setdefault('looping', False)
        self.properties.setdefault('play_on_start', False)
        self.properties.setdefault('state', 'off')

    def get_radius(self):
        return float(self.properties.get('radius', 512.0))


class Monster(Thing):
    """Enemy entity."""
    pixmap_path = "assets/sprites/monster.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'monster')
        self.properties.setdefault('id', 0)


class Pickup(Thing):
    """Collectible item entity."""
    pixmap_path = "assets/sprites/pickup.png"
    
    KEY_SPRITES = {
        'blue_key': 'assets/sprites/bluekey.png',
        'red_key': 'assets/sprites/redkey.png',
        'yellow_key': 'assets/sprites/yellowkey.png',
        'green_key': 'assets/sprites/greenkey.png',
    }
    
    GUN_SPRITES = {
        'gun1': 'assets/sprites/gun1.png',
        'gun2': 'assets/sprites/gun2.png'
    }
    
    _dynamic_sprite_cache = {}
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'pickup')
        self.properties.setdefault('item_type', 'health')
        self.properties.setdefault('value', 25)
        self.properties.setdefault('activation', 'walk_over')
        self.properties.setdefault('collected', False)
        self.properties.setdefault('respawns', False)
        self.properties.setdefault('respawn_time', 20.0)
        self.properties.setdefault('key_name', 'blue_key')
        self.properties.setdefault('custom_sprite', '')

    def is_gun(self):
        return self.properties.get('item_type') in ['gun1', 'gun2']
    
    def is_key(self):
        return self.properties.get('item_type') == 'key'
    
    def get_key_name(self):
        return self.properties.get('key_name', 'blue_key')
    
    def get_sprite_path(self):
        custom = self.properties.get('custom_sprite', '')
        if custom and not self.is_key() and not self.is_gun():
            return custom
        
        if self.is_key():
            key_name = self.get_key_name()
            return self.KEY_SPRITES.get(key_name, 'assets/sprites/pickup.png')
        
        item_type = self.properties.get('item_type')
        if item_type in self.GUN_SPRITES:
            return self.GUN_SPRITES[item_type]
        
        if custom:
            return custom
        
        return 'assets/sprites/pickup.png'
    
    def get_instance_pixmap(self):
        sprite_path = self.get_sprite_path()
        
        if sprite_path in Pickup._dynamic_sprite_cache:
            return Pickup._dynamic_sprite_cache[sprite_path]
        
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
        except NameError:
            project_root = os.path.abspath(os.path.join(os.getcwd()))
        
        if os.path.isabs(sprite_path):
            absolute_path = sprite_path
        else:
            absolute_path = os.path.join(project_root, sprite_path)
        
        pixmap = None
        if os.path.exists(absolute_path):
            loaded_pixmap = QPixmap(absolute_path)
            if not loaded_pixmap.isNull():
                custom = self.properties.get('custom_sprite', '')
                if custom and loaded_pixmap.width() != 75:
                    pixmap = loaded_pixmap.scaled(75, 75)
                else:
                    pixmap = loaded_pixmap
            else:
                print(f"Error: QPixmap failed to load sprite from {absolute_path}")
        else:
            print(f"Warning: Sprite file not found at: {absolute_path}")
            pixmap = Pickup.get_pixmap()
        
        Pickup._dynamic_sprite_cache[sprite_path] = pixmap
        return pixmap
    
    @classmethod
    def get_key_sprite_path(cls, key_name):
        return cls.KEY_SPRITES.get(key_name, 'assets/sprites/pickup.png')
    
    @classmethod
    def get_key_pixmap(cls, key_name):
        sprite_path = cls.get_key_sprite_path(key_name)
        
        if sprite_path in cls._dynamic_sprite_cache:
            return cls._dynamic_sprite_cache[sprite_path]
        
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
        except NameError:
            project_root = os.path.abspath(os.path.join(os.getcwd()))
        
        absolute_path = os.path.join(project_root, sprite_path)
        
        pixmap = None
        if os.path.exists(absolute_path):
            loaded_pixmap = QPixmap(absolute_path)
            if not loaded_pixmap.isNull():
                pixmap = loaded_pixmap
        
        cls._dynamic_sprite_cache[sprite_path] = pixmap
        return pixmap
    
    @classmethod
    def clear_sprite_cache(cls):
        cls._dynamic_sprite_cache.clear()


class Trigger(Thing):
    """Non-visible trigger volume (for point-entity triggers)."""
    pixmap_path = None
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'trigger')
        self.properties.setdefault('action', 'on_enter')


class Model(Thing):
    """Represents a 3D model placed in the world."""
    pixmap_path = "assets/sprites/model.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'model')
        self.properties.setdefault('model_path', "")
        self.properties.setdefault('rotation', [0, 0, 0])
        self.properties.setdefault('scale', [1, 1, 1])


# =============================================================================
# LOGIC ENTITIES
# =============================================================================

class LogicRelay(Thing):
    """
    A relay that fires OnTrigger when its Trigger input is called.
    Can be enabled/disabled. Useful for creating reusable trigger chains.
    """
    pixmap_path = "assets/sprites/logic_relay.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_relay'
        self.properties.setdefault('disabled', False)
        self.properties.setdefault('fire_once', False)
    
    def get_instance_pixmap(self):
        pix = super().get_instance_pixmap()
        if pix:
            return pix
        # Fallback to logic_gate sprite
        return LogicGate.get_pixmap()


class LogicGate(Thing):
    """
    Multi-input logic gate (AND, OR, XOR, NAND, NOR).
    Fires OnTrigger when gate condition is met.
    """
    pixmap_path = "assets/sprites/logic_gate.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_gate'
        self.properties.setdefault('logic_type', 'AND')
        self.properties.setdefault('initial_state', 'off')

    def get_instance_pixmap(self):
        l_type = self.properties.get('logic_type', 'and').lower()
        expected_path = f"assets/sprites/logic_{l_type}.png"
        
        if expected_path in self._pixmap_cache:
            return self._pixmap_cache[expected_path]
            
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
            abs_path = os.path.join(project_root, expected_path)
            
            if os.path.exists(abs_path):
                pix = QPixmap(abs_path)
                if not pix.isNull():
                    self._pixmap_cache[expected_path] = pix
                    return pix
        except:
            pass
            
        return super().get_instance_pixmap()


class LogicTimer(Thing):
    """
    Timer that fires OnTimer at a set interval.
    Can be enabled/disabled.
    """
    pixmap_path = "assets/sprites/logic_timer.png"
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_timer'
        self.properties.setdefault('interval', 1.0)
        self.properties.setdefault('timer_enabled', False)
        self.properties.setdefault('start_on', False)
    
    def get_instance_pixmap(self):
        pix = super().get_instance_pixmap()
        if pix:
            return pix
        return LogicGate.get_pixmap()


# =============================================================================
# ENTITY REGISTRY
# =============================================================================

# All placeable entity types for the editor
ENTITY_TYPES = {
    'PlayerStart': PlayerStart,
    'Light': Light,
    'Speaker': Speaker,
    'Monster': Monster,
    'Pickup': Pickup,
    'Model': Model,
    # Logic entities
    'LogicRelay': LogicRelay,
    'LogicGate': LogicGate,
    'LogicTimer': LogicTimer,
}

# Categories for editor UI
ENTITY_CATEGORIES = {
    'Gameplay': ['PlayerStart', 'Monster', 'Pickup'],
    'Environment': ['Light', 'Speaker', 'Model'],
    'Logic': ['LogicRelay', 'LogicGate', 'LogicTimer'],
}