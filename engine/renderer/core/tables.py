"""RenderTable / EntityTable lookups any renderer needs.

Batched model/normal matrices from table columns, per-table texture-id and
size resolution, sprite recipe resolution and model bounding radii. Instance
buffer layouts are technique-specific and live with each renderer.
"""

import math
import weakref

import numpy as np

from engine import change_detection, render_table
from engine.renderer.core.slot_transforms import SlotTransforms


#: Recipe lists whose GL resolution a renderer keeps parked: the two render
#: buffers' entity tables, the editor's own, and a spare for a table replaced
#: by a new play session.
_PARKED_RECIPE_LISTS = 4

#: Render tables a pass keeps derived arrays for: the two render buffers'
#: and one more (the editor's own, or a table replaced by a new session).
PASS_MEMO_TABLES = 3


def _seal_arrays(value):
    """Make every array in *value* (a dict, tuple, list or array) read-only."""
    if isinstance(value, np.ndarray):
        value.flags.writeable = False
    elif isinstance(value, dict):
        for item in value.values():
            _seal_arrays(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _seal_arrays(item)


def _same_value(kept, fresh):
    if isinstance(kept, dict):
        return kept.keys() == fresh.keys() and all(
            _same_value(kept[k], fresh[k]) for k in kept)
    if isinstance(kept, (list, tuple)):
        return len(kept) == len(fresh) and all(map(_same_value, kept, fresh))
    if isinstance(kept, np.ndarray) or isinstance(fresh, np.ndarray):
        return np.array_equal(kept, fresh)
    return kept == fresh


def _swap_recipe_cache(parked, current, recipes, fresh):
    """Park *current* (``(list, *state)``) and return *recipes*' state.

    A renderer resolves each interned recipe list once, but the lists come
    from several tables that take turns. Keyed by the list's identity, and
    the entry holds the list, so a recycled ``id`` cannot alias another.
    """
    if current[0] is not None:
        if len(parked) >= _PARKED_RECIPE_LISTS:
            parked.clear()
        parked[id(current[0])] = current
    entry = parked.pop(id(recipes), None)
    if entry is not None and entry[0] is recipes:
        return entry
    return (recipes,) + tuple(fresh)


def _forget_cache(caches, key, cache):
    """A render table was collected: drop its SlotTransforms (unless the id
    already names a newer table's)."""
    if caches.get(key) is cache:
        del caches[key]


class TablesMixin:
    """Dense-table lookups shared by renderer implementations."""

    def _slot_transforms(self, table):
        """*table*'s :class:`SlotTransforms`: every row's matrices, kept by
        change (one per render table; the two double-buffered tables have
        one each)."""
        caches = self._slot_transform_caches
        cache = caches.get(id(table))
        if cache is None or cache.table() is not table:
            for key in [k for k, c in caches.items() if c.table() is None]:
                del caches[key]
            cache = caches[id(table)] = SlotTransforms(table)
            # A table that goes (a finished session, a replaced projection)
            # takes its matrices with it at once.
            weakref.finalize(table, _forget_cache, caches, id(table), cache)
        return cache

    def _pass_memo(self, key, table, slots, state, objects, build):
        """What *build()* derives from *table* for *slots*, kept while
        neither changes (M3.1): the pass groups, payloads, run orders and
        face batches every frame used to derive again from the same columns.

        Kept per *key* (a pass, its view and its use in the frame) and per
        table -- the two render buffers' tables take turns, frame by frame,
        so one entry per key would be replaced every frame and never reused.
        An entry is reused while its table is at the same generation,
        :attr:`~engine.render_table.RenderTable.material_version` and
        ``extent_version``, *state* (a tuple of plain values) is equal, every
        one of *objects* is the same object (arrays a cache hands out, told
        by identity), and *slots* holds the same rows.

        *slots* is recognised by identity when it is a read-only array (M4):
        the visible set's producers -- the logic thread's cull, the passes'
        own kept arrays -- hand out the same immutable array while it stands,
        so a still frame costs no look at a row. Any other array is compared.
        The value's arrays are made read-only: shared, and versions in turn
        for what consumes them (an instance stream)."""
        tables = self._pass_memos.get(key)
        if tables is None:
            tables = self._pass_memos[key] = {}
        tid = id(table)
        entry = tables.get(tid)
        full = (table.generation, table.material_version, table.extent_version) + state
        if (entry is not None and entry[0]() is table and entry[1] == full
                and len(entry[2]) == len(objects)
                and all(a is b for a, b in zip(entry[2], objects))):
            if entry[3] is slots and not slots.flags.writeable:
                same = True                 # the same immutable rows
                if change_detection.ORACLE:
                    self._check_memo(key, entry, build)
            else:
                started = change_detection.now()
                same = change_detection.same_rows(entry[3], np.asarray(slots))
                change_detection.DETECT.add('pass_memo.rows', len(slots),
                                            change_detection.now() - started)
            if same:
                self.pass_memo_hits += 1
                return entry[4]
        self.pass_memo_misses += 1
        value = build()
        if entry is None and len(tables) >= PASS_MEMO_TABLES:
            # A table no longer drawn (a buffer replaced, a world unloaded):
            # the dead go first, then the longest kept.
            for dead in [t for t, e in tables.items() if e[0]() is None]:
                del tables[dead]
            while len(tables) >= PASS_MEMO_TABLES:
                del tables[next(iter(tables))]
        _seal_arrays(value)
        # The other tables' value for this pass, when it is the same: the two
        # render buffers' tables take turns, and a stream that is handed the
        # same arrays from both recognises them by identity (compared only
        # when a value is built, never on a still frame).
        for other_tid, other in tables.items():
            if other_tid != tid and other[0]() is not None and _same_value(other[4], value):
                value = other[4]
                break
        kept = (slots if isinstance(slots, np.ndarray) and not slots.flags.writeable
                else np.array(slots, copy=True))
        tables[tid] = (weakref.ref(table), full, tuple(objects), kept, value)
        return value

    @staticmethod
    def _check_memo(key, entry, build):
        """Oracle: a kept value is what deriving it afresh gives."""
        fresh = build()
        if not _same_value(entry[4], fresh):
            change_detection.missed(
                "pass %r kept arrays differ from a fresh derivation" % (key,))

    def _frame_transforms(self, table, slots):
        """Model and normal matrices for *slots*, into reusable buffers.

        Gathered from the table's :class:`SlotTransforms`, which rebuilds only
        the rows whose transform columns changed since it last built them
        (M3): a still world builds no matrix. The buffers are grown
        geometrically and never shrunk, so a steady-state frame allocates
        nothing.
        """
        count = len(slots)
        if len(self._brush_mat_buf) < count:
            capacity = max(count, 16, len(self._brush_mat_buf) * 2)
            self._brush_mat_buf = np.empty((capacity, 16), dtype=np.float32)
            self._brush_nmat_buf = np.empty((capacity, 9), dtype=np.float32)
        cache = self._slot_transforms(table)
        cache.sync(table)
        models = np.take(cache.models, slots, axis=0,
                         out=self._brush_mat_buf[:count])
        normals = np.take(cache.normals, slots, axis=0,
                          out=self._brush_nmat_buf[:count])
        return models, normals

    def _gl_texture_ids(self, table):
        """Return the dense texture-id array for one RenderTable projection.

        Texture ids are projection-local, so the renderer cache is keyed by
        table identity rather than by numeric id alone. A table resolves only
        names appended since its previous use; steady-state brush drawing still
        reads an array in the hot path.
        """
        names = table.texture_names()
        key = id(table)
        entry = self._gl_tex_by_table.get(key)
        # Valid only for the same table *and* the same name list: a table
        # that adopts another's state takes a copy of its list, whose ids need
        # not match the prefix this cache resolved.
        if entry is None or entry[0] is not table or entry[1] is not names:
            cached = np.zeros(0, dtype=np.int32)
        else:
            cached = entry[2]
        if len(cached) == len(names):
            return cached
        grown = np.zeros(len(names), dtype=np.int32)
        if len(cached):
            grown[:len(cached)] = cached
        for name_id in range(len(cached), len(names)):
            name = names[name_id]
            grown[name_id] = (
                self.texture_manager.get(self._tex_cache_path(name))
                or self.load_texture_callback(name, 'textures') or 0)
        self._gl_tex_by_table[key] = (table, names, grown)
        return grown

    def _texture_sizes_by_name_id(self, table):
        """Return texture dimensions using the same projection-local boundary."""
        names = table.texture_names()
        key = id(table)
        entry = self._tex_size_by_table.get(key)
        if entry is None or entry[0] is not table or entry[1] is not names:
            cached = np.zeros((0, 2), dtype=np.float32)
        else:
            cached = entry[2]
        if len(cached) == len(names):
            return cached
        grown = np.full((len(names), 2), 128.0, dtype=np.float32)
        if len(cached):
            grown[:len(cached)] = cached
        dims = getattr(self, '_texture_dimensions', {})
        for name_id in range(len(cached), len(names)):
            w, h = dims.get(self._tex_cache_path(names[name_id]), (128, 128))
            grown[name_id] = (w, h)
        self._tex_size_by_table[key] = (table, names, grown)
        return grown

    def _sprite_gl_ids(self, table):
        """``sprite id -> GL texture id``, for every recipe the table interned.

        The entity projection is GL-free, so it interns sprite *recipes* --
        ordered candidate cache keys and how to load each -- and the resolution
        to a GL id happens here, once per unique recipe, on the thread that has
        a context.  Exactly the shape :meth:`_gl_texture_ids` has for
        brush face textures.

        Each candidate is tried in the order the object path tried it: look the
        key up in the shared sprite-texture cache, and on a miss load the file
        if the recipe names one.  A recipe no candidate satisfies resolves to
        0, which is how the object path's "this sprite has no texture, draw
        nothing" is said numerically.
        """
        recipes = table.sprite_recipes()
        if recipes is not self._sprite_recipes_seen:
            # A different projection, so a different id space. The two render
            # buffers' tables alternate every frame, each with its own list,
            # so the other list's resolution is parked rather than dropped --
            # dropping it re-resolved every recipe on every frame, retrying
            # the file load of any sprite that is missing.
            (self._sprite_recipes_seen, self._sprite_gl_by_id,
             self._sprite_gl_resolved) = _swap_recipe_cache(
                self.__dict__.setdefault('_sprite_gl_parked', {}),
                (self._sprite_recipes_seen, self._sprite_gl_by_id,
                 self._sprite_gl_resolved),
                recipes, (np.zeros(0, dtype=np.int32), 0))
        cached = self._sprite_gl_by_id
        resolved = self._sprite_gl_resolved
        recipe_count = len(recipes)
        if resolved < recipe_count:
            if len(cached) < recipe_count:
                capacity = max(16, len(cached) * 2, recipe_count)
                grown = np.zeros(capacity, dtype=np.int32)
                if resolved:
                    grown[:resolved] = cached[:resolved]
                self._sprite_gl_by_id = grown
                cached = grown
            for sprite_id in range(resolved, recipe_count):
                cached[sprite_id] = self._resolve_sprite_recipe(recipes[sprite_id])
            self._sprite_gl_resolved = recipe_count
        return cached

    def _resolve_sprite_recipe(self, candidates):
        """The GL texture id for one interned candidate list, or 0."""
        for key, filename, subfolder, cache in candidates:
            if key:
                tex_id = self.sprite_textures.get(key)
                if tex_id:
                    return int(tex_id)
            if not filename:
                continue
            tex_id = self.load_texture(filename, subfolder)
            if tex_id:
                if cache and key:
                    self.sprite_textures[key] = tex_id
                return int(tex_id)
        return 0

    def _model_recipe_radii(self, table):
        """Bounding-sphere radius of each interned model recipe's mesh.

        Model space, measured once per recipe from the vertices the mesh was
        uploaded from. A recipe whose mesh is not loaded yet is infinite, so
        it is never culled before its bounds are known.
        """
        recipes = table.model_recipes()
        if recipes is not self._model_radius_recipes_seen:
            # As for sprites: park the other buffer's radii, do not re-measure
            # every mesh each time the buffers alternate.
            (self._model_radius_recipes_seen, self._model_radius_by_recipe,
             self._model_radius_pending) = _swap_recipe_cache(
                self.__dict__.setdefault('_model_radius_parked', {}),
                (self._model_radius_recipes_seen, self._model_radius_by_recipe,
                 self._model_radius_pending),
                recipes, (np.zeros(0, dtype=np.float64), ()))
        radii = self._model_radius_by_recipe
        if len(radii) < len(recipes):
            self._model_radius_pending += tuple(range(len(radii), len(recipes)))
            grown = np.full(len(recipes), np.inf, dtype=np.float64)
            grown[:len(radii)] = radii
            self._model_radius_by_recipe = radii = grown
        if not self._model_radius_pending:
            return radii
        pending = []
        for recipe_id in self._model_radius_pending:
            obj = self.load_model(recipes[recipe_id][0])
            vertices = getattr(obj, 'cpu_vertices', None) if obj else None
            if obj is None or not getattr(obj, 'is_loaded', False):
                pending.append(recipe_id)
                continue
            if vertices is None or not len(vertices):
                radii[recipe_id] = 0.0
                continue
            radii[recipe_id] = float(np.sqrt(
                (np.asarray(vertices, dtype=np.float64)[:, :3] ** 2)
                .sum(axis=1).max()))
            if math.isinf(radii[recipe_id]):
                pending.append(recipe_id)
        self._model_radius_pending = tuple(pending)
        return radii

    @staticmethod
    def _selected_slot(table, config):
        """The slot of the selected brush, or -1.

        One dictionary lookup per pass, so the per-brush ``brush is selected``
        identity compare becomes an integer compare.
        """
        selected = config.get('primary_selection')
        if table is None or selected is None or selected.brush_id is None:
            return -1
        slot = table.slot_of_id.get(selected.brush_id)
        return -1 if slot is None else int(slot)

    def entities_are_numeric(self, config, brush_slots=None):
        """Whether dense EntityTable state is available for this renderer."""
        etable = config.get('entity_table')
        thing_slots = config.get('visible_thing_slots')
        thing_hidden = config.get('thing_hidden')
        return (etable is not None and thing_slots is not None
                and thing_hidden is not None and len(thing_hidden) >= etable.count)
