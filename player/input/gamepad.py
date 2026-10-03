"""Game controller mapping -> normalized input.

Android exposes controllers (Xbox, DualShock, 8BitDo, etc.) through the same
SDL2 game-controller API that pygame uses on the desktop, so one mapping serves
both. This module is intentionally free of pygame: it takes an already-read
snapshot of axis/button values and folds them into an :class:`InputState`. The
platform host is responsible for reading the raw device each frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .state import (
    InputState,
    ACTION_JUMP,
    ACTION_FIRE,
    ACTION_USE,
    ACTION_CROUCH,
    ACTION_SPRINT,
    ACTION_PAUSE,
)


@dataclass
class GamepadSnapshot:
    """Raw, already-read controller values for one frame.

    Axes are SDL convention: ``[-1, 1]`` with +y pointing *down* on the sticks,
    triggers ``[0, 1]``.
    """

    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    buttons: Dict[str, bool] = field(default_factory=dict)


@dataclass
class GamepadMapping:
    """Maps SDL controller button names to logical actions.

    Defaults follow the common shooter convention (A = jump, right trigger =
    fire, X = use). Override the dict to remap.
    """

    button_actions: Dict[str, str] = field(
        default_factory=lambda: {
            "a": ACTION_JUMP,
            "x": ACTION_USE,
            "b": ACTION_CROUCH,
            "leftstick": ACTION_SPRINT,
            "start": ACTION_PAUSE,
        }
    )
    look_sensitivity: float = 3.0
    stick_dead_zone: float = 0.18
    trigger_threshold: float = 0.35


def _dead_zone(v: float, dz: float) -> float:
    if abs(v) < dz:
        return 0.0
    # Rescale outside the dead zone to keep full range reachable.
    sign = 1.0 if v > 0 else -1.0
    return sign * (abs(v) - dz) / (1.0 - dz)


def apply_gamepad(
    state: InputState, snap: GamepadSnapshot, mapping: GamepadMapping
) -> None:
    """Fold a controller snapshot into ``state``."""
    lx = _dead_zone(snap.left_x, mapping.stick_dead_zone)
    ly = _dead_zone(snap.left_y, mapping.stick_dead_zone)
    # SDL left stick +y is down; forward should be +y.
    state.add_move(lx, -ly)

    rx = _dead_zone(snap.right_x, mapping.stick_dead_zone)
    ry = _dead_zone(snap.right_y, mapping.stick_dead_zone)
    state.add_look(rx * mapping.look_sensitivity, -ry * mapping.look_sensitivity)

    # Right trigger fires; left trigger sprints (common shooter feel).
    if snap.right_trigger >= mapping.trigger_threshold:
        state.set_button(ACTION_FIRE, True)
    if snap.left_trigger >= mapping.trigger_threshold:
        state.set_button(ACTION_SPRINT, True)

    for name, down in snap.buttons.items():
        if not down:
            continue
        action = mapping.button_actions.get(name)
        if action:
            state.set_button(action, True)
