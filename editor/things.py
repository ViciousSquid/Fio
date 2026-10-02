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
    pixmap_path = None
    _pixmap_cache = {} # Class-level cache for loaded pixmaps
    _counters = {} # Class-level counter for unique naming

    def __init__(self, pos=None, properties=None):
        self.pos = pos if pos is not None else [0, 0, 0]
        self.properties = properties if properties is not None else {}
        self.properties.setdefault('type', self.__class__.__name__.lower())
        
        # --- Set a default and unique name ---
        if 'name' not in self.properties or not self.properties['name']:
            class_name = self.__class__.__name__
            if class_name not in Thing._counters:
                Thing._counters[class_name] = 1
            else:
                Thing._counters[class_name] += 1
            self.properties['name'] = f"{class_name}_{Thing._counters[class_name]}"

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
        the first time it's requested. This is the new on-demand system.
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

    def to_dict(self):
        serializable_props = {k: str(v) for k, v in self.properties.items()}
        return {'type': self.properties.get('type'), 'pos': self.pos, 'properties': serializable_props}

    @staticmethod
    def from_dict(data):
        thing_type = data.get('type')
        if not thing_type: return None

        properties = data.get('properties', {})
        for key, value in properties.items():
            if isinstance(value, str):
                try: properties[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError): pass

        for cls in find_subclasses(Thing):
            # Use lower() for case-insensitive matching with type property
            if cls.__name__.lower() == thing_type:
                return cls(pos=data.get('pos'), properties=properties)
        
        # Fallback for base Thing if no specific subclass matches
        if thing_type == 'thing':
            return Thing(pos=data.get('pos'), properties=properties)
        
        print(f"Warning: Unknown thing type '{thing_type}' found in map file.")
        return None

# --- Thing Subclasses ---

class PlayerStart(Thing):
    pixmap_path = "assets/player.png"
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'playerstart')
        self.properties.setdefault('angle', 0.0)

    def get_angle(self):
        return float(self.properties.get('angle', 0.0))

class Light(Thing):
    pixmap_path = "assets/light.png"
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
    pixmap_path = "assets/speaker.png"
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
        self.properties.setdefault('triggered', False)  # Can be triggered by a trigger brush
        self.properties.setdefault('state', 'off')  # 'on' or 'off' - current playback state

    def get_radius(self):
        return float(self.properties.get('radius', 512.0))

class Monster(Thing):
    pixmap_path = "assets/monster.png"
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'monster')
        self.properties.setdefault('id', 0)

class Pickup(Thing):
    pixmap_path = "assets/pickup.png"
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'pickup')
        self.properties.setdefault('item_type', 'health')
        self.properties.setdefault('value', 25)
        self.properties.setdefault('activation', 'walk_over')  # 'walk_over' or 'use'
        self.properties.setdefault('collected', False)  # Runtime state

class Trigger(Thing):
    pixmap_path = None
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'trigger')
        self.properties.setdefault('target', '')
        self.properties.setdefault('action', 'on_enter')

class Model(Thing):
    """Represents a 3D model placed in the world."""
    pixmap_path = "assets/model_icon.png"
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'model')
        self.properties.setdefault('model_path', "")
        self.properties.setdefault('rotation', [0, 0, 0])
        self.properties.setdefault('scale', [1, 1, 1])