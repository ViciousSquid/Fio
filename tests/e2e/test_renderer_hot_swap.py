"""The renderer swaps live through QtGameView.switch_renderer, in the editor and in Play.

Frames are drawn by ``QtGameView.paintGL`` on a real OpenGL context. A second
renderer that shares nothing with ``ForwardRenderer`` -- it fills the view with
one colour -- is registered by name and swapped in, then the built-in one is
swapped back. Swapping back must give the same picture as before: the host
reloads everything it holds (sprite table, grid, terrain binding) through the
renderer that takes over, so nothing depends on the retired one's resources.
"""

import time

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from editor.things import Light, PlayerStart      # noqa: E402
from engine.renderer import registry               # noqa: E402
from tests.helpers.renderers import MAGENTA, FlatRenderer  # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]


def _broken(config):
    raise RuntimeError("this renderer cannot start")


@pytest.fixture
def renderers():
    saved = dict(registry._factories)
    FlatRenderer.created.clear()
    FlatRenderer.cleaned.clear()
    registry.register_renderer("Flat", FlatRenderer)
    registry.register_renderer("Broken", _broken)
    yield
    registry._factories.clear()
    registry._factories.update(saved)


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, -200.0), radius=2000.0,
                      intensity=2.0, casts_shadows=True)
    crate = box_brush("crate", (0.0, 64.0, -300.0), (128.0, 128.0, 128.0))
    return level_data(brushes=room(size=2048.0, height=512.0) + [crate],
                      things=[spawn, lamp])


def _magenta_fraction(image):
    return float(np.all(image == MAGENTA, axis=2).mean())


def _magenta_hue_fraction(image):
    """Magenta seen through a host overlay (Play's fade dims the whole view)."""
    r, g, b = (image[..., i].astype(np.int16) for i in range(3))
    return float(((r > 10) & (g * 5 < r) & (np.abs(r - b) * 8 <= r)).mean())


def _open(fio_session, **kwargs):
    session = fio_session(_level(), **kwargs)
    # SysMon is host HUD with a throttled stats cache; it is not what these
    # frames compare.
    session.view.sysmon.set_active(False)
    return session


def _same_picture(a, b):
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float((diff > 2).mean())


def test_swapping_in_the_editor_and_back(fio_session, renderers):
    session = _open(fio_session, size=(240, 160))
    view = session.view
    session.publish()
    before = session.paint()
    assert before.std() > 4.0, "the forward frame came out blank"
    assert _magenta_fraction(before) < 0.001
    forward = view.renderer

    assert view.switch_renderer("Flat") is True
    assert view.renderer_mode == "Flat"
    assert isinstance(view.renderer, FlatRenderer)
    session.publish()
    # The host's own overlay still paints over part of the view.
    assert _magenta_fraction(session.paint()) > 0.5

    assert view.switch_renderer("Forward") is True
    assert view.renderer is not forward, "a new ForwardRenderer takes over"
    assert FlatRenderer.cleaned == FlatRenderer.created
    session.publish()
    after = session.paint()
    assert _same_picture(before, after) < 0.001, (
        "the forward frame changed across a swap round trip")
    assert 'Portal' not in view.sprite_textures, "portals have no editor sprite"


def test_swapping_during_play(fio_session, renderers):
    session = _open(fio_session, size=(240, 160)).start_play()
    view = session.view
    session.step(3)
    session.publish()
    before = session.paint()
    assert before.std() > 4.0

    started = time.perf_counter()
    assert view.switch_renderer("Flat")
    session.publish()
    assert _magenta_hue_fraction(session.paint()) > 0.5
    assert session.playing
    assert view.switch_renderer("Forward")
    session.publish()
    after = session.paint()
    took = time.perf_counter() - started

    # Play animates on the wall clock, so frames differ a little over any
    # gap. The swap round trip may change the picture no more than the same
    # gap does without one.
    time.sleep(took)
    session.publish()
    later = session.paint()
    drift = _same_picture(after, later)
    assert _same_picture(before, after) <= 2 * drift + 0.002, drift


def test_session_settings_carry_across_a_swap(fio_session, renderers):
    session = _open(fio_session, size=(160, 120))
    view = session.view
    view.renderer.shadows_enabled = False
    view.renderer.water_quality = 'cheap'
    assert view.switch_renderer("Flat")
    assert (view.renderer.shadows_enabled, view.renderer.water_quality) == (False, 'cheap')
    assert view.renderer.view_distance is view.view_distance
    assert view.switch_renderer("Forward")
    assert (view.renderer.shadows_enabled, view.renderer.water_quality) == (False, 'cheap')


def test_a_renderer_that_fails_to_start_leaves_the_current_one(fio_session, renderers):
    session = _open(fio_session, size=(160, 120))
    view = session.view
    current = view.renderer
    assert view.switch_renderer("Broken") is False
    assert view.switch_renderer("NoSuchRenderer") is False
    assert view.renderer is current and view.renderer_mode == "Forward"
    session.publish()
    assert session.paint().std() > 4.0


def test_the_console_swaps_the_renderer(fio_session, renderers):
    from editor.console_commands import ConsoleCommandHandler
    session = _open(fio_session, size=(160, 120))
    handler = ConsoleCommandHandler(session.window)
    handler.handle_command("r_renderer flat")
    assert session.view.renderer_mode == "Flat"
    handler.handle_command("r_renderer Forward")
    assert session.view.renderer_mode == "Forward"
