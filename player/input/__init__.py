"""Input subsystem for the Fio Player.

The player unifies three physical input sources — an on-screen touch overlay, a
game controller, and (on the desktop dev harness) keyboard + mouse — into a
single normalized :class:`~player.input.state.InputState` that the game loop
consumes. The desktop engine's player controller reads keyboard/mouse directly;
on mobile we instead feed it this synthesized state, so the gameplay code does
not need to know how the input was produced.

The state model and the touch/gamepad math are deliberately free of any
windowing dependency so they can be unit-tested without pygame or a device.
"""

from .state import InputState, ButtonEdge
from .touch import TouchControls, TouchStick, TouchButton
from .gamepad import GamepadMapping, apply_gamepad

__all__ = [
    "InputState",
    "ButtonEdge",
    "TouchControls",
    "TouchStick",
    "TouchButton",
    "GamepadMapping",
    "apply_gamepad",
]
