"""Live cutscene sampling against the frozen stable-sort implementation."""

import copy
import math
import random

import pytest

from engine.cutscene_runtime import CutsceneRuntime
from tests.helpers.cutscene_reference import reference_sample


sample = CutsceneRuntime._cutscene_sample


@pytest.mark.parametrize('elapsed', [-math.inf, -1.0, 0.0, 0.5, 1.0,
                                    1.5, 2.0, 3.0, math.inf, math.nan])
@pytest.mark.parametrize('initial', [None, [70.0, 80.0, 90.0]])
def test_equal_time_order_endpoints_and_nonfinite_elapsed(elapsed, initial):
    rows = [
        {'time': 2, 'pos': [20, 0, 0], 'yaw': 0.3},
        {'time': 0, 'pos': [0, 0, 0], 'yaw': 0.0},
        {'time': 1, 'pos': [10, 0, 0], 'yaw': 0.1},
        {'time': 0, 'pos': [1, 0, 0], 'yaw': 0.05},
        {'time': 1, 'pos': [11, 0, 0], 'yaw': 0.2},
        {'time': 2, 'pos': [21, 0, 0], 'yaw': 0.4},
    ]
    expected = reference_sample(rows, elapsed, initial, 0.75)
    actual = sample(rows, elapsed, initial, 0.75)
    assert actual == expected
    if any(expected is row for row in rows):
        assert actual is expected


@pytest.mark.parametrize('elapsed', [-1.0, 0.0, 0.25, 0.5, 1.0, 2.0])
def test_malformed_rows_and_normalized_timestamp_ties(elapsed):
    rows = [
        None, 'not a frame', 23, [],
        {'time': None, 'pos': [1, 2, 3]},
        {'time': 'nan', 'pos': [4, 5, 6]},
        {'time': math.inf, 'pos': [7, 8, 9]},
        {'time': [], 'pos': [10, 11, 12]},
        {'time': {}, 'pos': [13, 14, 15]},
        {'time': '0.5', 'pos': [16, 17, 18], 'teleport': True},
        {'time': 'invalid', 'pos': [19, 20, 21]},
        {'time': 1.0, 'pos': [22, 23, 24]},
    ]
    expected = reference_sample(rows, elapsed)
    actual = sample(rows, elapsed)
    assert actual == expected
    if any(expected is row for row in rows):
        assert actual is expected


@pytest.mark.parametrize('seed', range(8))
def test_random_tracks_match_original_sample_without_modification(seed):
    rng = random.Random(seed)
    choices = [None, False, 'bad', 'NaN', math.inf, -math.inf, [], {}, 0, 0.5, 1]
    rows = []
    for index in range(96):
        row = {
            'time': rng.choice(choices) if index % 4 == 0 else rng.uniform(-4, 12),
            'pos': [rng.uniform(-1000, 1000) for _ in range(3)],
            'yaw': rng.uniform(-6, 6),
            'pitch': rng.uniform(-1.5, 1.5),
            'fov': rng.uniform(30, 130),
            'teleport': rng.random() < 0.2,
            'look_at': {'actor': f'actor-{index}'},
        }
        rows.append(row)
        if index % 11 == 0:
            rows.append(None)
    before = copy.deepcopy(rows)
    for elapsed in [-5, -4, -1, 0, 0.5, 1, 12, 13, math.inf, math.nan] + [
            rng.uniform(-5, 13) for _ in range(40)]:
        assert sample(rows, elapsed, [1, 2, 3], 1.25) == reference_sample(
            rows, elapsed, [1, 2, 3], 1.25)
    assert rows == before


def test_track_edits_take_effect_on_the_next_sample():
    rows = [
        {'time': 0, 'pos': [0, 0, 0]},
        {'time': 1, 'pos': [10, 0, 0]},
        {'time': 2, 'pos': [20, 0, 0]},
    ]
    for mutate in (
            lambda: None,
            lambda: rows[1].update(time=3, pos=[30, 0, 0]),
            lambda: rows.append({'time': 0.75, 'pos': [40, 0, 0]}),
            lambda: rows.reverse(),
            lambda: rows.pop(0),
            lambda: rows.clear()):
        mutate()
        assert sample(rows, 0.5) == reference_sample(rows, 0.5)


def test_empty_invalid_and_single_frame_tracks_keep_original_identity():
    assert sample([], 1) is None
    assert sample([None, 1, 'frame'], 1) is None
    row = {'time': 2, 'pos': [1, 2, 3]}
    for elapsed in (-1, 2, 3, math.nan):
        assert sample([row], elapsed) is row
    assert sample([row], 1, [4, 5, 6], 2) == {
        'pos': [4, 5, 6], 'yaw': 2.0, '_before': True}


@pytest.mark.parametrize('elapsed', [0.0, 0.5, 1.0])
def test_duplicate_teleport_timestamp_preserves_destination_order(elapsed):
    rows = [
        {'time': 1, 'pos': [1000, 0, 0], 'teleport': True},
        {'time': 0, 'pos': [0, 0, 0]},
        {'time': 1, 'pos': [2000, 0, 0], 'teleport': True},
    ]
    assert sample(rows, elapsed) == reference_sample(rows, elapsed)
