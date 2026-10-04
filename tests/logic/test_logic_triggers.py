"""Focused unit tests for the extracted LogicTriggers runtime."""

from types import SimpleNamespace

import numpy as np
import pytest

from engine.logic_triggers import LogicTriggers


def _runtime():
    host = SimpleNamespace(
        player=SimpleNamespace(pos=np.array([0.0, 0.0, 0.0])),
        _trigger_use_prompt="",
        current_hud_message="",
        current_hud_key_name=None,
        io_manager=None,
        plugins=None,
    )
    return LogicTriggers(host)


def test_logic_triggers_filters_default_to_player():
    runtime = _runtime()
    assert runtime._trigger_filters({}) == {"player"}
    assert runtime._trigger_filters({"trigger_filters": "Player"}) == {"player"}


def test_logic_triggers_parses_multiple_filter_spellings():
    runtime = _runtime()
    assert runtime._trigger_filters({"trigger_filters": "player, props monsters"}) == {
        "player", "props", "monsters"
    }


def test_logic_triggers_use_radius_is_a_strict_sphere():
    runtime = _runtime()
    assert runtime.use_trigger_contains(99.0 ** 2, 100.0) is True
    assert runtime.use_trigger_contains(100.0 ** 2, 100.0) is False
    assert runtime.use_trigger_contains(101.0 ** 2, 100.0) is False


def test_logic_triggers_use_radius_supports_numpy_batches():
    runtime = _runtime()
    result = runtime.use_trigger_contains(
        np.array([25.0, 100.0, 125.0]), 10.0
    )
    assert result.tolist() == [True, False, False]


def test_logic_triggers_reset_clears_contact_state_in_place():
    runtime = _runtime()
    held = runtime.player_in_triggers
    held.add(42)
    runtime._trigger_contacts[42] = {"player"}

    runtime._reset_trigger_state()

    assert held is runtime.player_in_triggers
    assert held == set()
    assert runtime._trigger_contacts == {}


def test_logic_triggers_poll_interval_uses_authored_positive_value():
    runtime = _runtime()
    assert runtime._trigger_poll_interval({"trigger_poll_interval": 0.25}) == pytest.approx(0.25)


def test_logic_triggers_poll_interval_rejects_invalid_values():
    runtime = _runtime()
    assert runtime._trigger_poll_interval({"trigger_poll_interval": 0}) == pytest.approx(1.0)
    assert runtime._trigger_poll_interval({"trigger_poll_interval": -1}) == pytest.approx(1.0)
    assert runtime._trigger_poll_interval({"trigger_poll_interval": "bogus"}) == pytest.approx(1.0)
