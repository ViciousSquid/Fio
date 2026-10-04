"""The Surface Inspector and the derived geometry it edits, across a history step.

The inspector binds to ``(brush, face key)`` — a direct reference to a brush
dict.  Undo replaces that dict, so an inspector left bound across one edits an
object nothing draws: the panel reads sensible numbers, accepts edits, and
throws them away.

The second half is the same problem one layer down.  A face's *winding* carries
its texture, UV scale and texture basis, not just its shape, so editing a face's
mapping makes the cached ``ConvexGeometry`` — and the GPU mesh the renderer
built from it, which is keyed on the geometry signature — as stale as moving the
plane would.  Neither said so.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")


from editor import face_texture as ft  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402

# Qt tier: PyQt5 must be importable.  No display and no GPU - the suite runs
# against the offscreen platform plugin.
pytestmark = pytest.mark.qt



@pytest.fixture
def host(main_window):
    editor = main_window
    brush = {'name': 'wall', 'pos': [0, 0, 0], 'size': [256, 256, 256]}
    editor.state.brushes.append(brush)
    editor.set_selected_objects([brush])
    editor.state.save_state()
    return editor


# ---------------------------------------------------------------------------
# Rebinding across a history step
# ---------------------------------------------------------------------------

def test_the_inspector_follows_its_brush_through_an_undo(host):
    brush = host.state.brushes[0]
    host.show_surface_inspector(brush, 'top')
    panel = host.surface_inspector

    brush['size'] = [512, 256, 256]
    host.state.save_state()
    assert host.state.undo()
    host._rebind_face_targets()

    live = host.state.brushes[0]
    assert panel.target[0] is live
    assert panel.target[1] == 'top'
    assert host.face_texture_target[0] is live


def test_the_inspector_is_unbound_when_its_brush_is_undone_away(host):
    host.state.save_state()                  # checkpoint at "mouse-down"
    extra = {'name': 'pillar', 'pos': [512, 0, 0], 'size': [64, 256, 64]}
    host.state.brushes.append(extra)
    host.set_selected_objects([extra])
    host.show_surface_inspector(extra, 'top')
    panel = host.surface_inspector
    host.face_texture_target = (extra, 'top')

    assert host.state.undo()
    host._rebind_face_targets()

    assert panel.target is None
    assert host.face_texture_target is None


def test_rebinding_leaves_an_already_live_target_alone(host):
    brush = host.state.brushes[0]
    host.show_surface_inspector(brush, 'north')
    host._rebind_face_targets()
    assert host.surface_inspector.target == (brush, 'north')


def test_rebinding_copes_with_no_inspector_and_no_target(host):
    host._rebind_face_targets()          # must not raise
    assert host.face_texture_target is None


# ---------------------------------------------------------------------------
# Surface edits invalidate the derived geometry
# ---------------------------------------------------------------------------

def make_angled():
    brush = {'name': 'wedge', 'id': 'w', 'pos': [0, 0, 0],
             'size': [256, 256, 256], 'textures': {}}
    assert bg.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    return brush


def cut_face(brush):
    return next(k for k in ft.face_keys(brush) if k.startswith('#'))


def test_texturing_a_cut_face_rebuilds_its_winding():
    brush = make_angled()
    before = bg.get_convex(brush)
    key = cut_face(brush)

    ft.set_transform(brush, key, texture='NEW.png')

    after = bg.get_convex(brush)
    assert after is not before
    plane = int(key[1:])
    face = next(f for f in after.faces if f['plane'] == plane)
    assert face['texture'] == 'NEW.png'


def test_texturing_a_cut_face_moves_the_geometry_signature():
    """The renderer's GPU-mesh cache is keyed on it; it must notice."""
    brush = make_angled()
    before = bg.geometry_signature(brush)
    ft.set_transform(brush, cut_face(brush), texture='NEW.png')
    assert bg.geometry_signature(brush) != before


def test_changing_a_cut_faces_uv_scale_moves_the_signature():
    brush = make_angled()
    before = bg.geometry_signature(brush)
    ft.set_transform(brush, cut_face(brush), scale=(0.25, 0.25))
    assert bg.geometry_signature(brush) != before


def test_texturing_a_tagged_face_of_an_angled_brush_moves_the_signature():
    brush = make_angled()
    tagged = next(k for k in ft.face_keys(brush) if not k.startswith('#'))
    before = bg.geometry_signature(brush)
    ft.set_transform(brush, tagged, texture='TAG.png')
    assert bg.geometry_signature(brush) != before


def test_a_transform_edit_does_not_disturb_the_shape():
    brush = make_angled()
    import numpy as np
    before = np.sort(bg.brush_points(brush).copy(), axis=0)
    ft.set_transform(brush, cut_face(brush), shift=(8.0, 4.0), scale=(0.5, 0.5))
    after = np.sort(bg.brush_points(brush), axis=0)
    assert np.allclose(before, after)


def test_the_signature_is_stable_when_nothing_is_edited():
    """It must move on a change and not otherwise, or caches never hit."""
    brush = make_angled()
    first = bg.geometry_signature(brush)
    bg.get_convex(brush)
    assert bg.geometry_signature(brush) == first
