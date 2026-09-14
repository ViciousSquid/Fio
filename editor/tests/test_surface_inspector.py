"""Tests for the Surface Inspector panel itself.

These drive the real widget offscreen against a stand-in host, covering the
things the panel is responsible for rather than the transform maths (which
``test_face_texture`` covers): which faces a control reaches, and that a run of
edits is one undo step rather than one per click.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor import face_texture as ft  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402
from editor.surface_inspector import SurfaceInspector  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class _StubBrowser:
    def __init__(self, path=None):
        self.path = path

    def get_selected_filepath(self):
        return self.path


class FakeHost:
    """The slice of MainWindow the inspector talks to."""

    root_dir = os.getcwd()

    def __init__(self):
        self.state = EditorState()
        self.state.selected_objects = []
        self.saves = 0
        self.view_updates = 0
        self.toasts = []
        self.unsaved_changes = False
        self.asset_browser = _StubBrowser()

    def save_state(self):
        self.saves += 1

    def update_views(self):
        self.view_updates += 1

    def show_toast(self, message, is_error=False, duration=None):
        self.toasts.append(message)

    def _selected_brushes(self):
        return [b for b in self.state.selected_objects if isinstance(b, dict)]


def make_box(pos=(0, 0, 0), size=(512, 128, 64)):
    return {
        'pos': list(pos),
        'size': list(size),
        'textures': {tag: 'tex_%s.png' % tag for tag in bg.FACE_TAGS},
    }


@pytest.fixture
def inspector(qt_app):
    host = FakeHost()
    panel = SurfaceInspector(host)
    brush = make_box()
    host.state.brushes.append(brush)
    host.state.selected_objects = [brush]
    panel.set_target(brush, 'north')
    return host, panel, brush


# ---------------------------------------------------------------------------
# Binding and display
# ---------------------------------------------------------------------------

def test_it_shows_the_bound_face(inspector):
    _, panel, _ = inspector
    assert panel.face_label.text() == 'north'
    assert panel.tex_label.text() == 'tex_north.png'
    assert panel.size_label.text() == '512 x 128'


def test_it_labels_a_cut_face_as_one(qt_app):
    host = FakeHost()
    panel = SurfaceInspector(host)
    brush = make_box(size=(128, 128, 128))
    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)
    key = next(k for k in ft.face_keys(brush) if k.startswith('#'))
    panel.set_target(brush, key)
    assert 'cut face' in panel.face_label.text()


def test_it_reloads_the_values_of_whatever_face_it_is_pointed_at(inspector):
    host, panel, brush = inspector
    ft.set_transform(brush, 'east', scale=(3.0, 4.0), angle=90.0)
    panel.set_target(brush, 'east')
    assert panel.hstretch.value() == pytest.approx(3.0)
    assert panel.vstretch.value() == pytest.approx(4.0)
    assert panel.rotate.value() == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_face_scope_touches_only_the_bound_face(inspector):
    host, panel, brush = inspector
    panel.scope_face.setChecked(True)
    panel.fit_w.setValue(2.0)
    panel.fit_h.setValue(2.0)
    panel._apply_fit()
    assert ft.get_transform(brush, 'north')['scale'] == (2.0, 2.0)
    assert ft.get_transform(brush, 'east')['scale'] == (1.0, 1.0)


def test_brush_scope_touches_every_face(inspector):
    host, panel, brush = inspector
    panel.scope_brush.setChecked(True)
    panel.fit_w.setValue(2.0)
    panel.fit_h.setValue(3.0)
    panel._apply_fit()
    for key in ft.face_keys(brush):
        assert ft.get_transform(brush, key)['scale'] == (2.0, 3.0)


def test_brush_scope_spans_a_multi_selection(inspector):
    host, panel, brush = inspector
    other = make_box()
    host.state.brushes.append(other)
    host.state.selected_objects = [brush, other]
    panel.scope_brush.setChecked(True)
    panel._apply_fit()
    assert ft.get_transform(other, 'north')['scale'] == (1.0, 1.0)
    assert ft.get_transform(other, 'top')['scale'] == (1.0, 1.0)


def test_brush_scope_ignores_brushes_outside_the_selection(inspector):
    host, panel, brush = inspector
    stranger = make_box()
    host.state.brushes.append(stranger)          # present but not selected
    panel.scope_brush.setChecked(True)
    panel.fit_w.setValue(5.0)
    panel._apply_fit()
    assert ft.get_transform(stranger, 'north')['scale'] == (1.0, 1.0)


# ---------------------------------------------------------------------------
# Projection buttons
# ---------------------------------------------------------------------------

def test_fit_button_uses_the_repeat_spinners(inspector):
    _, panel, brush = inspector
    panel.fit_w.setValue(4.0)
    panel.fit_h.setValue(0.5)
    panel._apply_fit()
    assert ft.get_transform(brush, 'north')['scale'] == (4.0, 0.5)


def test_natural_button_uses_the_real_texture_size(inspector):
    _, panel, brush = inspector
    # default.png ships at 512x512; a 512-wide face therefore repeats once.
    ft.set_transform(brush, 'north', texture='default.png')
    panel._apply_natural()
    scale = ft.get_transform(brush, 'north')['scale']
    assert scale[0] == pytest.approx(1.0)
    assert scale[1] == pytest.approx(0.25)


def test_an_unreadable_texture_falls_back_instead_of_failing(inspector):
    _, panel, brush = inspector
    ft.set_transform(brush, 'north', texture='does_not_exist.png')
    panel._apply_natural()
    expected = 512.0 / ft.DEFAULT_TEXTURE_SIZE[0]
    assert ft.get_transform(brush, 'north')['scale'][0] == pytest.approx(expected)


def test_axial_button_clears_a_locked_basis(inspector):
    _, panel, brush = inspector
    bg.box_to_geometry(brush)
    bg.rotate_brush(brush, 35.0, [0.0, 1.0, 0.0])
    panel.set_target(brush, 'north')
    assert ft.face_plane(brush, 'north').get('uv_u') is not None
    panel._apply_axial()
    assert ft.face_plane(brush, 'north').get('uv_u') is None


def test_flip_buttons_mirror_the_texture(inspector):
    _, panel, brush = inspector
    panel._flip(horizontal=True)
    assert ft.get_transform(brush, 'north')['scale'][0] == pytest.approx(-1.0)
    panel._flip(vertical=True)
    assert ft.get_transform(brush, 'north')['scale'][1] == pytest.approx(-1.0)


def test_match_grid_snaps_to_the_step_fields(inspector):
    _, panel, brush = inspector
    panel.rotate.setValue(43.0)
    panel.rotate_step.setValue(45.0)
    panel._match_grid()
    assert ft.get_transform(brush, 'north')['angle'] == pytest.approx(45.0)


def test_applying_the_browser_texture(inspector):
    host, panel, brush = inspector
    host.asset_browser.path = os.path.join('assets', 'textures', 'brick.png')
    panel._apply_selected_texture()
    assert ft.get_transform(brush, 'north')['texture'] == 'brick.png'


def test_applying_with_no_texture_selected_says_so(inspector):
    host, panel, brush = inspector
    host.asset_browser.path = None
    panel._apply_selected_texture()
    assert any('texture' in t.lower() for t in host.toasts)
    assert ft.get_transform(brush, 'north')['texture'] == 'tex_north.png'


# ---------------------------------------------------------------------------
# Undo coalescing
# ---------------------------------------------------------------------------

def test_a_run_of_edits_is_one_undo_step(inspector):
    host, panel, _ = inspector
    assert host.saves == 0
    for value in (0.125, 0.25, 0.375, 0.5):
        panel.hshift.setValue(value)
    panel.rotate.setValue(45.0)
    panel._apply_fit()
    assert host.saves == 1


def test_a_new_run_after_a_pause_is_a_fresh_undo_step(inspector):
    host, panel, _ = inspector
    panel.hshift.setValue(0.25)
    assert host.saves == 1
    panel._close_undo_burst()          # what the idle timer does
    panel.hshift.setValue(0.5)
    assert host.saves == 2


def test_editing_marks_the_map_dirty_and_repaints(inspector):
    host, panel, _ = inspector
    panel.hshift.setValue(0.25)
    assert host.unsaved_changes
    assert host.view_updates > 0


def test_loading_the_panel_does_not_count_as_an_edit(inspector):
    host, panel, brush = inspector
    before = host.saves
    panel.set_target(brush, 'east')    # repopulates every spin box
    assert host.saves == before
