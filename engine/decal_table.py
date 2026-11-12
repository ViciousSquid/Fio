"""Projected decals as a dense table (no GL): blood and placed decals.

A decal is a box in the world. The renderer draws the box and, for every
pixel it covers, takes the surface already drawn there (from the depth
buffer), expresses that point in the box's own space and paints the decal's
texture where the point lies inside -- so one decal wraps over a wall, the
floor under it and the corner between them, whatever the geometry is (see
``decal.vert``/``decal.frag`` in engine/shaders.py and
engine/renderer/forward/decals.py).

:class:`DecalTable` is the logic side: a fixed ring of rows, the oldest
overwritten first, each a box (a model matrix taking the unit cube to the
world), what it landed on (a wall or a floor: the two take different
images), a texture variant, whether it is mirrored and the time it was made. The logic thread adds
rows when something bleeds; each frame :meth:`frame` hands the renderer three
arrays, the boxes, their ``(variant, opacity, kind, flags)`` and the boxes'
inverses (worked out once, when a row is made, rather than for every vertex
of every frame), faded out over the last part of a decal's life as bullet
marks are.

Placed decals -- the editor's ``Decal`` entities -- are rows of the same
table: the same box, image layer, kind and mirroring, published in the same
two arrays and painted by the same pass. They differ only in living until
the entity changes rather than fading, and in naming their image: ``IMAGE``
rows paint one (``engine.decal_textures`` gives its layer), ``WET`` rows use
it as a mask of where the surface is wet (an image with "wet" or
"puddle" in its name).

:meth:`DecalTable.place` keeps them in step with the entities, and -- as
every table in Fio -- rebuilds only what changed. It is told what changed
rather than looking: a decal moved by assigning ``pos`` is in the change
journal (``engine.change_journal``), as is one the Decal tool selects or
edits (it calls ``touch``); a decal added, removed or edited by an undoable
editor gesture moves the world revision. A tick where neither happened
costs a few integer compares, whatever the number of decals; a tick where
one decal moved remakes that one row.
"""

from __future__ import annotations

import math

import numpy as np

from engine.change_journal import JOURNAL, OVERFLOW

#: Most decals alive at once; a new one replaces the oldest.
CAPACITY = 128


#: What a decal is. Blood: what it landed on, its texture picked from that
#: kind's images. IMAGE: a placed decal painting one image. WET: a placed
#: decal whose image is a mask of where the surface is wet.
WALL, FLOOR, IMAGE, WET = 0, 1, 2, 3

#: The ``Decal`` entity properties a placed row is made from.
PLACED_KEYS = ('texture', 'width', 'height', 'depth', 'yaw',
               'pitch', 'roll', 'mirrored', 'opacity', 'hidden')


def placed_row(pos, props, layer):
    """``(model (16,), params (4,))`` of a placed decal at *pos* with
    *props*, painting image *layer*; None when it has no size. Its inverse
    is :func:`box_inverse` of the model."""
    width = _number(props.get('width'), 128.0)
    height = _number(props.get('height'), width)
    depth = _number(props.get('depth'), 32.0)
    if width <= 0.0 or height <= 0.0 or depth <= 0.0:
        return None
    yaw = math.radians(_number(props.get('yaw'), 0.0))
    pitch = math.radians(_number(props.get('pitch'), -90.0))
    # Yaw 0 throws along +X, 90 along -Z (as an entity's angle faces);
    # pitch -90 throws straight down, onto the floor.
    forward = (math.cos(pitch) * math.cos(yaw), math.sin(pitch),
               -math.cos(pitch) * math.sin(yaw))
    model = box_matrix(tuple(float(v) for v in pos), forward, width, depth,
                       math.radians(_number(props.get('roll'), 0.0)), height)
    from engine.decal_textures import is_wet_image
    # Wet or Image by the image's name (wet / puddle): nothing else says so.
    kind = WET if is_wet_image(props.get('texture')) else IMAGE
    opacity = min(max(_number(props.get('opacity'), 1.0), 0.0), 1.0)
    params = np.array((float(layer), opacity, float(kind),
                       1.0 if props.get('mirrored') else 0.0), dtype=np.float32)
    return model, params


def _number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def box_matrix(centre, forward, width, depth, roll=0.0, height=None):
    """The column-major 4x4 (as 16 float32) taking the unit cube
    (-0.5..0.5) to a decal box: *width* across, *height* up (*width* if
    not given), *depth* along
    *forward* -- the direction the decal is projected in, into the surface
    -- turned by *roll* radians about it. With a level *forward* the box's
    up is the world's, so a texture's top is up on a wall."""
    # Plain floats: numpy's cross and norm on 3-vectors cost more than the
    # arithmetic, and a level's decals are all made at once when it loads.
    fx, fy, fz = (float(v) for v in forward)
    length = max(math.sqrt(fx * fx + fy * fy + fz * fz), 1e-9)
    fx, fy, fz = fx / length, fy / length, fz / length
    # right = ref x f, with ref the world's side for a near-vertical throw,
    # else its up.
    if abs(fy) > 0.9:
        rx, ry, rz = 0.0, -fz, fy                       # (1, 0, 0) x f
    else:
        rx, ry, rz = fz, 0.0, -fx                       # (0, 1, 0) x f
    length = max(math.sqrt(rx * rx + ry * ry + rz * rz), 1e-9)
    rx, ry, rz = rx / length, ry / length, rz / length
    ux, uy, uz = fy * rz - fz * ry, fz * rx - fx * rz, fx * ry - fy * rx   # f x right
    if roll:
        c, s = math.cos(roll), math.sin(roll)
        (rx, ry, rz), (ux, uy, uz) = ((rx * c + ux * s, ry * c + uy * s, rz * c + uz * s),
                                      (ux * c - rx * s, uy * c - ry * s, uz * c - rz * s))
    h = width if height is None else height
    cx, cy, cz = (float(v) for v in centre)
    return np.array((rx * width, ry * width, rz * width, 0.0,
                     ux * h, uy * h, uz * h, 0.0,
                     fx * depth, fy * depth, fz * depth, 0.0,
                     cx, cy, cz, 1.0), dtype=np.float32)       # column-major


def box_inverse(model):
    """The inverse of column-major *model* (16 or (n, 16) floats), the same
    layout: what takes a world point into the box's own unit cube. The
    decal shader reads it per instance instead of inverting the box for
    every vertex."""
    m = np.asarray(model, dtype=np.float64).reshape(-1, 4, 4)
    # Column-major as rows is the transpose; inv(M^T) = inv(M)^T, so the
    # rows of the result are the inverse's columns again.
    inv = np.linalg.inv(m).reshape(-1, 16).astype(np.float32)
    return inv[0] if np.ndim(model) == 1 else inv


def _is_decal(thing):
    props = getattr(thing, 'properties', None)
    return props is not None and props.get('type') == 'decal'


class DecalTable:
    """A ring of decal rows; see the module docstring."""

    __slots__ = ('model', 'inverse', 'variant', 'kind', 'flip', 'born', '_next',
                 'count', 'version', 'placed_model', 'placed_inverse',
                 'placed_params', '_entries', '_stamp', '_missing',
                 'rows_made', '__weakref__')

    def __init__(self, capacity: int = CAPACITY):
        self.model = np.zeros((capacity, 16), dtype=np.float32)
        self.inverse = np.zeros((capacity, 16), dtype=np.float32)
        self.variant = np.zeros(capacity, dtype=np.float32)
        #: WALL or FLOOR, and whether the texture is mirrored.
        self.kind = np.zeros(capacity, dtype=np.float32)
        self.flip = np.zeros(capacity, dtype=np.float32)
        #: perf_counter time each row was made; -inf where there is none.
        self.born = np.full(capacity, -np.inf, dtype=np.float64)
        self._next = 0
        #: Rows ever added (tests, Debug Tables).
        self.count = 0
        self.version = 0
        #: Placed decals' rows, in the level's order: they do not fade;
        #: :meth:`place` sets them.
        self.placed_model = np.zeros((0, 16), dtype=np.float32)
        self.placed_inverse = np.zeros((0, 16), dtype=np.float32)
        self.placed_params = np.zeros((0, 4), dtype=np.float32)
        #: id(decal) -> [decal, key, row or -1 when it is not drawn].
        self._entries = {}
        #: What the placed rows were last reconciled against; see place().
        self._stamp = None
        self._missing = set()
        #: Placed rows made (not reused) since the table was built: what
        #: tests and the benchmark read to see only changed rows remade.
        self.rows_made = 0
        JOURNAL.subscribe(self)

    @property
    def capacity(self) -> int:
        return len(self.born)

    def add(self, centre, forward, width, depth, variant, now, roll=0.0,
            height=None, kind=WALL, flip=False) -> int:
        """Add a decal box; returns its row."""
        row = self._next
        model = box_matrix(centre, forward, width, depth, roll, height)
        self.model[row] = model
        self.inverse[row] = box_inverse(model)
        self.variant[row] = float(int(variant))
        self.kind[row] = float(kind)
        self.flip[row] = 1.0 if flip else 0.0
        self.born[row] = now
        self._next = (row + 1) % self.capacity
        self.count += 1
        self.version += 1
        return row

    # -- placed decals ---------------------------------------------------------

    def place(self, things, show_selection=True, epoch=None):
        """Make the placed rows match the ``Decal`` entities in *things*;
        True when a row changed.

        With *show_selection* (the editor, not play), a decal the Decal tool
        has selected is drawn tinted: its flags value (params[3], 1 for
        mirrored) gains 2.

        *epoch* is the editor's world revision. While it, the list of
        things, its length, the image list and *show_selection* stay as
        they were, only the decals in the change journal are looked at, and
        only those whose row would differ are remade -- in place. Anything
        else (a load, an undo, a decal added or removed, a property edited,
        or no *epoch* given) reconciles: one walk over *things* that reuses
        the row of every decal whose key is unchanged and remakes the rest.
        A decal naming an image that is not in ``textures/decals`` is left
        out, and said once.
        """
        from engine import decal_textures
        # Drained first: a change made while this runs is seen next tick.
        pending = JOURNAL.drain(self)
        version, _names = decal_textures.images()
        show = bool(show_selection)
        stamp = (epoch, id(things), len(things), version, show)
        if epoch is None or pending is OVERFLOW or stamp != self._stamp:
            return self._reconcile(things, show, epoch)
        if not pending:
            return False
        entries = self._entries
        # Whichever is smaller: a battle journals every monster each tick.
        if len(pending) < len(entries):
            touched = [entries[oid] for oid in pending if oid in entries]
        else:
            touched = [entry for oid, entry in entries.items() if oid in pending]
        changed = False
        for entry in touched:
            thing = entry[0]
            key = self._key(thing, show)
            if key == entry[1]:
                continue
            row = self._row(thing, key, _names)
            if (row is None) != (entry[2] < 0):
                # Drawn now and not before, or the other way: the rows'
                # count changes, which only a reconcile does.
                return self._reconcile(things, show, epoch)
            entry[1] = key
            if row is not None:
                i = entry[2]
                self.placed_model[i], _, self.placed_params[i] = row
                self.placed_inverse[i] = box_inverse(row[0])
            changed = True
        if changed:
            self.version += 1
        return changed

    @staticmethod
    def _key(thing, show):
        """What a placed row is made from."""
        props = thing.properties
        return (tuple(thing.pos), tuple([props.get(k) for k in PLACED_KEYS]),
                show and bool(getattr(thing, 'editor_selected', False)))

    def _row(self, thing, key, names):
        """``(model, inverse, params)`` for *thing*, or None when it is not
        drawn (hidden, no size, or an image the list does not have)."""
        from engine import decal_textures
        props = thing.properties
        if props.get('hidden'):
            return None
        layer = decal_textures.layer_of(props.get('texture'), names)
        if layer < 0:
            return None
        row = placed_row(thing.pos, props, layer)
        if row is None:
            return None
        self.rows_made += 1
        model, params = row
        if key[2]:
            params[3] += 2.0
        return model, None, params          # the inverse: by the caller, batched

    def _reconcile(self, things, show, epoch):
        """The rows for every decal in *things*, reusing unchanged ones."""
        from engine import decal_textures
        version, names = decal_textures.images()
        decals = [t for t in list(things) if _is_decal(t)]
        if any(decal_textures.layer_of(t.properties.get('texture'), names) < 0
               for t in decals if t.properties.get('texture')):
            version, names = decal_textures.refresh()       # an image was added?
        old = self._entries
        if self._stamp is not None and self._stamp[3] != version:
            old = {}            # the layers moved: every row names a new one
        entries, sources, made = {}, [], []
        for thing in decals:
            oid = id(thing)
            key = self._key(thing, show)
            entry = old.get(oid)
            if entry is not None and entry[0] is thing and entry[1] == key:
                if entry[2] >= 0:
                    entries[oid] = [thing, key, len(sources)]
                    sources.append(entry[2])
                else:
                    entries[oid] = entry
                continue
            row = self._row(thing, key, names)
            if row is None:
                entries[oid] = [thing, key, -1]
                self._say_missing(thing, names)
                continue
            entries[oid] = [thing, key, len(sources)]
            sources.append(-1)
            made.append((len(sources) - 1, row))
        sources = np.asarray(sources, dtype=np.intp)
        changed = bool(made) or not np.array_equal(
            sources, np.arange(len(self.placed_model)))
        if changed:
            reused = sources >= 0
            model = np.empty((len(sources), 16), dtype=np.float32)
            inverse = np.empty((len(sources), 16), dtype=np.float32)
            params = np.empty((len(sources), 4), dtype=np.float32)
            if reused.any():
                model[reused] = self.placed_model[sources[reused]]
                inverse[reused] = self.placed_inverse[sources[reused]]
                params[reused] = self.placed_params[sources[reused]]
            if made:
                at = np.fromiter((i for i, _row in made), dtype=np.intp, count=len(made))
                model[at] = np.stack([row[0] for _i, row in made])
                params[at] = np.stack([row[2] for _i, row in made])
                inverse[at] = box_inverse(model[at])           # one batched inverse
            self.placed_model, self.placed_inverse, self.placed_params = (
                model, inverse, params)
            self.version += 1
        self._entries = entries
        self._stamp = (epoch, id(things), len(things), version, show)
        return changed

    def _say_missing(self, thing, names):
        from engine import decal_textures
        props = thing.properties
        if props.get('hidden') or decal_textures.layer_of(props.get('texture'), names) >= 0:
            return
        name = str(props.get('texture') or '')
        if name not in self._missing:
            self._missing.add(name)
            print(f"[Decal] No image '{name}' in textures/decals: "
                  f"{props.get('name', 'decal')} is not drawn")

    # -- blood, and the frame ----------------------------------------------------

    def clear(self):
        """Forget the blood; placed decals belong to the level and stay."""
        self.born[:] = -np.inf
        self._next = 0
        self.version += 1

    def live(self, now, lifetime) -> np.ndarray:
        """The rows still alive at *now*, oldest first."""
        rows = np.flatnonzero(now - self.born < lifetime)
        return rows[np.argsort(self.born[rows], kind='stable')]

    def frame(self, now, lifetime, fade):
        """``(models (n, 16), params (n, 4), inverses (n, 16))`` float32 for
        the renderer, or None when nothing is alive. ``params`` is
        ``(variant, opacity, kind, flags)``; opacity falls to 0 over the
        last *fade* seconds of *lifetime*. ``inverses`` are the boxes'
        (:func:`box_inverse`).
        Copies: the logic thread goes on writing the table."""
        rows = self.live(now, lifetime)
        placed = len(self.placed_model)
        if not len(rows) and not placed:
            return None
        params = np.zeros((len(rows), 4), dtype=np.float32)
        params[:, 0] = self.variant[rows]
        age = now - self.born[rows]
        params[:, 1] = np.clip((lifetime - age) / max(fade, 1e-6), 0.0, 1.0)
        params[:, 2] = self.kind[rows]
        params[:, 3] = self.flip[rows]
        if not placed:
            return self.model[rows].copy(), params, self.inverse[rows].copy()
        # Placed decals first: blood lands on top of them.
        return (np.concatenate((self.placed_model, self.model[rows])),
                np.concatenate((self.placed_params, params)),
                np.concatenate((self.placed_inverse, self.inverse[rows])))
