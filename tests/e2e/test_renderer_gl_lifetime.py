"""No GL resource id survives the lifetime of the renderer that owns it.

Every GL object created on a renderer's behalf -- its programs, buffers,
vertex arrays, framebuffers and textures, the models and textures it loads,
the sprite array it builds -- is recorded as it is generated. A frame is drawn
in every display mode, with a selection, and in Play through linked portals;
then the renderer is swapped out. Afterwards:

* none of those names is still a live GL object, and
* nothing the host keeps (sprite table, terrain binding, frame input) still
  holds one of them.

Ownership is read from the call stack: an object belongs to the innermost
caller that is a renderer, the terrain or the view. Objects the terrain or the
host create for themselves are theirs, not the renderer's.
"""

import sys

import numpy as np
import pytest

pytest.importorskip("PyQt5")

import OpenGL.GL as gl                               # noqa: E402
import OpenGL.GL.shaders as gl_shaders               # noqa: E402

from editor.things import Light, Portal, PlayerStart  # noqa: E402
from engine.effect_entity import Effect             # noqa: E402
from engine.prop_entity import Prop                 # noqa: E402
from engine.renderer import registry                # noqa: E402
from engine.terrain import Terrain                  # noqa: E402
from tests.helpers.renderers import FlatRenderer    # noqa: E402
from tests.helpers.worlds import (                  # noqa: E402
    angled_brush, box_brush, level_data, make_thing, room)

pytestmark = [pytest.mark.gl, pytest.mark.integration]

#: generator -> (kind, the glIs* query for that kind)
_GENERATORS = {
    "glGenTextures": ("texture", "glIsTexture"),
    "glGenBuffers": ("buffer", "glIsBuffer"),
    "glGenVertexArrays": ("vertex array", "glIsVertexArray"),
    "glGenFramebuffers": ("framebuffer", "glIsFramebuffer"),
    "glGenRenderbuffers": ("renderbuffer", "glIsRenderbuffer"),
    "glCreateProgram": ("program", "glIsProgram"),
    "glCreateShader": ("shader", "glIsShader"),
}


class GLObjectLedger:
    """Records every GL name generated while installed, with its owner."""

    def __init__(self, owner_types):
        self.owner_types = owner_types
        self.created = []           # (kind, name, owner)
        self.sites = {}             # (kind, name) -> where it was made
        self._real = {}

    def _owner(self, frame):
        while frame is not None:
            candidate = frame.f_locals.get("self")
            if isinstance(candidate, self.owner_types):
                return candidate
            frame = frame.f_back
        return None

    def install(self):
        # Shader programs are made by OpenGL.GL.shaders.compileProgram, which
        # calls the generator bound in its own namespace.
        for module in (gl, gl_shaders):
            for name, (kind, _query) in _GENERATORS.items():
                real = getattr(module, name, None)
                if real is None:
                    continue
                self._real[(module, name)] = real

                def generate(*args, _real=real, _kind=kind, **kwargs):
                    result = _real(*args, **kwargs)
                    caller = sys._getframe(1)
                    owner = self._owner(caller)
                    for value in np.atleast_1d(np.asarray(result)).ravel():
                        if int(value):
                            self.created.append((_kind, int(value), owner))
                            self.sites[(_kind, int(value))] = _site(caller)
                    return result

                setattr(module, name, generate)

    def uninstall(self):
        for (module, name), real in self._real.items():
            setattr(module, name, real)
        self._real.clear()

    def owned_by(self, owner):
        return [(kind, name) for kind, name, who in self.created if who is owner]


_QUERIES = dict(_GENERATORS.values())


def _site(frame):
    """Where Fio code made a GL object: its innermost frame under engine/."""
    while frame is not None:
        path = frame.f_code.co_filename.replace("\\", "/")
        if "/engine/" in path and not path.endswith("core/resources.py"):
            return f"{path.split('/engine/')[1]}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    return "?"


def _still_alive(kind, name):
    return bool(getattr(gl, _QUERIES[kind])(name))


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, 0.0), radius=2500.0,
                      intensity=2.0, casts_shadows=True)
    model = make_thing(Prop, "drum", (200.0, 0.0, -200.0), render_mode="model",
                       model_path=Prop.DEFAULT_MODEL_PATH)
    sprite = make_thing(Prop, "sprite", (-200.0, 64.0, -200.0), render_mode="billboard",
                        sprite_path="assets/sprites/monster.png")
    effect = make_thing(Effect, "boom", (0.0, 96.0, -400.0), effect_type="EXPLOSION")
    near = make_thing(Portal, "near", (-300.0, 64.0, 0.0), portal_target="far")
    far = make_thing(Portal, "far", (300.0, 64.0, 0.0), portal_target="near")
    brushes = room(size=2048.0, height=512.0) + [
        box_brush("crate", (0.0, 64.0, -200.0), (128.0, 128.0, 128.0)),
        angled_brush("wedge", (-400.0, 64.0, -300.0), (128.0, 128.0, 128.0)),
        box_brush("pool", (400.0, 16.0, 200.0), (256.0, 32.0, 256.0), shader="Water"),
        box_brush("pane", (0.0, 128.0, 400.0), (256.0, 256.0, 8.0), shader="Glass"),
    ]
    return level_data(brushes=brushes, things=[spawn, lamp, model, sprite, effect, near, far])


@pytest.fixture
def flat():
    saved = dict(registry._factories)
    registry.register_renderer("Flat", FlatRenderer)
    yield
    registry._factories.clear()
    registry._factories.update(saved)


def _exercise(session):
    """Draw with everything the renderer has a resource for."""
    window = session.window
    session.publish()
    for mode in ("Textured", "Solid Lit", "Wireframe", "Points", "Overlay", "Textured"):
        window.display_mode_combobox.setCurrentText(mode)
        session.paint()
    window.set_selected_objects([window.state.brushes[-4]])     # the crate
    session.publish()
    session.paint()
    window.set_selected_objects([])
    session.start_play()
    session.step(3)
    session.publish()
    session.paint()
    session.stop_play()
    session.publish()
    session.paint()


def test_no_gl_name_outlives_the_renderer_that_owns_it(fio_session, flat):
    session = fio_session(_level(), size=(240, 160))
    session.view.sysmon.set_active(False)
    view = session.view
    forward_type = type(view.renderer)
    ledger = GLObjectLedger((forward_type, FlatRenderer, Terrain, type(view)))
    ledger.install()
    try:
        # A Forward renderer whose whole life is on the ledger.
        assert view.switch_renderer("Flat") and view.switch_renderer("Forward")
        forward = view.renderer
        _exercise(session)
        owned = ledger.owned_by(forward)
        kinds = {kind for kind, _name in owned}
        assert {"program", "buffer", "vertex array", "texture", "framebuffer"} <= kinds, kinds

        assert view.switch_renderer("Flat")
        view.makeCurrent()
        try:
            survivors = [(kind, name) for kind, name in owned if _still_alive(kind, name)]
        finally:
            view.doneCurrent()
    finally:
        ledger.uninstall()
    assert not survivors, "GL names that outlived their renderer:\n" + "\n".join(
        f"  {kind} {name}  made at {ledger.sites[(kind, name)]}" for kind, name in survivors)

    # The host keeps none of the retired renderer's names.
    retired = {name for kind, name in owned if kind == "texture"}
    assert not retired & {int(t) for t in view.sprite_textures.values()}
    terrain = session.window.terrain
    if terrain is not None:
        assert not retired & {terrain.grass_tex, terrain.rock_tex,
                              terrain.sand_tex, terrain.snow_tex}
        assert terrain.shader_program not in {n for k, n in owned if k == "program"}
    for value in view._render_config.values():
        if isinstance(value, (tuple, list)):
            assert not retired & {v for v in value if isinstance(v, int)}


def test_a_renderer_that_fails_to_start_leaves_no_gl_names(fio_session, flat):
    """The half-built renderer is cleaned up as completely as a retired one."""
    session = fio_session(_level(), size=(160, 120))
    view = session.view
    forward_type = type(view.renderer)

    class NotReady(forward_type):
        @property
        def ready(self):
            return False

    registry.register_renderer("NotReady", NotReady)
    ledger = GLObjectLedger((forward_type, Terrain, type(view)))
    ledger.install()
    try:
        assert view.switch_renderer("NotReady") is False
        refused = [entry for entry in ledger.created if isinstance(entry[2], NotReady)]
        assert refused, "the refused renderer created nothing to check"
        view.makeCurrent()
        try:
            survivors = [(k, n) for k, n, _o in refused if _still_alive(k, n)]
        finally:
            view.doneCurrent()
    finally:
        ledger.uninstall()
    assert not survivors, survivors


def test_the_terrain_keeps_no_name_of_a_retired_renderer(fio_session, flat):
    """The terrain is bound to the renderer's program and ground textures; a
    swap releases that binding before the renderer is retired."""
    session = fio_session("maps/Terrain_Test_medium.json", size=(160, 120))
    session.view.sysmon.set_active(False)
    view, terrain = session.view, session.window.terrain
    assert terrain is not None and terrain.enabled
    ledger = GLObjectLedger((type(view.renderer), FlatRenderer, Terrain, type(view)))
    ledger.install()
    try:
        assert view.switch_renderer("Flat") and view.switch_renderer("Forward")
        forward = view.renderer
        session.publish()
        session.paint()
        assert terrain.shader_program and terrain.grass_tex, "the terrain was not drawn"
        owned = ledger.owned_by(forward)
        assert view.switch_renderer("Flat")
        names = {name for _kind, name in owned}
        assert terrain.shader_program == 0 and terrain.uniforms == {}
        assert not names & {terrain.grass_tex, terrain.rock_tex,
                            terrain.sand_tex, terrain.snow_tex} - {0}
        view.makeCurrent()
        try:
            survivors = [(k, n) for k, n in owned if _still_alive(k, n)]
        finally:
            view.doneCurrent()
        # And the next renderer binds its own the first time it draws it.
        assert view.switch_renderer("Forward")
        session.publish()
        session.paint()
        assert terrain.shader_program == view.renderer.shaders["terrain"]
    finally:
        ledger.uninstall()
    assert not survivors, survivors
