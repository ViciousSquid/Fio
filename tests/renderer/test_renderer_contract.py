"""A renderer is anything that satisfies ``engine.renderer.Renderer``.

The architectural test for the plugin boundary: a renderer that shares
nothing with the built-in one -- no ``RendererCore``, no forward shaders, no
GL at all here -- registers by name and is created by the host's own path.
"""

import pytest

from engine.renderer import (
    DEFAULT_RENDERER, FRAME_INPUT, RenderStats, Renderer, available_renderers,
    create_renderer, register_renderer, renderer_factory)
from engine.renderer import registry


class StubRenderer:
    """The smallest conforming renderer: every member, no technique."""

    def __init__(self, config):
        self.config = config
        self.view_distance = None
        self.shadows_enabled = True
        self.water_quality = 'expensive'
        self.render_stats = RenderStats()
        self.frames = []

    @property
    def ready(self):
        return True

    def cleanup(self):
        pass

    def render_scene(self, projection, view, camera_pos, primary_selection,
                     config, clear=True, brush_slots=None):
        self.frames.append((primary_selection, dict(config), brush_slots))

    def set_grid(self, world_size, grid_size):
        pass

    def load_texture(self, texture_name, subfolder):
        return 0

    def set_sprite_textures(self, textures):
        pass

    def get_loaded_model(self, filename):
        return None

    def draw_billboards(self, projection, view, positions, size, tex_id):
        return 0

    def draw_player_glasses(self, projection, view, positions,
                            width=40.0, height=18.0, lift=0.0, sprites=()):
        pass

    def draw_bullet_marks(self, projection, view, marks):
        pass

    def draw_connection_lines(self, projection, view, connections):
        pass

    def draw_face_highlight(self, projection, view, brush, face_name):
        pass

    def draw_component_overlay(self, projection, view, overlay, version=None):
        pass

    def draw_collision_visualization(self, projection, view, brushes):
        pass


@pytest.fixture
def clean_registry():
    saved = dict(registry._factories)
    yield
    registry._factories.clear()
    registry._factories.update(saved)


def test_forward_is_the_built_in_default():
    assert DEFAULT_RENDERER == "Forward"
    assert "Forward" in available_renderers()


def test_a_renderer_with_nothing_from_fio_plugs_in(clean_registry):
    assert register_renderer("Stub", StubRenderer) is True
    assert "Stub" in available_renderers()
    renderer = create_renderer("Stub", config=None)
    assert isinstance(renderer, StubRenderer)
    assert isinstance(renderer, Renderer)
    renderer.render_scene(None, None, (0.0, 0.0, 0.0), None,
                          {key: None for key in FRAME_INPUT}, brush_slots=())
    assert renderer.frames


def test_an_object_missing_a_member_is_not_a_renderer():
    partial = type("Partial", (), {k: v for k, v in vars(StubRenderer).items()
                                    if k != "draw_bullet_marks"})(None)
    assert not isinstance(partial, Renderer)


def test_an_unknown_renderer_name_is_an_error():
    assert renderer_factory("NoSuchRenderer") is None
    with pytest.raises(KeyError):
        create_renderer("NoSuchRenderer")


@pytest.mark.gl
def test_the_forward_renderer_satisfies_the_contract():
    from engine.renderer.forward import ForwardRenderer
    from tests.helpers.gl import GLTestContext

    try:
        context = GLTestContext(64, 64)
        context.__enter__()
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no GL context: {exc}")
    try:
        renderer = create_renderer("Forward", None)
        try:
            assert isinstance(renderer, ForwardRenderer)
            assert isinstance(renderer, Renderer)
            assert renderer.ready
        finally:
            renderer.cleanup()
    finally:
        context.__exit__(None, None, None)
