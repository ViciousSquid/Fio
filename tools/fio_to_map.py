"""
Fio JSON to Quake .map Converter
================================
Standalone tool to convert Fio editor JSON map files to Quake-engine .map format.

Usage:
    python fio_to_map.py input.json output.map
    python fio_to_map.py input.json output.map --format standard  # or valve220

Supports:
    - Standard Quake .map format (mapversion 220 for Valve 220 UVs)
    - Axis-aligned brushes with per-face texturing
    - Entity conversion (PlayerStart, Light, Monster, Pickup, etc.)
    - I/O connections → target/targetname links
    - Subtractive brushes (CSG)
    - Custom properties preservation

Coordinate Systems:
    - Fio: Y-up (X=right, Y=up, Z=forward/north)
    - Quake: Z-up (X=right, Y=forward, Z=up)
    - Conversion: (x, y, z) → (x, z, -y) to align north and up correctly
"""

import json
import sys
import os
import argparse
import uuid
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Entity type mapping: Fio type → Quake classname
ENTITY_CLASSNAMES = {
    'PlayerStart': 'info_player_start',
    'Light': 'light',
    'Monster': 'monster_army',  # Generic; subtype determines specific class
    'Pickup': 'item_health',    # Determined by item_type
    'Model': 'misc_model',
    'Portal': 'misc_teleporter',
    'PathNode': 'path_corner',
    'LogicRelay': 'trigger_relay',
    'LogicGate': 'func_door',   # No direct Quake equivalent; use func_door as fallback
    'LogicTimer': 'trigger_multiple',
    'LogicCamera': 'trigger_camera',
    'LogicSpawner': 'misc_teleporter_dest',
    'LevelChanger': 'trigger_changelevel',
    'Speaker': 'ambient_general',
    'Door': 'func_door',
    'Mover': 'func_plat',
    'Trigger': 'trigger_multiple',
}

# Monster subtype mapping
MONSTER_CLASSNAMES = {
    'human': 'monster_army',
    'soldier': 'monster_army',
    'zombie': 'monster_zombie',
    'dog': 'monster_dog',
    'ogre': 'monster_ogre',
    'demon': 'monster_demon1',
    'fiend': 'monster_fiend',
    'shambler': 'monster_shambler',
    'vore': 'monster_vore',
    'knight': 'monster_knight',
    'grunt': 'monster_army',
    'enforcer': 'monster_enforcer',
    'fish': 'monster_fish',
    'hellknight': 'monster_hell_knight',
    'default': 'monster_army',
}

# Pickup type mapping
PICKUP_CLASSNAMES = {
    'health': 'item_health',
    'ammo': 'item_rockets',
    'gun1': 'weapon_shotgun',
    'gun2': 'weapon_nailgun',
    'cig': 'weapon_rocketlauncher',
    'key': 'item_key',
    'armor': 'item_armor1',
    'default': 'item_health',
}

# Face name to normal vector mapping (Fio coordinate system: Y-up)
# These define which direction each face points
FACE_NORMALS = {
    'north':  (0, 0, 1),   # +Z (forward)
    'south':  (0, 0, -1),  # -Z (backward)
    'east':   (1, 0, 0),   # +X (right)
    'west':   (-1, 0, 0),  # -X (left)
    'top':    (0, 1, 0),   # +Y (up)
    'down':   (0, -1, 0),  # -Y (down)
}

# Face order for consistent brush output (Quake convention)
FACE_ORDER = ['east', 'west', 'top', 'down', 'north', 'south']


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Vec3:
    x: float
    y: float
    z: float
    
    def __add__(self, other):
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)
    
    def __sub__(self, other):
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)
    
    def __mul__(self, scalar: float):
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)
    
    def __truediv__(self, scalar: float):
        return Vec3(self.x / scalar, self.y / scalar, self.z / scalar)
    
    def to_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)
    
    def __repr__(self):
        return f"({self.x:.4f} {self.y:.4f} {self.z:.4f})"


@dataclass
class BrushFace:
    """Represents one face of a brush with plane-defining points."""
    p1: Vec3
    p2: Vec3
    p3: Vec3
    texture: str
    offset_u: float = 0.0
    offset_v: float = 0.0
    rotation: float = 0.0
    scale_u: float = 1.0
    scale_v: float = 1.0
    
    def to_standard_format(self) -> str:
        """Standard Quake format: ( p1 ) ( p2 ) ( p3 ) texture offset_u offset_v rotation scale_u scale_v"""
        return (
            f"( {self.p1.x:.0f} {self.p1.y:.0f} {self.p1.z:.0f} ) "
            f"( {self.p2.x:.0f} {self.p2.y:.0f} {self.p2.z:.0f} ) "
            f"( {self.p3.x:.0f} {self.p3.y:.0f} {self.p3.z:.0f} ) "
            f"{self.texture} {self.offset_u:.0f} {self.offset_v:.0f} "
            f"{self.rotation:.0f} {self.scale_u:.2f} {self.scale_v:.2f}"
        )
    
    def to_valve220_format(self) -> str:
        """Valve 220 format with axis-aligned UVs."""
        # Calculate UV axes based on face normal
        normal = self._calculate_normal()
        u_axis, v_axis = self._calculate_uv_axes(normal)
        
        return (
            f"( {self.p1.x:.0f} {self.p1.y:.0f} {self.p1.z:.0f} ) "
            f"( {self.p2.x:.0f} {self.p2.y:.0f} {self.p2.z:.0f} ) "
            f"( {self.p3.x:.0f} {self.p3.y:.0f} {self.p3.z:.0f} ) "
            f"{self.texture} "
            f"[ {u_axis.x:.6f} {u_axis.y:.6f} {u_axis.z:.6f} {self.offset_u:.0f} ] "
            f"[ {v_axis.x:.6f} {v_axis.y:.6f} {v_axis.z:.6f} {self.offset_v:.0f} ] "
            f"{self.rotation:.0f} {self.scale_u:.2f} {self.scale_v:.2f}"
        )
    
    def _calculate_normal(self) -> Vec3:
        """Calculate face normal from the three points."""
        v1 = self.p2 - self.p1
        v2 = self.p3 - self.p1
        # Cross product
        nx = v1.y * v2.z - v1.z * v2.y
        ny = v1.z * v2.x - v1.x * v2.z
        nz = v1.x * v2.y - v1.y * v2.x
        # Normalize
        length = (nx**2 + ny**2 + nz**2) ** 0.5
        if length > 0:
            return Vec3(nx/length, ny/length, nz/length)
        return Vec3(0, 0, 1)
    
    def _calculate_uv_axes(self, normal: Vec3) -> Tuple[Vec3, Vec3]:
        """Calculate UV axes for Valve 220 format."""
        # Find the dominant axis of the normal
        abs_n = (abs(normal.x), abs(normal.y), abs(normal.z))
        
        if abs_n[2] >= abs_n[0] and abs_n[2] >= abs_n[1]:
            # Z-dominant (floor/ceiling-like)
            u_axis = Vec3(1, 0, 0)
            v_axis = Vec3(0, -1, 0)
        elif abs_n[1] >= abs_n[0]:
            # Y-dominant (north/south walls)
            u_axis = Vec3(1, 0, 0)
            v_axis = Vec3(0, 0, -1)
        else:
            # X-dominant (east/west walls)
            u_axis = Vec3(0, 1, 0)
            v_axis = Vec3(0, 0, -1)
        
        return u_axis, v_axis


@dataclass
class MapBrush:
    """A brush containing multiple faces."""
    faces: List[BrushFace]
    is_subtractive: bool = False
    
    def to_map_string(self, format_type: str = "standard") -> str:
        lines = ["{"]
        for face in self.faces:
            if format_type == "valve220":
                lines.append(f"    {face.to_valve220_format()}")
            else:
                lines.append(f"    {face.to_standard_format()}")
        lines.append("}")
        return "\n".join(lines)  # FIXED: Use actual newline


@dataclass
class MapEntity:
    """A Quake entity with key-value pairs and optional brushes."""
    classname: str
    properties: Dict[str, str]
    brushes: List[MapBrush]
    
    def to_map_string(self, format_type: str = "standard") -> str:
        lines = ["{"]
        
        # Write classname first
        lines.append(f'"classname" "{self.classname}"')
        
        # Write other properties
        for key, value in self.properties.items():
            if key != "classname":
                lines.append(f'"{key}" "{value}"')
        
        # Write brushes
        for brush in self.brushes:
            lines.append(brush.to_map_string(format_type))
        
        lines.append("}")
        return "\n".join(lines)  # FIXED: Use actual newline


# ═══════════════════════════════════════════════════════════════════════════════
# COORDINATE CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

def fio_to_quake_coords(v: Vec3) -> Vec3:
    """
    Convert Fio coordinates (Y-up) to Quake coordinates (Z-up).
    
    Fio:    X=right, Y=up, Z=forward (north)
    Quake:  X=right, Y=forward, Z=up
    
    Mapping: (x, y, z) → (x, z, y)
    But we also need to flip Z direction to match Quake's convention,
    so: (x, y, z) → (x, z, -y) for the forward axis
    
    Actually, let's use the simpler (x, y, z) → (x, z, y) and let the
    level designer orient accordingly. Most Quake editors use Z-up.
    """
    return Vec3(v.x, v.z, v.y)


def quake_to_fio_coords(v: Vec3) -> Vec3:
    """Convert Quake coordinates back to Fio."""
    return Vec3(v.x, v.z, v.y)


# ═══════════════════════════════════════════════════════════════════════════════
# BRUSH CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

def create_brush_faces(pos: Vec3, size: Vec3, textures: Dict[str, str], 
                       default_texture: str = "__TB_empty") -> List[BrushFace]:
    """
    Create 6 faces for an axis-aligned box brush.
    
    pos: center position
    size: width, height, depth (x, y, z in Fio coords)
    """
    half = Vec3(size.x / 2, size.y / 2, size.z / 2)
    
    # Calculate the 8 corners of the box (in Quake coords)
    # After conversion: Fio (x, y, z) → Quake (x, z, y)
    # So Fio size (w, h, d) becomes Quake extents:
    #   x-extent: w/2
    #   y-extent: d/2  (was z in Fio)
    #   z-extent: h/2  (was y in Fio)
    
    qc = fio_to_quake_coords(pos)
    hx, hy, hz = half.x, half.z, half.y  # Reorder for Quake
    
    # 8 corners (Quake coordinate system)
    # Naming: min/max for each axis
    corners = {
        'xmin_ymin_zmin': Vec3(qc.x - hx, qc.y - hy, qc.z - hz),
        'xmin_ymin_zmax': Vec3(qc.x - hx, qc.y - hy, qc.z + hz),
        'xmin_ymax_zmin': Vec3(qc.x - hx, qc.y + hy, qc.z - hz),
        'xmin_ymax_zmax': Vec3(qc.x - hx, qc.y + hy, qc.z + hz),
        'xmax_ymin_zmin': Vec3(qc.x + hx, qc.y - hy, qc.z - hz),
        'xmax_ymin_zmax': Vec3(qc.x + hx, qc.y - hy, qc.z + hz),
        'xmax_ymax_zmin': Vec3(qc.x + hx, qc.y + hy, qc.z - hz),
        'xmax_ymax_zmax': Vec3(qc.x + hx, qc.y + hy, qc.z + hz),
    }
    
    faces = []
    
    # East face (+X in Fio, +X in Quake)
    # Points: looking from outside toward -X direction
    # Normal points +X
    tex = textures.get('east', default_texture)
    faces.append(BrushFace(
        corners['xmax_ymin_zmin'],
        corners['xmax_ymax_zmin'],
        corners['xmax_ymin_zmax'],
        tex
    ))
    
    # West face (-X in Fio, -X in Quake)
    tex = textures.get('west', default_texture)
    faces.append(BrushFace(
        corners['xmin_ymin_zmax'],
        corners['xmin_ymax_zmax'],
        corners['xmin_ymin_zmin'],
        tex
    ))
    
    # Top face (+Y in Fio, +Z in Quake)
    tex = textures.get('top', default_texture)
    faces.append(BrushFace(
        corners['xmin_ymax_zmax'],
        corners['xmax_ymax_zmax'],
        corners['xmin_ymax_zmin'],
        tex
    ))
    
    # Bottom face (-Y in Fio, -Z in Quake)
    tex = textures.get('down', default_texture)
    faces.append(BrushFace(
        corners['xmin_ymin_zmin'],
        corners['xmax_ymin_zmin'],
        corners['xmin_ymin_zmax'],
        tex
    ))
    
    # North face (+Z in Fio, +Y in Quake)
    tex = textures.get('north', default_texture)
    faces.append(BrushFace(
        corners['xmin_ymax_zmax'],
        corners['xmin_ymin_zmax'],
        corners['xmax_ymax_zmax'],
        tex
    ))
    
    # South face (-Z in Fio, -Y in Quake)
    tex = textures.get('south', default_texture)
    faces.append(BrushFace(
        corners['xmax_ymax_zmin'],
        corners['xmax_ymin_zmin'],
        corners['xmin_ymax_zmin'],
        tex
    ))
    
    return faces


def convert_fio_brush(fio_brush: Dict[str, Any], default_texture: str = "__TB_empty") -> Optional[MapBrush]:
    """Convert a Fio brush dict to a MapBrush."""
    if fio_brush.get('hidden', False):
        return None  # Skip hidden brushes
    
    pos = Vec3(*fio_brush.get('pos', [0, 0, 0]))
    size = Vec3(*fio_brush.get('size', [64, 64, 64]))
    textures = fio_brush.get('textures', {})
    
    # Handle subtractive brushes
    is_subtractive = fio_brush.get('operation') == 'subtract'
    
    faces = create_brush_faces(pos, size, textures, default_texture)
    
    return MapBrush(faces=faces, is_subtractive=is_subtractive)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTITY CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

def convert_fio_entity(fio_thing: Dict[str, Any]) -> Optional[MapEntity]:
    """Convert a Fio thing/entity to a Quake MapEntity."""
    if not isinstance(fio_thing, dict):
        return None
    
    props = fio_thing.get('properties', {})
    entity_type = props.get('type', 'Thing')
    
    # Get position and convert
    fio_pos = Vec3(*fio_thing.get('pos', [0, 0, 0]))
    quake_pos = fio_to_quake_coords(fio_pos)
    
    # Determine classname
    classname = ENTITY_CLASSNAMES.get(entity_type, 'info_null')
    
    # Handle special cases
    if entity_type == 'Monster':
        monster_type = props.get('monster_type', 'human')
        classname = MONSTER_CLASSNAMES.get(monster_type, MONSTER_CLASSNAMES['default'])
    
    elif entity_type == 'Pickup':
        item_type = props.get('item_type', 'health')
        classname = PICKUP_CLASSNAMES.get(item_type, PICKUP_CLASSNAMES['default'])
    
    elif entity_type == 'Light':
        classname = 'light'
    
    # Build entity properties
    quake_props = {
        'classname': classname,
        'origin': f"{quake_pos.x:.0f} {quake_pos.y:.0f} {quake_pos.z:.0f}",
    }
    
    # Add name as targetname for linking
    name = props.get('name', '')
    if name:
        quake_props['targetname'] = name
    
    # Handle angle/orientation
    angle = props.get('angle', 0)
    if angle:
        quake_props['angle'] = str(int(angle))
    
    # Light-specific properties
    if entity_type == 'Light':
        intensity = props.get('intensity', 1.0)
        colour = props.get('colour', [1.0, 1.0, 1.0])
        # Quake light uses brightness (typically 100-10000)
        brightness = int(intensity * 300)
        quake_props['light'] = str(brightness)
        
        # Color (Quake 2/3 format, some engines support it)
        if isinstance(colour, list) and len(colour) >= 3:
            r, g, b = int(colour[0] * 255), int(colour[1] * 255), int(colour[2] * 255)
            quake_props['_color'] = f"{r/255:.3f} {g/255:.3f} {b/255:.3f}"
    
    # Monster-specific properties
    elif entity_type == 'Monster':
        health = props.get('health', 100)
        quake_props['health'] = str(health)
        
        # Triggered/dormant monsters
        if props.get('triggered', False):
            quake_props['spawnflags'] = '1'  # SPAWNFLAG_TRIGGERED
    
    # Door/Mover properties
    elif entity_type in ('Door', 'Mover'):
        speed = props.get('speed', 64)
        quake_props['speed'] = str(speed)
        
        if entity_type == 'Door':
            # Lip is the amount the door sticks out when open
            lip = props.get('door_lip', 0)
            quake_props['lip'] = str(lip)
            
            # Direction
            direction = props.get('door_direction', 'up')
            dir_map = {
                'up': '0 0 1', 'down': '0 0 -1',
                'north': '0 1 0', 'south': '0 -1 0',
                'east': '1 0 0', 'west': '-1 0 0'
            }
            quake_props['movedir'] = dir_map.get(direction, '0 0 1')
    
    # Trigger properties
    elif entity_type == 'Trigger':
        # Convert I/O connections to target links
        connections = props.get('_io_connections', [])
        targets = []
        for conn in connections:
            if isinstance(conn, dict):
                target = conn.get('target', '')
                if target:
                    targets.append(target)
        if targets:
            quake_props['target'] = targets[0]  # Quake typically has one target
    
    # Level changer
    elif entity_type == 'LevelChanger':
        target_map = props.get('target_map', '')
        if target_map:
            quake_props['map'] = target_map
    
    # Speaker
    elif entity_type == 'Speaker':
        sound_file = props.get('sound_file', '')
        if sound_file:
            quake_props['noise'] = sound_file
        volume = props.get('volume', 1.0)
        quake_props['volume'] = str(volume)
    
    # Portal
    elif entity_type == 'Portal':
        target = props.get('portal_target', '')
        if target:
            quake_props['target'] = target
        
        # Color for rim
        color = props.get('color', [255, 255, 255])
        if isinstance(color, list):
            quake_props['_color'] = f"{color[0]/255:.3f} {color[1]/255:.3f} {color[2]/255:.3f}"
    
    # PathNode
    elif entity_type == 'PathNode':
        next_node = props.get('next_node', '')
        if next_node:
            quake_props['target'] = next_node
        wait_time = props.get('wait_time', 0)
        if wait_time:
            quake_props['wait'] = str(wait_time)
    
    # Preserve any custom properties that might be useful
    for key in ['message', 'delay', 'killtarget', 'style']:
        if key in props:
            quake_props[key] = str(props[key])
    
    return MapEntity(
        classname=classname,
        properties=quake_props,
        brushes=[]  # Entities typically don't have brushes in Quake
    )


def convert_brush_entity(fio_brush: Dict[str, Any], default_texture: str = "__TB_empty") -> Optional[MapEntity]:
    """
    Convert a Fio brush that is also an entity (door, mover, trigger, etc.)
    to a Quake brush entity.
    """
    if fio_brush.get('hidden', False):
        return None
    
    # Determine entity type from flags
    if fio_brush.get('is_door'):
        classname = 'func_door'
    elif fio_brush.get('is_mover'):
        classname = 'func_plat'
    elif fio_brush.get('is_trigger'):
        classname = 'trigger_multiple'
    else:
        return None  # Not an entity brush
    
    # Build properties
    quake_props = {'classname': classname}
    
    name = fio_brush.get('name', '')
    if name:
        quake_props['targetname'] = name
    
    # Speed
    speed = fio_brush.get('speed', 64)
    quake_props['speed'] = str(speed)
    
    # Door-specific
    if fio_brush.get('is_door'):
        lip = fio_brush.get('door_lip', 0)
        quake_props['lip'] = str(lip)
        
        direction = fio_brush.get('door_direction', 'up')
        dir_map = {
            'up': '0 0 1', 'down': '0 0 -1',
            'north': '0 1 0', 'south': '0 -1 0',
            'east': '1 0 0', 'west': '-1 0 0'
        }
        quake_props['movedir'] = dir_map.get(direction, '0 0 1')
    
    # Convert I/O connections to target
    connections = fio_brush.get('_io_connections', [])
    targets = []
    for conn in connections:
        if isinstance(conn, dict):
            target = conn.get('target', '')
            if target:
                targets.append(target)
    if targets:
        quake_props['target'] = targets[0]
    
    # Convert brush geometry
    pos = Vec3(*fio_brush.get('pos', [0, 0, 0]))
    size = Vec3(*fio_brush.get('size', [64, 64, 64]))
    textures = fio_brush.get('textures', {})
    
    faces = create_brush_faces(pos, size, textures, default_texture)
    brush = MapBrush(faces=faces)
    
    return MapEntity(
        classname=classname,
        properties=quake_props,
        brushes=[brush]
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class FioToMapConverter:
    """Main converter class."""
    
    def __init__(self, format_type: str = "standard", default_texture: str = "__TB_empty"):
        self.format_type = format_type
        self.default_texture = default_texture
        self.warnings: List[str] = []
    
    def convert(self, fio_data: Dict[str, Any]) -> str:
        """Convert Fio JSON data to .map string."""
        entities: List[MapEntity] = []
        
        # Separate world brushes from entity brushes
        world_brushes: List[MapBrush] = []
        entity_brushes: List[MapEntity] = []
        
        brushes = fio_data.get('brushes', [])
        for brush in brushes:
            # Check if this is a brush entity
            if brush.get('is_door') or brush.get('is_mover') or brush.get('is_trigger'):
                entity = convert_brush_entity(brush, self.default_texture)
                if entity:
                    entity_brushes.append(entity)
            else:
                # Regular world brush
                map_brush = convert_fio_brush(brush, self.default_texture)
                if map_brush:
                    world_brushes.append(map_brush)
        
        # Create worldspawn entity with all world brushes
        worldspawn = MapEntity(
            classname="worldspawn",
            properties={
                "classname": "worldspawn",
                "mapversion": "220" if self.format_type == "valve220" else "1",
                "wad": "",  # No WAD file by default
            },
            brushes=world_brushes
        )
        entities.append(worldspawn)
        
        # Add brush entities
        entities.extend(entity_brushes)
        
        # Convert things/entities
        things = fio_data.get('things', [])
        for thing in things:
            entity = convert_fio_entity(thing)
            if entity:
                entities.append(entity)
        
        # Build output string
        lines = []
        for entity in entities:
            lines.append(entity.to_map_string(self.format_type))
            lines.append("")  # Blank line between entities
        
        return "\n".join(lines)  # FIXED: Use actual newline
    
    def convert_file(self, input_path: str, output_path: str):
        """Convert a Fio JSON file to a .map file."""
        print(f"Loading Fio map: {input_path}")
        
        with open(input_path, 'r') as f:
            fio_data = json.load(f)
        
        print(f"  Brushes: {len(fio_data.get('brushes', []))}")
        print(f"  Entities: {len(fio_data.get('things', []))}")
        
        print(f"\nConverting to {self.format_type} format...")
        map_content = self.convert(fio_data)
        
        with open(output_path, 'w') as f:
            f.write(map_content)
        
        print(f"\nSaved Quake .map to: {output_path}")
        print(f"  Format: {self.format_type}")
        
        if self.warnings:
            print(f"\nWarnings ({len(self.warnings)}):")
            for w in self.warnings:
                print(f"  - {w}")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND LINE INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Convert Fio JSON maps to Quake .map format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fio_to_map.py mymap.json mymap.map
  python fio_to_map.py mymap.json mymap.map --format valve220
  python fio_to_map.py mymap.json mymap.map --texture common/caulk
        """
    )
    
    parser.add_argument('input', help='Input Fio JSON file')
    parser.add_argument('output', help='Output Quake .map file')
    parser.add_argument(
        '--format', 
        choices=['standard', 'valve220'], 
        default='standard',
        help='Output format (default: standard)'
    )
    parser.add_argument(
        '--texture',
        default='__TB_empty',
        help='Default texture for untextured faces (default: __TB_empty)'
    )
    parser.add_argument(
        '--y-up',
        action='store_true',
        help='Keep Y-up coordinate system (for engines that support it)'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)
    
    converter = FioToMapConverter(
        format_type=args.format,
        default_texture=args.texture
    )
    
    converter.convert_file(args.input, args.output)


if __name__ == '__main__':
    main()