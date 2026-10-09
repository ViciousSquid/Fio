"""Editor view filters, GtkRadiant style: which kinds of object the editor shows.

Every brush and entity belongs to exactly one filter group. Turning a group off
hides its members in every editor view -- the 3D viewport through whichever
renderer is active, the 2D views, picking and the I/O / path-node connection
lines -- without touching the map. Filters apply while editing only; Play
always shows the world as it is.

The groups come from the same classification the dense tables already make
(:func:`engine.render_table._brush_class_bits`,
:func:`engine.entity_table._entity_class_bits`), so a filter can never
disagree with what the renderer was told a brush or entity is. Brushes are
filtered numerically over ``RenderTable.class_bits``; entities by their row in
the ``EntityTable``. The renderer is not involved: the logic thread publishes
fewer rows (see :meth:`ViewFilters.hidden_brush_rows`,
:meth:`ViewFilters.hidden_entity_rows`).

Beside the kinds of object (Hammer's *auto* visgroups) the editor has
**user visgroups** -- named sets of brushes and entities, by stable id, each
shown or hidden -- and a **cordon**: a box outside which nothing is shown
while it is on. Both are saved with the map (:meth:`ViewFilters.to_level_data`)
and, like the filter groups, apply while editing only.

GL-free and Qt-free.
"""

import numpy as np

from engine import entity_table as et
from engine import render_table as rt

#: ``(key, menu label)`` in menu order.
FILTERS = (
    ('world', 'World brushes'),
    ('movers', 'Movers & doors'),
    ('triggers', 'Triggers'),
    ('water', 'Water'),
    ('glass', 'Glass'),
    ('fog', 'Fog'),
    ('terrain', 'Terrain'),
    ('lights', 'Lights'),
    ('pathnodes', 'Path nodes'),
    ('monsters', 'Monsters'),
    ('props', 'Props & models'),
    ('effects', 'Effects'),
    ('portals', 'Portals'),
    ('logic', 'Logic entities'),
    ('sounds', 'Speakers'),
    ('player', 'Player starts'),
    ('entities', 'Other entities'),
)
KEYS = tuple(key for key, _label in FILTERS)

_LOGIC_CLASSES = frozenset({
    'LogicRelay', 'LogicGate', 'LogicTimer', 'LogicCommand', 'LogicCamera',
    'LogicSpawner', 'LogicState', 'LevelChanger'})


def brush_group(bits):
    """The filter group of a brush, from its RenderTable class bits."""
    bits = int(bits)
    if bits & rt.CLASS_TRIGGER:
        return 'triggers'
    if bits & rt.CLASS_DYNAMIC:
        return 'movers'
    if bits & rt.CLASS_WATER:
        return 'water'
    if bits & rt.CLASS_GLASS:
        return 'glass'
    if bits & rt.CLASS_FOG:
        return 'fog'
    return 'world'


def entity_group(thing):
    """The filter group of an entity."""
    bits = et._entity_class_bits(thing)
    if bits & et.ENT_EFFECT:
        return 'effects'
    if bits & et.ENT_PORTAL:
        return 'portals'
    if bits & et.ENT_PATH_NODE:
        return 'pathnodes'
    if bits & et.ENT_MONSTER:
        return 'monsters'
    if bits & et.ENT_LIGHT:
        return 'lights'
    if bits & (et.ENT_PROP | et.ENT_HAS_MODEL):
        return 'props'
    name = type(thing).__name__
    if name == 'Trigger':
        return 'triggers'
    if name == 'Speaker':
        return 'sounds'
    if name == 'PlayerStart':
        return 'player'
    if name in _LOGIC_CLASSES:
        return 'logic'
    return 'entities'


def object_id(obj):
    """The stable id of a brush dict or an entity, or ''."""
    if isinstance(obj, dict):
        return str(obj.get('id') or '')
    props = getattr(obj, 'properties', None)
    return str(props.get('id') or '') if isinstance(props, dict) else ''


class Visgroup:
    """A user visgroup: a name, whether it shows, and its members' ids."""

    def __init__(self, name, ids=(), visible=True):
        self.name = str(name)
        self.visible = bool(visible)
        self.ids = {str(i) for i in ids if i}

    def to_data(self):
        return {'name': self.name, 'visible': self.visible, 'ids': sorted(self.ids)}


class Cordon:
    """A box; while enabled, only what reaches into it is shown."""

    def __init__(self, enabled=False, lo=(-512.0, -512.0, -512.0), hi=(512.0, 512.0, 512.0)):
        self.enabled = bool(enabled)
        self.lo, self.hi = self._ordered(lo, hi)

    @staticmethod
    def _ordered(lo, hi):
        lo = [float(v) for v in lo]
        hi = [float(v) for v in hi]
        return ([min(a, b) for a, b in zip(lo, hi)], [max(a, b) for a, b in zip(lo, hi)])

    def contains_point(self, p):
        return all(self.lo[i] <= float(p[i]) <= self.hi[i] for i in range(3))

    def touches_box(self, lo, hi):
        return all(float(hi[i]) >= self.lo[i] and float(lo[i]) <= self.hi[i] for i in range(3))

    def to_data(self):
        return {'enabled': self.enabled, 'min': list(self.lo), 'max': list(self.hi)}


def brush_bounds(brush):
    """``(lo, hi)`` corners of a brush dict's box."""
    pos = brush.get('pos') or (0.0, 0.0, 0.0)
    size = brush.get('size') or (64.0, 64.0, 64.0)
    lo = [float(pos[i]) - float(size[i]) * 0.5 for i in range(3)]
    hi = [float(pos[i]) + float(size[i]) * 0.5 for i in range(3)]
    return lo, hi


class ViewFilters:
    """What the editor's views leave out: filter groups, visgroups, the cordon.

    Everything shows by default. ``version`` changes on every change, so views
    and caches can tell.
    """

    def __init__(self):
        self.hidden = set()
        self.version = 0
        self._entity_cache = (None, None)
        #: User visgroups, in the order they are listed.
        self.visgroups = []
        self.cordon = Cordon()
        #: Ids of every member of a hidden visgroup.
        self._hidden_ids = frozenset()

    @property
    def active(self):
        """Whether anything is filtered out."""
        return bool(self.hidden) or bool(self._hidden_ids) or self.cordon.enabled

    # -- user visgroups ------------------------------------------------------------

    def _changed(self):
        self._hidden_ids = frozenset().union(
            *(g.ids for g in self.visgroups if not g.visible))
        self.version += 1

    def add_visgroup(self, name, objects=()):
        """A new, shown visgroup holding *objects* (brush dicts or entities)."""
        group = Visgroup(name, (object_id(o) for o in objects))
        self.visgroups.append(group)
        self._changed()
        return group

    def remove_visgroup(self, group):
        if group in self.visgroups:
            self.visgroups.remove(group)
            self._changed()

    def rename_visgroup(self, group, name):
        group.name = str(name).strip() or group.name
        self.version += 1

    def set_visgroup_visible(self, group, visible):
        if group.visible != bool(visible):
            group.visible = bool(visible)
            self._changed()

    def add_to_visgroup(self, group, objects):
        group.ids.update(i for i in (object_id(o) for o in objects) if i)
        self._changed()

    def remove_from_visgroup(self, group, objects):
        group.ids.difference_update(object_id(o) for o in objects)
        self._changed()

    def visgroups_of(self, obj):
        oid = object_id(obj)
        return [g for g in self.visgroups if oid and oid in g.ids]

    # -- the cordon ------------------------------------------------------------------

    def set_cordon(self, enabled=None, lo=None, hi=None):
        cordon = self.cordon
        if enabled is not None:
            cordon.enabled = bool(enabled)
        if lo is not None or hi is not None:
            cordon.lo, cordon.hi = Cordon._ordered(
                lo if lo is not None else cordon.lo, hi if hi is not None else cordon.hi)
        self.version += 1

    # -- saved with the map ------------------------------------------------------------

    def to_level_data(self, live_ids=None):
        """``{'visgroups': [...], 'cordon': {...}}``; ids of deleted objects are dropped."""
        groups = []
        for g in self.visgroups:
            data = g.to_data()
            if live_ids is not None:
                data['ids'] = [i for i in data['ids'] if i in live_ids]
            groups.append(data)
        return {'visgroups': groups, 'cordon': self.cordon.to_data()}

    def load_level_data(self, level):
        """Take a map's visgroups and cordon (none in maps from before them)."""
        self.visgroups = []
        for data in (level or {}).get('visgroups') or []:
            if isinstance(data, dict) and data.get('name'):
                self.visgroups.append(Visgroup(data['name'], data.get('ids') or (),
                                               data.get('visible', True)))
        cordon = (level or {}).get('cordon') or {}
        try:
            self.cordon = Cordon(cordon.get('enabled', False),
                                 cordon.get('min', (-512, -512, -512)),
                                 cordon.get('max', (512, 512, 512)))
        except (TypeError, ValueError, AttributeError):
            self.cordon = Cordon()
        self._changed()

    def shows(self, key):
        return key not in self.hidden

    def set_shown(self, key, shown):
        """Show or hide group *key*. Returns True if that changed anything."""
        if key not in KEYS:
            raise KeyError(key)
        if shown == self.shows(key):
            return False
        (self.hidden.discard if shown else self.hidden.add)(key)
        self.version += 1
        return True

    def show_all(self):
        if self.hidden:
            self.hidden.clear()
            self.version += 1

    # -- authored objects (2D views, picking, connection lines) -----------------

    def hides_brush(self, brush):
        if self.hidden and brush_group(rt._brush_class_bits(brush)) in self.hidden:
            return True
        if self._hidden_ids and object_id(brush) in self._hidden_ids:
            return True
        return self.cordon.enabled and not self.cordon.touches_box(*brush_bounds(brush))

    def hides_thing(self, thing):
        if self.hidden and entity_group(thing) in self.hidden:
            return True
        if self._hidden_ids and object_id(thing) in self._hidden_ids:
            return True
        return self.cordon.enabled and not self.cordon.contains_point(thing.pos)

    def hides(self, obj):
        """Whether *obj* -- a brush dict or an entity -- is filtered out."""
        if isinstance(obj, dict):
            return self.hides_brush(obj)
        return self.hides_thing(obj)

    # -- dense rows (publication) ---------------------------------------------------

    def hidden_brush_rows(self, class_bits, table=None):
        """Bool mask over *class_bits* (``RenderTable.class_bits[:count]``).

        Visgroups and the cordon need the rows themselves: pass the
        RenderTable as *table* (without it only filter groups apply).
        """
        mask = self._hidden_brush_groups(class_bits)
        if table is not None and len(mask):
            count = len(mask)
            if self._hidden_ids:
                hidden_ids = self._hidden_ids
                mask |= np.fromiter((object_id(b) in hidden_ids
                                     for b in table.brushes[:count]),
                                    dtype=bool, count=count)
            if self.cordon.enabled:
                bounds = np.asarray(table.bounds[:count], dtype=np.float64)
                lo = bounds[:, :3] - bounds[:, 3:]
                hi = bounds[:, :3] + bounds[:, 3:]
                touches = ((hi >= self.cordon.lo) & (lo <= self.cordon.hi)).all(axis=1)
                mask |= ~touches
        return mask

    def _hidden_brush_groups(self, class_bits):
        bits = np.asarray(class_bits)
        if not self.hidden or not len(bits):
            return np.zeros(len(bits), dtype=bool)
        groups = np.select(
            [(bits & rt.CLASS_TRIGGER) != 0, (bits & rt.CLASS_DYNAMIC) != 0,
             (bits & rt.CLASS_WATER) != 0, (bits & rt.CLASS_GLASS) != 0,
             (bits & rt.CLASS_FOG) != 0],
            [KEYS.index(k) for k in ('triggers', 'movers', 'water', 'glass', 'fog')],
            default=KEYS.index('world'))
        hidden = np.zeros(len(KEYS), dtype=bool)
        hidden[[KEYS.index(k) for k in self.hidden]] = True
        return hidden[groups]

    def hidden_entity_rows(self, table):
        """Bool mask over the EntityTable's rows ``[:count]``.

        Entity groups need the entity's class, so the mask is built from the
        table's row objects -- once per filter or table change, not per frame.
        """
        count = int(table.count)
        if not self.active or not count:
            return np.zeros(count, dtype=bool)
        key = (self.version, id(table), table.generation, count)
        cached_key, mask = self._entity_cache
        if cached_key != key:
            hidden, hidden_ids = self.hidden, self._hidden_ids
            mask = np.fromiter(
                ((bool(hidden) and entity_group(thing) in hidden)
                 or (bool(hidden_ids) and object_id(thing) in hidden_ids)
                 for thing in table.things[:count]),
                dtype=bool, count=count)
            self._entity_cache = (key, mask)
        if self.cordon.enabled:
            # Entities move without a new generation: the cordon is checked
            # against where they are now, every time.
            pos = np.asarray(table.pos[:count], dtype=np.float64)
            inside = ((pos >= self.cordon.lo) & (pos <= self.cordon.hi)).all(axis=1)
            return mask | ~inside
        return mask
