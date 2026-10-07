"""Portals never display an editor sprite.

The aperture wireframe is how the editor shows a portal: the class has no
sprite image, its EntityTable row carries no sprite, and the sprite pass draws
nothing for it.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from engine import entity_table as et  # noqa: E402
from editor.things import Light, Portal  # noqa: E402
from tests.helpers.worlds import make_thing  # noqa: E402

pytestmark = pytest.mark.qt


def _table(*things):
    table = et.EntityTable()
    table.begin_frame(list(things), epoch=1)
    return table


def test_the_portal_class_has_no_sprite_image():
    assert Portal.pixmap_path is None
    assert Portal.get_pixmap() is None
    assert make_thing(Portal, "door", (0.0, 64.0, 0.0)).get_instance_pixmap() is None


def test_a_portal_row_carries_no_sprite():
    portal = make_thing(Portal, "door", (0.0, 64.0, 0.0))
    table = _table(portal)
    assert et.sprite_candidates(portal) is None
    assert int(table.sprite_key_id[0]) == et.SPRITE_NONE
    assert not any(cache == 'Portal' for recipe in table.sprite_recipes()
                   for cache, *_rest in recipe)


@pytest.mark.gl
def test_the_sprite_pass_draws_nothing_for_a_portal():
    import OpenGL.GL as gl
    from tests.helpers import gl as glh

    portal = make_thing(Portal, "door", (0.0, 64.0, 0.0))
    lamp = make_thing(Light, "lamp", (100.0, 64.0, 0.0))
    table = _table(portal, lamp)
    try:
        context = glh.GLTestContext(64, 64)
        context.__enter__()
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no GL context: {exc}")
    try:
        renderer = glh.make_renderer()
        renderer.set_sprite_textures({'Light': glh._white_texture()})
        projection, view, eye = glh.camera_matrices(aspect=1.0)
        calls = []
        real = gl.glDrawArraysInstanced
        gl.glDrawArraysInstanced = lambda mode, first, count, n, *a: (
            calls.append(int(n)), real(mode, first, count, n, *a))[1]
        try:
            portal_only = renderer.draw_sprites_instanced(
                projection, view, table, np.array([0], dtype=np.int32), camera_pos=eye)
            both = renderer.draw_sprites_instanced(
                projection, view, table, np.array([0, 1], dtype=np.int32), camera_pos=eye)
        finally:
            gl.glDrawArraysInstanced = real
        assert not portal_only, "the sprite pass drew a portal"
        assert both and sum(calls) == 1, "only the light is a sprite"
        renderer.cleanup()
    finally:
        context.__exit__(None, None, None)
        glh.reset_texture_cache()
