"""Tests for the Surface Inspector panel itself.

These drive the real widget offscreen against a stand-in host, covering the
things the panel is responsible for rather than the transform maths (which
``test_face_texture`` covers): which faces a control reaches, and that a run of
edits is one undo step rather than one per click.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

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
    assert panel.face_combo.currentData() == 'north'
    assert panel.face_combo.currentText() == 'north'
    assert panel.tex_label.text() == 'tex_north.png'
    assert panel.size_label.text() == '512 x 128'


def test_it_labels_a_cut_face_as_one(qt_app):
    host = FakeHost()
    panel = SurfaceInspector(host)
    brush = make_box(size=(128, 128, 128))
    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)
    key = next(k for k in ft.face_keys(brush) if k.startswith('#'))
    panel.set_target(brush, key)
    assert panel.face_combo.currentData() == key
    assert 'cut face' in panel.face_combo.currentText()


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


def turn_natural_on(panel):
    panel.natural_btn.setChecked(True)
    panel._toggle_natural()


def test_natural_button_uses_the_real_texture_size(inspector):
    _, panel, brush = inspector
    # default.png ships at 512x512; a 512-wide face therefore repeats once.
    ft.set_transform(brush, 'north', texture='default.png')
    turn_natural_on(panel)
    scale = ft.get_transform(brush, 'north')['scale']
    assert scale[0] == pytest.approx(1.0)
    assert scale[1] == pytest.approx(0.25)


def test_an_unreadable_texture_falls_back_instead_of_failing(inspector):
    _, panel, brush = inspector
    ft.set_transform(brush, 'north', texture='does_not_exist.png')
    turn_natural_on(panel)
    expected = 512.0 / ft.DEFAULT_TEXTURE_SIZE[0]
    assert ft.get_transform(brush, 'north')['scale'][0] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Natural as a mode
# ---------------------------------------------------------------------------

def test_natural_survives_a_resize_at_a_constant_texel_size(inspector):
    """The whole point: resizing reveals more texture, it does not stretch it."""
    _, panel, brush = inspector
    ft.set_transform(brush, 'north', texture='default.png')
    turn_natural_on(panel)
    assert ft.is_natural(brush, 'north')

    brush['size'][0] = 1024.0          # the wall is now twice as wide
    live = ft.get_transform(brush, 'north', texture_size=(512, 512))
    assert live['scale'][0] == pytest.approx(2.0)     # twice the repeats
    assert live['natural'] is True


def test_the_button_shows_whether_the_face_is_natural(inspector):
    _, panel, brush = inspector
    assert not panel.natural_btn.isChecked()
    turn_natural_on(panel)
    panel.refresh_from_face()
    assert panel.natural_btn.isChecked()


def test_turning_natural_off_freezes_the_current_scale(inspector):
    _, panel, brush = inspector
    ft.set_transform(brush, 'north', texture='default.png')
    turn_natural_on(panel)
    frozen = ft.get_transform(brush, 'north', texture_size=(512, 512))['scale']

    panel.natural_btn.setChecked(False)
    panel._toggle_natural()

    assert not ft.is_natural(brush, 'north')
    assert ft.get_transform(brush, 'north')['scale'] == pytest.approx(frozen)
    # And it now stays put when the brush is resized.
    brush['size'][0] = 1024.0
    assert ft.get_transform(brush, 'north')['scale'] == pytest.approx(frozen)


def test_setting_a_scale_by_hand_switches_natural_off(inspector):
    _, panel, brush = inspector
    turn_natural_on(panel)
    panel.hstretch.setValue(3.0)
    assert not ft.is_natural(brush, 'north')
    assert ft.get_transform(brush, 'north')['scale'][0] == pytest.approx(3.0)


def test_nudging_the_shift_leaves_natural_alone(inspector):
    """A shift edit must not silently drop the mode."""
    _, panel, brush = inspector
    turn_natural_on(panel)
    panel.hshift.setValue(0.25)
    assert ft.is_natural(brush, 'north')
    assert ft.get_transform(brush, 'north')['shift'][0] == pytest.approx(0.25)


def test_rotating_leaves_natural_alone(inspector):
    _, panel, brush = inspector
    turn_natural_on(panel)
    panel.rotate.setValue(90.0)
    assert ft.is_natural(brush, 'north')
    assert ft.get_transform(brush, 'north')['angle'] == pytest.approx(90.0)


def test_fit_switches_natural_off(inspector):
    _, panel, brush = inspector
    turn_natural_on(panel)
    panel._apply_fit()
    assert not ft.is_natural(brush, 'north')


# ---------------------------------------------------------------------------
# Face Mode toggle (moved here from the Asset Browser)
# ---------------------------------------------------------------------------

def test_the_face_button_drives_face_mode(inspector):
    host, panel, _ = inspector
    calls = []
    host.toggle_face_mode = calls.append
    panel.face_btn.setChecked(True)
    panel._on_face_mode_clicked()
    assert calls == [True]


def test_the_face_button_follows_face_mode_toggled_elsewhere(inspector):
    _, panel, _ = inspector
    panel.sync_face_button(True)
    assert panel.face_btn.isChecked()
    panel.sync_face_button(False)
    assert not panel.face_btn.isChecked()


def test_syncing_the_face_button_does_not_re_enter_face_mode(inspector):
    host, panel, _ = inspector
    calls = []
    host.toggle_face_mode = calls.append
    panel.sync_face_button(True)
    assert calls == []          # the signal was blocked


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
    panel.hshift.setValue(0.31)
    panel.hshift_step.setValue(0.125)
    panel._match_grid()
    assert ft.get_transform(brush, 'north')['shift'][0] == pytest.approx(0.25)


def test_match_grid_snaps_the_rotation_to_its_own_step(inspector):
    """Rotate has no Step field; the spin box's own increment stands in."""
    _, panel, brush = inspector
    assert panel.rotate.singleStep() == pytest.approx(panel.ROTATE_STEP)

    panel.rotate.setValue(43.0)
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


# ---------------------------------------------------------------------------
# The Asset Browser's route into the panel
# ---------------------------------------------------------------------------

def test_the_asset_browser_has_an_inspector_button(qt_app, tmp_path):
    from editor.asset_browser import AssetBrowserTab

    calls = []

    class _Editor:
        def toggle_surface_inspector(self):
            calls.append(True)

    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=_Editor())
    assert tab.inspector_btn is not None
    assert tab.inspector_btn.text() == 'INSPECTOR'
    tab.on_inspector_clicked()
    assert calls == [True]


def test_the_inspector_button_shares_the_face_toggle_colour(qt_app):
    from editor.asset_browser import INSPECTOR_BUTTON_STYLE
    from editor.surface_inspector import FACE_BUTTON_STYLE

    assert INSPECTOR_BUTTON_STYLE is FACE_BUTTON_STYLE


def test_the_asset_browser_no_longer_owns_a_face_toggle(qt_app, tmp_path):
    from editor.asset_browser import AssetBrowserTab

    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=None)
    assert not hasattr(tab, 'face_btn')


def test_the_asset_browser_owns_no_texture_controls(qt_app, tmp_path):
    """FIT and TILE duplicated the Inspector's Fit and Natural.

    TILE baked the very scale Natural keeps live, and FIT cleared it — the
    same two projections, reached a second way and a worse one, since a baked
    scale stretches when the brush is resized.  Texturing is the Inspector's
    job now; the only button left here opens it.
    """
    from editor.asset_browser import AssetBrowserTab

    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=None)

    for gone in ('fit_btn', 'tile_btn', 'face_btn',
                 'perform_main_action', 'apply_texture'):
        assert not hasattr(tab, gone), "%s should have moved to the Inspector" % gone


def test_the_asset_browser_action_bar_keeps_only_the_two_it_should(qt_app, tmp_path):
    """The hamburger folder toggle stays; INSPECTOR is the only other button."""
    from PyQt5.QtWidgets import QPushButton

    from editor.asset_browser import AssetBrowserTab

    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=None)
    labels = [b.text() for b in tab.action_bar.findChildren(QPushButton)]

    assert sorted(labels) == sorted(['\u2630', 'INSPECTOR'])


def test_the_inspector_button_needs_no_texture_selected(qt_app, tmp_path):
    """FIT and TILE were disabled until a texture was picked; this is not."""
    from editor.asset_browser import AssetBrowserTab

    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=None)

    assert tab.selected_item is None
    assert tab.inspector_btn.isEnabled()
    tab.update_buttons_enabled()
    assert tab.inspector_btn.isEnabled()


def test_the_editor_no_longer_carries_the_duplicate_apply_paths(qt_app):
    """The apply-texture entry points the Inspector replaced.

    ``apply_texture_to_brush`` was FIT/TILE's implementation and only theirs.
    ``apply_texture_to_selected_face`` had already lost its caller before
    that, and warned through a modal box the Inspector answers inline.
    """
    from editor.main_window import MainWindow

    assert not hasattr(MainWindow, 'apply_texture_to_brush')
    assert not hasattr(MainWindow, 'apply_texture_to_selected_face')
    # The face-level apply the 3D view uses in Face Mode stays.
    assert hasattr(MainWindow, 'apply_texture_to_specific_face')


def test_closing_the_panel_leaves_face_mode(inspector):
    """The panel owns the only FACE toggle, so it must not strand the mode."""
    host, panel, _ = inspector
    calls = []
    host.toggle_face_mode = calls.append

    panel.show()
    panel.face_btn.setChecked(True)
    panel._on_face_mode_clicked()
    assert calls == [True]

    panel.close()
    assert calls == [True, False]
    assert not panel.face_btn.isChecked()


def test_hiding_the_panel_also_leaves_face_mode(inspector):
    host, panel, _ = inspector
    calls = []
    host.toggle_face_mode = calls.append

    panel.show()
    panel.face_btn.setChecked(True)
    panel._on_face_mode_clicked()
    panel.hide()

    assert calls[-1] is False


def test_hiding_with_face_mode_off_changes_nothing(inspector):
    host, panel, _ = inspector
    calls = []
    host.toggle_face_mode = calls.append
    panel.show()
    panel.hide()
    assert calls == []


# ────────────────────────────
# Escape
# ────────────────────────────

def _escape(panel):
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    panel.keyPressEvent(event)
    return event


def test_escape_backs_out_of_the_mode_without_closing_the_panel(inspector):
    """A QDialog rejects itself on Escape; this one must not.

    Pressing Escape to leave Face Mode used to shut the window, and since the
    panel holds the only FACE toggle, the mode and the way out of it went at
    the same time.
    """
    host, panel, brush = inspector
    escapes = []
    host.handle_escape = lambda: (escapes.append(True), True)[1]
    panel.show()

    _escape(panel)

    assert escapes == [True]
    assert panel.isVisible()


def test_escape_is_accepted_so_the_dialog_never_sees_it(inspector):
    host, panel, brush = inspector
    host.handle_escape = lambda: False

    event = _escape(panel)

    assert event.isAccepted()


def test_escape_survives_a_host_without_the_hook(inspector):
    """An older host, or a panel built against a stand-in, must not crash."""
    host, panel, brush = inspector
    assert not hasattr(host, 'handle_escape')
    panel.show()

    _escape(panel)

    assert panel.isVisible()


def test_other_keys_still_reach_the_dialog(inspector):
    """Only Escape is intercepted."""
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent

    host, panel, brush = inspector
    host.handle_escape = lambda: pytest.fail("Tab must not be routed to escape")

    panel.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier))


# ────────────────────────────
# The face picker
# ────────────────────────────

def test_the_picker_lists_every_face_of_the_brush(inspector):
    host, panel, brush = inspector

    listed = [panel.face_combo.itemData(i)
              for i in range(panel.face_combo.count())]

    assert listed == list(ft.face_keys(brush))


def test_picking_a_face_rebinds_the_controls(inspector):
    """Choosing a face here must work like clicking it in the 3D view."""
    host, panel, brush = inspector
    ft.set_transform(brush, 'east', scale=(3.0, 4.0), angle=90.0)

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('east'))

    assert panel.target == (brush, 'east')
    assert panel.hstretch.value() == pytest.approx(3.0)
    assert panel.vstretch.value() == pytest.approx(4.0)
    assert panel.rotate.value() == pytest.approx(90.0)


def test_picking_a_face_makes_it_the_editor_s_current_one(inspector):
    """Page Up/Down and a reopened panel should follow the picker."""
    host, panel, brush = inspector
    assert not hasattr(host, 'face_texture_target')

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('top'))

    assert host.face_texture_target == (brush, 'top')


def test_picking_a_face_edits_nothing(inspector):
    """Switching faces is navigation, so it must not push an undo state."""
    host, panel, brush = inspector
    before = dict(brush)
    saves = host.saves

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('west'))

    assert host.saves == saves
    assert brush == before


def test_the_picker_follows_a_target_set_from_the_3d_view(inspector):
    host, panel, brush = inspector

    panel.set_target(brush, 'down')

    assert panel.face_combo.currentData() == 'down'


def test_the_picker_relists_when_the_brush_changes(qt_app):
    """A clipped brush grows a cut face; the picker has to pick it up."""
    host = FakeHost()
    panel = SurfaceInspector(host)
    brush = make_box(size=(128, 128, 128))
    panel.set_target(brush, 'north')
    before = panel.face_combo.count()

    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)
    key = next(k for k in ft.face_keys(brush) if k.startswith('#'))
    panel.set_target(brush, key)

    listed = [panel.face_combo.itemData(i)
              for i in range(panel.face_combo.count())]
    assert key in listed
    assert panel.face_combo.count() != before or listed == list(ft.face_keys(brush))


def test_a_face_the_brush_no_longer_has_is_still_shown(qt_app):
    """Never silently retarget: an orphaned face stays selected, and visible."""
    host = FakeHost()
    panel = SurfaceInspector(host)
    brush = make_box()
    panel.set_target(brush, '#99')          # a cut face this brush lacks

    assert panel.face_combo.currentData() == '#99'
    assert panel.target == (brush, '#99')


# ────────────────────────────
# Faces are independent
# ────────────────────────────

def test_one_face_can_be_natural_while_another_is_fit(inspector):
    """The whole point of the picker: per-face projections."""
    host, panel, brush = inspector

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('north'))
    panel.natural_btn.setChecked(True)
    panel._toggle_natural()

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('south'))
    panel._apply_fit()

    assert ft.is_natural(brush, 'north')
    assert not ft.is_natural(brush, 'south')


def test_the_natural_button_reflects_the_face_that_is_picked(inspector):
    """Switching faces must not leave the button showing the last one's mode."""
    host, panel, brush = inspector

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('north'))
    panel.natural_btn.setChecked(True)
    panel._toggle_natural()
    assert panel.natural_btn.isChecked()

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('south'))
    assert not panel.natural_btn.isChecked()

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('north'))
    assert panel.natural_btn.isChecked()


def test_natural_on_one_face_survives_a_resize_while_fit_stretches(inspector):
    """The difference the two projections exist for, side by side."""
    host, panel, brush = inspector
    size = (512, 512)

    ft.apply_natural(brush, 'north', size)
    ft.apply_fit(brush, 'south', (1.0, 1.0))
    brush['size'][0] *= 2

    natural = ft.get_transform(brush, 'north', texture_size=size)['scale']
    fitted = ft.get_transform(brush, 'south', texture_size=size)['scale']

    assert natural[0] == pytest.approx(brush['size'][0] / size[0])
    assert fitted == pytest.approx((1.0, 1.0))


# ────────────────────────────
# Chrome
# ────────────────────────────

def test_fit_wears_the_fio_accent(inspector):
    from editor.surface_inspector import ACCENT_BUTTON_STYLE

    host, panel, brush = inspector

    assert panel.fit_btn.styleSheet() == ACCENT_BUTTON_STYLE
    assert '#F08000' in ACCENT_BUTTON_STYLE


def test_apply_is_short_and_green(inspector):
    """The old "Apply selected" overran its column."""
    from editor.surface_inspector import APPLY_BUTTON_STYLE

    host, panel, brush = inspector

    assert panel.apply_tex_btn.text() == 'Apply'
    assert panel.apply_tex_btn.styleSheet() == APPLY_BUTTON_STYLE
    assert '#2E7D32' in APPLY_BUTTON_STYLE


def test_the_apply_button_still_applies(inspector):
    """Renaming it must not have cost it its job."""
    host, panel, brush = inspector
    host.asset_browser.path = os.path.join('assets', 'textures', 'brick.png')

    panel.apply_tex_btn.click()

    assert ft.get_transform(brush, 'north')['texture'] == 'brick.png'


def test_the_panel_carries_no_shortcut_blurb(inspector):
    from PyQt5.QtWidgets import QLabel

    host, panel, brush = inspector
    texts = [w.text() for w in panel.findChildren(QLabel)]

    assert not any('reopens this panel' in t for t in texts)


def test_fit_writes_a_scale_so_it_beats_the_legacy_tiling_flag(inspector):
    """Old maps can carry a brush-wide ``texture_tiling``.

    Both renderers check the face's own ``uv_scale`` before falling back to
    that flag, so Fit only stays per-face as long as it writes one.
    """
    host, panel, brush = inspector
    brush['texture_tiling'] = True          # as a legacy map would have it

    ft.apply_fit(brush, 'north', (1.0, 1.0))

    assert brush.get('uv_scale', {}).get('north') is not None
    assert not ft.is_natural(brush, 'north')


# ────────────────────────────
# Rotation is one field
# ────────────────────────────

def test_rotate_has_no_step_field(inspector):
    host, panel, brush = inspector

    assert not hasattr(panel, 'rotate_step')
    assert panel.rotate.suffix() == '°'


def test_the_rotate_field_spans_the_step_column(inspector):
    """A single control, not a value sitting beside an empty cell."""
    from PyQt5.QtWidgets import QGridLayout

    host, panel, brush = inspector
    grid = next(g for g in panel.findChildren(QGridLayout)
                if g.indexOf(panel.rotate) != -1)
    _, _, row_span, col_span = grid.getItemPosition(grid.indexOf(panel.rotate))

    assert (row_span, col_span) == (1, 3)
    # The rows that kept their Step still occupy one column.
    _, _, _, shift_span = grid.getItemPosition(grid.indexOf(panel.hshift))
    assert shift_span == 1


def test_rotation_applies_to_one_face_in_this_face_scope(inspector):
    host, panel, brush = inspector
    panel.scope_face.setChecked(True)

    panel.rotate.setValue(90.0)

    assert ft.get_transform(brush, 'north')['angle'] == pytest.approx(90.0)
    assert ft.get_transform(brush, 'south')['angle'] == pytest.approx(0.0)


def test_rotation_applies_to_every_face_in_whole_brush_scope(inspector):
    host, panel, brush = inspector
    panel.scope_brush.setChecked(True)

    panel.rotate.setValue(90.0)

    for key in ft.face_keys(brush):
        assert ft.get_transform(brush, key)['angle'] == pytest.approx(90.0)


def test_the_rotate_field_shows_the_picked_face_s_rotation(inspector):
    host, panel, brush = inspector
    ft.set_transform(brush, 'east', angle=30.0)

    panel.face_combo.setCurrentIndex(panel.face_combo.findData('east'))

    assert panel.rotate.value() == pytest.approx(30.0)


def test_rotating_does_not_drop_natural(inspector):
    """Rotation is not a scale, so it must not switch the mode off."""
    host, panel, brush = inspector
    ft.apply_natural(brush, 'north', (512, 512))
    panel.refresh_from_face()

    panel.rotate.setValue(45.0)

    assert ft.is_natural(brush, 'north')
    assert ft.get_transform(brush, 'north')['angle'] == pytest.approx(45.0)


def test_the_rotate_field_wraps(inspector):
    """Past 360 it comes round rather than sticking at the end."""
    host, panel, brush = inspector

    assert panel.rotate.wrapping()


# ────────────────────────────
# Opening with nothing selected
# ────────────────────────────

def test_it_opens_with_no_target_at_all(qt_app):
    """A tool should open when it is asked for, selection or not."""
    host = FakeHost()
    panel = SurfaceInspector(host)

    panel.set_target(None, None)

    assert panel.isVisible()
    assert panel.target is None


def test_an_untargeted_panel_greys_out_what_needs_a_face(qt_app):
    host = FakeHost()
    panel = SurfaceInspector(host)

    panel.set_target(None, None)

    for widget in (panel.apply_tex_btn, panel.face_combo, panel.fit_btn,
                   panel.natural_btn, panel.axial_btn, panel.match_grid_btn,
                   panel.flip_h_btn, panel.flip_v_btn, panel.hshift,
                   panel.rotate, panel.scope_face, panel.scope_brush):
        assert not widget.isEnabled()


def test_face_mode_stays_available_with_no_target(qt_app):
    """Turning Face Mode on is how you go and pick a face to edit."""
    host = FakeHost()
    panel = SurfaceInspector(host)

    panel.set_target(None, None)

    assert panel.face_btn.isEnabled()


def test_an_untargeted_panel_shows_no_stale_values(inspector):
    """It must not keep showing the last face's numbers as though live."""
    host, panel, brush = inspector
    ft.set_transform(brush, 'north', scale=(7.0, 9.0), angle=30.0)
    panel.set_target(brush, 'north')

    panel.set_target(None, None)

    assert panel.tex_label.text() == '(none)'
    assert panel.size_label.text() == ''
    assert panel.face_combo.count() == 0
    assert panel.hstretch.value() == pytest.approx(1.0)
    assert panel.rotate.value() == pytest.approx(0.0)


def test_the_controls_do_nothing_with_no_target(inspector):
    """Every action guards on the target; this proves none slipped through."""
    host, panel, brush = inspector
    panel.set_target(None, None)
    before = copy.deepcopy(brush)
    saves = host.saves

    panel._apply_fit()
    panel._apply_axial()
    panel._toggle_natural()
    panel._match_grid()
    panel._flip(horizontal=True)
    panel._apply_selected_texture()
    panel._on_value_changed('scale')

    assert brush == before
    assert host.saves == saves


def test_binding_a_face_enables_the_controls_again(inspector):
    host, panel, brush = inspector

    panel.set_target(None, None)
    panel.set_target(brush, 'north')

    assert panel.fit_btn.isEnabled()
    assert panel.face_combo.isEnabled()
    assert panel.face_combo.currentData() == 'north'


def test_re_binding_can_leave_the_focus_alone(inspector):
    """The editor re-binds on selection changes; it must not grab focus."""
    host, panel, brush = inspector

    panel.set_target(brush, 'east', raise_window=False)

    assert panel.target == (brush, 'east')
    assert panel.isVisible()


# ────────────────────────────
# The editor opening and re-binding it
# ────────────────────────────

class FakeEditorWindow(QWidget):
    """The slice of MainWindow the two Surface Inspector entry points use."""

    from editor.main_window import MainWindow
    show_surface_inspector = MainWindow.show_surface_inspector
    toggle_surface_inspector = MainWindow.toggle_surface_inspector
    sync_surface_inspector = MainWindow.sync_surface_inspector
    del MainWindow

    def __init__(self):
        super().__init__()
        self.state = EditorState()
        self.state.selected_objects = []
        self.surface_inspector = None
        self.view_3d = _StubView()
        self.toasts = []
        self.asset_browser = _StubBrowser()
        self.root_dir = os.getcwd()

    def save_state(self):
        pass

    def update_views(self):
        pass

    def show_toast(self, message, is_error=False, duration=None):
        self.toasts.append((message, is_error))

    def _selected_brushes(self):
        return [b for b in self.state.selected_objects if isinstance(b, dict)]


class _StubView:
    hovered_face_info = None


def test_the_shortcut_opens_the_panel_with_nothing_selected(qt_app):
    """It used to refuse with "Select a brush or a face first"."""
    host = FakeEditorWindow()

    host.toggle_surface_inspector()

    assert host.surface_inspector is not None
    assert host.surface_inspector.isVisible()
    assert host.toasts == []


def test_opening_it_empty_binds_nothing(qt_app):
    host = FakeEditorWindow()

    host.toggle_surface_inspector()

    assert host.surface_inspector.target is None


def test_the_shortcut_still_closes_it(qt_app):
    host = FakeEditorWindow()
    host.toggle_surface_inspector()

    host.toggle_surface_inspector()

    assert not host.surface_inspector.isVisible()


def test_an_open_empty_panel_binds_when_a_brush_is_selected(qt_app):
    """Otherwise opening it first would leave it useless."""
    host = FakeEditorWindow()
    host.toggle_surface_inspector()
    brush = make_box()

    host.state.selected_objects = [brush]
    host.sync_surface_inspector()

    assert host.surface_inspector.target[0] is brush


def test_a_panel_already_in_the_selection_is_left_alone(qt_app):
    """Re-binding on every click would undo a face picked from the dropdown."""
    host = FakeEditorWindow()
    brush = make_box()
    host.state.selected_objects = [brush]
    host.toggle_surface_inspector()
    host.surface_inspector.set_target(brush, 'east')

    host.sync_surface_inspector()

    assert host.surface_inspector.target == (brush, 'east')


def test_it_follows_the_selection_to_another_brush(qt_app):
    host = FakeEditorWindow()
    one, two = make_box(), make_box(pos=(256, 0, 0))
    host.state.selected_objects = [one]
    host.toggle_surface_inspector()
    assert host.surface_inspector.target[0] is one

    host.state.selected_objects = [two]
    host.sync_surface_inspector()

    assert host.surface_inspector.target[0] is two


def test_deselecting_does_not_blank_a_panel_being_worked_in(qt_app):
    host = FakeEditorWindow()
    brush = make_box()
    host.state.selected_objects = [brush]
    host.toggle_surface_inspector()

    host.state.selected_objects = []
    host.sync_surface_inspector()

    assert host.surface_inspector.target[0] is brush


def test_a_closed_panel_is_not_woken_by_a_selection(qt_app):
    host = FakeEditorWindow()

    host.state.selected_objects = [make_box()]
    host.sync_surface_inspector()

    assert host.surface_inspector is None
