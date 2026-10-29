"""Controller polling must never pump a second native window event loop."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.qt


@pytest.mark.parametrize("video_driver", ["cocoa", "windows", "dummy"])
def test_controller_startup_uses_dummy_video_whatever_driver_was_asked_for(video_driver):
    # A fresh process catches driver selection errors that an SDL instance
    # initialized by another editor test would hide.
    env = dict(os.environ, SDL_VIDEODRIVER=video_driver, SDL_AUDIODRIVER="dummy")
    result = subprocess.run(
        [sys.executable, "-c", """
import pygame
from engine.sdl_runtime import init_input

init_input()
init_input()  # idempotent
assert pygame.display.get_driver() == 'dummy'
assert pygame.display.get_surface() is None
assert pygame.joystick.get_init()
assert not pygame.mixer.get_init(), 'input setup must leave audio to the view'
for _ in range(100):
    pygame.event.pump()
pygame.event.post(pygame.event.Event(pygame.JOYDEVICEADDED, device_index=0))
assert pygame.event.get(pygame.JOYDEVICEADDED), 'hotplug events must still arrive'
pygame.quit()
"""],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_editor_controller_polling_stays_on_the_dummy_backend(main_window):
    import pygame

    assert pygame.display.get_driver() == "dummy"
    for _ in range(100):
        main_window.view_3d._poll_gamepad()
    assert pygame.display.get_driver() == "dummy"
    assert pygame.display.get_surface() is None
