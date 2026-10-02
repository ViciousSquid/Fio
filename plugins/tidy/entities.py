"""
Entity types for the Tidy plugin.

Three placeable ``Thing`` subclasses power "put everything away" games:

* :class:`TidyObject`   — a carryable prop the player picks up and stows.
* :class:`TidyReceptacle` — a shelf/bin/drop-zone that accepts objects of a
  category and arranges what it receives into a neat grid of slots.
* :class:`TidyGoal`     — an invisible logic entity that tracks how many
  objects have been stowed and fires ``OnComplete`` when the target is met.

``TidyObject`` subclasses the engine's ``Model`` so it renders as real 3D
geometry in play mode (via the existing ``draw_models`` path) and gets the
editor's model-path picker for free. The other two subclass ``Thing`` directly
and show as sprites in the editor.

These classes deliberately hold no runtime behaviour — that lives in
:mod:`plugins.tidy.runtime`. Here we only declare data (properties) so the
editor, serializer and property panel understand them.
"""

from __future__ import annotations

import random

# The Thing hierarchy normally lives in the editor package (PyQt-backed). In the
# standalone player / Android build there is no editor package, so fall back to
# the dependency-free base in plugins.entitybase. Whichever base is available in
# the current process is used for all three entity classes below.
try:
    from editor.things import Model, Thing
except Exception:  # pragma: no cover - exercised only in the PyQt-free player
    from plugins.entitybase import Model, Thing


#: The UV-mapped book model every TidyObject renders as. Its cover comes from
#: the per-instance ``texture`` (see COVERS). Resolved relative to the project
#: root by the renderer's model loader. Regenerate with tools/make_books.py.
BOOK_MODEL = "plugins/tidy/assets/book.obj"
#: Back-compat alias.
DEFAULT_TIDY_MODEL = BOOK_MODEL

#: Random book covers. Each TidyObject picks one at creation, so a pile/shelf of
#: books shows many different covers instead of identical boxes.
N_COVERS = 12
COVERS = [f"plugins/tidy/assets/covers/cover_{i:02d}.png" for i in range(1, N_COVERS + 1)]


class TidyObject(Model):
    """A single carryable object the player picks up and puts away.

    Key properties
    --------------
    category:      logical group (e.g. ``"book"``, ``"cup"``). A receptacle
                   only accepts objects whose category matches its ``accepts``.
    model_path:    3D model to render (defaults to the bundled box). Inherited
                   from ``Model``; editable via the model picker.
    tidied:        runtime flag — ``True`` once stowed. Reset on play start.
    no_collision:  defaults ``True`` so thousands of props don't each spawn a
                   collision brush; the player walks up to and through them.
    """

    # The plugin's own editor icon (a book). Resolved relative to the project
    # root, so it travels with the plugin. Regenerate with tools/make_sprites.py.
    pixmap_path = "plugins/tidy/assets/tidyobject.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        # Force the type regardless of what Model set, so serialization and the
        # I/O system route to the tidy handlers.
        self.properties['type'] = 'tidyobject'
        self.properties.setdefault('category', 'object')
        if not self.properties.get('model_path'):
            self.properties['model_path'] = BOOK_MODEL
        # Give each book a random cover (the renderer applies 'texture' as a
        # per-instance override). Kept once assigned so saved maps are stable.
        if not self.properties.get('texture'):
            self.properties['texture'] = random.choice(COVERS)
        self.properties.setdefault('scale', [1, 1, 1])
        self.properties.setdefault('rotation', [0, 0, 0])
        # Carryable props are non-solid by default (perf + feel).
        self.properties.setdefault('no_collision', True)
        # Runtime state (also serialised harmlessly).
        self.properties.setdefault('tidied', False)
        self.properties.setdefault('disabled', False)

    def get_category(self) -> str:
        return str(self.properties.get('category', 'object'))


class TidyReceptacle(Thing):
    """A drop-zone that accepts objects and arranges them into slots.

    Key properties
    --------------
    accepts:       category this receptacle takes, or ``"any"``.
    capacity:      maximum objects it can hold.
    slot_cols:     objects per row when arranging placed items.
    slot_spacing:  world-unit spacing between slots (X within a row, Y between
                   rows/shelves).
    slot_offset:   [x, y, z] offset of the first slot from the receptacle
                   origin (place it on the shelf surface).
    reach:         how close/aligned the player must be to place into it.
    """

    pixmap_path = "plugins/tidy/assets/tidyreceptacle.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'tidyreceptacle'
        self.properties.setdefault('accepts', 'any')
        self.properties.setdefault('capacity', 24)
        self.properties.setdefault('slot_cols', 6)
        self.properties.setdefault('slot_spacing', [28.0, 40.0, 0.0])
        self.properties.setdefault('slot_offset', [0.0, 0.0, 0.0])
        self.properties.setdefault('reach', 140.0)
        self.properties.setdefault('disabled', False)

    def accepts_category(self, category: str) -> bool:
        acc = str(self.properties.get('accepts', 'any')).strip().lower()
        return acc in ('', 'any', '*') or acc == str(category).strip().lower()

    def slot_world_pos(self, index: int):
        """World position of slot *index* (0-based) as a ``[x, y, z]`` list.

        Objects fill left-to-right along X, then stack upward in Y (shelves),
        centred on the receptacle's X. Purely geometric — no state.
        """
        cols = max(1, int(self.properties.get('slot_cols', 6)))
        sp = self.properties.get('slot_spacing', [28.0, 40.0, 0.0])
        off = self.properties.get('slot_offset', [0.0, 0.0, 0.0])
        try:
            sx, sy, sz = float(sp[0]), float(sp[1]), float(sp[2] if len(sp) > 2 else 0.0)
        except (TypeError, ValueError, IndexError):
            sx, sy, sz = 28.0, 40.0, 0.0
        col = index % cols
        row = index // cols
        # Centre the row horizontally around the receptacle origin.
        cx = (col - (cols - 1) / 2.0) * sx
        base = self.pos
        return [
            base[0] + float(off[0]) + cx,
            base[1] + float(off[1]) + row * sy,
            base[2] + float(off[2]) + row * sz,
        ]


class TidyGoal(Thing):
    """Tracks tidy progress and fires ``OnComplete`` when the target is met.

    Key properties
    --------------
    target:        ``"all"`` (every TidyObject in the map) or an integer count.
    category:      restrict the goal to objects of one category, or ``"any"``.
    show_hud:      show a live "N / M tidied" counter while playing.
    """

    pixmap_path = "plugins/tidy/assets/tidygoal.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'tidygoal'
        self.properties.setdefault('target', 'all')
        self.properties.setdefault('category', 'any')
        self.properties.setdefault('show_hud', True)
        self.properties.setdefault('disabled', False)

    def target_count(self, total_matching: int) -> int:
        """Resolve ``target`` against the number of matching objects in the map."""
        t = self.properties.get('target', 'all')
        if isinstance(t, str):
            if t.strip().lower() in ('all', '', '*'):
                return total_matching
            try:
                t = int(float(t))
            except ValueError:
                return total_matching
        try:
            t = int(t)
        except (TypeError, ValueError):
            return total_matching
        return max(0, min(t, total_matching)) if total_matching else max(0, t)

    def matches(self, category: str) -> bool:
        c = str(self.properties.get('category', 'any')).strip().lower()
        return c in ('', 'any', '*') or c == str(category).strip().lower()
