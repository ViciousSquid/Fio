"""
This module defines all placeable entities (Things) in the level editor.
Each Thing type has associated I/O definitions (inputs/outputs) for
the event-driven entity communication system.
"""

import os
import math
import uuid
from PyQt5.QtGui import QPixmap, QColor
from PyQt5.QtCore import Qt
import json
import ast


try:
    from .debug_console import debug_log
except ImportError:
    try:
        from editor.debug_console import debug_log
    except ImportError:
        def debug_log(category, message):
            print(f"[{category}] {message}")

def find_subclasses(cls):
    """Recursively finds all subclasses of a given class."""
    all_subclasses = []
    for subclass in cls.__subclasses__():
        all_subclasses.append(subclass)
        all_subclasses.extend(find_subclasses(subclass))
    return all_subclasses

def update_all_counters_from_entities(entities):
    """
    Update the class-level _counters for Thing subclasses based on existing entity names.
    entities: list of brushes (dict) and Thing instances.
    For brushes, they have a 'name' key; for Things, they have a 'name' property.
    The counter for each class is set to the highest numeric suffix found + 1.
    """
    import re
    from collections import defaultdict

    # Map class name to highest numeric index found
    max_indices = defaultdict(int)

    for entity in entities:
        # Get name
        if isinstance(entity, dict):
            name = entity.get('name', '')
        else:
            name = entity.properties.get('name', '')

        if not name:
            continue

        # Names are typically "ClassName_number" (e.g., "Monster_5", "Light_12")
        match = re.match(r'^([A-Za-z]+)_(\d+)$', name)
        if match:
            class_name = match.group(1)
            num = int(match.group(2))
            if num > max_indices[class_name]:
                max_indices[class_name] = num

    # Update counters for all Thing subclasses
    for cls in find_subclasses(Thing):
        class_name = cls.__name__
        if class_name in max_indices:
            cls._counters[class_name] = max_indices[class_name]
        else:
            # Ensure counter exists, starting at 0 (next created gets 1)
            cls._counters[class_name] = 0


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
        
        # Stable unique ID (persists across save/load)
        if 'id' not in self.properties:
            self.properties['id'] = str(uuid.uuid4())

        # I/O Connections - stored as list of OutputConnection objects
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

    def get_icon_pixmap(self):
        """Return a 60×60 icon for 2D views. Default implementation returns scaled instance pixmap."""
        pixmap = self.get_instance_pixmap()
        if pixmap and not pixmap.isNull():
            # Scale to 60×60 for consistent icon size in 2D view
            return pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return None
    
    def get_instance_pixmap(self):
        """
        Gets the QPixmap for this specific instance. 
        Override in subclasses for dynamic sprite selection.
        """
        return self.__class__.get_pixmap()

    def to_dict(self):
        """Serialize to dictionary for saving."""
        props_copy = {k: v for k, v in self.properties.items() if k != '_io_connections'}
        
        serializable_props = {}
        for k, v in props_copy.items():
            # Coerce legacy string-typed values to their native types on save.
            # This heals old map files automatically on next save.
            if isinstance(v, str):
                try:
                    v = ast.literal_eval(v)
                except (ValueError, SyntaxError):
                    pass  # Genuinely a string — keep it
            
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                serializable_props[k] = v
            else:
                serializable_props[k] = str(v)  # QColor etc.

        result = {
            'type': self.properties.get('type'),
            'pos': self.pos,
            'properties': serializable_props
        }

        # Always emit io_connections, even if empty, so loaders are unambiguous
        try:
            from .io_system import serialize_connections
            result['io_connections'] = serialize_connections(self)
        except ImportError:
            io_connections = self.properties.get('_io_connections', [])
            result['io_connections'] = [
                conn.to_dict() if hasattr(conn, 'to_dict') else conn
                for conn in io_connections
            ]

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

        thing = None
        for cls in find_subclasses(Thing):
            if cls.__name__.lower() == thing_type.replace('_', ''):
                thing = cls(pos=data.get('pos'), properties=properties)
                break
        
        if thing is None:
            if thing_type == 'thing':
                thing = Thing(pos=data.get('pos'), properties=properties)
            else:
                print(f"Warning: Unknown thing type '{thing_type}' found in map file.")
                return None
        
        io_data = data.get('io_connections', [])
        if io_data:
            try:
                from .io_system import OutputConnection
                connections = [OutputConnection.from_dict(d) for d in io_data]
                thing.properties['_io_connections'] = connections
            except ImportError:
                thing.properties['_io_connections'] = io_data
        
        return thing
    
    def add_output_connection(self, output_name, target_name, input_name, 
                               parameter="", delay=0.0, fire_once=False,
                               target_id=""):
        """Helper method to add an output connection."""
        try:
            from .io_system import OutputConnection, add_connection
            conn = OutputConnection(
                output_name=output_name,
                target_name=target_name,
                input_name=input_name,
                parameter=parameter,
                delay=delay,
                fire_once=fire_once,
                target_id=target_id
            )
            add_connection(self, conn)
            return conn
        except ImportError:
            if '_io_connections' not in self.properties:
                self.properties['_io_connections'] = []
            self.properties['_io_connections'].append({
                'output': output_name,
                'target': target_name,
                'target_id': target_id,
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

        # Parenting: attach this light to a mover brush by name.
        # When parent_mover is non-empty the logic thread moves this
        # light to (mover.pos + parent_offset) every tick during play.
        self.properties.setdefault('parent_mover', '')
        self.properties.setdefault('parent_offset', [0.0, 0.0, 0.0])

    def get_color(self):
        color = self.properties.get('colour', [255, 255, 255])
        return [c / 255.0 for c in color]

    def get_intensity(self):
        return float(self.properties.get('intensity', 1.0))

    def get_radius(self):
        return float(self.properties.get('radius', 512.0))

    def get_show_radius(self):
        val = self.properties.get('show_radius', False)
        if isinstance(val, str):
            return val.lower() == 'true'
        return bool(val)

    def set_show_radius(self, value):
        self.properties['show_radius'] = bool(value)

    show_radius = property(get_show_radius, set_show_radius)


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
    """Enemy entity with subtypes (human, flying)."""
    pixmap_path = "assets/sprites/monsters/human/idle.png"   # fallback
    _subtype_sprites = {}  # cache keyed by full sprite path (includes dead/alive state)
    _icon_cache = {}       # 1.2.6.0: cache for 2D view icons (60×60)

    # PERF: get_sprite_path used to run up to 5 os.path.isfile() calls every
    # time it was called — and it is called per-Monster per-frame from
    # qt_game_view.update_instance_textures. This cache memoises the resolved
    # (idle, dead, shoot) default paths by (monster_type, variant) so the
    # filesystem only sees the stats once per distinct monster configuration.
    # Keyed tuple: (mtype, variant)
    # Stored tuple: (default_idle, default_dead, default_shoot)
    _default_path_cache = {}
    # Separate cache for custom-path validity — the Customise dialog can set
    # arbitrary paths per-monster. Caches the result of the existence check so
    # subsequent frames don't re-stat the file.
    # Keyed tuple: (abs_path,) -> bool
    _custom_path_exists_cache = {}
    # Cache the project_root lookup once per class; it never changes at runtime.
    _cached_project_root = None

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'monster')
        self.properties.setdefault('monster_type', 'human')  # 'human' or 'flying'
        self.properties.setdefault('monster_id', 0)
        self.properties.setdefault('health', 100)
        self.properties.setdefault('damage', 10)

        # --- Wake / AI behaviour ---
        self.properties.setdefault('triggered', False)
        self.properties.setdefault('wake_on_sight', True)
        self.properties.setdefault('awake', False)

        # --- Patrol behaviour (requires a PathNode as patrol_target) ---
        # When patrol=True and patrol_target names a valid PathNode, the
        # monster will walk toward that node whenever the player is NOT in
        # sight range. Sight/chase always overrides patrol.
        self.properties.setdefault('patrol', False)
        self.properties.setdefault('patrol_target', '')
        # patrol_mode: how the monster traverses a chain of nodes:
        #   'loop'      — A→B→C→A→B→C (wraps via first node in chain)
        #   'ping_pong' — A→B→C→B→A→B→C (reverses at each dead-end)
        #   'once'      — A→B→C  then holds at the last node
        self.properties.setdefault('patrol_mode', 'loop')

        # --- Sprite variant (alternate skin) ---
        # '<None>' = base sprites in assets/sprites/monsters/<type>/
        # 'variant1' etc = assets/sprites/monsters/<type>/variant1/
        self.properties.setdefault('variant', '<None>')

        # --- Set default sprite dimensions based on monster_type ---
        from engine.monster_constants import MONSTER_SPRITE_SIZES, MONSTER_SPRITE_SIZE_DEFAULT
        mtype = self.properties.get('monster_type', 'human')
        default_w, default_h = MONSTER_SPRITE_SIZES.get(mtype, MONSTER_SPRITE_SIZE_DEFAULT)
        self.properties.setdefault('sprite_width', default_w)
        self.properties.setdefault('sprite_height', default_h)

    @classmethod
    def _get_project_root(cls) -> str:
        """Return the project root path, computed once per process."""
        if cls._cached_project_root is None:
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                cls._cached_project_root = os.path.abspath(
                    os.path.join(script_dir, os.pardir))
            except Exception:
                cls._cached_project_root = os.getcwd()
        return cls._cached_project_root

    @classmethod
    def _path_exists_cached(cls, abs_path: str) -> bool:
        """os.path.isfile wrapper that memoises the result. Call
        invalidate_sprite_caches() if sprite files change on disk at runtime."""
        cached = cls._custom_path_exists_cache.get(abs_path)
        if cached is None:
            cached = os.path.isfile(abs_path)
            cls._custom_path_exists_cache[abs_path] = cached
        return cached

    @classmethod
    def _get_default_paths(cls, mtype: str, variant: str):
        """Return (idle, dead, shoot) default sprite paths for this
        (monster_type, variant) pair. Filesystem stats happen only on the
        first call per unique key; results are cached thereafter."""
        key = (mtype, variant)
        cached = cls._default_path_cache.get(key)
        if cached is not None:
            return cached

        project_root = cls._get_project_root()

        base_idle  = f"assets/sprites/monsters/{mtype}/idle.png"
        base_dead  = f"assets/sprites/monsters/{mtype}/dead.png"
        base_shoot = f"assets/sprites/monsters/{mtype}/shoot.png"

        if variant and variant != '<None>':
            var_idle  = f"assets/sprites/monsters/{mtype}/{variant}/idle.png"
            var_dead  = f"assets/sprites/monsters/{mtype}/{variant}/dead.png"
            var_shoot = f"assets/sprites/monsters/{mtype}/{variant}/shoot.png"
            default_idle  = var_idle  if cls._path_exists_cached(os.path.join(project_root, var_idle))  else base_idle
            default_dead  = var_dead  if cls._path_exists_cached(os.path.join(project_root, var_dead))  else base_dead
            default_shoot = var_shoot if cls._path_exists_cached(os.path.join(project_root, var_shoot)) else base_shoot
        else:
            default_idle  = base_idle
            default_dead  = base_dead
            default_shoot = base_shoot

        # Verify default dead/shoot files exist; fall back to idle if not
        if not cls._path_exists_cached(os.path.join(project_root, default_dead)):
            default_dead = default_idle
        if not cls._path_exists_cached(os.path.join(project_root, default_shoot)):
            default_shoot = default_idle

        result = (default_idle, default_dead, default_shoot)
        cls._default_path_cache[key] = result
        return result

    @staticmethod
    def _resolve_sprite(custom_path: str, default_path: str, project_root: str) -> str:
        """
        Return *custom_path* when the file exists on disk, otherwise return
        *default_path*.  Falls back silently — the caller guarantees the
        default path is the safest possible choice.

        Uses the class-level existence cache so repeated calls don't re-stat.
        """
        if custom_path:
            abs_custom = os.path.join(project_root, custom_path)
            if Monster._path_exists_cached(abs_custom):
                return custom_path
            print(f"[Monster] Custom sprite not found, using default: {custom_path}")
        return default_path

    def get_render_snapshot(self):
        """Return a lightweight dictionary snapshot for the renderer."""
        return {
            'pos': list(self.pos),                         # copy list
            'dead': self.properties.get('dead', False),
            'is_shooting': self.properties.get('is_shooting', False),
            'monster_type': self.properties.get('monster_type', 'human'),
            'variant': self.properties.get('variant', '<None>'),
            'sprite_width': self.properties.get('sprite_width', 128),
            'sprite_height': self.properties.get('sprite_height', 128),
            'custom_idle': self.properties.get('custom_idle', ''),
            'custom_shoot': self.properties.get('custom_shoot', ''),
            'custom_dead': self.properties.get('custom_dead', ''),
        }

    def get_sprite_path(self) -> str:
        """
        Return the sprite path for the current monster type and state.
        Priority: dead > shooting > idle.

        When a variant is set (anything other than '<None>'), the sprite
        subfolder changes:
          base:    assets/sprites/monsters/<type>/<frame>.png
          variant: assets/sprites/monsters/<type>/<variant>/<frame>.png
        If the variant file is missing on disk, falls back to the base path.

        Custom sprites set via the Customise dialog are tried first.
        Any missing custom file is logged once and falls back to the
        appropriate default sprite for this monster_type automatically.

        PERF: this used to do up to 5 os.path.isfile() calls every time, on a
        hot path (called per-Monster per-frame). The default path resolution
        is now memoised in the class-level _default_path_cache keyed by
        (monster_type, variant), and custom-path existence is memoised in
        _custom_path_exists_cache.
        """
        mtype       = self.properties.get('monster_type', 'human')
        variant     = self.properties.get('variant', '<None>')
        is_dead     = self.properties.get('dead', False)
        is_shooting = self.properties.get('is_shooting', False)

        project_root = Monster._get_project_root()
        default_idle, default_dead, default_shoot = Monster._get_default_paths(mtype, variant)

        if is_dead:
            return self._resolve_sprite(
                self.properties.get('custom_dead', ''), default_dead, project_root)
        elif is_shooting:
            return self._resolve_sprite(
                self.properties.get('custom_shoot', ''), default_shoot, project_root)
        else:
            return self._resolve_sprite(
                self.properties.get('custom_idle', ''), default_idle, project_root)

    def get_instance_pixmap(self):
        """
        Return the correct pixmap for the current alive/dead state.

        The cache is keyed by the full sprite path returned by get_sprite_path(),
        which already encodes both monster type AND state (idle vs dead).
        This means alive and dead sprites are cached independently, so setting
        'dead' = True on a monster that was previously rendered alive will
        correctly switch to dead.png on the next frame without a stale cache hit.
        """
        sprite_path = self.get_sprite_path()

        # Return from cache if available
        if sprite_path in Monster._subtype_sprites:
            return Monster._subtype_sprites[sprite_path]

        # Load from disk
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
            abs_path = os.path.join(project_root, sprite_path)
            if os.path.exists(abs_path):
                pixmap = QPixmap(abs_path)
                if not pixmap.isNull():
                    Monster._subtype_sprites[sprite_path] = pixmap
                    return pixmap
        except Exception:
            pass

        # Fallback: dark red square so death is still visually obvious
        if self.properties.get('dead', False):
            fallback = QPixmap(128, 128)
            fallback.fill(QColor(128, 0, 0))
            return fallback

        return super().get_instance_pixmap()

    def get_icon_pixmap(self):
        """Return a generic small monster icon for 2D views."""
        icon_path = "assets/sprites/monster.png"   # 60×60 icon
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
            abs_path = os.path.join(project_root, icon_path)
            if os.path.exists(abs_path):
                pix = QPixmap(abs_path)
                if not pix.isNull():
                    return pix
        except Exception:
            pass
        # Fallback: scale the full sprite down
        # Use get_instance_pixmap() instead of super().get_icon_pixmap()
        return self.get_instance_pixmap()

    @classmethod
    def clear_sprite_cache(cls):
        """Clear all sprite-related caches."""
        cls._subtype_sprites.clear()
        cls._default_path_cache.clear()
        cls._custom_path_exists_cache.clear()
        # Invalidate the base class cache for Monster’s default pixmap
        if cls.__name__ in Thing._pixmap_cache:
            del Thing._pixmap_cache[cls.__name__]

    @classmethod
    def invalidate_icon_cache(cls):
        """Clear the 2D icon cache (called after sprite_2d changes)."""
        cls._icon_cache.clear()


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
        'gun2': 'assets/sprites/gun2.png',
        'cig': 'assets/sprites/cig.png'
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
        return self.properties.get('item_type') in ['gun1', 'gun2', 'cig']
    
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
        except Exception:
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


class LogicCommand(Thing):
    """Runs a console command when its RunCommand input fires.

    Lets map logic drive the console via the I/O system: wire a trigger brush's
    OnTrigger (or any output) to this entity's RunCommand input. The command
    string comes from the connection's parameter, or falls back to the entity's
    'command' property when the parameter is blank. Example command: "cam 2".
    """
    pixmap_path = "assets/sprites/logic_command.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_command'
        self.properties.setdefault('command', 'cam')
        self.properties.setdefault('disabled', False)

    def get_instance_pixmap(self):
        pix = super().get_instance_pixmap()
        if pix:
            return pix
        return LogicGate.get_pixmap()


class LevelChanger(Thing):
    """Entity that loads a new level when triggered."""
    pixmap_path = "assets/sprites/levelchanger.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'levelchanger'
        self.properties.setdefault('target_map', 'maps/Simple_Map_Test.json')
        self.properties.setdefault('delay', 0.0)
        self.properties.setdefault('fade_time', 0.5)
        self.properties.setdefault('show_radius', False)
        self.properties.setdefault('radius', 128.0)
        self.properties.setdefault('usable', False)
        
        # Store direct reference to MainWindow for reliable level changing
        self._main_window = None
        try:
            from PyQt5.QtWidgets import QApplication
            for widget in QApplication.topLevelWidgets():
                if widget.__class__.__name__ == 'MainWindow':
                    self._main_window = widget
                    break
        except Exception:
            pass

    def on_input(self, input_name: str, parameter: str = ""):
        """Called by I/O system and by ent_fire."""
        entity_name = self.properties.get('name', 'LevelChanger')
        map_name = (parameter or self.properties.get('target_map', '')).strip()

        debug_log("IO", f"LevelChanger '{entity_name}' on_input('{input_name}'), map='{map_name}'")

        if input_name == "ChangeLevel":
            return self.change_level(map_name)

        debug_log("Warning", f"LevelChanger '{entity_name}': unknown input '{input_name}'")
        return False

    def change_level(self, parameter: str = ""):
        """Load a new map when triggered."""

        target_map = parameter.strip() if parameter else self.properties.get('target_map', '').strip()

        if not target_map:
            debug_log("Error", "LevelChanger has no target_map and no parameter was provided!")
            return False

        if not target_map.lower().endswith('.json'):
            target_map += '.json'

        # ENFORCE MAPS FOLDER: Prepend maps/ if not already present
        if not (target_map.startswith('maps/') or target_map.startswith('maps\\')):
            target_map = f"maps/{target_map}"

        debug_log("IO", f"LevelChanger target resolved → '{target_map}'")

        # Get MainWindow reference
        main_window = getattr(self, '_main_window', None)

        if not main_window:
            try:
                from PyQt5.QtWidgets import QApplication
                for w in QApplication.topLevelWidgets():
                    if w.__class__.__name__ == 'MainWindow':
                        main_window = w
                        self._main_window = w
                        break
            except Exception as e:
                debug_log("Error", f"LevelChanger QApplication lookup failed: {e}")

        if not main_window:
            debug_log("Error", "LevelChanger could not find MainWindow!")
            return False

        # Use signal instead of direct call
        if hasattr(main_window, 'load_level_signal'):
            try:
                main_window.load_level_signal.emit(target_map)
                debug_log("IO", f"LevelChanger emitted load_level_signal('{target_map}')")
                return True
            except Exception as e:
                debug_log("Error", f"LevelChanger failed to emit signal: {e}")
                import traceback
                traceback.print_exc()
                return False
        else:
            debug_log("Error", "MainWindow has no load_level_signal!")
            return False


# =============================================================================
# AI / NAVIGATION ENTITIES
# =============================================================================

class PathNode(Thing):
    """
    Navigation waypoint for monster AI patrol behaviour and general-purpose
    path chains (mover waypoints, cinematic cameras, teleport destinations,
    spawn points).

    Combines the role of Source's `path_corner` (a point monsters navigate to)
    with `info_node` (a hint that says "this is a valid AI position").

    Properties:
      - radius (float, world units): monsters try to stay within this distance
        of the node once they arrive. Shown as a preview circle in 2D views
        when `show_radius` is toggled on, identical in behaviour to how Light
        and Speaker expose their influence radius.
      - show_radius (bool): toggle the preview circle in the 2D viewport.
      - affects_type (str): which monster types can use this node as a patrol
        target. One of 'human', 'flying', 'both'. Monsters whose monster_type
        does not match are rejected at pathfinding time (and the rejection is
        logged to the debug console under the 'Pathfinding' category).
      - next_node (str): name of the next PathNode in the patrol chain.
        Leave empty for a dead-end node (the monster will hold position
        or reverse direction depending on the monster's patrol_mode).
      - wait_time (float, seconds): how long a monster pauses at this node
        before moving to next_node. 0 = no wait (pass through immediately).
      - speed (float, multiplier): speed factor applied while an entity is
        heading toward this node.  1.0 = normal speed, 0.5 = half speed,
        2.0 = double, etc.  (Renamed from legacy 'patrol_speed'.)

    PathNodes are invisible at runtime — they are editor-only aids that the
    monster AI consults during _update_monsters in the logic thread.
    """
    # No billboard sprite — PathNodes render as small orange cubes in 3D
    # and as filled orange squares in 2D viewports.
    pixmap_path = None

    AFFECTS_TYPES = ('human', 'flying', 'both')

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'path_node'
        self.properties.setdefault('radius', 256.0)
        self.properties.setdefault('show_radius', False)
        self.properties.setdefault('affects_type', 'both')
        self.properties.setdefault('next_node', '')
        self.properties.setdefault('wait_time', 0.0)
        self.properties.setdefault('speed', 1.0)

        # Migrate legacy key transparently on load
        if 'patrol_speed' in self.properties and 'speed' not in self.properties:
            self.properties['speed'] = self.properties.pop('patrol_speed')
        elif 'patrol_speed' in self.properties:
            self.properties.pop('patrol_speed', None)

    def get_radius(self) -> float:
        """Radius in world units — read by the 2D preview and the monster AI."""
        try:
            return float(self.properties.get('radius', 256.0))
        except (TypeError, ValueError):
            return 256.0

    def get_affects_type(self) -> str:
        """Normalised affects_type string. Invalid values fall back to 'both'."""
        val = str(self.properties.get('affects_type', 'both')).lower().strip()
        if val not in PathNode.AFFECTS_TYPES:
            return 'both'
        return val

    def get_wait_time(self) -> float:
        """Wait time in seconds at this node."""
        try:
            return max(0.0, float(self.properties.get('wait_time', 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def get_speed(self) -> float:
        """Speed multiplier for entities approaching this node."""
        try:
            # Accept legacy 'patrol_speed' key transparently
            raw = self.properties.get('speed',
                    self.properties.get('patrol_speed', 1.0))
            return max(0.01, float(raw))
        except (TypeError, ValueError):
            return 1.0

    # Keep old name as alias so MonsterAI still works without changes
    get_patrol_speed = get_speed

    def get_next_node_name(self) -> str:
        """Return the name of the next node in the chain, or ''."""
        return str(self.properties.get('next_node', '') or '').strip()

    def accepts_monster_type(self, monster_type: str) -> bool:
        """Return True if a monster of *monster_type* may patrol to this node."""
        affects = self.get_affects_type()
        if affects == 'both':
            return True
        return affects == (monster_type or '').lower()


# =============================================================================
# CINEMATIC / SPAWNING ENTITIES
# =============================================================================

class LogicCamera(Thing):
    """
    Cinematic camera that lerps along a PathNode chain when triggered.
    Player input is suppressed for the duration of the sequence.
    """
    pixmap_path = "assets/sprites/logic_camera.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_camera'
        # Name of the first PathNode in the chain
        self.properties.setdefault('path_target', '')
        # World-units per second along the chain
        self.properties.setdefault('speed', 200.0)
        # If set, overrides the player's FOV during the sequence
        self.properties.setdefault('fov_override', 0.0)
        # If True the camera looks at the *next* node; if False it
        # follows the tangent of the spline (forward direction).
        self.properties.setdefault('look_ahead', True)


class LogicSpawner(Thing):
    """
    Instantiates a new entity at a PathNode's coordinates when triggered.
    """
    pixmap_path = "assets/sprites/logic_spawner.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_spawner'
        # Entity class name to spawn (key into ENTITY_TYPES)
        self.properties.setdefault('spawn_type', 'Monster')
        # PathNode whose position is used as the spawn point
        self.properties.setdefault('target_node', '')
        # Max entities this spawner will ever create (0 = unlimited)
        self.properties.setdefault('max_spawn', 0)
        # Extra properties merged into the spawned entity
        self.properties.setdefault('spawn_properties', {})


# =============================================================================
# PORTAL ENTITY
# =============================================================================

class Portal(Thing):
    """
    A rectangular world portal for non-Euclidean environmental design,
    inspired by Prey (2006).

    Two Portal entities are linked by name via the portal_target property.
    When the player looks through Portal A they see the world rendered from
    the perspective of Portal B (using stencil-buffer masking in the
    renderer).  Walking through A teleports the player to B with position,
    velocity, and view-angle all correctly transformed by the rotational
    delta between the two portal normals.

    Properties
    ----------
    portal_target  (str)   Name of the paired Portal entity.
    width          (float) Aperture width in world units.  Default 128.
    height         (float) Aperture height in world units.  Default 256.
    rotation       (list)  [yaw_degrees, pitch_degrees, roll_degrees].
                           All three orient the aperture: pitch tilts it off
                           vertical (pitch = ±90 gives a floor/ceiling portal)
                           and roll spins it about its facing axis.  The view
                           through the portal and the teleport both honour the
                           full orientation.
    active         (bool)  When False the portal acts as a solid wall.
                           Toggle at runtime via Enable/Disable/Toggle inputs.
    color          (list)  [r, g, b] 0-255 rim/glow tint.  Default white.
    show_rim       (bool)  Draw outline.

    I/O outputs
    -----------
    OnPlayerEnter  Fired once per transit when the player passes through.
    OnActivate     Fired when the portal is enabled.
    OnDeactivate   Fired when the portal is disabled.

    I/O inputs
    ----------
    Enable         Sets active = True  and fires OnActivate.
    Disable        Sets active = False and fires OnDeactivate.
    Toggle         Flips the active state.

    Non-Euclidean design tips
    -------------------------
    * Infinite corridor  — face both portals toward each other in a short
      hallway.
    * Floor/ceiling drop — pitch a portal to ±90° so it lies flat; the player
      falls in and is flung out of the paired portal along its normal.  (Gravity
      itself stays world-down — the transit reorients position, velocity and
      view, not the world's up-axis.)
    * Loop room          — four portals forming a closed square so exiting any
      wall re-enters the opposite one.
    * Size distortion    — make the apertures different sizes; the scene
      appears scaled when viewed through the smaller end.
    """

    pixmap_path = "assets/sprites/portal.png"

    DEFAULT_WIDTH  = 128.0
    DEFAULT_HEIGHT = 256.0

    # Transit cooldown prevents rapid back-and-forth oscillation (seconds).
    TRANSIT_COOLDOWN = 0.5

    # Duration of a full fade-in or fade-out transition (seconds).
    FADE_DURATION = 0.35

    # Minimum clearance (world units) pushed along the destination normal when a
    # body emerges, so it never spawns embedded in the wall behind portal B.
    # Call sites add the body's own half-depth on top of this.
    EXIT_CLEARANCE = 8.0

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'portal'

        # Link to the other portal half
        self.properties.setdefault('portal_target', '')

        # Aperture geometry
        self.properties.setdefault('width',  self.DEFAULT_WIDTH)
        self.properties.setdefault('height', self.DEFAULT_HEIGHT)

        # Orientation — [yaw_degrees, pitch_degrees, roll_degrees]
        self.properties.setdefault('rotation', [0.0, 0.0, 0.0])
        self.properties.setdefault('angle', 0.0)

        # Runtime state
        self.properties.setdefault('active', True)

        # Portal directionality: controls which way the portal renders
        # "forward"  = This portal sees out of its target (one-way)
        # "reverse"  = Target portal sees out of this one (one-way)
        # "both"     = Both directions render (default)
        self.properties.setdefault('portal_direction', 'both')

        # Visual rim
        self.properties.setdefault('color',    [255, 255, 255])
        self.properties.setdefault('show_rim', True)

        # Parenting: attach this portal to a mover brush by name.
        # When parent_mover is non-empty the logic thread moves this
        # portal to (mover.pos + parent_offset) every tick during play.
        self.properties.setdefault('parent_mover', '')
        self.properties.setdefault('parent_offset', [0.0, 0.0, 0.0])

        # FIX: New properties for local position and yaw offset (supports rotation)
        self.properties.setdefault('parent_local_pos', None)   # None = use parent_offset (legacy)
        self.properties.setdefault('parent_local_yaw', 0.0)

        # Internal transit cooldown (not saved to map file)
        self._transit_cooldown = 0.0
        # Last signed distance of player from this portal's plane (for edge detection)
        self._last_player_side = None

        # Fade state (runtime only, never serialised to map JSON).
        # _fade_alpha: current rendered opacity 0.0 (invisible) → 1.0 (fully visible)
        # _fade_target: opacity we are animating toward
        _start_active = self.is_active()
        self._fade_alpha: float = 1.0 if _start_active else 0.0
        self._fade_target: float = self._fade_alpha

    # ── Geometry helpers ──────────────────────────────────────────────────────

    def get_width(self) -> float:
        try:
            return max(16.0, float(self.properties.get('width', self.DEFAULT_WIDTH)))
        except (TypeError, ValueError):
            return self.DEFAULT_WIDTH

    def get_height(self) -> float:
        try:
            return max(16.0, float(self.properties.get('height', self.DEFAULT_HEIGHT)))
        except (TypeError, ValueError):
            return self.DEFAULT_HEIGHT

    def _rotation_component(self, index: int) -> float:
        rot = self.properties.get('rotation', [0.0, 0.0, 0.0])
        try:
            return float(rot[index])
        except (TypeError, ValueError, IndexError):
            return 0.0

    def get_yaw_radians(self) -> float:
        """Return the portal's yaw (facing direction) in radians."""
        return math.radians(self._rotation_component(0))

    def get_yaw_degrees(self) -> float:
        """Return the portal's yaw (facing direction) in degrees."""
        return self._rotation_component(0)

    def get_pitch_radians(self) -> float:
        """Pitch in radians. Non-zero pitch tilts the aperture off vertical
        (e.g. a floor/ceiling portal uses pitch = ±90)."""
        return math.radians(self._rotation_component(1))

    def get_pitch_degrees(self) -> float:
        return self._rotation_component(1)

    def get_roll_radians(self) -> float:
        """Roll in radians (spin about the portal's facing axis)."""
        return math.radians(self._rotation_component(2))

    def get_roll_degrees(self) -> float:
        return self._rotation_component(2)

    def set_yaw_degrees(self, yaw: float) -> None:
        self.properties['rotation'][0] = yaw
        self.properties['angle'] = yaw

    def get_basis(self):
        """
        Return the portal's orthonormal frame as ``(right, up, normal)`` where
        each axis is a 3-tuple of floats.  Built from yaw/pitch/roll via
        ``R = Ry(yaw) @ Rx(pitch) @ Rz(roll)`` applied to the base frame
        right=(1,0,0), up=(0,1,0), normal=(0,0,1).

        With pitch = roll = 0 this reduces exactly to the historical
        horizontal-facing portal: normal = (sin yaw, 0, cos yaw),
        right = (cos yaw, 0, -sin yaw), up = (0, 1, 0).  ``normal`` points out
        of the front face (toward the viewer that approaches the portal).
        """
        yaw = self.get_yaw_radians()
        pitch = self.get_pitch_radians()
        roll = self.get_roll_radians()
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cr, sr = math.cos(roll), math.sin(roll)
        right  = (cy * cr + sy * sp * sr,  cp * sr,  -sy * cr + cy * sp * sr)
        up     = (-cy * sr + sy * sp * cr, cp * cr,   sy * sr + cy * sp * cr)
        normal = (sy * cp,                 -sp,       cy * cp)
        return right, up, normal

    def set_parent_local_transform(self, mover_pos, mover_yaw) -> None:
        """Compute and store local position and yaw offset from the mover's current transform."""
        self.properties['parent_local_pos'] = [
            self.pos[0] - mover_pos[0],
            self.pos[1] - mover_pos[1],
            self.pos[2] - mover_pos[2]
        ]
        self.properties['parent_local_yaw'] = self.get_yaw_degrees() - mover_yaw

    def get_normal(self):
        """
        Return the outward-facing unit normal of this portal as a 3-tuple
        (x, y, z).  The normal points toward the viewer side of the portal
        (the face the player approaches from).  Supports full yaw/pitch/roll
        orientation, so floor/ceiling and tilted portals report a normal that
        actually points up/down.
        """
        return self.get_basis()[2]

    def get_corners_world(self):
        """
        Return the four world-space corners of the portal aperture as a list
        of [x, y, z] triples, winding counter-clockwise when viewed from the
        front:  [bottom-left, bottom-right, top-right, top-left].
        Used by the renderer to build the stencil mask quad.
        """
        px, py, pz = self.pos
        w2 = self.get_width()  / 2.0
        h2 = self.get_height() / 2.0
        (rx, ry, rz), (ux, uy, uz), _ = self.get_basis()

        return [
            [px - rx * w2 - ux * h2, py - ry * w2 - uy * h2, pz - rz * w2 - uz * h2],
            [px + rx * w2 - ux * h2, py + ry * w2 - uy * h2, pz + rz * w2 - uz * h2],
            [px + rx * w2 + ux * h2, py + ry * w2 + uy * h2, pz + rz * w2 + uz * h2],
            [px - rx * w2 + ux * h2, py - ry * w2 + uy * h2, pz - rz * w2 + uz * h2],
        ]

    # ── Shared portal link transform ──────────────────────────────────────────
    # These two methods are the single source of truth for how space maps from
    # this portal to its destination.  BOTH the renderer's virtual camera and
    # the logic thread's teleport call them, so the view you look *through* and
    # the frame you *teleport into* can never disagree (previously each computed
    # its own, subtly different, rotation).
    #
    # The map is the standard portal transform  M_dest · flip · M_self^-1  where
    # flip is a 180° rotation about the aperture's up axis — i.e. mirror the
    # right and normal local components.  A point just past this portal's plane
    # therefore lands just in front of the destination, and a velocity heading
    # into this portal emerges heading out of the destination.

    def _local_of(self, x: float, y: float, z: float):
        """World point → (right, up, normal) coordinates in this portal's frame."""
        r, u, n = self.get_basis()
        dx = x - self.pos[0]
        dy = y - self.pos[1]
        dz = z - self.pos[2]
        return (dx * r[0] + dy * r[1] + dz * r[2],
                dx * u[0] + dy * u[1] + dz * u[2],
                dx * n[0] + dy * n[1] + dz * n[2])

    def map_point(self, dest, x: float, y: float, z: float):
        """Map a world-space point through this portal to ``dest``."""
        lr, lu, ln = self._local_of(x, y, z)
        lr, ln = -lr, -ln  # 180° about up
        r, u, n = dest.get_basis()
        return (dest.pos[0] + lr * r[0] + lu * u[0] + ln * n[0],
                dest.pos[1] + lr * r[1] + lu * u[1] + ln * n[1],
                dest.pos[2] + lr * r[2] + lu * u[2] + ln * n[2])

    def map_direction(self, dest, x: float, y: float, z: float):
        """Map a world-space direction/velocity through this portal to ``dest``
        (rotation only, no translation)."""
        r, u, n = self.get_basis()
        lr = x * r[0] + y * r[1] + z * r[2]
        lu = x * u[0] + y * u[1] + z * u[2]
        ln = x * n[0] + y * n[1] + z * n[2]
        lr, ln = -lr, -ln
        r2, u2, n2 = dest.get_basis()
        return (lr * r2[0] + lu * u2[0] + ln * n2[0],
                lr * r2[1] + lu * u2[1] + ln * n2[1],
                lr * r2[2] + lu * u2[2] + ln * n2[2])

    def contains_point(self, x: float, y: float, z: float, margin: float = 0.0) -> bool:
        """True when the world point projects inside the aperture rectangle.

        ``margin`` grows the rectangle on every side — pass the transiting
        body's radius so something is only considered "through" once its centre
        clears the frame, avoiding half-in/half-out pops.  Uses the full
        yaw/pitch/roll basis so tilted portals test correctly.
        """
        lr, lu, _ = self._local_of(x, y, z)
        hw = self.get_width()  / 2.0 + margin
        hh = self.get_height() / 2.0 + margin
        return abs(lr) <= hw and abs(lu) <= hh

    def tick_fade(self, delta: float) -> None:
        """Advance _fade_alpha toward _fade_target.  Called every logic tick."""
        if self._fade_alpha == self._fade_target:
            return
        step = delta / max(self.FADE_DURATION, 0.001)
        if self._fade_target > self._fade_alpha:
            self._fade_alpha = min(self._fade_target, self._fade_alpha + step)
        else:
            self._fade_alpha = max(self._fade_target, self._fade_alpha - step)

    def is_active(self) -> bool:
        v = self.properties.get('active', True)
        if isinstance(v, bool):
            return v
        return str(v).lower() not in ('false', '0', 'no')

    def get_parent_local_pos(self) -> list:
        """Return local position relative to the parent mover's origin.
        Uses parent_local_pos when set; falls back to legacy parent_offset.
        """
        local = self.properties.get('parent_local_pos')
        if local is not None:
            return local[:3]
        return self.properties.get('parent_offset', [0.0, 0.0, 0.0])

    def get_parent_local_yaw(self) -> float:
        return float(self.properties.get('parent_local_yaw', 0.0))

    # ── I/O interface ─────────────────────────────────────────────────────────
    # Enable / Disable / Toggle are handled by the registered portal input
    # handlers (see editor/io_handlers.py: portal_enable / portal_disable /
    # portal_toggle), which fire the declared OnEnabled / OnDisabled / OnToggled
    # outputs and drive the fade via _fade_target. No on_input() override is
    # defined here so the runtime IOManager and the console 'ent_fire' path stay
    # consistent (both dispatch through those handlers).



# =============================================================================
# KEY/VALUE STORE ENTITY
# =============================================================================

class LogicKeyValueStore(Thing):
    """
    A persistent key/value store that survives level transitions.

    Stores up to 25 key/value pairs (strings). Other entities can query
    values via I/O inputs, and values can be set/read during gameplay.

    The store is identified by its store_name — if a store with the same name
    exists in the destination level, its values are preserved across
    the transition. This allows cross-level state like quest progress,
    puzzle solutions, or flags to persist.

    Properties:
      - store_name (str): Unique identifier for this store (defaults to entity name).
                          Levels that share the same store_name will sync values.
      - initial_data (dict): Up to 25 key/value pairs set at design time.

    I/O Inputs:
      - SetValue:    Set a key/value pair. Parameter format: "key=value"
      - GetValue:    Fire OnValueRead with the value of the given key as parameter.
      - ClearKey:    Remove a single key.
      - ClearAll:    Remove all keys.
      - CopyFrom:    Copy all keys from another LogicKeyValueStore by name.
      - Increment:   Treat value as int and increment. Parameter: "key,amount"
      - Decrement:   Treat value as int and decrement. Parameter: "key,amount"

    I/O Outputs:
      - OnValueSet:      Fired when any key is set (parameter = "key=value")
      - OnValueRead:     Fired by GetValue (parameter = value, or "<missing>")
      - OnKeyCleared:    Fired when a key is removed (parameter = key name)
      - OnStoreFull:     Fired when trying to add a 26th key
      - OnKeyNotFound:   Fired when GetValue targets a missing key
    """
    pixmap_path = "assets/sprites/logic_keyvalue.png"

    # Class-level registry of persistent stores across level transitions.
    # Keyed by store_name, stores the dict of values. Survives as long as
    # the Python process lives (i.e., across level loads within one session).
    _persistent_registry = {}

    MAX_PAIRS = 25

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_keyvalue'
        self.properties.setdefault('store_name', self.properties.get('name', ''))

        # initial_data holds the designer-specified defaults.
        # Stored as a dict; serialised to/from JSON as needed.
        self.properties.setdefault('initial_data', {})

        # Runtime data — merged from persistent registry + initial_data
        self._runtime_data = {}
        self._sync_from_persistent()

    def _sync_from_persistent(self):
        """Load values from the persistent registry, falling back to initial_data."""
        store_name = self.properties.get('store_name', '')
        if store_name and store_name in LogicKeyValueStore._persistent_registry:
            self._runtime_data = dict(LogicKeyValueStore._persistent_registry[store_name])
        else:
            # Deep copy initial_data so we don't mutate the property directly
            initial = self.properties.get('initial_data', {})
            if isinstance(initial, dict):
                self._runtime_data = {k: str(v) for k, v in initial.items()}
            else:
                self._runtime_data = {}

    def _sync_to_persistent(self):
        """Write current runtime values back to the persistent registry."""
        store_name = self.properties.get('store_name', '')
        if store_name:
            LogicKeyValueStore._persistent_registry[store_name] = dict(self._runtime_data)

    def set_value(self, key, value):
        """Set a key/value pair. Returns False if store is full (25 keys)."""
        key = str(key).strip()
        value = str(value)
        if not key:
            return False

        if key in self._runtime_data:
            self._runtime_data[key] = value
            self._sync_to_persistent()
            return True

        if len(self._runtime_data) >= self.MAX_PAIRS:
            return False

        self._runtime_data[key] = value
        self._sync_to_persistent()
        return True

    def get_value(self, key, default="<missing>"):
        """Get the value for a key, or default if not present."""
        return self._runtime_data.get(str(key).strip(), default)

    def clear_key(self, key):
        """Remove a single key. Returns True if the key existed."""
        key = str(key).strip()
        if key in self._runtime_data:
            del self._runtime_data[key]
            self._sync_to_persistent()
            return True
        return False

    def clear_all(self):
        """Remove all keys."""
        self._runtime_data.clear()
        self._sync_to_persistent()

    def get_all_pairs(self):
        """Return a copy of all key/value pairs."""
        return dict(self._runtime_data)

    def get_pair_count(self):
        """Return the number of stored pairs."""
        return len(self._runtime_data)

    def copy_from(self, other_store_name):
        """Copy all key/value pairs from another store by name."""
        if other_store_name in LogicKeyValueStore._persistent_registry:
            source = LogicKeyValueStore._persistent_registry[other_store_name]
            # Only copy up to MAX_PAIRS total
            for k, v in source.items():
                if len(self._runtime_data) >= self.MAX_PAIRS and k not in self._runtime_data:
                    break
                self._runtime_data[k] = v
            self._sync_to_persistent()
            return True
        return False

    def increment(self, key, amount=1):
        """Treat value as integer and increment. Returns new value or 0 if not numeric."""
        key = str(key).strip()
        try:
            current = int(self._runtime_data.get(key, "0"))
        except ValueError:
            current = 0
        new_val = current + amount
        self._runtime_data[key] = str(new_val)
        self._sync_to_persistent()
        return new_val

    def decrement(self, key, amount=1):
        """Treat value as integer and decrement."""
        return self.increment(key, -amount)

    def to_dict(self):
        """Override to_dict to include runtime data in the persistent registry."""
        # Ensure persistent registry is up to date before serialising
        self._sync_to_persistent()
        result = super().to_dict()
        # Also embed current runtime data so save files are self-contained
        result['runtime_data'] = dict(self._runtime_data)
        return result

    @staticmethod
    def from_dict(data):
        """Override from_dict to restore runtime data from save file."""
        thing = Thing.from_dict(data)
        if thing and isinstance(thing, LogicKeyValueStore):
            # Restore runtime data from save file if present
            runtime = data.get('runtime_data', {})
            if isinstance(runtime, dict):
                thing._runtime_data = {k: str(v) for k, v in runtime.items()}
                thing._sync_to_persistent()
            else:
                thing._sync_from_persistent()
        return thing


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
    'LogicRelay': LogicRelay,
    'LogicGate': LogicGate,
    'LogicTimer': LogicTimer,
    'LogicCommand': LogicCommand,
    'LevelChanger': LevelChanger,
    'PathNode': PathNode,
    'LogicCamera': LogicCamera,
    'LogicSpawner': LogicSpawner,
    'Portal': Portal,
    'LogicKeyValueStore': LogicKeyValueStore,
}

# Categories for editor UI
ENTITY_CATEGORIES = {
    'Gameplay': ['PlayerStart', 'Monster', 'Pickup', 'LevelChanger'],
    'Environment': ['Light', 'Speaker', 'Model', 'Portal'],
    'Logic': ['LogicRelay', 'LogicGate', 'LogicTimer', 'LogicCommand', 'LogicCamera', 'LogicSpawner', 'LogicKeyValueStore'],
    'AI': ['PathNode'],
}