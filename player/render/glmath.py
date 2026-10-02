"""Tiny numpy-backed matrix helpers for the player's own rendering.

The engine uses PyGLM, which has no python-for-android recipe (see
``player/README.md``). The player's *own* GL needs — a view/projection matrix, a
2D orthographic overlay, per-widget translate/scale — are small, so we express
them with numpy (which *does* have a p4a recipe) and keep PyGLM out of the
mobile render path until the full engine simulation is ported.

Matrices are returned as row-major ``float32`` 4x4 arrays. Upload them with
``glUniformMatrix4fv(loc, 1, GL_TRUE, mat)`` — the ``GL_TRUE`` transpose flag
tells GL to read our row-major data as its expected column-major layout.
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
# row-major float32, like everything else here (upload with GL_TRUE).


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
