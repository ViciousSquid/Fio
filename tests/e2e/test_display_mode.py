"""The editor's Display box is host state; renderers only receive it.

The Display box ("Wireframe", "Points", "Solid Lit", "Textured", "Overlay") and
``r_wireframe`` set the view's display mode. The host publishes it as the
``brush_display_mode`` frame input, so:

* any renderer -- here one that shares nothing with ``ForwardRenderer`` -- sees
  a change on the very next frame, with nothing set on the renderer itself;
* in Play the game is drawn textured; only Wireframe, Points and Overlay
  carry over;
* the built-in renderer draws the modes as the box names them: Textured as
  normal, Solid Lit with no textures, and Wireframe (true brush edges) and
  Points (a laser scan, after Scanner Sombre) on black, coloured by distance
  from the eye -- warm up close, cold far away; Overlay is Textured with every
  brush triangle drawn over it in white.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from editor.things import Light, PlayerStart      # noqa: E402
from engine.renderer import registry               # noqa: E402
from tests.helpers.renderers import FlatRenderer   # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]

MODES = ("Wireframe", "Points", "Solid Lit", "Textured", "Overlay")


@pytest.fixture
def flat(monkeypatch):
    saved = dict(registry._factories)
    registry.register_renderer("Flat", FlatRenderer)
    yield
    registry._factories.clear()
    registry._factories.update(saved)


def _level(textured=True):
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, 0.0), radius=3000.0, intensity=2.0)
    brushes = room(size=2048.0, height=512.0) + [
        box_brush("crate", (0.0, 64.0, -200.0), (256.0, 256.0, 256.0))]
    if not textured:
        for brush in brushes:
            brush["textures"] = {}
    return level_data(brushes=brushes, things=[spawn, lamp])


def _open(fio_session, level=None):
    session = fio_session(level or _level(), size=(240, 160))
    session.view.sysmon.set_active(False)
    return session


def _published_modes(renderer):
    return [config.get("brush_display_mode") for _sel, config, _slots in renderer.frames]


def _diff(a, b):
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float((d > 2).mean())


def test_the_display_box_reaches_any_renderer_on_the_next_frame(fio_session, flat):
    session = _open(fio_session)
    window, view = session.window, session.view
    assert view.switch_renderer("Flat")
    renderer = view.renderer
    before = dict(vars(renderer))
    session.publish()

    for mode in MODES:
        requested = []
        real_update = view.update
        view.update = lambda *a: (requested.append(True), real_update(*a))
        try:
            window.display_mode_combobox.setCurrentText(mode)
        finally:
            del view.update
        assert requested, f"choosing {mode} did not ask for a repaint"
        renderer.frames.clear()
        session.paint()
        assert _published_modes(renderer)[-1] == mode

    # The renderer was told nothing out of band: only its frame log moved.
    after = dict(vars(renderer))
    after.pop("frames"), before.pop("frames")
    assert after.keys() == before.keys()


def test_r_wireframe_reaches_any_renderer_as_frame_input(fio_session, flat):
    from editor.console_commands import ConsoleCommandHandler
    session = _open(fio_session)
    view = session.view
    assert view.switch_renderer("Flat")
    renderer = view.renderer
    handler = ConsoleCommandHandler(session.window)
    session.window.display_mode_combobox.setCurrentText("Textured")
    session.publish()

    handler.handle_command("r_wireframe on")
    renderer.frames.clear()
    session.paint()
    assert _published_modes(renderer) == ["Wireframe"]
    assert not hasattr(renderer, "wireframe")

    handler.handle_command("r_wireframe off")
    renderer.frames.clear()
    session.paint()
    assert _published_modes(renderer) == ["Textured"]


def test_play_is_drawn_textured_unless_wireframe_is_on(fio_session, flat):
    session = _open(fio_session)
    view, combo = session.view, session.window.display_mode_combobox
    assert view.switch_renderer("Flat")
    combo.setCurrentText("Solid Lit")
    session.start_play()
    session.step(2)
    view.renderer.frames.clear()
    session.paint()
    assert set(_published_modes(view.renderer)) == {"Textured"}

    for mode in ("Wireframe", "Points", "Overlay"):
        combo.setCurrentText(mode)
        view.renderer.frames.clear()
        session.paint()
        assert set(_published_modes(view.renderer)) == {mode}


def _pictures(session):
    combo = session.window.display_mode_combobox
    session.publish()
    pictures = {}
    for mode in MODES:
        combo.setCurrentText(mode)
        # No publish: the mode change alone must show on the next frame.
        pictures[mode] = session.paint()
    return pictures


def test_the_built_in_renderer_draws_each_display_mode(fio_session):
    session = _open(fio_session, _level(textured=True))
    textured = _pictures(session)
    assert session.window._load_level(_level(textured=False))
    plain = _pictures(session)
    assert {p.shape for p in (*textured.values(), *plain.values())} == {textured["Textured"].shape}

    assert textured["Textured"].std() > 4.0
    # Textured shows the textures ...
    assert _diff(textured["Textured"], plain["Textured"]) > 0.05
    # ... Solid Lit shows none: a textured world and an untextured one match.
    assert _diff(textured["Solid Lit"], plain["Solid Lit"]) < 0.001
    assert _diff(textured["Solid Lit"], textured["Textured"]) > 0.05
    # Wireframe and Points are drawn on black, coloured by distance, and
    # textures play no part in either.
    for mode in ("Wireframe", "Points"):
        assert _diff(textured[mode], plain[mode]) < 0.001, mode
    assert _diff(textured["Wireframe"], textured["Points"]) > 0.05

    # Points is a laser scan (after Scanner Sombre): points of light on black,
    # warm on the floor at our feet, cold on the far wall.
    scan = textured["Points"].astype(np.int16)
    dark = float((scan.max(axis=2) < 24).mean())
    assert 0.5 < dark < 0.98, dark
    h = scan.shape[0]
    near = scan[int(h * 0.85):].reshape(-1, 3).sum(axis=0)
    far = scan[int(h * 0.35):int(h * 0.5)].reshape(-1, 3).sum(axis=0)
    assert near[0] > 2 * near[2], f"near points are not warm: {near}"
    assert far[2] > far[0], f"far points are not cold: {far}"

    # Wireframe is lines on black: the coloured pixels are edges, the near
    # crate's warm and the far walls' cold.
    wire = textured["Wireframe"].astype(np.int16)
    assert float((wire.max(axis=2) < 24).mean()) > 0.5
    rows = np.arange(h)[:, None].repeat(wire.shape[1], axis=1)
    warm = (wire[..., 0] - wire[..., 2]) > 80
    cold = (wire[..., 2] - wire[..., 0]) > 80
    assert warm.any() and cold.any()
    assert rows[warm].mean() > rows[cold].mean(), "the near lines are not the warm ones"


def test_overlay_is_the_textured_frame_with_white_triangles_over_it(fio_session):
    session = _open(fio_session, _level(textured=True))
    pictures = _pictures(session)
    textured = pictures["Textured"].astype(np.int16)
    overlay = pictures["Overlay"].astype(np.int16)
    changed = np.abs(overlay - textured).max(axis=2) > 2
    # Lines, not a different picture: a thin set of pixels changes ...
    assert 0.002 < changed.mean() < 0.15, changed.mean()
    # ... and every one of them turns white.
    assert (overlay[changed].min(axis=1) > 200).mean() > 0.9


def test_wireframe_draws_true_brush_edges(fio_session):
    """Box brushes as their 12 edges -- not the triangles of a filled pass."""
    import OpenGL.GL as gl
    session = _open(fio_session)
    view = session.view
    session.window.display_mode_combobox.setCurrentText("Wireframe")
    session.publish()
    calls, filled = [], []
    real_draw = gl.glDrawArrays
    renderer = view.renderer
    real_lit = renderer.draw_lit_brushes_optimized
    gl.glDrawArrays = lambda mode, first, count: (calls.append((mode, count)),
                                                  real_draw(mode, first, count))[1]
    def lit(*args, **kwargs):
        if len(args[3]) and not kwargs.get("is_transparent_pass"):
            filled.append(len(args[3]))
        return real_lit(*args, **kwargs)
    renderer.draw_lit_brushes_optimized = lit
    try:
        session.paint()
    finally:
        gl.glDrawArrays = real_draw
        del renderer.draw_lit_brushes_optimized
    box_edges = [c for c in calls if c == (gl.GL_LINES, 24)]
    assert len(box_edges) == renderer.render_stats.visible_brushes > 0
    assert not filled, "Wireframe went through the filled brush pass"
