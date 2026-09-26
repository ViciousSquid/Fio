"""
This module defines all placeable entities (Things) in the level editor.
Each Thing type has associated I/O definitions (inputs/outputs) for
the event-driven entity communication system.
"""

import copy
import os
import math
import uuid
import importlib

from engine.portal_transform import (
    basis_from_rotation as _portal_basis_from_rotation,
    map_point as _portal_map_point,
    map_direction as _portal_map_direction,
    corners as _portal_corners,
    contains_point as _portal_contains_point,
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtCore import Qt
import ast

from . import state_values as _sv


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

    # Update counters for all Thing subclasses (including core entities
    # defined outside this module, e.g. Prop).
    _load_core_entity_types()
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

    # Editor property classification.
    #
    # Properties listed here are shown on the main Properties tab.
    # Properties listed in EDITOR_ADVANCED_PROPERTIES are shown on
    # the Advanced tab.
    #
    # Subclasses should explicitly opt properties into Advanced.
    # Everything else should remain available to the main Properties
    # editor unless handled by a specialised widget.
    EDITOR_PRIMARY_PROPERTIES = ()
    EDITOR_ADVANCED_PROPERTIES = ()

    def __init__(self, pos=None, properties=None):
        self.pos = pos if pos is not None else [0, 0, 0]
        self.properties = properties if properties is not None else {}
        self.properties.setdefault('type', self.__class__.__name__.lower())
        self.properties.setdefault('io_enabled', True)
        
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

    def _pixmap_for_path(self, sprite_path):
        """Cached QPixmap for an authored sprite path (project-relative or absolute).

        Returns None when the file is missing or unreadable. Shared by any
        entity whose 2D icon comes from an authored path rather than its class.
        """
        cache_key = ('sprite_path', sprite_path)
        if cache_key in self._pixmap_cache:
            return self._pixmap_cache[cache_key]
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        absolute_path = sprite_path if os.path.isabs(sprite_path) else os.path.join(project_root, sprite_path)
        pixmap = QPixmap(absolute_path) if os.path.exists(absolute_path) else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self._pixmap_cache[cache_key] = pixmap
        return pixmap

    def duplicate(self, existing_names=()):
        """An independent copy of this entity, ready to place.

        Everything the entity carries comes across — every property, its I/O
        connections, its position — but the copy gets its own identity: a fresh
        UUID and a name with ``(copy)`` appended.  ``existing_names`` is the set
        of names already in use; a number is added when needed so two clones of
        the same entity never end up sharing a name, which would otherwise make
        every name-addressed I/O connection ambiguous between them.

        A plain ``copy.copy`` will not do here: it leaves the copy sharing the
        original's ``properties`` dict, so editing one silently edits both.
        """
        clone = copy.deepcopy(self)
        # Position is mutated in place by dragging, so it must not be shared
        # even if a subclass stored something exotic there.
        clone.pos = [float(self.pos[0]), float(self.pos[1]), float(self.pos[2])]
        clone.properties['id'] = str(uuid.uuid4())

        base = self.properties.get('name', '')
        if base:
            taken = set(existing_names)
            name = '%s (copy)' % base
            counter = 2
            while name in taken:
                name = '%s (copy %d)' % (base, counter)
                counter += 1
            clone.properties['name'] = name
        return clone

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

        # Untouched copy for opaque preservation of unresolvable types; the
        # loop below rewrites string property values in place.
        original_record = copy.deepcopy(data)

        if not thing_type:
            # Deliberately *not* preserved, unlike an unresolvable type token.
            # Preservation exists so an entity whose plugin is missing survives
            # a load/save round trip; that entity is identifiable, carries
            # authored content, and a plugin may supply its class later. A
            # record with no type at all is none of those things -- there is
            # nothing to resolve it to and nothing to show the author but a
            # nameless ghost. Skipping it (without stopping the load) is the
            # contract test_a_thing_with_no_type_is_skipped_rather_than_
            # crashing_the_load pins.
            return None

        properties = data.get('properties', {})
        for key, value in properties.items():
            if isinstance(value, str):
                try:
                    properties[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    pass

        # A subclass may declare `map_type` when its serialised type token
        # differs from its class name; otherwise the class name is used.
        thing = None
        token = thing_type.replace('_', '').lower()
        _load_core_entity_types()
        subclasses = find_subclasses(Thing)
        match = next((c for c in subclasses
                      if token == getattr(c, 'map_type', c.__name__.lower())), None)
        if match is not None:
            thing = match(pos=data.get('pos'), properties=properties)

        if thing is None:
            if thing_type == 'thing':
                thing = Thing(pos=data.get('pos'), properties=properties)
            else:
                # Never drop an entity: a later save would erase it for good.
                # Keep the record opaque so it round-trips byte-for-byte.
                print(f"Warning: Unknown thing type '{thing_type}' found in map file; "
                      f"preserved unchanged (is a plugin missing or disabled?).")
                return UnresolvedThing(original_record)
        
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
    EDITOR_PRIMARY_PROPERTIES = ('angle',)
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'playerstart')
        self.properties.setdefault('angle', 0.0)

    def get_angle(self):
        return float(self.properties.get('angle', 0.0))


class UnresolvedThing(Thing):
    """Opaque stand-in for an entity whose type this build cannot resolve.

    Typically a plugin entity whose plugin is missing or disabled, or a type
    from a newer/older Fio with no migration. The original map record is kept
    verbatim and written back unchanged on save (only ``pos`` follows edits,
    so it can still be moved or deleted in the editor). It has no runtime
    behaviour: no class-specific handling matches it, and it declares no I/O.

    ``map_type`` is set to a token no map can contain, so the from_dict
    subclass walk can never resolve a record *to* this class.
    """
    map_type = '\x00unresolved'

    def __init__(self, record=None, pos=None, properties=None):
        record = copy.deepcopy(record) if isinstance(record, dict) else {}
        self._record = record
        raw_props = record.get('properties')
        props = copy.deepcopy(raw_props) if isinstance(raw_props, dict) else {}
        if properties:
            props.update(properties)
        super().__init__(pos=list(record.get('pos') or pos or [0, 0, 0]),
                         properties=props)
        self.properties['type'] = record.get('type') or self.properties.get('type')

    @property
    def unresolved_type(self):
        return self._record.get('type')

    def to_dict(self):
        data = copy.deepcopy(self._record)
        data['pos'] = [float(v) for v in self.pos]
        return data

    def duplicate(self, existing_names=()):
        """Copies need their own identity inside the preserved record too."""
        clone = super().duplicate(existing_names)
        props = clone._record.setdefault('properties', {})
        if isinstance(props, dict):
            props['id'] = clone.properties['id']
            props['name'] = clone.properties['name']
        return clone


class Light(Thing):
    """Dynamic light source."""
    pixmap_path = "assets/sprites/light.png"
    EDITOR_PRIMARY_PROPERTIES = (
    'intensity',
    'radius',
    'state',
    'show_radius',
    'casts_shadows',
    'shadow_map_size',
    )

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
    EDITOR_PRIMARY_PROPERTIES = (
        'sound_file',
        'radius',
        'global',
        'show_radius',
        'volume',
        'looping',
        'play_on_start',
        'state',
    )
    
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
    EDITOR_PRIMARY_PROPERTIES = (
        'monster_type',
        'monster_id',
        'health',
        'damage',
        'awake',
        'sprite_width',
        'sprite_height',
    )
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
        """Return the icon matching this gate's current logic type."""
        logic_type = str(
            self.properties.get('logic_type', 'AND')
        ).strip().lower()

        if logic_type not in ('and', 'or', 'xor', 'nand', 'nor'):
            logic_type = 'and'

        cache_key = f"LogicGate:{logic_type}"

        if cache_key in self._pixmap_cache:
            return self._pixmap_cache[cache_key]

        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(script_dir, os.pardir))
            image_path = os.path.join(
                project_root,
                "assets",
                "sprites",
                f"logic_{logic_type}.png"
            )

            if os.path.exists(image_path):
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    self._pixmap_cache[cache_key] = pixmap
                    return pixmap
        except Exception:
            pass

        # Fall back to the generic LogicGate icon.
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
    EDITOR_PRIMARY_PROPERTIES = (
        'item_type',
        'weapon',
        'value',
        'activation',
        'collected',
    )
    
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

        # Migrate the old pickup representation before installing defaults.
        # Pre-2.5.6 maps stored gun1/gun2/cig directly in item_type.  Applying
        # the new weapon='gun1' default first would erase gun2/cig on load.
        legacy_weapon = self.properties.get('item_type')
        if legacy_weapon in self.GUN_SPRITES and 'weapon' not in self.properties:
            self.properties['weapon'] = legacy_weapon
            self.properties['item_type'] = 'weapon'

        self.properties.setdefault('item_type', 'health')
        self.properties.setdefault('weapon', 'gun1')
        self.properties.setdefault('value', 25)
        self.properties.setdefault('activation', 'walk_over')
        self.properties.setdefault('collected', False)
        self.properties.setdefault('respawns', False)
        self.properties.setdefault('respawn_time', 20.0)
        self.properties.setdefault('key_name', 'blue_key')
        self.properties.setdefault('custom_sprite', '')

    def get_weapon(self):
        """Return the pickup's weapon id, including legacy pickup maps."""
        item_type = self.properties.get('item_type')
        if item_type in self.GUN_SPRITES:
            return item_type
        return self.properties.get('weapon', 'gun1')

    def is_gun(self):
        return self.properties.get('item_type') == 'weapon' or self.properties.get('item_type') in self.GUN_SPRITES
    
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
        
        weapon = self.get_weapon()
        if self.is_gun():
            return self.GUN_SPRITES.get(weapon, self.GUN_SPRITES['gun1'])
        
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
    EDITOR_PRIMARY_PROPERTIES = (
        'action',
    )
    pixmap_path = None
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'trigger')
        self.properties.setdefault('action', 'on_enter')


class Model(Thing):
    """Represents a 3D model placed in the world."""
    pixmap_path = "assets/sprites/pickup.png"
    EDITOR_PRIMARY_PROPERTIES = (
        'model_path',
        'scale',
        'rotation',
        'no_collision',
        'collision_size',
    )
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault('type', 'model')
        self.properties.setdefault('model_path', "")
        self.properties.setdefault('rotation', [0, 0, 0])
        self.properties.setdefault('scale', [1, 1, 1])
        self.properties.setdefault('collision_shape', 'auto')


# Prop is a core engine primitive shared with the headless player, so it is
# defined once in engine/prop_entity.py and exposed here lazily (see the
# module-level __getattr__ at the end of this file).


# =============================================================================
# LOGIC ENTITIES
# =============================================================================

class LogicRelay(Thing):
    """
    A relay that fires OnTrigger when its Trigger input is called.
    Can be enabled/disabled. Useful for creating reusable trigger chains.
    """
    pixmap_path = "assets/sprites/logic_relay.png"
    EDITOR_PRIMARY_PROPERTIES = (
        'disabled',
        'fire_once',
    )
    
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
    EDITOR_PRIMARY_PROPERTIES = (
        'logic_type',
        'initial_state',
    )
    
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
    EDITOR_PRIMARY_PROPERTIES = (
        'interval',
        'timer_enabled',
        'start_on',
        'one_shot',
    )
    
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_timer'
        self.properties.setdefault('interval', 1.0)
        self.properties.setdefault('timer_enabled', False)
        self.properties.setdefault('start_on', False)
        # A one-shot timer fires once and switches itself off, so a delayed
        # one-off event does not need the chain it drives to remember to stop
        # it.  Defaults off: an existing timer keeps repeating as it always did.
        self.properties.setdefault('one_shot', False)
    
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
    EDITOR_PRIMARY_PROPERTIES = (
        'command',
        'disabled',
    )

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
    EDITOR_PRIMARY_PROPERTIES = (
        'target_map',
        'delay',
        'fade_time',
        'show_radius',
        'radius',
        'usable',
    )

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
    EDITOR_PRIMARY_PROPERTIES = (
    'portal_target',
    'width',
    'height',
    'angle',
    'active',
    'portal_direction',
    'color',
    'show_rim',
    )

    DEFAULT_WIDTH  = 128.0
    DEFAULT_HEIGHT = 256.0

    # Transit cooldown prevents rapid back-and-forth oscillation (seconds).
    TRANSIT_COOLDOWN = 0.5

    # Duration of a full fade-in or fade-out transition (seconds).
    FADE_DURATION = 0.35

    # Minimum projectile exit clearance along the destination normal. Player
    # transit uses a near-zero plane epsilon so crossing remains visually continuous.
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
        """Return the portal frame from the shared engine-level transform."""
        return _portal_basis_from_rotation(
            self.properties.get(
                'rotation',
                [self.properties.get('angle', 0.0), 0.0, 0.0],
            )
        )

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
        """Return the shared engine-level aperture corners."""
        return _portal_corners(
            self.pos, self.get_basis(), self.get_width(), self.get_height()
        )

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
        """Map a world point through the shared engine transform."""
        return _portal_map_point(
            self.pos, self.get_basis(), dest.pos, dest.get_basis(), (x, y, z)
        )


    def map_direction(self, dest, x: float, y: float, z: float):
        """Map a world direction through the shared engine transform."""
        return _portal_map_direction(
            self.get_basis(), dest.get_basis(), (x, y, z)
        )


    def contains_point(self, x: float, y: float, z: float, margin: float = 0.0) -> bool:
        """Return whether a point lies inside the shared aperture shape."""
        return _portal_contains_point(
            self.pos, self.get_basis(), self.get_width(), self.get_height(),
            (x, y, z), margin,
        )

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
# PERSISTENT STATE ENTITY
# =============================================================================

class LogicState(Thing):
    """Fio's persistent state primitive: named values that survive the level.

    A ``LogicState`` holds keyed values under a *store name*, answers questions
    about them, and turns every change into an ordinary I/O event.  That is the
    whole of it.  It does not know what a door is, it never runs, and nothing
    about it is evaluated per frame — state is looked at because a connection
    asked it to be.

    Behaviour is composed around it rather than built into it::

        Monster.OnDeath -> LogicState.Increment("killed")
                        -> OnValueChanged
                        -> LogicState.Compare("killed>=5")
                        -> OnTrue -> Door.Open

    Nothing in that chain knows about the rest of it, and the engine has no idea
    what the five kills *mean*.  The map supplies the composition; the game
    supplies the meaning.

    Values are typed
    ----------------
    ``string``, ``int``, ``float``, ``bool``, ``null`` and ``uuid`` (see
    :mod:`editor.state_values`).  I/O parameters are text, so a value's type is
    inferred from what is written — ``5`` is an integer, ``true`` a boolean —
    and can be stated outright when inference is not wanted::

        SetValue    door_code:string=007

    Legacy stores held only strings and are left exactly as they were: nothing
    re-types data on load, because comparison and arithmetic already understand
    ``"5"``.  An old map behaves the same or better, never worse.

    Persistence
    -----------
    Values live in a process-wide registry keyed by ``store_name``, so two
    stores sharing a name in different levels are the same store, and a level
    transition carries their contents across.  Plugins reach the same registry
    through ``api.global_store``.  It is one registry, and this class does not
    own it — it reads and writes it like anything else does.

    Properties
    ----------
    store_name
        Which store this entity is a view of.  Defaults to the entity name.
    initial_data
        Designer-authored defaults, used when the store does not exist yet.
    capacity
        How many keys this store accepts.  Defaults to :data:`MAX_PAIRS`, so a
        map that never sets it behaves exactly as before.

    Inputs and outputs are declared in :mod:`editor.io_system` and implemented
    in :mod:`editor.io_handlers`; see ``editor/LOGIC.md`` for the whole model.
    """
    pixmap_path = "assets/sprites/logic_state.png"

    #: Type token written to map files.
    map_type = 'logicstate'

    # Class-level registry of persistent stores across level transitions.
    # Keyed by store_name, stores the dict of values. Survives as long as
    # the Python process lives (i.e., across level loads within one session).
    _persistent_registry = {}

    #: Default capacity.  A per-entity ``capacity`` property may raise it; the
    #: default is unchanged so existing maps hit ``OnStoreFull`` where they
    #: always did.
    MAX_PAIRS = 25

    #: Prefix for object-local keys (see :meth:`object_key`).  Chosen because a
    #: designer-authored key never starts with it, so object-local state and
    #: world state share one store without either being able to shadow the
    #: other.
    OBJECT_KEY_PREFIX = '@'

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'logic_state'
        self.properties.setdefault('store_name', self.properties.get('name', ''))

        # initial_data holds the designer-specified defaults.
        # Stored as a dict; serialised to/from JSON as needed.
        self.properties.setdefault('initial_data', {})

        # Runtime data — merged from persistent registry + initial_data
        self._runtime_data = {}
        self._sync_from_persistent()

    # ------------------------------------------------------------------
    # Persistence plumbing
    # ------------------------------------------------------------------

    @property
    def capacity(self):
        """How many keys this store accepts, never below one."""
        try:
            return max(1, int(self.properties.get('capacity', self.MAX_PAIRS)))
        except (TypeError, ValueError):
            return self.MAX_PAIRS

    @property
    def store_name(self):
        """The name of the store this entity is a view of."""
        return str(self.properties.get('store_name', '') or '')

    def _sync_from_persistent(self):
        """Load values from the persistent registry, falling back to initial_data."""
        store_name = self.store_name
        if store_name and store_name in LogicState._persistent_registry:
            self._runtime_data = _sv.load_store(
                LogicState._persistent_registry[store_name])
        else:
            # Copy initial_data so we don't mutate the property directly.
            self._runtime_data = _sv.load_store(
                self.properties.get('initial_data', {}))

    def _sync_to_persistent(self):
        """Write current runtime values back to the persistent registry."""
        store_name = self.store_name
        if store_name:
            LogicState._persistent_registry[store_name] = dict(self._runtime_data)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    #: Sentinel returned for a key that is not there.  A plain string, and the
    #: same one the pre-2.4 store used, so a map that wired a connection
    #: against it still matches.
    MISSING = "<missing>"

    def get_value(self, key, default=MISSING):
        """The value stored under *key*, or *default*.

        Returns the value with its type intact — an ``int`` key comes back an
        ``int``.  Callers that need the wire form use
        :func:`editor.state_values.format_value`.
        """
        return self._runtime_data.get(str(key).strip(), default)

    def has_key(self, key) -> bool:
        """Whether *key* is present, regardless of its value.

        A key holding ``null`` exists; that is the difference between a flag
        that was cleared and one that was never set.
        """
        return str(key).strip() in self._runtime_data

    def value_type(self, key) -> str:
        """The state type of *key*'s value, or ``'null'`` when it is absent."""
        if not self.has_key(key):
            return 'null'
        return _sv.type_of(self.get_value(key, None))

    def get_all_pairs(self):
        """A copy of every key/value pair."""
        return dict(self._runtime_data)

    def get_pair_count(self):
        """How many pairs are stored."""
        return len(self._runtime_data)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def apply_value(self, key, value, value_type=None):
        """Store *value* under *key*; the full result of one write.

        Returns ``(ok, changed, stored)``:

        ``ok``
            False when the write could not happen — a blank key, a full store,
            or text that does not denote a value of the requested type.
        ``changed``
            Whether the stored value is different from what was there.  This is
            the distinction the event model is built on: ``OnValueSet`` fires
            for every accepted write, ``OnValueChanged`` only when the world
            actually changed, so a chain can be driven by real transitions
            without needing a watcher to notice them.
        ``stored``
            The typed value now in the store.

        A string *value* is typed by :func:`editor.state_values.parse` unless
        *value_type* names the type outright.
        """
        key = str(key).strip()
        if not key:
            return False, False, None

        if value_type:
            typed, ok = _sv.coerce(value, value_type)
            if not ok:
                return False, False, None
        else:
            typed = _sv.parse(value)

        existed = key in self._runtime_data
        if not existed and len(self._runtime_data) >= self.capacity:
            return False, False, None

        previous = self._runtime_data.get(key, self.MISSING)
        changed = (not existed) or not _same_value(previous, typed)
        self._runtime_data[key] = typed
        if changed:
            self._sync_to_persistent()
        return True, changed, typed

    def set_value(self, key, value, value_type=None) -> bool:
        """Store *value* under *key*.  True unless the write was refused.

        The pre-2.4 signature, kept because plugins and the I/O manager call it.
        Use :meth:`apply_value` when the caller cares whether anything changed.
        """
        ok, _changed, _stored = self.apply_value(key, value, value_type)
        return ok

    def clear_key(self, key) -> bool:
        """Remove *key*.  True if it was there."""
        key = str(key).strip()
        if key in self._runtime_data:
            del self._runtime_data[key]
            self._sync_to_persistent()
            return True
        return False

    def clear_all(self):
        """Remove every key."""
        self._runtime_data.clear()
        self._sync_to_persistent()

    def copy_from(self, other_store_name) -> bool:
        """Copy every pair from another store by name.  True if it existed."""
        other = LogicState._persistent_registry.get(str(other_store_name))
        if other is None:
            return False
        capacity = self.capacity
        for k, v in _sv.load_store(other).items():
            if len(self._runtime_data) >= capacity and k not in self._runtime_data:
                break
            self._runtime_data[k] = v
        self._sync_to_persistent()
        return True

    def copy_value(self, source_key, dest_key, source_store=None):
        """Copy one value between keys, optionally from another store.

        Returns ``(ok, changed, stored)`` like :meth:`apply_value`; ``ok`` is
        False when the source key does not exist, so a copy of nothing is a
        no-op rather than a write of ``"<missing>"``.
        """
        source_key = str(source_key).strip()
        if source_store:
            data = LogicState._persistent_registry.get(str(source_store))
            if data is None or source_key not in data:
                return False, False, None
            value = _sv.load_store(data)[source_key]
        else:
            if not self.has_key(source_key):
                return False, False, None
            value = self.get_value(source_key, None)
        return self.apply_value(dest_key, value)

    # ------------------------------------------------------------------
    # Arithmetic
    # ------------------------------------------------------------------

    def arithmetic(self, key, operation, operand):
        """Apply one arithmetic operation to *key* in place.

        Returns ``(ok, changed, stored)``.  A key that is absent (or holds
        something non-numeric) counts as zero, so ``Increment`` on a fresh
        counter yields one without the map having to seed it first.
        """
        key = str(key).strip()
        if not key:
            return False, False, None
        current = self.get_value(key, 0)
        if current is self.MISSING:
            current = 0
        new_value, ok = _sv.arithmetic(current, operation, operand)
        if not ok:
            return False, False, current
        return self.apply_value(key, new_value)

    def clamp(self, key, low, high):
        """Confine *key* to ``[low, high]``.  ``(ok, changed, stored)``."""
        key = str(key).strip()
        if not key:
            return False, False, None
        current = self.get_value(key, 0)
        if current is self.MISSING:
            current = 0
        return self.apply_value(key, _sv.clamp(current, low, high))

    def toggle(self, key):
        """Invert *key* as a boolean.  ``(ok, changed, stored)``.

        A key that was never set toggles to ``True``, which is the only useful
        reading of "turn this flag the other way" for a flag that is not there.
        """
        key = str(key).strip()
        if not key:
            return False, False, None
        current = self.get_value(key, None)
        if current is self.MISSING:
            current = None
        return self.apply_value(key, _sv.toggled(current))

    def increment(self, key, amount=1):
        """Add *amount* to *key*, returning the new value.

        The pre-2.4 signature and return type, kept for callers that only want
        the number.
        """
        _ok, _changed, value = self.arithmetic(key, 'add', amount)
        return value if value is not None else 0

    def decrement(self, key, amount=1):
        """Subtract *amount* from *key*, returning the new value."""
        return self.increment(key, -_sv.to_number(amount, 1))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def compare(self, key, operator, expected):
        """Answer one question about *key*.  ``(found, result)``.

        *expected* is typed the same way a stored value is, so ``"5"`` from a
        connection parameter compares numerically against a stored ``5`` and
        ``"true"`` compares as a boolean against a stored flag.  When the key is
        missing, *found* is False and the caller fires ``OnKeyNotFound`` instead
        of guessing what the comparison would have meant.

        This is the whole of Fio's conditional logic.  Everything more
        complicated — two conditions, a negation, a sequence — is a
        :class:`LogicGate`, a :class:`LogicRelay` and more connections, which is
        cheaper than an expression language and visible in the I/O editor.
        """
        key = str(key).strip()
        if key not in self._runtime_data:
            return False, False
        actual = self._runtime_data[key]
        return True, _sv.compare(actual, _sv.parse(expected), operator)

    # ------------------------------------------------------------------
    # Object-local state
    # ------------------------------------------------------------------

    @classmethod
    def object_key(cls, entity_id, key) -> str:
        """The store key holding *key* for the object with UUID *entity_id*.

        Object-local state is not a second database and not a second entity
        framework: it is ordinary keys in this same store, namespaced by the
        object's UUID.  So it persists, saves, transitions levels and shows up
        in the Property Manager exactly like every other value, and a dormant
        entity's state is simply data that outlives the entity's dormancy.
        """
        return '%s%s/%s' % (cls.OBJECT_KEY_PREFIX, entity_id, str(key).strip())

    def set_object_value(self, entity_id, key, value, value_type=None):
        """Store a value against one object's UUID.  ``(ok, changed, stored)``."""
        if not entity_id:
            return False, False, None
        return self.apply_value(self.object_key(entity_id, key), value, value_type)

    def get_object_value(self, entity_id, key, default=MISSING):
        """Read a value stored against one object's UUID."""
        if not entity_id:
            return default
        return self.get_value(self.object_key(entity_id, key), default)

    def clear_object_state(self, entity_id) -> int:
        """Forget every value held against one object.  Returns how many."""
        if not entity_id:
            return 0
        prefix = '%s%s/' % (self.OBJECT_KEY_PREFIX, entity_id)
        doomed = [k for k in self._runtime_data if k.startswith(prefix)]
        for key in doomed:
            del self._runtime_data[key]
        if doomed:
            self._sync_to_persistent()
        return len(doomed)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self):
        """Serialise, embedding the live values so a save is self-contained."""
        # Ensure persistent registry is up to date before serialising
        self._sync_to_persistent()
        result = super().to_dict()
        result['runtime_data'] = _sv.save_store(self._runtime_data)
        return result

    @staticmethod
    def from_dict(data):
        """Deserialise, restoring the embedded values into the registry."""
        thing = Thing.from_dict(data)
        if thing is not None and isinstance(thing, LogicState):
            runtime = data.get('runtime_data')
            if isinstance(runtime, dict):
                thing._runtime_data = _sv.load_store(runtime)
                thing._sync_to_persistent()
            else:
                thing._sync_from_persistent()
        return thing


def _same_value(a, b) -> bool:
    """Whether two stored values are the same value *and* the same type.

    ``1`` and ``True`` compare equal in Python and are not the same state, so
    change detection has to ask about the type as well — otherwise setting a
    counter to ``1`` over a flag of ``True`` would report "unchanged" and the
    chain hanging off ``OnValueChanged`` would never run.
    """
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return type(a) is type(b) and a == b



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
    'LogicState': LogicState,
    # Core primitive is imported only after editor Thing/Model definitions exist.
    'Effect': importlib.import_module('engine.effect_entity').Effect,
}

# Categories for editor UI
ENTITY_CATEGORIES = {
    'Gameplay': ['PlayerStart', 'Monster', 'Pickup', 'LevelChanger'],
    'Environment': ['Light', 'Effect', 'Speaker', 'Model', 'Portal'],
    'Logic': ['LogicRelay', 'LogicGate', 'LogicTimer', 'LogicCommand', 'LogicCamera', 'LogicSpawner', 'LogicState'],
    'AI': ['PathNode'],
}


# =============================================================================
# CORE ENTITIES DEFINED OUTSIDE THIS MODULE
# =============================================================================
#
# Core primitives that the headless player also needs (currently Prop) live in
# engine/, subclassing this module's Model when the editor tier is present.
# They cannot be imported at the top of this file (they import it), so they are
# loaded on first use: by name via __getattr__ (``from editor.things import
# Prop``), and before map deserialization via _load_core_entity_types() so the
# subclass walk in Thing.from_dict can resolve their type tokens.

_CORE_ENTITY_MODULES = {
    'Prop': 'engine.prop_entity',
    'Effect': 'engine.effect_entity',
}


def _load_core_entity_types():
    import importlib
    for module_name in set(_CORE_ENTITY_MODULES.values()):
        importlib.import_module(module_name)


def __getattr__(name):
    module_name = _CORE_ENTITY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
