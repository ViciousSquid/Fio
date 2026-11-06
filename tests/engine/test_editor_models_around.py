"""The editor camera loads at the spawn, inside its sign: the sign is not drawn then."""

import numpy as np

from engine.constants import PLAYER_HULL_SIZE
from engine.entity_models import editor_models_around

HALF = [size / 2.0 for size in PLAYER_HULL_SIZE]


def test_a_camera_at_the_spawn_or_its_eye_is_inside_the_sign():
    spawns = np.array([[0.0, 40.0, 300.0], [500.0, 40.0, 0.0]])
    assert editor_models_around(spawns, (0.0, 40.0, 300.0), HALF).tolist() == [True, False]
    # The player's eye is 40 above the hull centre: still inside.
    assert editor_models_around(spawns, (0.0, 80.0, 300.0), HALF).tolist() == [True, False]


def test_a_camera_outside_the_sign_and_its_near_plane_still_sees_it():
    spawn = np.array([[0.0, 40.0, 300.0]])
    edge = 300.0 + HALF[2]
    assert editor_models_around(spawn, (0.0, 40.0, edge + 0.5), HALF).tolist() == [True]
    assert editor_models_around(spawn, (0.0, 40.0, edge + 2.0), HALF).tolist() == [False]
    assert editor_models_around(spawn, (0.0, 40.0, 900.0), HALF).tolist() == [False]


def test_no_rows():
    assert editor_models_around(np.empty((0, 3)), (0.0, 0.0, 0.0), HALF).shape == (0,)
