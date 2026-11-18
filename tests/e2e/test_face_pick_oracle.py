"""Retexturing in face mode changes the brush under the cursor -- two oracles.

Reported: "sometimes when I retexture a brush, a completely different brush
changes". The cause was the face picker: a ray starting inside a brush -- the
editor camera standing in a trigger, fog or water volume, a room-sized brush --
hit that brush at distance 0, so every face-mode click retextured the volume
around the camera instead of the wall it was aimed at (and an angled volume
answered with the far face the ray leaves through).

Each click here is a real face-mode mouse click in the 3D view, and what it
changed is judged twice, by oracles that share nothing:

* **Debug Tables** (Debug -> Debug Tables): the instrument's copy of the
  published RenderTable before and after the click; the rows whose material
  changed, named by the ids the table keys them on.
* **The depth buffer** (never reads a table or the picker): the world point
  drawn under the cursor, unprojected from the frame just painted; the brush
  whose surface holds it.

Both must name exactly one brush, the same one. The applied texture is one in
``textures/decals``, which must load as itself, not as the default.
"""

import os

import numpy as np
import pytest

pytest.importorskip("PyQt5")

import glm                                                   # noqa: E402
import OpenGL.GL as gl                                       # noqa: E402

from editor.things import Light, PlayerStart                 # noqa: E402
from engine import brush_placement                           # noqa: E402
from tests.helpers.paths import REPO_ROOT                    # noqa: E402
from tests.helpers.session import process_events             # noqa: E402
from tests.helpers.worlds import (angled_brush, box_brush, level_data,   # noqa: E402
                                  make_thing, room)

pytestmark = [pytest.mark.gl, pytest.mark.integration]

DECAL_TEXTURE = "decals/Signage.png"
#: One texture per click, each unlike the last, so every click changes its face.
#: None of them is in the level (whose brushes are all Dev/512.jpg).
TEXTURES = ("decals/Signage.png", "decals/vent.jpg", "cobblestone.jpg",
            "decals/Decal02.jpg", "Metal_08-512x512.png", "decals/compscreen01.jpg")
MATERIAL = ("tex_name_id", "uv_scale", "uv_shift", "uv_angle")


def _level(volume):
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 200.0, 0.0), radius=3000.0,
                      intensity=2.0)
    crate = box_brush("crate", (-250.0, 64.0, -150.0), (128.0, 128.0, 128.0))
    pillar = angled_brush("pillar", (-200.0, 96.0, 200.0), (96.0, 192.0, 96.0),
                          clip_normal=(0.4, 1.0, 0.3))
    brushes = room(size=1024.0, height=384.0) + [crate, pillar]
    if volume == "box":
        brushes.append(box_brush("volume", (0.0, 150.0, 0.0), (360.0, 280.0, 360.0),
                                 is_trigger=True))
    elif volume == "angled":
        brushes.append(angled_brush("volume", (0.0, 150.0, 0.0), (360.0, 280.0, 360.0),
                                    clip_normal=(0.2, 1.0, -0.3), is_trigger=True))
    # The current map version: the angled brushes' planes are in their own
    # frame about ``pos`` (an unversioned map is read as world-space planes).
    return level_data(brushes=brushes, things=[spawn, lamp],
                      version=brush_placement.MAP_VERSION)


def _frame(session):
    process_events(session.app)
    session.publish()
    session.paint()
    session.publish()
    return session.paint()


def _depth_point(session, px, py):
    """The world point drawn at widget pixel (px, py), from the depth buffer.

    Trigger volumes are hidden while it is read: from inside one, its back
    faces are drawn translucent over everything and would be the depth.
    """
    state = session.state
    volumes = [b for b in state.brushes if b.get("is_trigger")]
    for brush in volumes:
        brush["hidden"] = True
    state.mark_world_changed(volumes)
    try:
        _frame(session)
        return _read_depth(session, px, py)
    finally:
        for brush in volumes:
            brush.pop("hidden", None)
        state.mark_world_changed(volumes)
        _frame(session)


def _read_depth(session, px, py):
    view = session.view
    view.makeCurrent()
    try:
        view.paintGL()
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, view.defaultFramebufferObject())
        scale = view.devicePixelRatioF()
        height = int(view.height() * scale)
        z = gl.glReadPixels(int(px * scale), height - 1 - int(py * scale), 1, 1,
                            gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT)
        z = float(np.asarray(z).ravel()[0])
    finally:
        view.doneCurrent()
    ndc = glm.vec4(2.0 * (px + 0.5) / view.width() - 1.0,
                   1.0 - 2.0 * (py + 0.5) / view.height(), 2.0 * z - 1.0, 1.0)
    point = glm.inverse(view.projection_matrix * view.view_matrix) * ndc
    return np.array(glm.vec3(point) / point.w), z


def _brushes_drawn_at(state, point, tol=3.0):
    """Brushes with the point on their surface (their box's boundary)."""
    found = []
    for brush in state.brushes:
        if brush.get("is_trigger"):
            continue        # hidden while the depth was read
        b = brush_placement.world_bounds(brush)
        inside = all(b[i] - tol <= point[i] <= b[i + 3] + tol for i in range(3))
        edge = min(min(abs(point[i] - b[i]), abs(point[i] - b[i + 3])) for i in range(3))
        if inside and edge <= tol:
            found.append(brush.get("id"))
    return found


def _instrument(session):
    from tools.debug_tables import DebugTablesWindow
    instrument = DebugTablesWindow(session.window)
    instrument.timer.stop()
    return instrument


def _table(instrument):
    """Debug Tables' copied RenderTable material, by brush id: a box's face
    columns, and an angled brush's geometry record (its serial: a retextured
    plane is a new record)."""
    instrument.refresh()
    table = instrument.render
    serials = table.fields["geometry_serial"]
    rows = {}
    for ident, slot in table.slot_of_id.items():
        if slot < table.count:
            gid = int(table.geometry_id[slot])
            record = int(serials[gid]) if 0 <= gid < len(serials) else -1
            rows[ident] = tuple(table.fields[name][slot].tobytes()
                                for name in MATERIAL) + (record,)
    return rows


def _click(session, px, py):
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest
    view = session.view
    QTest.mouseMove(view, QPoint(px, py))
    process_events(session.app)
    QTest.mousePress(view, Qt.LeftButton, Qt.NoModifier, QPoint(px, py))
    QTest.mouseRelease(view, Qt.LeftButton, Qt.NoModifier, QPoint(px, py))


@pytest.mark.parametrize("volume", ["none", "box", "angled"])
def test_a_face_mode_click_retextures_the_brush_under_the_cursor(fio_session, volume):
    session = fio_session(_level(volume), size=(240, 180))
    session.view.sysmon.set_active(False)
    window, state = session.window, session.state
    chosen = [DECAL_TEXTURE]
    window.asset_browser.get_selected_filepath = lambda: os.path.join(
        REPO_ROOT, "assets", "textures", *chosen[0].split("/"))
    instrument = _instrument(session)
    camera = session.view.logic_thread.camera.get_editor_camera()
    # Inside the volume (when there is one), looking at walls, the crate and
    # the angled pillar in turn.
    camera.pos = glm.vec3(40.0, 120.0, 20.0)
    volume = next((b for b in state.brushes if b.get("is_trigger")), None)
    if volume is not None:
        # Inside it: its shape's centroid (the shape is about ``pos``).
        convex = brush_placement.shape_convex(volume)
        centre = np.asarray(volume["pos"], dtype=float)
        if convex is not None:
            centre = centre + convex.verts.mean(axis=0)
        camera.pos = glm.vec3(*(float(v) for v in centre))
        assert brush_placement.ray_box(brush_placement.authored_box(volume),
                                       tuple(centre), (1.0, 0.0, 0.0)) == 0.0
    window.toggle_face_mode(True)
    rng = np.random.default_rng(7)
    clicks = 0
    try:
        for step, (yaw, pitch) in enumerate((
                (180.0, -8.0), (215.0, -12.0), (140.0, -20.0),
                (0.0, -5.0), (90.0, -30.0), (270.0, 10.0))):
            camera.yaw, camera.pitch = yaw, pitch
            chosen[0] = TEXTURES[step]
            _frame(session)
            px = int(rng.integers(40, 200))
            py = int(rng.integers(40, 140))
            point, depth = _depth_point(session, px, py)
            if depth >= 1.0:
                continue                                # sky: nothing to texture
            drawn = _brushes_drawn_at(state, point)
            before = _table(instrument)
            _click(session, px, py)
            _frame(session)
            after = _table(instrument)
            changed = sorted(i for i in after if before.get(i) != after[i])
            hovered = session.view.hovered_face_info
            where = (f"view yaw={yaw} pitch={pitch} pixel=({px},{py}) point="
                     f"{np.round(point).tolist()} picked="
                     f"{hovered and (hovered[0].get('name'), hovered[1])}")
            assert len(changed) == 1, f"{where}: Debug Tables rows changed {changed}"
            assert changed[0] in drawn, (
                f"{where}: the click changed {changed[0]} but the depth buffer "
                f"shows {drawn} under the cursor")
            clicks += 1
        assert clicks >= 4
        # The texture from textures/decals is named with its folder and loads
        # as itself, not as the default texture.
        with session.render_state() as frame:
            table = frame.render_table
            assert DECAL_TEXTURE in table.texture_names()
        renderer = session.view.renderer
        session.view.makeCurrent()
        try:
            default = renderer.load_texture("default.png", "textures")
            decal = renderer.load_texture(DECAL_TEXTURE, "textures")
        finally:
            session.view.doneCurrent()
        assert decal and decal != default
        assert renderer._texture_pixel_size(DECAL_TEXTURE) != (1, 1)
    finally:
        instrument.close()
        instrument.deleteLater()


@pytest.mark.parametrize("gesture", ["click", "rotate"])
def test_after_the_camera_moves_under_a_still_cursor_the_new_face_is_edited(
        fio_session, gesture):
    """The hover was re-picked on mouse moves only: fly the camera (WASD,
    mouse look) with the cursor still and a click -- or Page Up/Down -- went
    to the face that had been under the cursor before, on another brush."""
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QCursor
    session = fio_session(_level("none"), size=(240, 180))
    session.view.sysmon.set_active(False)
    window, state, view = session.window, session.state, session.view
    window.asset_browser.get_selected_filepath = lambda: os.path.join(
        REPO_ROOT, "assets", "textures", "decals", "vent.jpg")
    instrument = _instrument(session)
    camera = view.logic_thread.camera.get_editor_camera()
    camera.pos = glm.vec3(40.0, 120.0, 20.0)
    window.toggle_face_mode(True)
    px, py = 120, 90
    try:
        # The cursor rests on one face...
        camera.yaw, camera.pitch = 180.0, 5.0
        _frame(session)
        QCursor.setPos(view.mapToGlobal(QPoint(px, py)))
        from PyQt5.QtTest import QTest
        QTest.mouseMove(view, QPoint(px, py))
        process_events(session.app)
        first = view.hovered_face_info
        # ...then the camera turns to another brush; the cursor does not move.
        camera.yaw, camera.pitch = 0.0, 5.0
        _frame(session)
        point, depth = _depth_point(session, px, py)
        drawn = _brushes_drawn_at(state, point)
        assert depth < 1.0 and drawn and first[0].get("id") not in drawn
        before = _table(instrument)
        if gesture == "click":
            _click(session, px, py)
        else:
            window.rotate_textures(1)
        _frame(session)
        after = _table(instrument)
        changed = sorted(i for i in after if before.get(i) != after[i])
        assert len(changed) == 1 and changed[0] in drawn, (
            f"{gesture} changed {changed}; under the cursor now {drawn}, "
            f"before the camera turned {first[0].get('id')}")
    finally:
        instrument.close()
        instrument.deleteLater()


def test_the_inspectors_face_stays_highlighted_while_another_is_hovered(fio_session):
    """The Surface Inspector's face is drawn as the selection (orange) whatever
    the cursor is over; the face under the cursor only faintly."""
    session = fio_session(_level("none"), size=(240, 180))
    session.view.sysmon.set_active(False)
    window, state, view = session.window, session.state, session.view
    floor, wall = state.brushes[0], state.brushes[2]
    window.toggle_face_mode(True)
    window.show_surface_inspector(wall, 'south', raise_window=False)
    view.face_under_cursor = lambda pos=None: (floor, 'top')
    drawn = []
    renderer = view.renderer
    real_highlight, real_hover = renderer.draw_face_highlight, renderer.draw_face_hover
    renderer.draw_face_highlight = lambda p, v, t: (drawn.append(('selected', len(t))),
                                                    real_highlight(p, v, t))
    renderer.draw_face_hover = lambda p, v, t: (drawn.append(('hover', len(t))),
                                                real_hover(p, v, t))
    try:
        _frame(session)
    finally:
        del renderer.draw_face_highlight, renderer.draw_face_hover
    from engine.overlay_geometry import face_highlight_triangles
    assert ('selected', len(face_highlight_triangles(wall, 'south'))) in drawn
    assert ('hover', len(face_highlight_triangles(floor, 'top'))) in drawn
