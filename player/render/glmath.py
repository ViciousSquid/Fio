"""Tiny numpy-backed matrix helpers for the player's own rendering.

The shared simulation uses the real PyGLM binding, built by our local Android
recipe. The player renderer uses NumPy arrays for interleaved buffers and
explicit matrix uploads on both desktop OpenGL and Android GLES.

Matrices are row-major float32 arrays. Upload ``column_major(mat)`` with
``transpose=GL_FALSE``: OpenGL ES forbids transposed matrix uploads.
"""

from __future__ import annotations

import math

import numpy as np


def identity() -> "np.ndarray":
    return np.identity(4, dtype=np.float32)


def translate(x: float, y: float, z: float = 0.0) -> "np.ndarray":
    m = identity()
    m[0, 3] = x
    m[1, 3] = y
    m[2, 3] = z
    return m


def scale(sx: float, sy: float, sz: float = 1.0) -> "np.ndarray":
    m = identity()
    m[0, 0] = sx
    m[1, 1] = sy
    m[2, 2] = sz
    return m


def rotate_z(theta: float) -> "np.ndarray":
    c, s = math.cos(theta), math.sin(theta)
    m = identity()
    m[0, 0] = c
    m[0, 1] = -s
    m[1, 0] = s
    m[1, 1] = c
    return m


def ortho(left: float, right: float, bottom: float, top: float,
          near: float = -1.0, far: float = 1.0) -> "np.ndarray":
    """Row-major orthographic projection (same formula as glm::ortho)."""
    m = identity()
    m[0, 0] = 2.0 / (right - left)
    m[1, 1] = 2.0 / (top - bottom)
    m[2, 2] = -2.0 / (far - near)
    m[0, 3] = -(right + left) / (right - left)
    m[1, 3] = -(top + bottom) / (top - bottom)
    m[2, 3] = -(far + near) / (far - near)
    return m


def mul(*mats: "np.ndarray") -> "np.ndarray":
    """Matrix product left-to-right (mul(A, B, C) == A @ B @ C)."""
    out = mats[0]
    for m in mats[1:]:
        out = out @ m
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# 3D camera
# ---------------------------------------------------------------------------
#
# The player's own 3D pass needs a view and a projection matrix, and it has to
# agree with the engine's conventions exactly or a map would look different in
# the player than in the editor: Y is up, yaw/pitch are degrees, and yaw -90
# looks down -Z.  These reproduce ``glm::perspective`` and ``glm::lookAt`` in
# row-major float32, like everything else here (upload with column_major and GL_FALSE).


def perspective(fovy: float, aspect: float, near: float, far: float) -> "np.ndarray":
    """Row-major perspective projection (same formula as glm::perspective).

    ``fovy`` is the vertical field of view in **radians**, matching GLM.
    """
    f = 1.0 / math.tan(fovy * 0.5)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def look_at(eye, target, up=(0.0, 1.0, 0.0)) -> "np.ndarray":
    """Row-major view matrix (same result as glm::lookAt).

    Maps ``eye`` to the view-space origin and points -Z at ``target``.
    """
    eye = np.asarray(eye, dtype=np.float32)
    target = np.asarray(target, dtype=np.float32)
    up = np.asarray(up, dtype=np.float32)

    forward = target - eye
    length = float(np.linalg.norm(forward))
    if length < 1e-8:
        forward = np.array([0.0, 0.0, -1.0], dtype=np.float32)
    else:
        forward = forward / length

    side = np.cross(forward, up)
    side_len = float(np.linalg.norm(side))
    if side_len < 1e-8:
        # Looking straight up or down: pick any horizontal axis for the roll.
        side = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    else:
        side = side / side_len
    true_up = np.cross(side, forward)

    m = identity()
    m[0, :3] = side
    m[1, :3] = true_up
    m[2, :3] = -forward
    m[0, 3] = -float(side @ eye)
    m[1, 3] = -float(true_up @ eye)
    m[2, 3] = float(forward @ eye)
    return m


def front_from_angles(yaw: float, pitch: float) -> "np.ndarray":
    """Unit facing direction from yaw/pitch in **degrees**.

    The engine's convention: yaw -90 with no pitch looks down -Z.
    """
    yaw_r = math.radians(yaw)
    pitch_r = math.radians(pitch)
    cos_pitch = math.cos(pitch_r)
    front = np.array([math.cos(yaw_r) * cos_pitch,
                      math.sin(pitch_r),
                      math.sin(yaw_r) * cos_pitch], dtype=np.float32)
    length = float(np.linalg.norm(front))
    if length < 1e-8:
        return np.array([0.0, 0.0, -1.0], dtype=np.float32)
    return front / length


def column_major(mat):
    """Contiguous column-major data for an ES-compatible matrix upload."""
    return np.ascontiguousarray(np.asarray(mat, dtype=np.float32).T)


def entity_matrix(position, properties):
    rotation = properties.get('rotation', (0, 0, 0))
    pitch, yaw, roll = (math.radians(float(v)) for v in rotation)
    cp, sp, cy, sy = math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    rx = identity()
    rx[1:3, 1:3] = ((cp, -sp), (sp, cp))
    ry = identity()
    ry[0, 0], ry[0, 2], ry[2, 0], ry[2, 2] = cy, sy, -sy, cy
    size = properties.get('scale', (1, 1, 1))
    if isinstance(size, (float, int)):
        size = (size, size, size)
    return mul(translate(*position), ry, rx, rotate_z(roll), scale(*size))
