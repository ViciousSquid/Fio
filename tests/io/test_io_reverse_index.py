"""The I/O reverse index has to stay an index, not a stale copy of the scene.

"What points at this entity?" is asked on every Property Editor build, so the
answer is served from a cached index rather than a scan.  The cache is only
allowed to be a *cache*: everything it reports must still be derivable from the
live scene, and it must not become a second place where the scene's facts are
written down.

It was.  Each entry baked in the *name* of the source entity at build time, and
renaming an entity changes no connection, so nothing invalidated the index — the
"Targeted by" list went on quoting a name nothing in the scene answered to.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from editor import io_system as io  # noqa: E402

# The module itself is Qt-free; one test reaches into the property editor
# and skips on its own when PyQt5 is absent.



class FakeThing:
    def __init__(self, properties):
        self.properties = properties


@pytest.fixture(autouse=True)
def fresh_index():
    """Every test starts from a cold index."""
    io.bump_io_revision()
    yield
    io.bump_io_revision()


def connection(output, target_name, target_id, input_name='Open'):
    return io.OutputConnection(output_name=output, target_name=target_name,
                               target_id=target_id, input_name=input_name)


@pytest.fixture
def scene():
    source = {'name': 'button', 'id': 'B1', '_io_connections': [
        connection('OnPressed', 'door', 'D1')]}
    target = {'name': 'door', 'id': 'D1'}
    return [source, target], []


# ---------------------------------------------------------------------------

def test_a_source_rename_shows_up_immediately(scene):
    brushes, things = scene
    assert io.find_targeting_sources(brushes, things, 'door', 'D1') == [
        ('button', 'I/O: OnPressed')]

    brushes[0]['name'] = 'big_red_button'

    assert io.find_targeting_sources(brushes, things, 'door', 'D1') == [
        ('big_red_button', 'I/O: OnPressed')]


def test_a_target_rename_keeps_an_identity_addressed_connection(scene):
    brushes, things = scene
    brushes[1]['name'] = 'vault_door'
    assert io.find_targeting_sources(brushes, things, 'vault_door', 'D1') == [
        ('button', 'I/O: OnPressed')]


def test_deleting_the_source_removes_it_from_the_answer(scene):
    brushes, things = scene
    del brushes[0]
    io.bump_io_revision()
    assert io.find_targeting_sources(brushes, things, 'door', 'D1') == []


def test_duplicating_a_source_reports_both(scene):
    brushes, things = scene
    copy = {'name': 'button_copy', 'id': 'B2', '_io_connections': [
        connection('OnPressed', 'door', 'D1')]}
    brushes.append(copy)
    io.bump_io_revision()

    found = io.find_targeting_sources(brushes, things, 'door', 'D1')
    assert sorted(found) == [('button', 'I/O: OnPressed'),
                             ('button_copy', 'I/O: OnPressed')]


def test_a_connection_addressed_by_both_id_and_name_is_reported_once(scene):
    brushes, things = scene
    assert len(io.find_targeting_sources(brushes, things, 'door', 'D1')) == 1


def test_object_replacement_is_picked_up_when_the_revision_moves(scene):
    """Undo rebuilds every object; the revision bump is what says so."""
    brushes, things = scene
    io.find_targeting_sources(brushes, things, 'door', 'D1')      # warm the cache

    replacement = [{'name': 'lever', 'id': 'L1', '_io_connections': [
        connection('OnUsed', 'door', 'D1')]},
        {'name': 'door', 'id': 'D1'}]
    io.bump_io_revision()

    assert io.find_targeting_sources(replacement, things, 'door', 'D1') == [
        ('lever', 'I/O: OnUsed')]


def test_a_thing_source_is_reported_by_its_live_name():
    thing = FakeThing({'name': 'trigger', 'id': 'T1', '_io_connections': [
        connection('OnTrigger', 'door', 'D1')]})
    brushes = [{'name': 'door', 'id': 'D1'}]

    assert io.find_targeting_sources(brushes, [thing], 'door', 'D1') == [
        ('trigger', 'I/O: OnTrigger')]

    thing.properties['name'] = 'pressure_plate'
    assert io.find_targeting_sources(brushes, [thing], 'door', 'D1') == [
        ('pressure_plate', 'I/O: OnTrigger')]


def test_the_legacy_target_property_is_still_indexed():
    brushes = [{'name': 'old_trigger', 'id': 'T0', 'is_trigger': True,
                'target': 'door'},
               {'name': 'door', 'id': 'D1'}]
    io.bump_io_revision()
    assert io.find_targeting_sources(brushes, [], 'door', '') == [
        ('old_trigger', 'trigger')]


def test_a_legacy_source_rename_also_shows_up():
    brushes = [{'name': 'old_trigger', 'id': 'T0', 'is_trigger': True,
                'target': 'door'},
               {'name': 'door', 'id': 'D1'}]
    io.find_targeting_sources(brushes, [], 'door', '')
    brushes[0]['name'] = 'renamed_trigger'
    assert io.find_targeting_sources(brushes, [], 'door', '') == [
        ('renamed_trigger', 'trigger')]


def test_the_index_is_reused_while_nothing_changes(scene):
    """It exists to avoid a scan per panel build; that must still hold."""
    brushes, things = scene
    first = io.target_index(brushes, things)
    assert io.target_index(brushes, things) is first


def test_the_index_is_rebuilt_when_connections_change(scene):
    brushes, things = scene
    first = io.target_index(brushes, things)
    io.add_connection(brushes[0], connection('OnReleased', 'door', 'D1'))
    assert io.target_index(brushes, things) is not first


def test_renaming_bumps_the_revision_so_dependent_panels_rebuild(scene):
    """A target's panel quotes its sources' names but cannot see them change."""
    brushes, things = scene
    before = io.io_revision()

    pytest.importorskip("PyQt5", reason="the property editor is a Qt widget")
    from editor.property_editor import PropertyEditor
    editor = PropertyEditor.__new__(PropertyEditor)
    editor.current_object = brushes[0]
    editor._populating = True          # suppress the widget work
    PropertyEditor.update_object_prop(editor, 'name', 'renamed')

    assert brushes[0]['name'] == 'renamed'
    assert io.io_revision() != before
