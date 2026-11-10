"""A map's textures are loaded when the map is, and as Play starts -- never
every texture in ``assets/textures`` at start-up (47 textures, 0.3 s of
decoding and ~50 MB, whatever the map)."""

import os

import pytest

pytest.importorskip("PyQt5")

from engine.asset_archives import asset_exists, asset_listdir   # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

MAP = "maps/_SHOWCASE.json"
IMAGES = (".png", ".jpg", ".jpeg", ".tga", ".gif")


def _loaded(session):
    renderer = session.view.renderer
    if renderer is None or not renderer.ready:
        pytest.skip("no OpenGL context")
    prefix = "textures" + os.sep
    return {key[len(prefix):] for key in renderer.texture_manager if key.startswith(prefix)}


def _brush_textures(session):
    """The textures the map's brushes name, of those that exist (a missing
    one draws as the default and is not kept under its own name)."""
    names = set()
    for brush in session.state.brushes:
        names.update(t for t in brush.get("textures", {}).values() if t and t != "caulk.jpg")
    return {n for n in names if asset_exists(os.path.join("assets", "textures", n))}


def test_a_loaded_map_has_its_textures_and_not_the_whole_folder(fio_session):
    session = fio_session(MAP)
    loaded, used = _loaded(session), _brush_textures(session)
    assert len(used) > 3 and used <= loaded
    shipped = {f for f in asset_listdir(os.path.join("assets", "textures"))
               if f.lower().endswith(IMAGES)}
    unused = shipped - used
    assert len(unused) > 10, "the map uses most of the folder: pick another"
    # The renderer's own few (the water normal map) and its models' aside,
    # what the map does not name was not decoded.
    assert len(loaded & unused) < len(unused) // 2, sorted(loaded & unused)


def test_play_starts_with_every_texture_of_the_world_loaded(fio_session):
    session = fio_session(MAP)
    _loaded(session)
    calls = []
    real = session.view.renderer.load_texture_callback
    session.view.renderer.load_texture_callback = (
        lambda name, folder: calls.append(name) or real(name, folder))
    session.start_play()
    session.step(3)
    session.view.grabFramebuffer()
    session.stop_play()
    assert _brush_textures(session) <= _loaded(session)
    # Nothing was left for a frame of play to decode.
    assert not [name for name in calls if name != "caulk.jpg"], calls


def test_a_texture_applied_later_is_loaded_when_asked_for(fio_session):
    session = fio_session(MAP)
    before = _loaded(session)
    shipped = sorted(f for f in asset_listdir(os.path.join("assets", "textures"))
                     if f.lower().endswith(IMAGES) and f not in before)
    assert shipped
    session.state.brushes[0].setdefault("textures", {})["top"] = shipped[0]
    session.view.preload_level_textures()
    assert shipped[0] in _loaded(session)
