"""Drawing a Terrain in tests, the way the editor does since M5.

A renderer is handed what the terrain *publishes* (``Terrain.publish()``, a
``TerrainFrame``) after the terrain has been brought up to date for the
camera (``Terrain.advance``); every GL name belongs to a ``TerrainGL`` the
renderer owns. These helpers do that sequence for a test: with a renderer
(:func:`scene`), or with a ``TerrainGL`` of the test's own (:func:`draw`).
"""

import weakref

from engine.terrain_frame import frustum_planes

_GL = weakref.WeakKeyDictionary()


def gl_for(terrain):
    """The test's own GL side for *terrain* (made on first use)."""
    from engine.renderer.core.terrain_gl import TerrainGL
    held = _GL.get(terrain)
    if held is None:
        held = _GL[terrain] = TerrainGL()
    return held


def bound(terrain):
    """:func:`gl_for` with the terrain's current publication bound, for
    calling one of its upload or uniform methods outside a frame."""
    held = gl_for(terrain)
    held.frame = terrain.publish()
    held._adopt(held.frame.table)
    return held


def _mirror(terrain, source):
    terrain.visible_chunks = source.visible_chunks
    terrain.culled_chunks = source.culled_chunks
    terrain.total_triangles = source.total_triangles
    terrain.drawn_slots = source.drawn_slots


TEXTURES = ("grass_tex", "rock_tex", "sand_tex", "snow_tex")


def _textures(terrain, held):
    """Ground textures a test put on the terrain (``t.grass_tex = name``)
    are the ones its GL side draws with."""
    for name in TEXTURES:
        value = getattr(terrain, name, 0)
        if value:
            setattr(held, name, value)


def draw(terrain, projection, view, camera_pos, planes=None, lights=None,
         active_lights_count=0, **kwargs):
    """One frame of *terrain* alone: advance, publish, draw."""
    terrain.advance(camera_pos, planes)
    held = gl_for(terrain)
    _textures(terrain, held)
    held.render(terrain.publish(), projection, view, camera_pos, planes,
                active_lights_count, **kwargs)
    _mirror(terrain, held)
    return held


def scene(renderer, terrain, projection, view, camera_pos, config, **kwargs):
    """One ``render_scene`` with *terrain* published into *config*."""
    terrain.advance(camera_pos, frustum_planes(projection * view))
    config["terrain"] = terrain.publish()
    held = renderer._terrain_gl(terrain._publication_key)
    if held.shader_program != renderer.shaders.get("terrain"):
        renderer.setup_terrain_shader(held)
    _textures(terrain, held)
    renderer.render_scene(projection, view, camera_pos, None, config, **kwargs)
    held = scene_gl(renderer)
    if held is not None:
        _mirror(terrain, held)
    return held


def scene_gl(renderer):
    """The renderer's own GL side of the terrain it last drew, or None."""
    state = getattr(renderer, "_terrain_gl_state", None)
    return state[1] if state is not None else None
