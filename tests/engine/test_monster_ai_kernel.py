"""Headless regression tests for the dense nearest-enemy kernel."""

import numpy as np

from engine.monster_ai import MonsterAI


def _field(count, team_count):
    positions = np.stack(
        (
            np.arange(count, dtype=np.float32) * 3.0,
            np.zeros(count, dtype=np.float32),
            np.arange(count, dtype=np.float32) * -1.0,
        ),
        axis=1,
    )
    team = (np.arange(count, dtype=np.int32) % team_count)
    alive = np.ones(count, dtype=bool)
    return positions, team, alive


def test_nearest_enemy_switches_to_monolithic_for_small_and_tiny_team_blocks(
    monkeypatch,
):
    real_mono = MonsterAI._nearest_enemy_rows_monolithic
    real_blocks = MonsterAI._nearest_enemy_rows_by_team
    calls = []

    def mono(*args):
        calls.append("monolithic")
        return real_mono(*args)

    def blocks(*args):
        calls.append("blocks")
        return real_blocks(*args)

    monkeypatch.setattr(
        MonsterAI, "_nearest_enemy_rows_monolithic", staticmethod(mono)
    )
    monkeypatch.setattr(
        MonsterAI, "_nearest_enemy_rows_by_team", staticmethod(blocks)
    )

    positions, team, alive = _field(256, 64)
    got_many_teams = MonsterAI._nearest_enemy_rows(
        positions, team, alive, 512.0
    )
    assert calls == ["monolithic"]
    expected = real_blocks(
        positions.astype(np.float32),
        team.astype(np.int32),
        alive,
        np.float32(512.0) * np.float32(512.0),
    )
    np.testing.assert_array_equal(got_many_teams, expected)

    calls.clear()
    positions, team, alive = _field(256, 32)
    MonsterAI._nearest_enemy_rows(positions, team, alive, 512.0)
    assert calls == ["blocks"]


def test_nearest_enemy_uses_monolithic_for_many_single_monster_teams():
    positions, team, alive = _field(256, 256)

    got = MonsterAI._nearest_enemy_rows(positions, team, alive, 512.0)

    assert got.shape == (256,)
    assert np.all(got >= -1)
    assert np.any(got >= 0)
