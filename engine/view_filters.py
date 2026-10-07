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


class ViewFilters:
    """The set of filter groups currently hidden. Everything shows by default.

    ``version`` changes on every change, so views and caches can tell.
    """

    def __init__(self):
        self.hidden = set()
        self.version = 0
        self._entity_cache = (None, None)

    @property
    def active(self):
        """Whether anything is filtered out."""
        return bool(self.hidden)

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
        return bool(self.hidden) and brush_group(rt._brush_class_bits(brush)) in self.hidden

    def hides_thing(self, thing):
        return bool(self.hidden) and entity_group(thing) in self.hidden

    def hides(self, obj):
        """Whether *obj* -- a brush dict or an entity -- is filtered out."""
        if isinstance(obj, dict):
            return self.hides_brush(obj)
        return self.hides_thing(obj)

    # -- dense rows (publication) ---------------------------------------------------

    def hidden_brush_rows(self, class_bits):
        """Bool mask over *class_bits* (``RenderTable.class_bits[:count]``)."""
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
        if not self.hidden or not count:
            return np.zeros(count, dtype=bool)
        key = (self.version, id(table), table.generation, count)
        cached_key, mask = self._entity_cache
        if cached_key != key:
            hidden = self.hidden
            mask = np.fromiter(
                (entity_group(thing) in hidden for thing in table.things[:count]),
                dtype=bool, count=count)
            self._entity_cache = (key, mask)
        return mask
