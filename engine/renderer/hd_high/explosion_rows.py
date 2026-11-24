"""EXPLOSION instance rows: one per explosion drawn, back to front.

A row is the explosion's authored data -- where, how big, how bright, when it
started, whether it is the editor's frozen Preview (a Preview that is not
playing: one played from the panel runs like any other) -- laid out by
:data:`engine.shaders.EXPLOSION_INSTANCE_LAYOUT`, which also generates the
shader's attribute declarations. Nothing in a row moves with the clock (the
shader reads the time from a uniform), so the renderer uploads rows only when
they differ from the last upload.

A map plays a handful of explosions at once, so the rows are built by a
Python loop below :data:`EXPLOSION_ROWS_VECTOR_FROM` and by NumPy from it;
both give the same rows, to the bit, in the same order.
"""

import numpy as np

from engine.shaders import (
    EXPLOSION_COLUMNS, EXPLOSION_INSTANCE_FLOATS, EXPLOSION_TIME_WRAP)

#: Explosions a frame from which :func:`explosion_rows` builds the rows with
#: NumPy rather than a Python loop. A small-N crossover (the invariant in
#: tests/performance/test_small_n_invariant.py): below it the loop is
#: cheaper, because NumPy's fixed cost a call is paid however few the rows.
#: Measured (CPython 3.13, NumPy 2; re-measure with ``pytest
#: tests/performance/test_small_n_invariant.py -k benchmark -s
#: --run-benchmarks``)::
#:
#:     explosions    loop      NumPy
#:              1     2.0 us   14.3 us
#:              4     8.4 us   24.4 us
#:              8    15.9 us   24.9 us
#:             16    32.4 us   25.2 us
#:             32    67.3 us   26.8 us
#:            128   245.2 us   37.9 us
EXPLOSION_ROWS_VECTOR_FROM = 12

_X = EXPLOSION_COLUMNS['x']
_START = EXPLOSION_COLUMNS['start']
_WIDTH = EXPLOSION_COLUMNS['width']
_HEIGHT = EXPLOSION_COLUMNS['height']
_INTENSITY = EXPLOSION_COLUMNS['intensity']
_LIFETIME = EXPLOSION_COLUMNS['lifetime']
_SEED = EXPLOSION_COLUMNS['seed']
_PREVIEW = EXPLOSION_COLUMNS['preview']
_SKIP = EXPLOSION_COLUMNS['skip']
_R = EXPLOSION_COLUMNS['r']


def explosion_rows(table, slots, camera_xz, clock_origin):
    """The instance rows of the explosions *slots* (int32) of *table*,
    farthest from *camera_xz* (``(x, z)``, or None to keep their order)
    first. A list of row tuples below :data:`EXPLOSION_ROWS_VECTOR_FROM`
    explosions, a float32 array from it; :func:`as_float32` makes either
    the array the GPU takes."""
    if len(slots) < EXPLOSION_ROWS_VECTOR_FROM:
        return _rows_loop(table, slots, camera_xz, clock_origin)
    return _rows_table(table, slots, camera_xz, clock_origin)


def as_float32(rows):
    """*rows* (either form :func:`explosion_rows` gives) as float32."""
    if isinstance(rows, np.ndarray):
        return rows
    return np.asarray(rows, dtype=np.float32).reshape(
        len(rows), EXPLOSION_INSTANCE_FLOATS)


def same_rows(rows, previous):
    """Whether *rows* are what *previous* (an earlier result, or None)
    held: nothing needs uploading."""
    if previous is None or type(rows) is not type(previous):
        return False
    if isinstance(rows, np.ndarray):
        return rows.shape == previous.shape and np.array_equal(rows, previous)
    return rows == previous


def _f32(value):
    """*value* rounded to float32, as a Python float: what the loop's rows
    hold, so they compare equal to the NumPy rows once converted."""
    return float(np.float32(value))


def _rows_loop(table, slots, camera_xz, clock_origin):
    pos, params, size = table.pos, table.effect_params, table.sprite_size
    spawn, lifetime, seed = (table.effect_spawn_time, table.effect_lifetime,
                             table.effect_seed)
    preview, colour = table.effect_preview, table.effect_color
    active, skip = table.effect_active, table.effect_skip
    wrap = EXPLOSION_TIME_WRAP
    rows = []
    for slot in slots.tolist():
        x, y, z = pos[slot].tolist()
        r, g, b = colour[slot].tolist()
        rows.append((
            x, y, z,
            _f32((float(spawn[slot]) - clock_origin) % wrap),
            float(params[slot, 0]), float(size[slot, 1]),
            float(params[slot, 1]), float(lifetime[slot]),
            _f32(seed[slot]), 1.0 if preview[slot] and not active[slot] else 0.0,
            float(skip[slot]), 0.0,
            r, g, b, 0.0,
        ))
    if camera_xz is not None and len(rows) > 1:
        cx, cz = camera_xz
        far = [-((row[0] - cx) ** 2 + (row[2] - cz) ** 2) for row in rows]
        rows = [rows[i] for i in sorted(range(len(rows)), key=far.__getitem__)]
    return rows


def _rows_table(table, slots, camera_xz, clock_origin):
    if camera_xz is not None and len(slots) > 1:
        cx, cz = camera_xz
        dx = table.pos[slots, 0].astype(np.float64) - cx
        dz = table.pos[slots, 2].astype(np.float64) - cz
        slots = slots[np.argsort(-(dx * dx + dz * dz), kind='stable')]
    rows = np.zeros((len(slots), EXPLOSION_INSTANCE_FLOATS), dtype=np.float32)
    rows[:, _X:_X + 3] = table.pos[slots]
    rows[:, _START] = np.mod(
        table.effect_spawn_time[slots].astype(np.float64) - clock_origin,
        EXPLOSION_TIME_WRAP)
    rows[:, _WIDTH] = table.effect_params[slots, 0]
    rows[:, _HEIGHT] = table.sprite_size[slots, 1]
    rows[:, _INTENSITY] = table.effect_params[slots, 1]
    rows[:, _LIFETIME] = table.effect_lifetime[slots]
    rows[:, _SEED] = table.effect_seed[slots]
    rows[:, _PREVIEW] = table.effect_preview[slots] & ~table.effect_active[slots]
    rows[:, _SKIP] = table.effect_skip[slots]
    rows[:, _R:_R + 3] = table.effect_color[slots]
    return rows
