"""Tests for the Property Editor's rebuild avoidance.

Building a panel constructs several tabs' worth of widgets, and
``set_object()`` is called after every editor operation — including once per
mouse-move during a rotate drag.  These cover when a rebuild happens, when a
previously built page is put back instead, and that neither path can show
stale values.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

import configparser  # noqa: E402

from PyQt5.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from editor import io_system  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402
from editor.io_system import OutputConnection  # noqa: E402
from editor.property_editor import PropertyEditor  # noqa: E402
from editor.things import Light  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class FakeHost(QWidget):
    """The slice of MainWindow the property editor talks to."""

    def __init__(self):
        super().__init__()
        self.state = EditorState()
        self.state.selected_objects = []
        self.config = configparser.ConfigParser()
        self.grid_size = 16
        self.saves = 0

    def save_state(self):
        self.saves += 1

    def update_views(self):
        pass

    def update_all_ui(self):
        pass

    def show_toast(self, message, is_error=False, duration=None):
        pass


def make_brush(name='wall', **extra):
    brush = {
        'pos': [0, 0, 0],
        'size': [128, 64, 32],
        'name': name,
        'id': 'id-%s' % name,
        'textures': {tag: 'default.png' for tag in bg.FACE_TAGS},
    }
    brush.update(extra)
    return brush


@pytest.fixture
def panel(qt_app):
    host = FakeHost()
    editor = PropertyEditor(host)
    return host, editor


def page_of(editor):
    """The widget currently showing the object's properties."""
    return editor._page


# ---------------------------------------------------------------------------
# Skipping the rebuild entirely
# ---------------------------------------------------------------------------

def test_selecting_the_same_object_again_does_not_rebuild(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)

    editor.set_object(brush)
    first = page_of(editor)
    assert first is not None

    editor.set_object(brush)
    assert page_of(editor) is first          # the very same widgets


def test_moving_a_brush_does_not_rebuild_the_panel(panel):
    """Position is not shown, so a drag or rotate must not cost a rebuild."""
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    first = page_of(editor)

    brush['pos'] = [512, 0, 256]
    brush['size'] = [200, 64, 32]
    editor.set_object(brush)

    assert page_of(editor) is first


def test_rotating_a_brush_does_not_rebuild_the_panel(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    first = page_of(editor)

    bg.box_to_geometry(brush)
    bg.rotate_brush(brush, 30.0, [0.0, 1.0, 0.0])
    editor.set_object(brush)

    assert page_of(editor) is first


def test_a_displayed_property_change_does_rebuild(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    first = page_of(editor)

    brush['name'] = 'renamed'
    editor.set_object(brush)

    assert page_of(editor) is not first
    assert editor._widgets['name_input'].text() == 'renamed'


def test_toggling_a_behaviour_rebuilds_for_the_new_tabs(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    before = editor.tab_widget.isTabVisible(editor.trigger_tab_index)

    brush['is_trigger'] = True
    editor.set_object(brush)

    assert not before
    assert editor.tab_widget.isTabVisible(editor.trigger_tab_index)


def test_force_rebuilds_even_when_nothing_changed(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    first = page_of(editor)

    editor.set_object(brush, force=True)

    assert page_of(editor) is not first


def test_skipping_a_rebuild_leaves_the_panel_usable(panel):
    """The _populating guard must not be left latched by the fast path."""
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    editor.set_object(brush)
    assert editor._populating is False


# ---------------------------------------------------------------------------
# Reusing a page built earlier
# ---------------------------------------------------------------------------

def test_switching_back_to_an_object_reuses_its_page(panel):
    host, editor = panel
    a = make_brush('a')
    b = make_brush('b')
    host.state.brushes.extend([a, b])

    editor.set_object(a)
    page_a = page_of(editor)
    editor.set_object(b)
    page_b = page_of(editor)
    assert page_b is not page_a

    editor.set_object(a)
    assert page_of(editor) is page_a         # widgets reused, not rebuilt
    editor.set_object(b)
    assert page_of(editor) is page_b


def test_a_reused_page_brings_its_widget_handles_with_it(panel):
    """Callbacks look widgets up by name; a restored page must supply its own."""
    host, editor = panel
    a = make_brush('a')
    b = make_brush('b')
    host.state.brushes.extend([a, b])

    editor.set_object(a)
    name_a = editor._widgets['name_input']
    editor.set_object(b)
    assert editor._widgets['name_input'] is not name_a

    editor.set_object(a)
    assert editor._widgets['name_input'] is name_a
    assert editor._widgets['name_input'].text() == 'a'


def test_a_cached_page_is_discarded_when_its_object_changed(panel):
    host, editor = panel
    a = make_brush('a')
    b = make_brush('b')
    host.state.brushes.extend([a, b])

    editor.set_object(a)
    page_a = page_of(editor)
    editor.set_object(b)

    a['name'] = 'a_renamed'                  # changed while parked
    editor.set_object(a)

    assert page_of(editor) is not page_a
    assert editor._widgets['name_input'].text() == 'a_renamed'


def test_the_cache_is_bounded(panel):
    host, editor = panel
    brushes = [make_brush('b%d' % i) for i in range(editor.PAGE_CACHE_SIZE + 4)]
    host.state.brushes.extend(brushes)
    for brush in brushes:
        editor.set_object(brush)
    assert len(editor._page_cache) <= editor.PAGE_CACHE_SIZE


def test_the_oldest_page_is_the_one_dropped(panel):
    host, editor = panel
    brushes = [make_brush('b%d' % i) for i in range(editor.PAGE_CACHE_SIZE + 2)]
    host.state.brushes.extend(brushes)
    for brush in brushes:
        editor.set_object(brush)
    cached = [entry[0] for entry in editor._page_cache]
    assert brushes[0] not in cached
    assert brushes[-2] in cached


def test_invalidating_the_cache_forces_fresh_pages(panel):
    host, editor = panel
    a = make_brush('a')
    b = make_brush('b')
    host.state.brushes.extend([a, b])
    editor.set_object(a)
    page_a = page_of(editor)
    editor.set_object(b)

    editor.invalidate_cache()
    editor.set_object(a)
    assert page_of(editor) is not page_a


def test_entities_get_the_same_treatment(panel):
    host, editor = panel
    one = Light(pos=[0, 0, 0])
    two = Light(pos=[64, 0, 0])
    host.state.things.extend([one, two])

    editor.set_object(one)
    page_one = page_of(editor)
    editor.set_object(two)
    editor.set_object(one)
    assert page_of(editor) is page_one


def test_moving_an_entity_does_not_rebuild(panel):
    host, editor = panel
    light = Light(pos=[0, 0, 0])
    host.state.things.append(light)
    editor.set_object(light)
    first = page_of(editor)

    light.pos = [256, 0, 128]
    editor.set_object(light)

    assert page_of(editor) is first


def test_selecting_nothing_clears_the_panel(panel):
    host, editor = panel
    brush = make_brush()
    host.state.brushes.append(brush)
    editor.set_object(brush)
    editor.set_object(None)
    assert editor.current_object is None
    assert page_of(editor) is None


# ---------------------------------------------------------------------------
# The "Targeted by" line, which depends on the rest of the scene
# ---------------------------------------------------------------------------

def test_a_new_connection_elsewhere_refreshes_the_targeted_by_line(panel):
    host, editor = panel
    target = make_brush('door')
    source = make_brush('button', is_trigger=True)
    host.state.brushes.extend([target, source])

    editor.set_object(target)
    first = page_of(editor)

    io_system.add_connection(source, OutputConnection(
        output_name='OnTrigger', target_name='door', input_name='Open'))
    editor.set_object(target)

    # The object itself did not change, but what points at it did.
    assert page_of(editor) is not first


def test_targeting_sources_are_found_through_the_index(panel):
    host, editor = panel
    target = make_brush('door')
    source = make_brush('button', is_trigger=True)
    host.state.brushes.extend([target, source])
    io_system.add_connection(source, OutputConnection(
        output_name='OnTrigger', target_name='door', input_name='Open'))

    sources = editor._find_targeting_sources('door', 'id-door')
    assert sources == [('button', 'I/O: OnTrigger')]


def test_a_connection_addressed_by_id_is_found_once(panel):
    """Filed under both name and id, but reported once."""
    host, editor = panel
    target = make_brush('door')
    source = make_brush('button', is_trigger=True)
    host.state.brushes.extend([target, source])
    conn = OutputConnection(output_name='OnTrigger', target_name='door',
                            input_name='Open')
    conn.target_id = 'id-door'
    io_system.add_connection(source, conn)

    sources = editor._find_targeting_sources('door', 'id-door')
    assert sources == [('button', 'I/O: OnTrigger')]


def test_a_renamed_target_is_still_found_by_id(panel):
    host, editor = panel
    target = make_brush('door')
    source = make_brush('button', is_trigger=True)
    host.state.brushes.extend([target, source])
    conn = OutputConnection(output_name='OnTrigger', target_name='door',
                            input_name='Open')
    conn.target_id = 'id-door'
    io_system.add_connection(source, conn)

    assert editor._find_targeting_sources('renamed_door', 'id-door')


def test_the_legacy_target_property_is_still_found(panel):
    host, editor = panel
    target = make_brush('door')
    source = make_brush('button', is_trigger=True, target='door')
    host.state.brushes.extend([target, source])
    io_system.bump_io_revision()

    assert editor._find_targeting_sources('door', 'id-door') == \
        [('button', 'trigger')]


def test_entities_are_searched_as_well_as_brushes(panel):
    host, editor = panel
    target = make_brush('door')
    relay = Light(pos=[0, 0, 0])
    relay.properties['name'] = 'relay'
    host.state.brushes.append(target)
    host.state.things.append(relay)
    io_system.add_connection(relay, OutputConnection(
        output_name='OnFire', target_name='door', input_name='Open'))

    assert editor._find_targeting_sources('door', 'id-door') == \
        [('relay', 'I/O: OnFire')]


def test_nothing_targeting_it_returns_nothing(panel):
    host, editor = panel
    target = make_brush('door')
    host.state.brushes.append(target)
    io_system.bump_io_revision()
    assert editor._find_targeting_sources('door', 'id-door') == []


def test_an_anonymous_target_is_not_searched_for(panel):
    host, editor = panel
    assert editor._find_targeting_sources('', '') == []


def test_target_existence_uses_the_name_set(panel):
    host, editor = panel
    host.state.brushes.append(make_brush('door'))
    light = Light(pos=[0, 0, 0])
    light.properties['name'] = 'lamp'
    host.state.things.append(light)

    assert editor._check_target_exists('door')
    assert editor._check_target_exists('lamp')
    assert not editor._check_target_exists('nothing')
    assert not editor._check_target_exists('')


# ---------------------------------------------------------------------------
# The index itself
# ---------------------------------------------------------------------------

def test_the_index_is_reused_until_connections_change():
    brushes = [make_brush('a'), make_brush('b')]
    things = []
    first = io_system.target_index(brushes, things)
    assert io_system.target_index(brushes, things) is first

    io_system.add_connection(brushes[0], OutputConnection(
        output_name='OnTrigger', target_name='b', input_name='Open'))
    assert io_system.target_index(brushes, things) is not first


def test_removing_a_connection_marks_the_index_stale():
    brushes = [make_brush('a'), make_brush('b')]
    conn = OutputConnection(output_name='OnTrigger', target_name='b',
                            input_name='Open')
    io_system.add_connection(brushes[0], conn)
    before = io_system.target_index(brushes, [])
    io_system.remove_connection(brushes[0], conn)
    assert io_system.target_index(brushes, []) is not before


def test_replacing_the_scene_marks_the_index_stale():
    brushes = [make_brush('a')]
    io_system.target_index(brushes, [])
    revision = io_system.io_revision()
    io_system.bump_io_revision()
    assert io_system.io_revision() != revision


# ────────────────────────────
# Lazy I/O tab
# ────────────────────────────

def _io_tab_index(editor):
    for i in range(editor.tab_widget.count()):
        if 'I/O' in editor.tab_widget.tabText(i):
            return i
    return None


def test_io_tab_is_added_but_left_empty_until_shown(panel):
    """The tab bar looks the same; the expensive widget is not built yet."""
    host, editor = panel
    thing = Light(pos=[0, 0, 0])
    host.state.things = [thing]
    editor.set_object(thing, force=True)

    index = _io_tab_index(editor)
    if index is None:
        pytest.skip("the I/O system is not available in this environment")

    assert editor.tab_widget.currentIndex() != index
    placeholder = editor.tab_widget.widget(index)
    assert not placeholder.property('io_built')
    assert placeholder.layout().count() == 0


def test_io_tab_builds_on_first_switch_and_only_once(panel):
    host, editor = panel
    thing = Light(pos=[0, 0, 0])
    host.state.things = [thing]
    editor.set_object(thing, force=True)

    index = _io_tab_index(editor)
    if index is None:
        pytest.skip("the I/O system is not available in this environment")

    editor.tab_widget.setCurrentIndex(index)
    placeholder = editor.tab_widget.widget(index)
    assert placeholder.property('io_built')
    assert placeholder.layout().count() == 1

    # Switching away and back must not stack a second copy.
    editor.tab_widget.setCurrentIndex(0)
    editor.tab_widget.setCurrentIndex(index)
    assert placeholder.layout().count() == 1


def _find_mover_combo(editor):
    """The 'Parent Mover' combo on the page currently shown."""
    from editor.property_editor import ClickableComboBox
    for combo in editor._page.findChildren(ClickableComboBox):
        if _is_mover_combo(combo):
            return combo
    return None


def _is_mover_combo(combo):
    row = combo.parentWidget()
    return row is not None and any(
        isinstance(w, QLabel) and w.text() == "Parent Mover:"
        for w in row.findChildren(QLabel))


def _find_attach_checkbox(editor):
    from PyQt5.QtWidgets import QCheckBox
    for box in editor._page.findChildren(QCheckBox):
        if box.text() == "Attach to Mover":
            return box
    return None


# ────────────────────────────
# Where parked pages live
# ────────────────────────────

def test_parked_pages_are_not_top_level_windows(panel):
    """A parked page must stay parented, or it slows every later rebuild.

    Qt walks top-level windows when it propagates style, font and palette
    changes, so pages parked with ``setParent(None)`` made building the next
    page slower in proportion to how many were held.
    """
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]

    editor.set_object(one)
    page = editor._page
    editor.set_object(two)

    assert page.parent() is editor._parking
    assert not page.isVisible()
    assert page.window() is not page


def test_restored_page_is_shown_again(panel):
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]

    editor.set_object(one)
    page = editor._page
    editor.set_object(two)
    editor.set_object(one)

    assert editor._page is page
    assert page.parent() is not editor._parking
    assert not page.isHidden()


def test_parking_survives_a_page_restore(panel):
    """``_parking`` is the editor's, not a page's — restoring must not drop it."""
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]

    parking = editor._parking
    editor.set_object(one)
    editor.set_object(two)
    editor.set_object(one)

    assert editor._parking is parking


# ────────────────────────────
# Deferred mover list
# ────────────────────────────

def test_mover_list_is_not_built_for_an_unattached_light(panel):
    """The combo scans every brush, and is hidden unless the light is attached."""
    host, editor = panel
    host.state.brushes = [make_brush('lift', is_mover=True)]
    light = Light(pos=[0, 0, 0])
    host.state.things = [light]

    editor.set_object(light)

    combo = _find_mover_combo(editor)
    assert combo is not None
    assert combo.count() == 0
    assert not combo.isVisible()


def test_mover_list_fills_when_the_light_is_attached(panel):
    host, editor = panel
    host.state.brushes = [make_brush('lift', is_mover=True)]
    light = Light(pos=[0, 0, 0])
    light.properties['parent_mover'] = 'lift'
    host.state.things = [light]

    editor.set_object(light)

    combo = _find_mover_combo(editor)
    assert combo is not None
    assert combo.currentText() == 'lift'


def test_mover_list_fills_on_toggling_attach_on(panel):
    """Filling late must pick up a mover added after the page was built."""
    host, editor = panel
    host.state.brushes = []
    light = Light(pos=[0, 0, 0])
    host.state.things = [light]

    editor.set_object(light)
    host.state.brushes = [make_brush('lift', is_mover=True)]

    checkbox = _find_attach_checkbox(editor)
    assert checkbox is not None
    checkbox.setChecked(True)

    combo = _find_mover_combo(editor)
    assert [combo.itemText(i) for i in range(combo.count())] == ['(none)', 'lift']


def test_mover_list_is_filled_only_once(panel):
    host, editor = panel
    host.state.brushes = [make_brush('lift', is_mover=True)]
    light = Light(pos=[0, 0, 0])
    host.state.things = [light]

    editor.set_object(light)
    checkbox = _find_attach_checkbox(editor)
    combo = _find_mover_combo(editor)

    checkbox.setChecked(True)
    checkbox.setChecked(False)
    checkbox.setChecked(True)

    assert [combo.itemText(i) for i in range(combo.count())] == ['(none)', 'lift']
