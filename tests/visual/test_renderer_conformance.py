"""The same contract-conformance tests, run against Classic and HD_High.

Both built-in renderers implement ``engine.renderer.api.Renderer`` and
nothing else is asked of either, so every test here is parametrized over the
two registry names and makes no exception for one of them:

* what the registry creates satisfies the whole contract
  (``contract_violations``), before a frame and after one;
* a frame is drawn from the documented frame input alone: the keys of
  ``FRAME_INPUT`` and no others are handed over, and no key outside it is
  asked for;
* the settings the host assigns hold values of the declared types;
* ``cleanup()`` deletes every GL object the renderer made;
* a malformed variant of the renderer -- a wrong signature, a missing
  attribute, a value of the wrong type, a property that raises, another
  contract version -- is refused by the registry, and none of the GL objects
  its constructor made is left behind.

GL objects are counted by asking the driver which names are live, so nothing
depends on how either renderer keeps track of its own.
"""

import pytest

import OpenGL.GL as gl

from engine.renderer import (
    FRAME_INPUT, RENDERER_CONTRACT_VERSION, Renderer, RendererContractError,
    contract_violations, create_renderer, registry, renderer_factory)
from engine.view_distance import ViewDistance
from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

BUILT_IN = ("Forward", "HD_High")

_KINDS = {
    "texture": gl.glIsTexture, "buffer": gl.glIsBuffer,
    "vertex array": gl.glIsVertexArray, "framebuffer": gl.glIsFramebuffer,
    "renderbuffer": gl.glIsRenderbuffer, "program": gl.glIsProgram,
    "shader": gl.glIsShader, "query": gl.glIsQuery, "sampler": gl.glIsSampler,
}
#: Names are handed out from 1 upwards; a renderer makes a few hundred.
_NAME_LIMIT = 4096


def gl_census():
    """How many live GL objects of each kind the current context holds."""
    return {kind: sum(1 for name in range(1, _NAME_LIMIT) if alive(name))
            for kind, alive in _KINDS.items()}


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64) as made:
        yield made
    glh.reset_texture_cache()


@pytest.fixture
def variant():
    """Register a variant of a built-in renderer; the registry is restored."""
    saved = dict(registry._factories)

    def register(factory):
        registry.register_renderer("Variant", factory)
        return "Variant"
    yield register
    registry._factories.clear()
    registry._factories.update(saved)


class RecordingFrameInput(dict):
    """A frame input that notes every key it is asked for."""

    def __init__(self, values):
        super().__init__(values)
        self.asked = set()

    def __getitem__(self, key):
        self.asked.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.asked.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.asked.add(key)
        return super().__contains__(key)


def _frame_input():
    scene = glh.lit_cube_scene()
    config = glh.render_config(all_brushes=scene[0], all_things=scene[1])
    return RecordingFrameInput({key: value for key, value in config.items()
                                if key in FRAME_INPUT})


@pytest.mark.parametrize("name", BUILT_IN)
def test_the_registry_creates_a_renderer_that_satisfies_the_contract(context, name):
    renderer = create_renderer(name, None)
    try:
        assert contract_violations(renderer) == []
        assert isinstance(renderer, Renderer)
        assert renderer.ready is True
        cls = renderer_factory(name)
        assert type(renderer) is cls
        assert vars(cls).get("contract_version") == RENDERER_CONTRACT_VERSION, (
            "%s declares the contract it is written against itself" % name)
        for optional in ("animate_textures", "draw_face_hover", "draw_lens_water"):
            assert callable(getattr(renderer, optional)), optional
    finally:
        renderer.cleanup()


@pytest.mark.parametrize("name", BUILT_IN)
def test_a_frame_is_drawn_from_the_documented_frame_input_alone(context, name):
    renderer = create_renderer(name, None)
    try:
        renderer.set_grid(4096, 64)
        renderer.set_sprite_textures({})
        frame = _frame_input()
        assert set(frame) <= set(FRAME_INPUT)
        projection, view, eye = glh.camera_matrices()
        for play_mode in (False, True):
            frame["play_mode"] = play_mode
            for _ in range(2):
                context.bind()
                with glh.no_gl_errors("%s frame" % name):
                    renderer.render_scene(projection, view, eye, None, frame,
                                          brush_slots=frame["all_brush_slots"])
        assert not glh.is_blank(context.read_pixels()), "%s drew nothing" % name
        assert frame.asked <= set(FRAME_INPUT), sorted(frame.asked - set(FRAME_INPUT))
        assert contract_violations(renderer) == [], "a frame left it conforming"
    finally:
        renderer.cleanup()


@pytest.mark.parametrize("name", BUILT_IN)
def test_the_settings_the_host_assigns_keep_the_contract(context, name):
    renderer = create_renderer(name, None)
    try:
        shared = ViewDistance()
        renderer.view_distance = shared
        for shadows in (False, True):
            for quality in ("cheap", "expensive"):
                renderer.shadows_enabled = shadows
                renderer.water_quality = quality
                assert contract_violations(renderer) == []
        assert renderer.view_distance is shared
        renderer.render_stats.reset()
        assert renderer.render_stats.draw_calls == 0
    finally:
        renderer.cleanup()


@pytest.mark.parametrize("name", BUILT_IN)
def test_cleanup_deletes_every_gl_object_the_renderer_made(context, name):
    before = gl_census()
    renderer = create_renderer(name, None)
    renderer.set_grid(4096, 64)
    frame = _frame_input()
    projection, view, eye = glh.camera_matrices()
    context.bind()
    renderer.render_scene(projection, view, eye, None, frame,
                          brush_slots=frame["all_brush_slots"])
    alive = gl_census()
    assert sum(alive.values()) > sum(before.values()) + 10, (
        "the census sees what a renderer makes")
    renderer.cleanup()
    assert gl_census() == before


# -- malformed variants of each renderer ------------------------------------------

def _wrong_signature(cls):
    class Variant(cls):
        def draw_billboards(self, projection, view, positions, size):
            return 0
    return Variant


def _missing_attribute(cls):
    class Variant(cls):
        def __init__(self, config):
            super().__init__(config)
            del self.render_stats
    return Variant


def _invalid_type(cls):
    class Variant(cls):
        def __init__(self, config):
            super().__init__(config)
            self.shadows_enabled = 1
    return Variant


def _invalid_value(cls):
    class Variant(cls):
        def __init__(self, config):
            super().__init__(config)
            self.water_quality = "ultra"
    return Variant


def _broken_property(cls):
    class Variant(cls):
        @property
        def ready(self):
            raise RuntimeError("no context")
    return Variant


def _other_version(cls):
    class Variant(cls):
        contract_version = "2.0.0"
    return Variant


def _undeclared_version(cls):
    class Variant(cls):
        @property
        def contract_version(self):
            raise AttributeError("contract_version")
    return Variant


MALFORMED = [
    (_wrong_signature, "takes no 'tex_id' argument"),
    (_missing_attribute, "render_stats is missing"),
    (_invalid_type, "shadows_enabled must be bool, not 1 (int)"),
    (_invalid_value, "water_quality must be one of"),
    (_broken_property, "ready raised RuntimeError: no context"),
    (_other_version, "written for renderer contract 2.0.0"),
    (_undeclared_version, "contract_version raised AttributeError"),
]


@pytest.mark.parametrize("name", BUILT_IN)
@pytest.mark.parametrize("malform, why", MALFORMED,
                         ids=[m[0].__name__.strip("_") for m in MALFORMED])
def test_a_malformed_variant_is_refused_and_leaves_no_gl_object(
        context, variant, name, malform, why):
    made = []

    def factory(config):
        made.append(malform(renderer_factory(name))(config))
        return made[0]
    before = gl_census()
    with pytest.raises(RendererContractError) as caught:
        create_renderer(variant(factory), None)
    assert why in str(caught.value), str(caught.value)
    assert not getattr(caught.value, "__notes__", ()), "its cleanup ran clean"
    assert len(made) == 1
    assert gl_census() == before, "the refused renderer's GL objects were deleted"
