"""Adaptive crossfire preserves CANARY's sphere and tick-cache contracts."""

import numpy as np
import pytest

from engine.monster_table import MonsterTable
from tests.helpers.crossfire import make_table, sorted_crossfire


@pytest.fixture(params=[0, 1 << 30], ids=['numpy', 'scalar'])
def path(request, monkeypatch):
    monkeypatch.setattr(MonsterTable, 'CROSSFIRE_NUMPY_MIN', request.param)


@pytest.mark.parametrize('count', [0, 1, 4, 15, 16, 17, 64, 257])
def test_matches_sorted_reference(path, count):
    rng = np.random.default_rng(27 + count)
    positions = rng.uniform(-400, 400, (count, 3))
    table = make_table(positions)
    for row, p in enumerate(table.props):
        p.update(team=['', 'red', 'blue'][row % 3],
                 hidden=row % 7 == 0, dead=row % 11 == 0)
    table.gather(table.monsters)
    for _ in range(100):
        shooter = int(rng.integers(max(count, 1)))
        start, end = rng.uniform(-500, 500, (2, 3))
        radius = float(rng.choice([1, 64, 80, 120]))
        # Both calls begin with exactly the same cache, including evictions.
        previous = None if table._live is None else table._live.copy()
        expected = sorted_crossfire(table, shooter, start, end, radius)
        expected_live = None if table._live is None else table._live.copy()
        table._live = previous
        assert table.crossfire(shooter, start, end, radius) is expected
        np.testing.assert_array_equal(table._live, expected_live)


@pytest.mark.parametrize('centre,end,radius,hit', [
    ((0, 0, 100), (0, 0, 200), 10, True),
    ((0, 0, -100), (0, 0, 200), 10, False),
    ((10, 0, 100), (0, 0, 200), 10, True),  # tangent
    ((10.000001, 0, 100), (0, 0, 200), 10, False),
    ((0, 0, 10), (0, 0, 200), 10, False),  # entry at origin
    ((0, 0, 0), (0, 0, 200), 10, False),   # inside sphere
    ((0, 0, 110), (0, 0, 100), 10, False), # entry at end
    ((0, 0, 109.999999), (0, 0, 100), 10, True),
    ((0, 0, .75), (0, 0, .999999), .1, False),  # too short
    ((0, 0, .75), (0, 0, 1), .1, True),
    ((100, 0, 0), (200, 0, 0), 10, True),
    ((0, -100, 0), (0, -200, 0), 10, True),
    ((0, 0, 100), (0, 0, 0), 10, False),
])
def test_ray_boundaries(path, centre, end, radius, hit):
    x, y, z = centre
    table = make_table([(500, 500, 500), (x, y - 64, z)])
    got = table.crossfire(0, (0, 0, 0), end, radius)
    assert got is (table.monsters[1] if hit else None)


def test_oblique_near_tangents_and_endpoints_keep_numpy_rounding(path):
    rng = np.random.default_rng(195)
    for trial in range(300):
        start = rng.uniform(-1000, 1000, 3)
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        end = start + direction * 1000
        if trial % 2:
            centre = end + direction * 80
        else:
            normal = np.cross(direction, [0, 0, 1])
            normal /= np.linalg.norm(normal)
            centre = start + direction * 500 + normal * 80
        centre[1] -= 64
        table = make_table([start + 10000, centre])
        expected = sorted_crossfire(table, 0, start, end)
        assert table.crossfire(0, start, end) is expected, trial


def test_near_equal_oblique_entries_keep_the_reference_winner(path):
    rng = np.random.default_rng(49)
    for trial in range(300):
        start = rng.uniform(-1000, 1000, 3)
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        normal = np.cross(direction, [0, 0, 1])
        normal /= np.linalg.norm(normal)
        centre = start + direction * 400
        left, right = centre + normal * 40, centre - normal * 40
        left[1] -= 64
        right[1] -= 64
        table = make_table([start + 10000, left, right])
        end = start + direction * 1000
        expected = sorted_crossfire(table, 0, start, end)
        assert table.crossfire(0, start, end) is expected, trial


def test_stale_front_keeps_near_equal_farther_entries_in_reference_order(path):
    rng = np.random.default_rng(49)
    for trial in range(100):
        start = rng.uniform(-1000, 1000, 3)
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        normal = np.cross(direction, [0, 0, 1])
        normal /= np.linalg.norm(normal)
        front = start + direction * 200
        left = start + direction * 400 + normal * 40
        right = start + direction * 400 - normal * 40
        for centre in (front, left, right):
            centre[1] -= 64
        table = make_table([start + 10000, front, left, right])
        end = start + direction * 1000
        assert table.crossfire(0, start, end) is table.monsters[1]
        table.props[1]['dead'] = True
        previous = table._live.copy()
        expected = sorted_crossfire(table, 0, start, end)
        expected_live = table._live.copy()
        table._live = previous
        assert table.crossfire(0, start, end) is expected, trial
        np.testing.assert_array_equal(table._live, expected_live)


def test_equal_distance_is_row_order_even_after_stale_ties(path):
    table = make_table([(0, -64, -200)] + [(0, -64, 100)] * 5)
    for row in range(1, 6):
        assert table.crossfire(0, (0, 0, 0), (0, 0, 300)) is table.monsters[row]
        table.props[row]['dead' if row % 2 else 'hidden'] = True
    assert table.crossfire(0, (0, 0, 0), (0, 0, 300)) is None
    np.testing.assert_array_equal(table._live, [True, False, False, False, False, False])


def test_exclusions_and_stale_nearest_fall_back_in_distance_order(path):
    table = make_table([(0, -64, 500), (0, -64, 400), (0, -64, 200),
                        (0, -64, 100), (0, -64, 300)])
    table.props[0]['team'] = table.props[3]['team'] = 'red'
    table.gather(table.monsters)
    shoot = lambda: table.crossfire(0, (0, 0, 0), (0, 0, 600))
    assert shoot() is table.monsters[2]  # shooter/ally never selected
    live = table._live
    table.props[2]['dead'] = True
    table.props[4]['hidden'] = True
    assert shoot() is table.monsters[1]
    assert table._live is live
    assert not live[2] and not live[4]
    # Evicted rows remain excluded even if shown/revived until gather.
    table.props[2]['dead'] = table.props[4]['hidden'] = False
    assert shoot() is table.monsters[1]
    table.gather(table.monsters)
    assert table._live is None
    assert shoot() is table.monsters[2]


def test_initially_hidden_target_joins_next_tick_and_far_flags_are_lazy(path):
    table = make_table([(0, -64, -100), (0, -64, 100),
                        (0, -64, 200), (0, -64, 300)])
    table.props[1]['hidden'] = True
    shoot = lambda: table.crossfire(0, (0, 0, 0), (0, 0, 500))
    assert shoot() is table.monsters[2]
    table.props[1]['hidden'] = False
    table.props[3]['hidden'] = True
    assert shoot() is table.monsters[2]
    assert table._live[3]  # farther hit has not been examined/evicted
    table.props[3]['hidden'] = False
    table.props[2]['dead'] = True
    assert shoot() is table.monsters[3]
    table.gather(table.monsters)
    assert shoot() is table.monsters[1]


def test_query_does_not_modify_geometry_and_gather_replaces_same_size_rows(path):
    table = make_table([(0, -64, -200), (0, -64, 100)])
    before = table.pos.copy()
    assert table.crossfire(0, (0, 0, 0), (0, 0, 300)) is table.monsters[1]
    np.testing.assert_array_equal(table.pos, before)
    other = make_table([(0, -64, -200), (0, -64, 200)])
    table.gather(other.monsters)
    assert table._live is None
    assert table.crossfire(0, (0, 0, 0), (0, 0, 300)) is other.monsters[1]


def test_threshold_dispatch(monkeypatch):
    used = []
    monkeypatch.setattr(MonsterTable, '_crossfire_scalar', lambda *a: used.append('scalar'))
    monkeypatch.setattr(MonsterTable, '_crossfire_numpy', lambda *a: used.append('numpy'))
    threshold = MonsterTable.CROSSFIRE_NUMPY_MIN
    for n in (threshold - 1, threshold):
        table = make_table([(0, 0, 0)] * n)
        table.crossfire(0, (0, 0, 0), (0, 0, 100))
    assert used == ['scalar', 'numpy']


def test_common_hit_does_not_sort(path, monkeypatch):
    table = make_table([(0, -64, -200)] + [(0, -64, 100)] * 100)
    def unexpected_sort(*args, **kwargs):
        pytest.fail('a live nearest hit should not sort the hit population')
    monkeypatch.setattr(np, 'lexsort', unexpected_sort)
    monkeypatch.setattr('engine.monster_table.sorted', unexpected_sort, raising=False)
    assert table.crossfire(0, (0, 0, 0), (0, 0, 300)) is table.monsters[1]


@pytest.mark.parametrize('count', [4, 8, 16, 24])
def test_small_queries_do_not_dispatch_numpy_even_on_first_shot(monkeypatch, count):
    table = make_table([(0, -64, -200)] +
                       [(0, -64, 100 * row) for row in range(1, count)])
    monkeypatch.setattr(MonsterTable, 'CROSSFIRE_NUMPY_MIN', count + 1)
    def unexpected_numpy(*args, **kwargs):
        pytest.fail('ordinary small queries must not dispatch NumPy')
    for name in ('asarray', 'array', 'sqrt', 'fromiter', 'flatnonzero',
                 'einsum', 'subtract', 'argmin', 'lexsort'):
        monkeypatch.setattr(np, name, unexpected_numpy)
    shoot = lambda: table.crossfire(0, (0, 0, 0), (0, 0, 10000))
    assert table._live is None
    assert shoot() is table.monsters[1]
    cache = table._live
    assert shoot() is table.monsters[1]
    table.props[1]['dead'] = True
    table.props[2]['hidden'] = True
    assert shoot() is table.monsters[3]
    assert table._live is cache
    assert not cache[1] and not cache[2]


def test_oblique_one_unit_cutoff_preserves_original_cache_initialization(path):
    rng = np.random.default_rng(83)
    table = make_table([(0, -64, -200), (0, -64, .75)])
    for _ in range(100):
        end = rng.normal(size=3)
        end /= np.linalg.norm(end)
        for scale in (np.nextafter(1., 0.), 1., np.nextafter(1., 2.)):
            table._live = None
            expected = sorted_crossfire(table, 0, (0, 0, 0), end * scale, .1)
            expected_live = None if table._live is None else table._live.copy()
            table._live = None
            assert table.crossfire(0, (0, 0, 0), end * scale, .1) is expected
            np.testing.assert_array_equal(table._live, expected_live)
