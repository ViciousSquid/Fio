"""Headless tests for engine.view_distance (no GL context required).

The contract under test is the one the whole feature rests on: whatever the
view distance is set to, and whatever a map or a console command asks for, the
fog reaches full opacity strictly *before* the far plane clips. If that ever
stops holding, reducing the view distance starts popping geometry out of
existence at a hard edge, which is the exact failure this design exists to
prevent.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.view_distance import (  # noqa: E402
    AUTO_FOG_END_FRAC, AUTO_FOG_START_FRAC, DEFAULT_FOG_COLOR,
    DEFAULT_VIEW_DISTANCE, MAX_FOG_END_FRAC, MAX_VIEW_DISTANCE,
    MIN_VIEW_DISTANCE, ViewDistance, clamp_color,
)


# ---------------------------------------------------------------------------
# Defaults: an untouched ViewDistance must reproduce Fio's historical behaviour
# ---------------------------------------------------------------------------

def test_default_matches_the_editors_spinbox_default():
    assert ViewDistance().distance == DEFAULT_VIEW_DISTANCE == 4096.0


def test_default_ambient_is_black_so_existing_maps_are_unchanged():
    # Ambient is *added* to each shader's baked term, so black is a no-op.
    assert ViewDistance().ambient == (0.0, 0.0, 0.0)


def test_default_fog_color_is_the_editor_background():
    assert ViewDistance().fog_color == tuple(DEFAULT_FOG_COLOR)


def test_far_plane_is_the_view_distance():
    vd = ViewDistance(2500.0)
    assert vd.far_plane == vd.distance == 2500.0


def test_distance_sq_is_the_squared_radius_the_cull_compares():
    vd = ViewDistance(1500.0)
    assert vd.distance_sq == 1500.0 ** 2


# ---------------------------------------------------------------------------
# The core guarantee: fog is opaque before the clip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("distance", [500.0, 1000.0, 4096.0, 8192.0, 20000.0])
def test_fog_is_fully_opaque_before_the_far_plane(distance):
    vd = ViewDistance(distance)
    start, end = vd.resolve()
    assert start < end < vd.far_plane
    assert vd.fog_factor(end) == 1.0
    # ...and everything from there to the clip stays fully fogged, so the
    # fragments the far plane is about to discard are already invisible.
    assert vd.fog_factor(vd.far_plane) == 1.0


@pytest.mark.parametrize("distance", [500.0, 4096.0, 8192.0, 20000.0])
def test_an_explicit_fog_end_past_the_clip_is_pulled_back(distance):
    vd = ViewDistance(distance)
    vd.fog_end = distance * 10          # far beyond the far plane
    start, end = vd.resolve()
    assert end <= distance * MAX_FOG_END_FRAC
    assert end < vd.far_plane
    assert vd.fog_factor(vd.far_plane) == 1.0


def test_fog_end_exactly_at_the_clip_is_still_pulled_back():
    # Ending *at* the far plane leaves no margin: depth-test rounding would
    # show a sliver of un-fogged geometry right at the boundary.
    vd = ViewDistance(8192.0)
    vd.fog_end = 8192.0
    _, end = vd.resolve()
    assert end < 8192.0


def test_shrinking_the_view_distance_drags_automatic_fog_in_with_it():
    vd = ViewDistance(8192.0)
    wide_start, wide_end = vd.resolve()
    vd.distance = 2048.0
    near_start, near_end = vd.resolve()
    assert near_start < wide_start and near_end < wide_end
    assert near_end < vd.far_plane
    assert vd.fog_factor(vd.far_plane) == 1.0


def test_worked_example_from_the_spec():
    """8192 clip -> fog begins ~5000, dense ~7500, geometry clipped at 8192."""
    vd = ViewDistance(8192.0)
    start, end = vd.resolve()
    assert start == pytest.approx(8192.0 * AUTO_FOG_START_FRAC)   # ~4915
    assert end == pytest.approx(8192.0 * AUTO_FOG_END_FRAC)       # ~7537
    assert 4500.0 < start < 5500.0
    assert 7000.0 < end < 8000.0
    assert vd.far_plane == 8192.0


# ---------------------------------------------------------------------------
# The fog ramp itself
# ---------------------------------------------------------------------------

def test_no_fog_before_the_start_distance():
    vd = ViewDistance(4096.0)
    start, _ = vd.resolve()
    assert vd.fog_factor(0.0) == 0.0
    assert vd.fog_factor(start) == 0.0
    assert vd.fog_factor(start * 0.5) == 0.0


def test_the_ramp_is_monotonic_across_the_band():
    vd = ViewDistance(4096.0)
    start, end = vd.resolve()
    samples = [vd.fog_factor(start + (end - start) * i / 20.0) for i in range(21)]
    assert samples == sorted(samples)
    assert samples[0] == 0.0 and samples[-1] == 1.0


def test_density_thickens_the_band_without_moving_the_opaque_point():
    vd = ViewDistance(4096.0)
    start, end = vd.resolve()
    mid = (start + end) * 0.5
    linear = vd.fog_factor(mid)
    vd.fog_density = 0.002
    assert vd.fog_factor(mid) > linear
    # The band's ends are untouched: the clip stays hidden at any density.
    assert vd.fog_factor(start) == 0.0
    assert vd.fog_factor(end) == 1.0
    assert vd.resolve() == (start, end)


def test_disabling_fog_zeroes_the_factor_everywhere():
    vd = ViewDistance(4096.0)
    vd.fog_enabled = False
    assert vd.fog_factor(0.0) == 0.0
    assert vd.fog_factor(vd.far_plane) == 0.0
    # The band still resolves, so re-enabling restores it unchanged.
    assert vd.resolve()[1] < vd.far_plane


# ---------------------------------------------------------------------------
# Pinning and un-pinning the band
# ---------------------------------------------------------------------------

def test_explicit_values_are_used_verbatim_when_they_fit():
    vd = ViewDistance(8192.0)
    vd.fog_start = 5000.0
    vd.fog_end = 7500.0
    assert vd.resolve() == (5000.0, 7500.0)


def test_a_pinned_band_does_not_follow_the_view_distance():
    vd = ViewDistance(8192.0)
    vd.fog_start = 1000.0
    vd.fog_end = 2000.0
    vd.distance = 6000.0
    assert vd.resolve() == (1000.0, 2000.0)


def test_setting_back_to_none_resumes_tracking():
    vd = ViewDistance(8192.0)
    vd.fog_start = 100.0
    vd.fog_end = 200.0
    vd.fog_start = None
    vd.fog_end = None
    assert vd.resolve() == (8192.0 * AUTO_FOG_START_FRAC, 8192.0 * AUTO_FOG_END_FRAC)


def test_a_pinned_band_is_still_pulled_back_when_the_clip_moves_inside_it():
    vd = ViewDistance(8192.0)
    vd.fog_start = 5000.0
    vd.fog_end = 7500.0
    vd.distance = 2000.0                  # the player turns the draw distance down
    start, end = vd.resolve()
    assert end < 2000.0
    assert start < end
    assert vd.fog_factor(vd.far_plane) == 1.0


def test_start_above_end_is_ordered_not_inverted():
    vd = ViewDistance(4096.0)
    vd.fog_end = 1000.0
    vd.fog_start = 3000.0
    start, end = vd.resolve()
    assert start < end == 1000.0


def test_negative_inputs_are_floored_not_rejected():
    vd = ViewDistance(4096.0)
    vd.fog_start = -500.0
    vd.fog_end = -100.0
    start, end = vd.resolve()
    assert 0.0 <= start < end


# ---------------------------------------------------------------------------
# Clamping of the view distance itself
# ---------------------------------------------------------------------------

def test_view_distance_is_clamped_to_the_supported_span():
    vd = ViewDistance()
    vd.distance = 1.0
    assert vd.distance == MIN_VIEW_DISTANCE
    vd.distance = 10 ** 9
    assert vd.distance == MAX_VIEW_DISTANCE


def test_the_constructor_clamps_too():
    assert ViewDistance(0.0).distance == MIN_VIEW_DISTANCE
    assert ViewDistance(10 ** 9).distance == MAX_VIEW_DISTANCE


# ---------------------------------------------------------------------------
# Colour and ambient
# ---------------------------------------------------------------------------

def test_colors_are_clamped_into_range():
    vd = ViewDistance()
    vd.fog_color = (2.0, -1.0, 0.5)
    assert vd.fog_color == (1.0, 0.0, 0.5)


def test_an_unusable_color_leaves_the_previous_one_alone():
    vd = ViewDistance()
    before = vd.fog_color
    vd.fog_color = "not a colour"
    assert vd.fog_color == before


def test_clamp_color_falls_back_to_its_default():
    assert clamp_color(None, (0.25, 0.5, 0.75)) == (0.25, 0.5, 0.75)
    assert clamp_color((0.1, 0.2, 0.3)) == (0.1, 0.2, 0.3)


def test_ambient_level_sets_neutral_grey():
    vd = ViewDistance()
    vd.set_ambient_level(0.3)
    assert vd.ambient == (0.3, 0.3, 0.3)
    vd.set_ambient_level(5.0)
    assert vd.ambient == (1.0, 1.0, 1.0)


def test_ambient_accepts_a_colour_too():
    vd = ViewDistance()
    vd.ambient = (0.2, 0.1, 0.4)
    assert vd.ambient == (0.2, 0.1, 0.4)


def test_fog_density_cannot_go_negative():
    vd = ViewDistance()
    vd.fog_density = -3.0
    assert vd.fog_density == 0.0


# ---------------------------------------------------------------------------
# The console readout
# ---------------------------------------------------------------------------

def test_describe_marks_automatic_values_and_covers_every_setting():
    vd = ViewDistance(8192.0)
    rows = dict(vd.describe())
    assert rows["View Distance"] == "8192"
    assert "(auto)" in rows["Fog Start"] and "(auto)" in rows["Fog End"]
    for label in ("Distance Fog", "Fog Density", "Fog Color", "Ambient Light"):
        assert label in rows
    vd.fog_end = 6000.0
    assert "(auto)" not in dict(vd.describe())["Fog End"]
