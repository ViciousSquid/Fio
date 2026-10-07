"""Renderers for tests of the renderer contract and its host.

Neither shares anything with ``ForwardRenderer``: they implement the
``engine.renderer.Renderer`` protocol directly, which is the point -- a
replacement renderer needs nothing from Fio's built-in one.
"""

from engine.renderer import RenderStats

#: The colour FlatRenderer fills a view with.
MAGENTA = (255, 0, 255)


class StubRenderer:
    """The smallest conforming renderer: every member, no technique, no GL."""

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


class FlatRenderer(StubRenderer):
    """A complete renderer with a technique of its own: one flat colour.

    Records every instance created and cleaned up, so tests can check the
    host retires what it swaps out.
    """

    created = []
    cleaned = []

    def __init__(self, config):
        super().__init__(config)
        FlatRenderer.created.append(self)

    def cleanup(self):
        FlatRenderer.cleaned.append(self)

    def render_scene(self, projection, view, camera_pos, primary_selection,
                     config, clear=True, brush_slots=None):
        import OpenGL.GL as gl
        super().render_scene(projection, view, camera_pos, primary_selection,
                             config, clear, brush_slots)
        self.render_stats.reset()
        gl.glClearColor(*(c / 255.0 for c in MAGENTA), 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
