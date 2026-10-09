"""portal_enable / portal_disable fade a portal, as its I/O inputs do."""

import pytest

pytest.importorskip("PyQt5")

from editor.things import Portal                             # noqa: E402
from tests.helpers.worlds import make_thing                  # noqa: E402

pytestmark = pytest.mark.qt


def test_console_portal_commands_fade_the_portal(main_window):
    portal = make_thing(Portal, "door", (0.0, 0.0, 0.0))
    main_window.state.things.append(portal)
    assert portal._fade_alpha == 1.0

    main_window.console_handler.handle_command("portal_disable door")
    assert portal.properties["active"] is False and portal._fade_target == 0.0
    portal.tick_fade(0.5)
    assert portal._fade_alpha == pytest.approx(0.5)      # fading out, over a second

    main_window.console_handler.handle_command("portal_enable door")
    assert portal.properties["active"] is True and portal._fade_target == 1.0
    portal.tick_fade(0.25)
    assert portal._fade_alpha == pytest.approx(0.75)
