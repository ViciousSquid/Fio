"""The guide's DeferredRenderer is a real, drop-in renderer.

``docs/examples/deferred_renderer`` is the worked example from the wiki's
Renderer Development Guide. It shares nothing with ``ForwardRenderer``; these
tests hold it to everything the guide promises:

* it imports only the public contract and the dense tables -- nothing from
  the built-in renderer or its infrastructure;
* registered through its plugin, it is swapped in live and draws a lit frame
  from the dense tables;
* Debug Tables shows it as DEFERRED and reads it, unchanged, like any other --
  and what it reports matches the GL draws actually issued;
* no GL name it made survives it.
"""

import ast
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from engine.renderer import Renderer, registry                  # noqa: E402
from tests.e2e.test_debug_tables_oracle import (                # noqa: E402
    _close, _instrument, _measure_independently, _read_debug_tables)
from tests.e2e.test_renderer_gl_lifetime import (               # noqa: E402
    GLObjectLedger, _still_alive)
from tests.e2e.test_renderer_hot_swap import _level             # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]

EXAMPLES = pathlib.Path(__file__).resolve().parents[2] / "docs" / "examples"
EXAMPLE = EXAMPLES / "deferred_renderer"


def _imports(path):
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module)
    return names


@pytest.mark.parametrize("module", ["__init__.py", "renderer.py"])
def test_the_example_uses_only_the_public_contract(module):
    for name in _imports(EXAMPLE / module):
        assert not name.startswith(("engine.renderer.forward", "engine.renderer.core",
                                    "engine.renderer.api", "engine.renderer.registry",
                                    "editor", "engine.logic")), name


@pytest.fixture
def deferred():
    saved = dict(registry._factories)
    sys.path.insert(0, str(EXAMPLES))
    try:
        import deferred_renderer
        from deferred_renderer.renderer import DeferredRenderer
        # What the plugin manager does with it: register(api) at load.
        deferred_renderer.PLUGIN.register(
            SimpleNamespace(register_renderer=registry.register_renderer))
        assert "Deferred" in registry.available_renderers()
        yield DeferredRenderer
    finally:
        sys.path.remove(str(EXAMPLES))
        for name in [m for m in sys.modules if m.split(".")[0] == "deferred_renderer"]:
            del sys.modules[name]
        registry._factories.clear()
        registry._factories.update(saved)


def _open(fio_session):
    session = fio_session(_level(), size=(240, 160))
    session.view.sysmon.set_active(False)
    return session


def test_it_swaps_in_live_and_draws_a_lit_frame(fio_session, deferred):
    session = _open(fio_session)
    view = session.view
    session.publish()
    forward = session.paint()

    assert view.switch_renderer("Deferred")
    assert isinstance(view.renderer, deferred)
    assert isinstance(view.renderer, Renderer)
    session.publish()
    image = session.paint().astype(np.int16)
    assert image.std() > 4.0, "the deferred frame came out blank"
    assert np.abs(image - forward.astype(np.int16)).max(axis=2).mean() > 2.0, (
        "the frame is Forward's, not the deferred renderer's")
    # Lit by the lamp: the room is brighter near it than its ambient floor.
    assert image.max() > 3 * 0.14 * 255 * 0.5

    # And it hands back cleanly, in the editor and in Play.
    assert view.switch_renderer("Forward")
    session.start_play()
    session.step(2)
    assert view.switch_renderer("Deferred")
    session.publish()
    assert session.paint().std() > 4.0


def test_debug_tables_shows_and_reads_it_unchanged(fio_session, deferred):
    session = _open(fio_session)
    instrument = _instrument(session)
    try:
        assert session.view.switch_renderer("Deferred")
        session.publish()
        measured = _measure_independently(session)
        reported = _read_debug_tables(instrument)
        assert instrument.renderer_label.text() == "DEFERRED"
        assert reported["rows"] == measured["rows"]
        assert reported["visible"] == measured["visible"]
        assert reported["draw_calls"] + reported["shadow_draws"] == measured["gl_draws"] > 1
        # Its own diagnostics appear without Debug Tables knowing about them.
        assert "G-buffer" in instrument.dashboard.toPlainText()
    finally:
        _close(instrument)


def test_no_gl_name_outlives_it(fio_session, deferred):
    session = _open(fio_session)
    view = session.view
    ledger = GLObjectLedger((deferred, type(view.renderer), type(view)))
    ledger.install()
    try:
        assert view.switch_renderer("Deferred")
        renderer = view.renderer
        session.publish()
        session.paint()
        session.view.resize(200, 140)          # a new G-buffer size
        session.publish()
        session.paint()
        owned = ledger.owned_by(renderer)
        assert {"program", "framebuffer", "texture", "renderbuffer"} <= {k for k, _ in owned}
        assert view.switch_renderer("Forward")
        view.makeCurrent()
        try:
            survivors = [(k, n) for k, n in owned if _still_alive(k, n)]
        finally:
            view.doneCurrent()
    finally:
        ledger.uninstall()
    assert not survivors, survivors
    retired = {n for k, n in owned if k == "texture"}
    assert not retired & {int(t) for t in view.sprite_textures.values()}
