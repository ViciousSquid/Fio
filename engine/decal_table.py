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
rows when something bleeds; each frame :meth:`frame` hands the renderer two
arrays, the boxes and their ``(variant, opacity, kind, mirrored)``, faded out over the last
part of a decal's life as bullet marks are.

Placed decals -- the editor's ``Decal`` entities -- are rows of the same
table: the same box, image layer, kind and mirroring, published in the same
two arrays and painted by the same pass. They differ only in living until
the entity changes rather than fading, and in naming their image: ``IMAGE``
rows paint one (``engine.decal_textures`` gives its layer), ``WET`` rows use
it as a mask of where the surface is wet (an image with "wet" or
"puddle" in its name). :meth:`DecalTable.place` keeps
them in step with the entities, rebuilding only when one has changed.
"""

from __future__ import annotations

import math

import numpy as np

#: Most decals alive at once; a new one replaces the oldest.
CAPACITY = 128

_UP = np.array([0.0, 1.0, 0.0])
_SIDE = np.array([1.0, 0.0, 0.0])


#: What a decal is. Blood: what it landed on, its texture picked from that
#: kind's images. IMAGE: a placed decal painting one image. WET: a placed
#: decal whose image is a mask of where the surface is wet.
WALL, FLOOR, IMAGE, WET = 0, 1, 2, 3

#: The ``Decal`` entity properties a placed row is made from.
PLACED_KEYS = ('texture', 'width', 'height', 'depth', 'yaw',
               'pitch', 'roll', 'mirrored', 'opacity', 'hidden')


def placed_row(pos, props, layer):
    """``(model (16,), params (4,))`` of a placed decal at *pos* with
    *props*, painting image *layer*; None when it has no size."""
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
    f = np.asarray(forward, dtype=np.float64)
    f = f / max(np.linalg.norm(f), 1e-9)
    ref = _SIDE if abs(f[1]) > 0.9 else _UP
    right = np.cross(ref, f)
    right /= max(np.linalg.norm(right), 1e-9)
    up = np.cross(f, right)
    if roll:
        c, s = math.cos(roll), math.sin(roll)
        right, up = right * c + up * s, up * c - right * s
    m = np.zeros((4, 4), dtype=np.float64)
    m[:3, 0] = right * width
    m[:3, 1] = up * (width if height is None else height)
    m[:3, 2] = f * depth
    m[:3, 3] = centre
    m[3, 3] = 1.0
    return m.T.reshape(16).astype(np.float32)        # column-major


class DecalTable:
    """A ring of decal rows; see the module docstring."""

    __slots__ = ('model', 'variant', 'kind', 'flip', 'born', '_next', 'count', 'version',
                 'placed_model', 'placed_params', '_placed_key', '_missing')

    def __init__(self, capacity: int = CAPACITY):
        self.model = np.zeros((capacity, 16), dtype=np.float32)
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
        #: Placed decals' rows: they do not fade; :meth:`place` sets them.
        self.placed_model = np.zeros((0, 16), dtype=np.float32)
        self.placed_params = np.zeros((0, 4), dtype=np.float32)
        self._placed_key = None
        self._missing = set()

    @property
    def capacity(self) -> int:
        return len(self.born)

    def add(self, centre, forward, width, depth, variant, now, roll=0.0,
            height=None, kind=WALL, flip=False) -> int:
        """Add a decal box; returns its row."""
        row = self._next
        self.model[row] = box_matrix(centre, forward, width, depth, roll, height)
        self.variant[row] = float(int(variant))
        self.kind[row] = float(kind)
        self.flip[row] = 1.0 if flip else 0.0
        self.born[row] = now
        self._next = (row + 1) % self.capacity
        self.count += 1
        self.version += 1
        return row

    def place(self, things, show_selection=True):
        """Make the placed rows match the ``Decal`` entities in *things*.

        With *show_selection* (the editor, not play), a decal the Decal tool
        has selected is drawn tinted: its flags value (params[3], 1 for
        mirrored) gains 2.

        Rebuilt only when a decal was added, removed, moved or edited (a
        signature of what the rows are made from), so a still level costs a
        walk over its things and a tuple compare. A decal naming an image
        that is not in ``textures/decals`` is left out, and said once.
        """
        from engine import decal_textures
        decals = [t for t in things
                  if getattr(t, 'properties', None) is not None
                  and t.properties.get('type') == 'decal']
        version, names = decal_textures.images()
        key = (version, bool(show_selection)) + tuple(
            (id(t), tuple(t.pos), tuple(t.properties.get(k) for k in PLACED_KEYS),
             bool(getattr(t, 'editor_selected', False)))
            for t in decals)
        if key == self._placed_key:
            return False
        if any(decal_textures.layer_of(t.properties.get('texture'), names) < 0
               for t in decals if t.properties.get('texture')):
            version, names = decal_textures.refresh()       # an image was added?
            key = (version,) + key[1:]
        models, params = [], []
        for thing in decals:
            props = thing.properties
            if props.get('hidden'):
                continue
            layer = decal_textures.layer_of(props.get('texture'), names)
            if layer < 0:
                name = str(props.get('texture') or '')
                if name not in self._missing:
                    self._missing.add(name)
                    print(f"[Decal] No image '{name}' in textures/decals: "
                          f"{props.get('name', 'decal')} is not drawn")
                continue
            row = placed_row(thing.pos, props, layer)
            if row is not None:
                if show_selection and getattr(thing, 'editor_selected', False):
                    row[1][3] += 2.0
                models.append(row[0])
                params.append(row[1])
        self.placed_model = (np.stack(models) if models
                             else np.zeros((0, 16), dtype=np.float32))
        self.placed_params = (np.stack(params) if params
                              else np.zeros((0, 4), dtype=np.float32))
        self._placed_key = key
        self.version += 1
        return True

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
        """``(models (n, 16), params (n, 4))`` float32 for the renderer, or
        None when nothing is alive. ``params`` is ``(variant, opacity, kind,
        mirrored)``; opacity falls to 0 over the last *fade* seconds of
        *lifetime*.
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
            return self.model[rows].copy(), params
        # Placed decals first: blood lands on top of them.
        return (np.concatenate((self.placed_model, self.model[rows])),
                np.concatenate((self.placed_params, params)))
