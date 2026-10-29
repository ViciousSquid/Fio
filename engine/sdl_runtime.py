"""SDL audio and controllers coexist with Qt's native window/event loop."""

import os


def init_input():
    """Enable controller events without opening a second native UI backend.

    pygame's event API needs an initialized video subsystem even when only
    joysticks are used. Its Cocoa backend also pumps NSApplication events,
    interfering with Qt's keyboard delivery in every text field. Use SDL's
    dummy video driver; Qt owns the windows, keyboard, mouse and OpenGL.
    """
    import pygame

    os.environ["SDL_VIDEODRIVER"] = "dummy"
    # SDL has no focused window of its own; controllers still need updates.
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    pygame.display.init()
    pygame.joystick.init()
