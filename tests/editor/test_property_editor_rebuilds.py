"""Tests for the Property Editor's rebuild avoidance.

Building a panel constructs several tabs' worth of widgets, and
``set_object()`` is called after every editor operation — including once per
mouse-move during a rotate drag.  These cover when a rebuild happens, when a
previously built page is put back instead, and that neither path can show
stale values.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

import configparser  # noqa: E402

from PyQt5.QtCore import QEvent, QObject  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from editor import io_system  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402
from editor.io_system import OutputConnection  # noqa: E402
from editor.property_editor import (  # noqa: E402
    PropertyEditor,
    _normalise_project_asset_path,
)
from editor.things import Light, Speaker  # noqa: E402
from engine.prop_entity import Prop  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402
from engine.render_table import (  # noqa: E402
    RenderTable, CLASS_FOG, CLASS_TRIGGER,
)

# Qt tier: PyQt5 must be importable.  No display and no GPU - the suite runs
# against the offscreen platform plugin.
pytestmark = pytest.mark.qt



@pytest.fixture(scope="session")
def qt_app():
    # Only when there is no display: the offscreen plugin cannot create an
    # OpenGL context, and forcing it here would disable the visual tier for
    # the whole session when the suite is run under Xvfb.
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app



