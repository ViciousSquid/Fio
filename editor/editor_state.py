"""
State Manager

Manages all the data for the current level being edited, including:
- Brushes (solid geometry)
- Things (entities)
- Undo/redo history
- Serialization/deserialization with I/O connections
- Lightmap bake state (Stage 1: data structures only)
"""

import json
import copy
import uuid
from collections import deque
from .things import Thing, Model
from editor.things import update_all_counters_from_entities

# Import I/O system for serialization
try:
    from .io_system import OutputConnection, get_connections, set_connections
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False

# Convex-brush geometry helpers (angled brushes & clipping).  NumPy-only, safe
# to import head-less / off the GL thread.
from engine.brush_geometry import (
    GEO_RUNTIME_KEYS, clip_brush as _geo_clip_brush,
    rotate_brush as _geo_rotate_brush, brush_has_geometry as _geo_has_geometry,
    is_axis_aligned_box as _geo_is_box,
)

# Keys written to brush dicts by the renderer/geometry layer at runtime.
# They hold GLM/NumPy objects (not JSON-serialisable) and must be stripped
# before any serialisation path: undo stack, file save, or deepcopy-for-JSON.
# GEO_RUNTIME_KEYS covers the convex-geometry cache and mesh-collision data
# attached to angled brushes during play.
_RENDERER_PRIVATE_KEYS = frozenset({
    '_mat_cache_key', '_mat_cache',      # model matrix cache (renderer_F)
    '_nmat_cache_key', '_nmat_cache',    # normal matrix cache (renderer_F)
    '_render_mesh', '_render_sig',       # angled-brush GPU mesh cache (renderer_F)
}) | GEO_RUNTIME_KEYS

# Import lightmap bake state
try:
    from lightmap.bake_state import BakeState
    LIGHTMAP_AVAILABLE = True
except ImportError:
    LIGHTMAP_AVAILABLE = False
    print("[LIGHTMAP_AVAILABLE] False")


class EditorState:
    """Manages all the data for the current level being edited."""

    def __init__(self):
        self.brushes = []
        self.things = []
        self.selected_object = None
        self.terrain_data = None
        self._logic_graph_positions = {}  # Persisted node positions for the logic graph
        self.undo_stack = deque(maxlen=50)
        self.redo_stack = []

        # Lightmap bake state — always present, even if baking is unavailable
        self.bake_state = BakeState() if LIGHTMAP_AVAILABLE else None

        # Initial empty state for the undo stack
        self.save_state()

    # =========================================================================
    # LIGHTMAP HELPERS
    # =========================================================================


    def mark_lighting_dirty(self) -> None:
        """
        Call whenever static geometry or static lights change so the next
        Play automatically triggers a rebake.

        Safe to call even when the lightmap system is unavailable.
        """
        if self.bake_state is not None:
            self.bake_state.mark_dirty()

    def get_static_brushes(self) -> list:
        """Return the subset of brushes that participate in lightmap baking."""
        return [b for b in self.brushes if b.get('lightmap_static', False)]

    def mark_brush_static(self, brush: dict, static: bool) -> None:
        """
        Set or clear the lightmap_static flag on a single brush and mark the
        bake dirty so the change is picked up on the next Play.
        """
        if brush.get('lightmap_static') == static:
            return  # no change
        brush['lightmap_static'] = static
        self.mark_lighting_dirty()

    def count_static_brushes(self) -> int:
        return sum(1 for b in self.brushes if b.get('lightmap_static', False))

    # =========================================================================
    # ANGLED BRUSHES  (convex geometry / clipping)
    # =========================================================================

    def brush_is_angled(self, brush: dict) -> bool:
        """True if a brush carries convex geometry (i.e. has been clipped/angled)."""
        return _geo_has_geometry(brush)

    def clip_brush(self, brush: dict, normal, offset, keep_positive=False,
                   texture=None, uv_scale=None) -> bool:
        """Cut ``brush`` with a plane, turning it into an angled brush.

        ``normal``/``offset`` define the plane ``dot(normal, p) == offset``; the
        kept half is the inside (``<= offset``) side unless ``keep_positive``.
        Pushes an undo state and returns ``True`` on success (``False`` and no
        change if the cut would empty the brush).
        """
        self.save_state()
        if _geo_clip_brush(brush, normal, offset, keep_positive=keep_positive,
                           texture=texture, uv_scale=uv_scale):
            self.mark_lighting_dirty()
            return True
        # Nothing changed -> discard the undo snapshot we just pushed.
        if self.undo_stack:
            self.undo_stack.pop()
        return False

    def rotate_brush(self, brush: dict, angle_deg, axis, pivot=None) -> bool:
        """Rotate ``brush`` about ``pivot`` (default: its centre), making it angled."""
        self.save_state()
        if _geo_rotate_brush(brush, angle_deg, axis, pivot=pivot):
            self.mark_lighting_dirty()
            return True
        if self.undo_stack:
            self.undo_stack.pop()
        return False

    def reset_brush_to_box(self, brush: dict) -> None:
        """Drop convex geometry, returning the brush to its axis-aligned box form."""
        if not _geo_has_geometry(brush):
            return
        self.save_state()
        brush.pop('geometry', None)
        brush.pop('_geo_cache', None)
        brush.pop('_geo_cache_sig', None)
        self.mark_lighting_dirty()

    def simplify_brush_geometry(self, brush: dict) -> None:
        """If a geometry brush is really an axis-aligned box, drop to pos/size."""
        if _geo_has_geometry(brush) and _geo_is_box(brush):
            brush.pop('geometry', None)
            brush.pop('_geo_cache', None)
            brush.pop('_geo_cache_sig', None)

    # =========================================================================
    # SCENE MANAGEMENT
    # =========================================================================

    def set_selected_object(self, obj):
        """Sets the currently selected object."""
        self.selected_object = obj

    def clear_scene(self):
        """Resets the scene to an empty state."""
        self.brushes.clear()
        self.things.clear()
        self.selected_object = None
        self.terrain_data = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.mark_lighting_dirty()
        self.save_state()

    def get_level_data(self):
        """Serializes the current scene state into a dictionary."""
        data = {
            'version': 3,  # Version 3 adds stable entity IDs and logic graph layout
            'brushes': self._serialize_brushes(),
            'things': [t.to_dict() for t in self.things]
        }

        # Include terrain data if present
        if hasattr(self, 'terrain_data') and self.terrain_data:
            data['terrain_data'] = self.terrain_data

        # Persist the scene hash so we can skip a rebake on reload
        if self.bake_state is not None and self.bake_state.scene_hash:
            data['lightmap_scene_hash'] = self.bake_state.scene_hash

        # Persist logic graph node positions (if the window has been opened)
        graph_positions = self._collect_logic_graph_positions()
        if graph_positions:
            data['logic_graph'] = {'node_positions': graph_positions}

        return data

    def _collect_logic_graph_positions(self):
        """Collect node positions from the logic graph window, if open."""
        win = getattr(self, '_logic_graph_win', None)
        if win is None:
            # Fall back to previously-loaded positions so they survive
            # a save even when the graph window hasn't been opened.
            return getattr(self, '_logic_graph_positions', {})
        try:
            scene = win.get_scene()
            positions = {}
            for entity_id, node in scene._nodes.items():
                positions[entity_id] = {'x': node.x(), 'y': node.y()}
            return positions
        except Exception:
            return getattr(self, '_logic_graph_positions', {})

    def _serialize_brushes(self):
        """Serialize brushes with I/O connections."""
        serialized = []

        for brush in self.brushes:
            # Ensure every brush has a stable ID
            brush.setdefault('id', str(uuid.uuid4()))

            brush_copy = brush.copy()

            # Strip renderer-internal cache keys (GLM objects, not JSON-safe)
            for k in _RENDERER_PRIVATE_KEYS:
                brush_copy.pop(k, None)

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

            # Backfill stable ID for legacy maps
            brush.setdefault('id', str(uuid.uuid4()))

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
        """Populates the scene from a dictionary."""

        # Handle both old and new format
        version = level_data.get('version', 1)

        if version >= 2:
            # New format with I/O connections stored separately
            self.brushes = self._deserialize_brushes(level_data.get('brushes', []))
        else:
            # Old format - brushes are plain dicts
            self.brushes = level_data.get('brushes', [])
            # Backfill stable IDs for v1 brushes
            for brush in self.brushes:
                brush.setdefault('id', str(uuid.uuid4()))
            # Migrate legacy 'target' property to I/O connections
            if IO_AVAILABLE:
                for brush in self.brushes:
                    self._migrate_legacy_target(brush)

        self.terrain_data = level_data.get('terrain_data', None)

        # Store logic graph positions for later use by the graph window
        self._logic_graph_positions = {}
        lg = level_data.get('logic_graph', {})
        if lg:
            self._logic_graph_positions = lg.get('node_positions', {})

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

        # ===== NEW: Reset class counters based on loaded entity names =====
        update_all_counters_from_entities(self.brushes + self.things)

        self.selected_object = None
        self.undo_stack.clear()
        self.redo_stack.clear()

        # Restore lightmap bake state
        if self.bake_state is not None:
            saved_hash = level_data.get('lightmap_scene_hash', '')
            if saved_hash:
                # The scene has a saved hash — treat as dirty until the bake
                # system verifies the hash at Play time.  (Stage 5 feature.)
                self.bake_state.scene_hash = saved_hash
            # Always treat a freshly loaded scene as dirty so the bake runs
            # at least once before the first Play in this session.
            self.bake_state.mark_dirty()

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

    def _serialize_brushes_for_undo(self):
        """Serialize brushes for undo stack (deep copy with I/O)."""
        result = []
        for brush in self.brushes:
            brush_copy = copy.deepcopy(brush)

            # Strip renderer-internal cache keys.  These hold GLM matrix
            # objects (mat4x4 / mat3x3) that are not JSON-serialisable and
            # have no meaning outside the renderer's own lifetime.
            for k in _RENDERER_PRIVATE_KEYS:
                brush_copy.pop(k, None)

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

    def find_entity_by_id(self, entity_id: str):
        """Find an entity (brush or thing) by stable UUID."""
        if not entity_id:
            return None

        for brush in self.brushes:
            if brush.get('id') == entity_id:
                return brush

        for thing in self.things:
            if thing.properties.get('id') == entity_id:
                return thing

        return None

    def get_entity_id(self, entity):
        """Return the stable ID of an entity (brush dict or Thing)."""
        if isinstance(entity, dict):
            return entity.get('id', '')
        if hasattr(entity, 'properties'):
            return entity.properties.get('id', '')
        return ''

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

    def find_entities_targeting(self, target_name: str, target_id: str = ""):
        """Find all entities that have I/O connections to the target.

        Matches identity-addressed connections (by stable UUID) as well as
        name-addressed ones, so the result stays correct across renames.
        """
        sources = []

        if not IO_AVAILABLE:
            return sources

        def _matches(conn):
            if target_id and getattr(conn, 'target_id', '') == target_id:
                return True
            return bool(target_name) and conn.target_name == target_name

        for brush in self.brushes:
            for conn in get_connections(brush):
                if _matches(conn):
                    sources.append({
                        'entity': brush,
                        'connection': conn,
                        'type': 'brush'
                    })

        for thing in self.things:
            for conn in get_connections(thing):
                if _matches(conn):
                    sources.append({
                        'entity': thing,
                        'connection': conn,
                        'type': 'thing'
                    })

        return sources
