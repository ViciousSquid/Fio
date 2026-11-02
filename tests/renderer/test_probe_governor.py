"""ProbeGovernor: the probe budget must pay for itself.

The occlusion probes cost grouping work and draw calls on every probed
frame; whether that buys anything depends on the scene. These tests pin
the governor's rules with plain integers: shrink when the window shows no
savings, park at the floor, re-arm after the cooldown, and never let an
unprobed frame count against the probes.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.cell_cull import ProbeGovernor  # noqa: E402


def _fill(governor, frames, probed, skipped):
    for _ in range(frames):
        governor.record(probed, skipped)


def test_a_paying_window_keeps_the_full_budget():
    governor = ProbeGovernor(max_probes=48)
    _fill(governor, 32, 1000, 300)
    assert governor.probe_count() == 48


def test_the_boundary_counts_as_paying():
    # Exactly keep_fraction removed (10%) is not a shrink.
    governor = ProbeGovernor(max_probes=48, keep_fraction=0.10)
    _fill(governor, 32, 1000, 100)
    assert governor.probe_count() == 48


def test_an_unpaying_window_halves_the_budget():
    governor = ProbeGovernor(max_probes=48)
    _fill(governor, 32, 1000, 0)
    assert governor.probe_count() == 24
    _fill(governor, 32, 1000, 0)
    assert governor.probe_count() == 12
    _fill(governor, 32, 1000, 0)
    assert governor.probe_count() == 8


def test_at_the_floor_with_no_savings_it_parks_then_rearms():
    governor = ProbeGovernor(max_probes=48)
    for expected in (24, 12, 8):
        _fill(governor, 32, 1000, 0)
        assert governor.probe_count() == expected
    # Budget at the floor and the window still shows nothing saved: park.
    _fill(governor, 32, 1000, 0)
    assert governor.probe_count() == 0
    assert governor.state().startswith("parked")
    # Parked dark for the whole cooldown, then re-armed small to re-sample.
    for _ in range(governor.cooldown):
        assert governor.probe_count() == 0
    assert governor.probe_count() == 0  # the re-arming frame stays dark
    assert governor.probe_count() == 16
    assert governor.state() == "budget 16"


def test_a_good_window_after_a_shrink_steps_the_budget_back_up():
    governor = ProbeGovernor(max_probes=48)
    _fill(governor, 32, 1000, 0)
    assert governor.probe_count() == 24
    _fill(governor, 32, 1000, 900)
    assert governor.probe_count() == 48


def test_unprobed_frames_do_not_count_against_the_probes():
    governor = ProbeGovernor(max_probes=48)
    _fill(governor, 31, 1000, 300)
    governor.record(0, 0)  # a turn-suppressed frame: no sample
    # Window not full: no decision, budget untouched.
    assert governor.probe_count() == 48
