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
import datetime
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
# Runtime-only AABB cache keys written by the physics/AI hot paths (see
# engine.constants.brush_aabb_bounds). Stripped on save/undo like the rest.
from engine.constants import AABB_RUNTIME_KEYS

_RENDERER_PRIVATE_KEYS = frozenset({
    '_mat_cache_key', '_mat_cache',      # model matrix cache (renderer_F)
    '_nmat_cache_key', '_nmat_cache',    # normal matrix cache (renderer_F)
    '_render_mesh', '_render_sig',       # angled-brush GPU mesh cache (renderer_F)
}) | GEO_RUNTIME_KEYS | frozenset(AABB_RUNTIME_KEYS)

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
        # The multi-selection lives here rather than being bolted on by the
        # main window, so undo/redo and scene loads can keep it pointing at
        # objects that are actually in the scene.
        self.selected_objects = []
        self.terrain_data = None
        self._logic_graph_positions = {}  # Persisted node positions for the logic graph
        self.created_at = ''              # ISO timestamp, set on first save
        self.undo_stack = deque(maxlen=50)
        self.redo_stack = []
        # The redo branch the most recent save_state() cleared, so a checkpoint
        # that guarded no change can be discarded without losing it.
        self._discarded_redo = None

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
        self.discard_last_checkpoint()
        return False

    def rotate_brush(self, brush: dict, angle_deg, axis, pivot=None) -> bool:
        """Rotate ``brush`` about ``pivot`` (default: its centre), making it angled."""
        self.save_state()
        if _geo_rotate_brush(brush, angle_deg, axis, pivot=pivot):
            self.mark_lighting_dirty()
            return True
        self.discard_last_checkpoint()
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
        self.selected_objects = [] if obj is None else [obj]

    def _invalidate_entity_caches(self):
        """Tell anything caching per-object data that the objects are changing.

        Undo, redo and loading a map all replace the brush dicts and Thing
        instances rather than editing them, so a cache keyed on an object's
        identity or contents cannot see it happen and would go on showing the
        entities that used to be there.
        """
        if IO_AVAILABLE:
            try:
                from .io_system import bump_io_revision
                bump_io_revision()
            except ImportError:      # pragma: no cover - I/O system optional
                pass

    def clear_scene(self):
        """Resets the scene to an empty state."""
        self._invalidate_entity_caches()
        self.brushes.clear()
        self.things.clear()
        self.selected_object = None
        self.selected_objects = []
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

        # When this map was first written. Set once and carried forward on every
        # later save, so it means "created" and not "saved most recently" — the
        # file's own mtime already answers the second question.
        if not getattr(self, 'created_at', ''):
            self.created_at = datetime.datetime.now().isoformat(timespec='seconds')
        data['created'] = self.created_at

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
        """The logic graph's node positions, for the map file.

        Read from ``_logic_graph_positions``, which the graph window keeps up to
        date (see ``LogicGraphScene.store_positions``). It used to try to reach
        the open window through ``self._logic_graph_win`` — but that attribute
        lives on the *main window*, not here, so the lookup always missed and
        every save fell back to whatever had been loaded from disk. Laid-out
        graphs were never saved.

        Having the graph push instead of this pulling also means the positions
        are right whether the window is open, has been closed, or was never
        opened at all.
        """
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

    def _deserialize_brushes(self, brushes_data, yield_hook=None):
        """Deserialize brushes with I/O connections."""
        result = []

        for index, brush_data in enumerate(brushes_data):
            if yield_hook is not None and index % 64 == 0:
                yield_hook()
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

    def load_from_data(self, level_data, *, yield_hook=None, save_undo=True):
        """Populates the scene from a dictionary.

        ``yield_hook`` is an optional cooperative callback used by long-running
        imports. Normal editor loads remain unchanged.
        """
        self._invalidate_entity_caches()

        # Handle both old and new format
        version = level_data.get('version', 1)

        if version >= 2:
            # New format with I/O connections stored separately
            self.brushes = self._deserialize_brushes(level_data.get('brushes', []), yield_hook=yield_hook)
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
        # Absent in maps written before this existed; the overview falls back to
        # the file's own timestamps rather than inventing one.
        self.created_at = level_data.get('created', '')

        # Store logic graph positions for later use by the graph window
        self._logic_graph_positions = {}
        lg = level_data.get('logic_graph', {})
        if lg:
            self._logic_graph_positions = lg.get('node_positions', {})

        # Load things
        things_data = level_data.get('things', [])
        new_things = []
        for index, t_data in enumerate(things_data):
            if yield_hook is not None and index % 25 == 0:
                yield_hook()
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
        self.selected_objects = []
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

        if save_undo:
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

    def _selection_identifiers(self):
        """Stable identifiers for the whole selection, for state restoration.

        Every selected object is recorded, not just ``selected_object``: the
        editor acts on ``selected_objects`` (component picking, the clip tool,
        the Surface Inspector's "whole brush" scope, group transforms, delete),
        so a restore that put back only the primary would leave the rest of the
        selection pointing at objects that are no longer in the scene.

        An object is identified by its position in its list *and* by its stable
        id, and :meth:`_restore_selection` prefers the id — indices shift when a
        state with a different object count is restored, ids do not.
        """
        selection = self._selection_list()
        if not selection:
            return []
        # One pass to build the position lookups rather than a list.index() per
        # selected object: save_state runs on every operation, and a big
        # selection on a big map would otherwise make it quadratic. Keyed by
        # identity, because two brushes can compare equal without being the
        # same brush.
        brush_at = {id(b): i for i, b in enumerate(self.brushes)}
        thing_at = {id(t): i for i, t in enumerate(self.things)}

        out = []
        for obj in selection:
            if isinstance(obj, dict):
                index = brush_at.get(id(obj))
                if index is not None:
                    out.append(['brush', index, obj.get('id', '')])
            else:
                index = thing_at.get(id(obj))
                if index is not None:
                    out.append(['thing', index, obj.properties.get('id', '')])
        return out

    def _selection_list(self):
        """The current selection, primary object first, with no duplicates."""
        selection = []
        if self.selected_object is not None:
            selection.append(self.selected_object)
        for obj in getattr(self, 'selected_objects', None) or ():
            if not any(obj is existing for existing in selection):
                selection.append(obj)
        return selection

    def _restore_selection(self, identifiers):
        """Re-point the selection at the objects the restore just rebuilt.

        Undo and redo replace every brush dict and Thing rather than editing
        them, so a selection held across one is a set of references to objects
        that are no longer in the scene.  Anything that goes on using them edits
        detached copies: the change appears to do nothing, and the detached
        objects stay alive in whatever widget or cache is holding them.
        """
        if not identifiers:
            self.selected_objects = []
            self.selected_object = None
            return
        # Same reasoning as _selection_identifiers: one pass to build the id
        # lookups instead of scanning the scene once per selected object.
        by_id = {'brush': {}, 'thing': {}}
        for brush in self.brushes:
            brush_id = brush.get('id')
            if brush_id:
                by_id['brush'].setdefault(brush_id, brush)
        for thing in self.things:
            thing_id = thing.properties.get('id')
            if thing_id:
                by_id['thing'].setdefault(thing_id, thing)

        restored = []
        seen = set()
        for entry in identifiers:
            kind, index, obj_id = (list(entry) + ['', -1, ''])[:3]
            pool = self.brushes if kind == 'brush' else self.things
            match = by_id.get(kind, {}).get(obj_id) if obj_id else None
            if match is None and isinstance(index, int) and 0 <= index < len(pool):
                match = pool[index]
            if match is not None and id(match) not in seen:
                seen.add(id(match))
                restored.append(match)
        self.selected_objects = restored
        self.selected_object = restored[0] if restored else None

    def snapshot(self):
        """The scene as it stands right now, as a JSON checkpoint string."""
        return json.dumps({
            'brushes': self._serialize_brushes_for_undo(),
            'things': [t.to_dict() for t in self.things],
            'selection': self._selection_identifiers(),
        })

    def save_state(self):
        """Checkpoint the scene *before* an operation changes it.

        Every tool in Fio calls this at the start of a gesture, not the end, so
        an entry on the undo stack is the state to go back *to* rather than a
        record of what the scene now looks like.  :meth:`undo` therefore has to
        capture the live scene itself — see the note there.
        """
        # Keep the redo branch we are about to drop, so an operation that turns
        # out to change nothing can put it back (see discard_last_checkpoint).
        self._discarded_redo = list(self.redo_stack)
        self.undo_stack.append(self.snapshot())
        self.redo_stack.clear()

    def discard_last_checkpoint(self):
        """Undo a :meth:`save_state` that turned out to guard no change.

        Several tools push a checkpoint optimistically at the start of a gesture
        — a component drag at mouse-down, the clip tool before it knows whether
        the plane cut anything — and drop it again when nothing moved, so the
        user's history has no empty step in it.  Popping the undo entry is only
        half of that: ``save_state`` also cleared the redo branch, and a
        cancelled drag that silently threw away everything the user could have
        redone is its own surprise.  Both are restored here.
        """
        if not self.undo_stack:
            return False
        self.undo_stack.pop()
        redo = getattr(self, '_discarded_redo', None)
        if redo is not None:
            self.redo_stack = redo
            self._discarded_redo = None
        return True

    def _serialize_brushes_for_undo(self):
        """Serialize brushes for undo stack (deep copy with I/O)."""
        result = []
        for brush in self.brushes:
            # Strip renderer-internal cache keys *before* the deep copy.  They
            # hold GLM matrices and cached convex geometry that are neither
            # JSON-serialisable nor meaningful outside the renderer's lifetime,
            # and deep-copying them first only to throw them away made every
            # undo checkpoint pay for geometry it discards.
            # Give every brush a stable id before it is checkpointed. Undo
            # rebuilds brush dicts from JSON, so the id is what lets the
            # selection (and anything else holding a reference) be re-pointed at
            # the brush that replaced it; a brush drawn in a view and not saved
            # since would otherwise have nothing to be recognised by.
            brush.setdefault('id', str(uuid.uuid4()))
            shallow = {k: v for k, v in brush.items()
                       if k not in _RENDERER_PRIVATE_KEYS}
            brush_copy = copy.deepcopy(shallow)

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
        self._invalidate_entity_caches()
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

        if 'selection' in state:
            self._restore_selection(state['selection'])
        else:
            # A checkpoint written before the whole selection was recorded.
            kind = state.get('selected_type')
            index = state.get('selected_index', -1)
            self._restore_selection([[kind, index, '']] if kind else [])

    def undo(self):
        """Step back one operation, making the current scene redoable.

        Two things about this are easy to get wrong, and both have been.

        What goes on the **redo** stack is a snapshot of the scene *as it is
        now*, not the checkpoint being popped.  The two are not the same thing:
        tools checkpoint at the *start* of a gesture (mouse-down), so the top of
        the undo stack is the state before the operation, and the operation's
        actual result only ever exists in the live scene.  Pushing the popped
        entry instead meant redo re-applied the state the undo had just
        restored — i.e. redo did nothing at all.

        What gets **restored** is the entry just popped, not the one under it.
        The popped entry *is* the state before the operation being undone;
        restoring its predecessor instead stepped back two gestures at a time,
        so a mapper who made three edits and pressed undo once lost two of them.
        """
        if len(self.undo_stack) <= 1:
            return False
        self.redo_stack.append(self.snapshot())
        self.restore_state(self.undo_stack.pop())
        return True

    def redo(self):
        """Re-apply the operation the last :meth:`undo` stepped back over."""
        if not self.redo_stack:
            return False
        state_json = self.redo_stack.pop()
        # Symmetrically: what makes the redo undoable again is the scene as it
        # is before the redo lands, which is exactly what undo restored.
        self.undo_stack.append(self.snapshot())
        self.restore_state(state_json)
        return True

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
