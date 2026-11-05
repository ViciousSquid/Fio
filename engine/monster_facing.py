"""Eight camera-relative views of a monster's world-space heading.

Angles follow PlayerStart: degrees, 0 = +Z, 90 = +X. Rotation 1 is
front, 2 front-left, 3 left, 4 back-left, 5 back, 6 back-right,
7 right and 8 front-right. A corpse uses its single rotation-0 image.
"""

import math
import os
import re

import numpy as np


def rotation_filename(filename, rotation):
    """Accept either a sprite prefix or any selected view of an eight-view set."""
    stem, ext = os.path.splitext(filename)
    stem = re.sub(r'_[1-8]$', '', stem)
    return '%s_%d%s' % (stem, rotation, ext)


def face_direction(thing, direction):
    """Face a horizontal movement/attack vector, retaining heading at rest."""
    x, z = float(direction[0]), float(direction[2])
    if not (math.isfinite(x) and math.isfinite(z)) or x*x + z*z < 1e-12:
        return
    angle = math.degrees(math.atan2(x, z)) % 360.0
    previous = float(thing.properties.get('angle', 0.0))
    # Ignore round-off in repeated float32 movement vectors.
    if abs((angle - previous + 180.0) % 360.0 - 180.0) > 1e-4:
        from .change_journal import touch
        thing.properties['angle'] = angle
        touch(thing)


def view_rotations(positions, angles, camera_pos):
    """Vectorised zero-based view indices; overhead/coincident views use front."""
    delta = np.asarray(camera_pos, dtype=np.float64)[[0, 2]] - positions[:, [0, 2]]
    bearing = np.degrees(np.arctan2(delta[:, 0], delta[:, 1]))
    relative = bearing - angles
    rotations = np.floor((relative + 22.5) / 45.0).astype(np.int64) % 8
    rotations[np.einsum('ij,ij->i', delta, delta) < 1e-12] = 0
    return rotations


def sprite_keys_for_view(table, slots, camera_pos):
    """Select sprite recipes per viewport without mutating the render table."""
    slots = np.asarray(slots, dtype=np.intp)
    keys = table.sprite_key_id[slots].copy()
    directions = getattr(table, 'sprite_direction_ids', None)
    if camera_pos is None or directions is None:
        return keys
    directional = directions[slots, 0] >= 0
    if directional.any():
        rows = slots[directional]
        rotations = view_rotations(table.pos[rows], table.monster_angle[rows], camera_pos)
        keys[directional] = directions[rows, rotations]
    return keys
