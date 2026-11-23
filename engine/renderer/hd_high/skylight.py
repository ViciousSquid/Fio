"""The sky light volume: how much daylight reaches each part of a map.

No OpenGL here -- NumPy over the render table's columns, so it is testable
headlessly; ``environment.py`` uploads the result as a 3D texture and the
surface programs read it (``surface.fioSkyVisible``).

Sky light used to be all or nothing by what is overhead (a depth map of the
map seen from straight above): a room was dark however large its windows.
Here the map's standing shadow casters are voxelised into a grid of cells,
every cell with nothing above it is given the sky's full light, and the light
then spreads from cell to open cell -- sideways through a window or a door,
down a stairwell, under an overhang -- losing a fixed share with distance
(:data:`SPREAD`). It never passes through a solid cell, and a wall always
makes at least one closed layer of them (a wall thinner than a cell makes
two), so daylight reaches a room only through its openings.

The texture holds two numbers a cell: the light (0 in a solid cell) and
whether the cell is open. A shader divides one filtered value by the other,
which leaves the solid cells out of the interpolation: a point beside a wall
is lit by its own side of it.
"""

import math

import numpy as np

#: World units over which spreading sky light falls to 1/e (to a half in
#: about 150 units, to a tenth in about 500).
SPREAD = 220.0

#: Cell sizes tried, finest first: the first whose grid has no more than
#: :data:`MAX_CELLS` cells. A wall thinner than a cell still blocks; an
#: opening narrower than a cell may close.
CELL_SIZES = (16.0, 24.0, 32.0)
MAX_CELLS = 1_400_000

#: A map too large for the coarsest grid gets a window of this many cells
#: across (and at most this many high) that follows the view, in steps.
FOLLOW_CELLS = 160
FOLLOW_LAYERS = 48
FOLLOW_STEP = 8

#: Open cells kept around the casters (so light can flow round a building)
#: and above them (the sky).
MARGIN = 3

#: A filtered sample is trusted from this share of open cells around it
#: (none at OPEN_LOW, fully at OPEN_HIGH); surface.fioSkyVisible has the same.
OPEN_LOW = 0.02
OPEN_HIGH = 0.2

#: A surface point is read this many cells off its surface, along its
#: normal: into the room it faces, clear of the cells its own wall fills.
SURFACE_OFFSET = 0.75

#: Sweeps of the grid at most; a pass that changes nothing ends them sooner.
MAX_PASSES = 6


class Grid:
    """Where the volume is: its corner, its cell size and its cell counts
    ``(nx, ny, nz)``. ``follows`` is whether it is a window on a larger map
    (points outside it are then simply under the sky)."""

    __slots__ = ('origin', 'cell', 'shape', 'follows')

    def __init__(self, origin, cell, shape, follows=False):
        self.origin = tuple(float(v) for v in origin)
        self.cell = float(cell)
        self.shape = tuple(int(v) for v in shape)
        self.follows = bool(follows)

    @property
    def key(self):
        return (self.origin, self.cell, self.shape, self.follows)

    @property
    def extent(self):
        return tuple(n * self.cell for n in self.shape)


def grid_for(low, high, camera=None):
    """The grid for casters whose bounds are *low* .. *high* (world), seen
    from *camera* (used only when the map is too large for one grid)."""
    low = np.asarray(low, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    for cell in CELL_SIZES:
        first = np.floor(low / cell) - MARGIN
        last = np.ceil(high / cell) + MARGIN
        shape = (last - first).astype(np.int64)
        if int(shape[0]) * int(shape[1]) * int(shape[2]) <= MAX_CELLS:
            return Grid(first * cell, cell, shape)
    cell = CELL_SIZES[-1]
    first = np.floor(low / cell) - MARGIN
    last = np.ceil(high / cell) + MARGIN
    shape = (last - first).astype(np.int64)
    centre = np.asarray(camera if camera is not None else (low + high) * 0.5,
                        dtype=np.float64)
    step = FOLLOW_STEP
    for axis, limit in ((0, FOLLOW_CELLS), (1, FOLLOW_LAYERS), (2, FOLLOW_CELLS)):
        if shape[axis] <= limit:
            continue
        # Centred on the view, moved a step at a time so that a walk does
        # not rebuild the volume every frame.
        middle = math.floor(centre[axis] / (cell * step) + 0.5) * step
        start = middle - limit // 2
        if axis == 1:
            # The sky is above: never cut the top off before the bottom.
            start = max(first[axis], min(start, last[axis] - limit))
        first[axis] = start
        shape[axis] = limit
    return Grid(first * cell, cell, shape, follows=True)


def _rotation(rot):
    """The 3x3 rotation of a table row's ``[axis x, y, z, angle degrees]``."""
    x, y, z, angle = (float(v) for v in rot)
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9 or abs(angle) < 1e-6:
        return np.eye(3)
    x, y, z = x / length, y / length, z / length
    theta = math.radians(angle)
    c, s = math.cos(theta), math.sin(theta)
    t = 1.0 - c
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


def _mark_boxes(solid, grid, low, high):
    """Mark the cells of axis-aligned boxes (*low*, *high*: (N, 3) world).

    A cell is solid when its centre is inside the box, so a box wider than
    a cell neither grows nor shrinks by more than half a cell. A box thinner
    than that along one axis -- a wall, a floor -- still closes what it
    closes: it is given the two layers of cells it lies between the centres
    of. One thin along two axes (a beam, a post) blocks no light worth the
    name and is left out."""
    if not len(low):
        return
    shape = np.asarray(grid.shape)
    origin = np.asarray(grid.origin)
    glo = (low - origin) / grid.cell
    ghi = (high - origin) / grid.cell
    first = np.ceil(glo - 0.5).astype(np.int64)
    last = np.floor(ghi - 0.5).astype(np.int64)
    # Thin: the box lies between two neighbouring cells' centres. Both
    # cells are marked, so that no point beside it is filtered with a cell
    # of the other side (it would be lit, or darkened, through the wall).
    thin = last < first
    before = np.floor(glo - 0.5).astype(np.int64)
    first = np.where(thin, before, first)
    last = np.where(thin, before + 1, last)
    keep = (thin.sum(axis=1) <= 1) & (last >= 0).all(axis=1) & (first < shape).all(axis=1)
    first = np.clip(first[keep], 0, shape - 1)
    last = np.clip(last[keep], 0, shape - 1) + 1
    for (x0, y0, z0), (x1, y1, z1) in zip(first.tolist(), last.tolist()):
        solid[x0:x1, y0:y1, z0:z1] = True


def _mark_shape(solid, grid, centre, half, rot, record):
    """Mark the cells of one turned box or convex brush: those whose centre
    is inside it, grown by a third of a cell so a thin slanted wall leaves
    no holes between cells."""
    rotation = _rotation(rot)
    size = np.maximum(np.asarray(half, dtype=np.float64) * 2.0, 1e-6)
    corners = np.array([[x, y, z] for x in (-0.5, 0.5) for y in (-0.5, 0.5)
                        for z in (-0.5, 0.5)]) * size
    world = corners @ rotation.T + centre
    origin = np.asarray(grid.origin)
    shape = np.asarray(grid.shape)
    first = np.clip(np.floor((world.min(axis=0) - origin) / grid.cell).astype(np.int64) - 1,
                    0, shape)
    last = np.clip(np.ceil((world.max(axis=0) - origin) / grid.cell).astype(np.int64) + 1,
                   0, shape)
    if (last <= first).any():
        return
    axes = [origin[a] + (np.arange(first[a], last[a]) + 0.5) * grid.cell for a in range(3)]
    points = np.stack(np.meshgrid(*axes, indexing='ij'), axis=-1).reshape(-1, 3)
    local = (points - centre) @ rotation          # the brush's own axes, world units
    grow = grid.cell / 3.0
    convex = getattr(record, 'convex', None) if record is not None else None
    if convex is not None and getattr(convex, 'is_valid', False):
        # The planes are in the record's anchor box (origin, scale), which
        # the row's transform carries onto centre / half.
        anchor_origin = np.asarray(record.origin, dtype=np.float64)
        anchor_scale = np.asarray(record.scale, dtype=np.float64)
        normals, offsets = convex.plane_arrays()
        inside = ((anchor_origin + local / size * anchor_scale) @ np.asarray(normals).T
                  - np.asarray(offsets) <= grow).all(axis=1)
    else:
        inside = (np.abs(local) <= size * 0.5 + grow).all(axis=1)
    block = solid[first[0]:last[0], first[1]:last[1], first[2]:last[2]]
    block |= inside.reshape(block.shape)


def voxelise(table, slots, grid):
    """Which cells of *grid* the casters *slots* of *table* fill, as a bool
    array indexed ``[x, y, z]``."""
    solid = np.zeros(grid.shape, dtype=bool)
    slots = np.asarray(slots, dtype=np.intp)
    if not len(slots):
        return solid
    centre = np.asarray(table.center[slots], dtype=np.float64)
    half = np.asarray(table.half[slots], dtype=np.float64)
    rot = np.asarray(table.rot[slots])
    geometry = np.asarray(table.geometry_id[slots])
    shaped = (np.abs(rot[:, 3]) > 1e-6) | (geometry >= 0)
    plain = ~shaped
    _mark_boxes(solid, grid, centre[plain] - half[plain], centre[plain] + half[plain])
    records = getattr(table, 'geometry_records', None)
    for index in np.flatnonzero(shaped).tolist():
        record = None
        gid = int(geometry[index])
        if gid >= 0 and records is not None and gid < len(records):
            record = records[gid]
        _mark_shape(solid, grid, centre[index], half[index], rot[index], record)
    return solid


def _sweep(light, is_open, scratch, axis, keep):
    """One pass along *axis*, both ways: each slice takes what its
    neighbour hands on."""
    count = light.shape[axis]
    index = [slice(None)] * 3

    def at(i):
        index[axis] = i
        return tuple(index)

    view = scratch[at(0)]
    for order, step in ((range(1, count), 1), (range(count - 2, -1, -1), -1)):
        for i in order:
            here = light[at(i)]
            np.multiply(light[at(i - step)], keep, out=view)
            np.maximum(here, view, out=here)
            np.multiply(here, is_open[at(i)], out=here)


def spread(solid, cell, reach=SPREAD):
    """Sky light in every cell of *solid* (``[x, y, z]``, y up), 0..1.

    A cell with nothing above it has the sky's whole light. From there the
    light goes from cell to neighbouring open cell, keeping ``exp(-cell /
    reach)`` of itself each time; a cell holds the most that reaches it."""
    is_open = (~solid).astype(np.float32)
    # Lit from straight above: open, and so is everything over it.
    direct = np.logical_and.accumulate(~solid[:, ::-1, :], axis=1)[:, ::-1, :]
    light = direct.astype(np.float32)
    keep = np.float32(math.exp(-float(cell) / max(float(reach), 1e-3)))
    scratch = np.empty_like(light)
    before = None
    for _ in range(MAX_PASSES):
        for axis in (0, 2, 1):
            _sweep(light, is_open, scratch, axis, keep)
        total = float(light.sum(dtype=np.float64))
        if before is not None and total - before <= 1e-3 * max(before, 1.0):
            break
        before = total
    # Rounded off: the spread goes cell by cell along the axes, which alone
    # would draw its falloff as diamonds. Each open cell is averaged with its
    # open neighbours (solid ones are left out, so nothing crosses a wall).
    total = light * 2.0
    weight = is_open * 2.0
    for axis in range(3):
        for a, b in ((slice(1, None), slice(None, -1)), (slice(None, -1), slice(1, None))):
            to = [slice(None)] * 3
            frm = [slice(None)] * 3
            to[axis], frm[axis] = a, b
            total[tuple(to)] += light[tuple(frm)]
            weight[tuple(to)] += is_open[tuple(frm)]
    np.divide(total, weight, out=total, where=weight > 0.0)
    total *= is_open
    # What stands under the open sky keeps all of it.
    np.maximum(total, direct, out=total)
    return total


def texels(light, solid):
    """The volume as the texture holds it: ``uint16 [z, y, x, 2]`` -- the
    light (0 where solid) and whether the cell is open."""
    out = np.empty(light.shape[::-1] + (2,), dtype=np.uint16)
    out[..., 0] = np.rint(np.clip(light, 0.0, 1.0) * 65535.0).astype(np.uint16).transpose(2, 1, 0)
    out[..., 1] = np.where(solid, 0, 65535).astype(np.uint16).transpose(2, 1, 0)
    return out


def build(table, slots, grid, reach=SPREAD):
    """``(light, solid)`` for the casters *slots* of *table* in *grid*."""
    solid = voxelise(table, slots, grid)
    return spread(solid, grid.cell, reach), solid


def sample(light, solid, grid, point):
    """What a shader reads at world *point*: the light there, filtered over
    the open cells around it (the reference for ``surface.fioSkyVisible``)."""
    g = (np.asarray(point, dtype=np.float64) - np.asarray(grid.origin)) / grid.cell - 0.5
    shape = np.asarray(grid.shape)
    if (g[[0, 2]] < -0.5).any() or (g[[0, 2]] > shape[[0, 2]] - 0.5).any() or g[1] > shape[1] - 0.5:
        return 1.0
    g = np.clip(g, 0.0, shape - 1.0)
    base = np.minimum(np.floor(g).astype(np.int64), shape - 2)
    frac = g - base
    total = weight = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = ((frac[0] if dx else 1.0 - frac[0]) * (frac[1] if dy else 1.0 - frac[1])
                     * (frac[2] if dz else 1.0 - frac[2]))
                cell = (base[0] + dx, base[1] + dy, base[2] + dz)
                if not solid[cell]:
                    total += w * float(light[cell])
                    weight += w
    if weight <= OPEN_LOW:
        return 0.0
    # Hardly any open cell in reach: what little there is may lie round a
    # corner of thin walls, and is not to be trusted.
    trust = min(1.0, (weight - OPEN_LOW) / (OPEN_HIGH - OPEN_LOW))
    return total / weight * trust * trust * (3.0 - 2.0 * trust)
