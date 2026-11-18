"""engine.lens_water: water on the camera's lens after the eye surfaces.

Pinned: surfacing splashes the glass; going under clears it; the drops run
down (the bigger the faster, a bead barely creeps), wander, leave beads and
merge; turning the camera pushes them back; and -- whatever happens -- the
glass is dry again within LENS_WATER_SECONDS of surfacing, fading over the
last FADE_SECONDS, and is never drawn after it.
"""

import random

import pytest

from engine import lens_water as lw


def _run(lens, seconds, motion=(0.0, 0.0), step=1 / 60.0):
    for _ in range(int(round(seconds / step))):
        lens.update(step, motion)


def test_dry_until_the_eye_surfaces():
    lens = lw.LensWater(random.Random(1))
    _run(lens, 1.0)
    assert not lens.active and lens.frame() == []
    lens.surfaced()
    assert lens.active and 20 <= len(lens.drops) <= lw.MAX_DROPS
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and r > 0.0 and 0.0 < w <= 1.0
               for x, y, r, w, _s in lens.frame())


def test_going_under_clears_the_glass():
    lens = lw.LensWater(random.Random(2))
    lens.surfaced()
    lens.submerged()
    assert not lens.active and lens.frame() == []


@pytest.mark.parametrize("seed", range(12))
def test_never_longer_than_six_seconds(seed):
    """Whatever the drops do (and however the camera turns), nothing is left
    at LENS_WATER_SECONDS -- and it has faded by then."""
    assert lw.LENS_WATER_SECONDS == 6.0
    lens = lw.LensWater(random.Random(seed))
    lens.surfaced()
    rng = random.Random(seed + 100)
    elapsed = 0.0
    while elapsed < lw.LENS_WATER_SECONDS - 0.05:
        step = rng.choice((1 / 144.0, 1 / 60.0, 1 / 30.0, 0.1))
        lens.update(step, (rng.uniform(-1, 1), rng.uniform(-1, 1)))
        elapsed += step
        if elapsed > lw.LENS_WATER_SECONDS - 0.1 and lens.drops:
            assert max(w for _x, _y, _r, w, _s in lens.frame()) < 0.1   # all but gone
    lens.update(0.1)
    assert not lens.active and lens.frame() == [] and lens.drops == []


def test_a_long_frame_does_not_keep_it_past_the_limit():
    lens = lw.LensWater(random.Random(3))
    lens.surfaced()
    for _ in range(30):                        # 30 frames of 0.25 s (the cap)
        lens.update(1.0)
    assert not lens.active


def test_surfacing_again_wets_it_afresh():
    lens = lw.LensWater(random.Random(4))
    lens.surfaced()
    _run(lens, 5.5)
    lens.surfaced()
    assert lens.age == 0.0 and lens.fade() == 1.0 and len(lens.drops) > 20


def test_bigger_drops_run_down_faster():
    assert lw.run_speed(0.005) == lw.run_speed(lw.PIN_RADIUS) == lw.CREEP_SPEED
    sizes = [0.005, 0.015, 0.025, 0.035, lw.RUN_RADIUS, 0.06]
    speeds = [lw.run_speed(r) for r in sizes]
    assert speeds == sorted(speeds) and len(set(speeds)) == len(speeds)
    moved = {}
    for r in (0.008, 0.022, 0.04):
        lens = lw.LensWater(random.Random(5))
        lens.drops = [lw.Drop(0.5, 0.1, r, 1.0, rng=lens.rng)]
        lens.age = 0.0
        _run(lens, 1.0)
        moved[r] = (max(lens.drops, key=lambda d: d.r).y - 0.1) if lens.drops else 1.0
    assert 0.0 < moved[0.008] < 0.03 < moved[0.022] < moved[0.04]


def test_runners_leave_beads_and_gather_others():
    lens = lw.LensWater(random.Random(6))
    runner = lw.Drop(0.5, 0.05, 0.035, 1.0, rng=lens.rng)
    lens.drops = [runner]
    lens.age = 0.0
    _run(lens, 0.6)
    assert any(d.trail for d in lens.drops)                    # beads behind
    lens.drops = [lw.Drop(0.5, 0.2, 0.03, 1.0, rng=lens.rng),
                  lw.Drop(0.505, 0.215, 0.012, 1.0, rng=lens.rng)]
    lens._merge()
    assert len(lens.drops) == 1 and lens.drops[0].r > 0.03     # area adds


def test_turning_pushes_the_drops_back():
    def x_after(motion):
        lens = lw.LensWater(random.Random(7))
        lens.drops = [lw.Drop(0.5, 0.3, 0.008, 1.0, rng=lens.rng)]  # a bead: no wander
        lens.age = 0.0
        _run(lens, 0.5, motion)
        return lens.drops[0].x
    assert x_after((1.0, 0.0)) < 0.5 < x_after((-1.0, 0.0))


def test_the_sources_say_where_they_came_from():
    """The effect is MiniWind's camera-lens water, credited."""
    import inspect
    from engine.renderer.core import lens_water_gl
    assert "MiniWind" in inspect.getsource(lw) and "MiniWind" in inspect.getsource(lens_water_gl)


def test_standing_in_water_wets_the_bottom_half_only():
    lens = lw.LensWater(random.Random(8))
    for _ in range(120):                        # two seconds in the water
        lens.standing_in_water(1 / 60.0)
        lens.update(1 / 60.0)
    assert lens.active and len(lens.drops) >= 5
    assert all(y >= 0.45 for _x, y, _r, _w, _s in lens.frame())   # bottom half
    assert min(d.deadline for d in lens.drops) <= lens.clock + lw.WADE_SECONDS


def test_out_of_the_water_the_bottom_half_dries_within_its_time():
    lens = lw.LensWater(random.Random(9))
    for _ in range(60):
        lens.standing_in_water(1 / 60.0)
        lens.update(1 / 60.0)
    _run(lens, lw.WADE_SECONDS + 0.05)          # out of the water
    assert not lens.active and lens.frame() == []


@pytest.mark.parametrize("depth, under, wets", [
    (0.0, False, False), (0.2, False, False), (0.49, False, False),   # shallow
    (0.5, False, True), (0.9, False, True),                          # waist deep
    (1.0, True, False),                                              # under
])
def test_only_waist_deep_water_wets_the_bottom_half(depth, under, wets):
    assert lw.wets_bottom_half(depth, under) is wets


def test_bottom_half_drops_are_fewer_and_fainter_than_a_soaking():
    wading = lw.LensWater(random.Random(10))
    for _ in range(120):
        wading.standing_in_water(1 / 60.0)
        wading.update(1 / 60.0)
    soaked = lw.LensWater(random.Random(10))
    soaked.surfaced()
    soaked.update(1 / 60.0)
    assert 0 < len(wading.drops) < len(soaked.drops) / 3
    assert max(w for *_rest, w, _s in wading.frame()) <= lw.WADE_STRENGTH + 1e-9
    assert max(r for _x, _y, r, _w, _s in wading.frame()) <= lw.WADE_RADIUS[1] * 1.5
