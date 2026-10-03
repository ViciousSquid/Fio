"""Connections: the entity-to-entity wiring the I/O system dispatches.

A connection is stored on its *source* entity and names its target twice — by
stable id and by name.  The id is what survives a rename; the name is what a
legacy map (and the property panel) has.  These tests cover creating, reading,
removing and serialising connections through the public helpers in
``editor.io_system``, for both of Fio's entity shapes (brush dicts and ``Thing``
instances), plus the revision counter every cached consumer keys off.
"""

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                       # noqa: E402
from editor.io_system import OutputConnection            # noqa: E402
from editor.things import LogicRelay, Trigger            # noqa: E402
from tests.helpers.worlds import box_brush, make_thing   # noqa: E402

pytestmark = pytest.mark.qt


def _conn(target="door", output="OnTrigger", input_name="Open", **kw):
    return OutputConnection(output_name=output, target_name=target,
                            input_name=input_name, **kw)


@pytest.fixture(params=["brush", "thing"])
def entity(request):
    """A source entity in each of the two shapes Fio uses."""
    if request.param == "brush":
        return box_brush("source_brush", is_trigger=True)
    return make_thing(LogicRelay, "source_relay")


# ---------------------------------------------------------------------------
# Creation and removal
# ---------------------------------------------------------------------------

def test_a_connection_is_stored_on_its_source(entity):
    connection = _conn()
    io.add_connection(entity, connection)
    assert io.get_connections(entity) == [connection], (
        "the connection is not on its source; got %s"
        % (io.get_connections(entity),))


def test_an_entity_with_no_connections_reads_as_an_empty_list():
    assert io.get_connections(box_brush("plain")) == []
    assert io.get_connections(object()) == [], \
        "an object that is neither a brush nor a Thing must not raise"


def test_connections_keep_the_order_they_were_added(entity):
    first, second, third = _conn(output="A"), _conn(output="B"), _conn(output="C")
    for connection in (first, second, third):
        io.add_connection(entity, connection)
    assert [c.output_name for c in io.get_connections(entity)] == ["A", "B", "C"], (
        "dispatch order follows storage order; got %s"
        % ([c.output_name for c in io.get_connections(entity)],))


def test_removing_a_connection_leaves_the_others(entity):
    keep, drop = _conn(output="Keep"), _conn(output="Drop")
    io.add_connection(entity, keep)
    io.add_connection(entity, drop)

    io.remove_connection(entity, drop)

    assert io.get_connections(entity) == [keep], (
        "after removing 'Drop' the source holds %s"
        % ([c.output_name for c in io.get_connections(entity)],))


def test_removing_a_connection_that_is_not_there_is_harmless(entity):
    io.add_connection(entity, _conn(output="Keep"))
    io.remove_connection(entity, _conn(output="Stranger"))
    assert len(io.get_connections(entity)) == 1


def test_clearing_removes_everything(entity):
    io.add_connection(entity, _conn())
    io.add_connection(entity, _conn(output="Other"))
    io.clear_connections(entity)
    assert io.get_connections(entity) == []


# ---------------------------------------------------------------------------
# Identity: id and name
# ---------------------------------------------------------------------------

def test_a_connection_carries_both_the_target_id_and_its_name():
    connection = _conn(target="big_door", target_id="uuid-1234")
    assert connection.target_name == "big_door"
    assert connection.target_id == "uuid-1234"


def test_renaming_the_target_does_not_touch_an_id_addressed_connection():
    """The id is the durable half; a rename must not invalidate the wiring."""
    target = box_brush("door", is_door=True)
    source = box_brush("button", is_trigger=True)
    io.add_connection(source, _conn(target="door", target_id=target["id"]))

    target["name"] = "renamed_door"

    connection = io.get_connections(source)[0]
    assert connection.target_id == target["id"], (
        "the connection's target_id changed with the rename")


def test_entity_type_for_io_distinguishes_the_brush_roles():
    assert io.get_entity_type_for_io(box_brush("b")) == "brush"
    assert io.get_entity_type_for_io(box_brush("t", is_trigger=True)) == "trigger"
    assert io.get_entity_type_for_io(box_brush("d", is_door=True)) == "door"
    assert io.get_entity_type_for_io(box_brush("m", is_mover=True)) == "mover"


def test_entity_type_for_io_of_a_thing_is_its_type_property():
    relay = make_thing(LogicRelay, "relay")
    assert io.get_entity_type_for_io(relay) == relay.properties["type"] == "logic_relay", (
        "the I/O type of a Thing is its 'type' property verbatim; got %r"
        % io.get_entity_type_for_io(relay))


def test_entity_type_for_io_of_something_else_is_unknown():
    assert io.get_entity_type_for_io(object()) == "unknown"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def test_a_connection_survives_a_dict_round_trip():
    original = OutputConnection(output_name="OnStartTouch", target_name="lift",
                                input_name="Open", parameter="fast", delay=1.5,
                                fire_once=True, target_id="uuid-9")
    restored = OutputConnection.from_dict(original.to_dict())
    assert restored == original, (
        "round trip changed the connection:\n  before %r\n  after  %r"
        % (original, restored))


def test_an_empty_target_id_is_omitted_from_the_serialised_form():
    """Legacy maps have no ids; writing an empty one would be noise."""
    data = _conn().to_dict()
    assert "target_id" not in data, \
        "an empty target_id was serialised: %r" % (data,)


def test_a_legacy_connection_without_an_id_still_deserialises():
    restored = OutputConnection.from_dict({
        "output": "OnTrigger", "target": "door", "input": "Open"})
    assert restored.target_id == ""
    assert restored.delay == 0.0 and restored.fire_once is False, (
        "missing optional fields must take their documented defaults; got %r"
        % (restored,))


def test_serialising_an_entity_produces_one_dict_per_connection(entity):
    io.add_connection(entity, _conn(output="A"))
    io.add_connection(entity, _conn(output="B"))
    data = io.serialize_connections(entity)
    assert [d["output"] for d in data] == ["A", "B"]
    assert all(isinstance(d, dict) for d in data)


def test_deserialising_replaces_whatever_was_there(entity):
    io.add_connection(entity, _conn(output="Stale"))
    io.deserialize_connections(entity, [_conn(output="Fresh").to_dict()])
    assert [c.output_name for c in io.get_connections(entity)] == ["Fresh"]


# ---------------------------------------------------------------------------
# fire_once state
# ---------------------------------------------------------------------------

def test_reset_clears_the_fired_flag_across_a_whole_scene():
    """Entering play mode must not inherit the last session's fired state."""
    brush = box_brush("button", is_trigger=True)
    thing = make_thing(Trigger, "area")
    for host in (brush, thing):
        connection = _conn(fire_once=True)
        connection._fired = True
        io.add_connection(host, connection)

    io.reset_all_connections([brush, thing])

    for host in (brush, thing):
        assert io.get_connections(host)[0]._fired is False, (
            "a fire_once connection on %r is still marked fired after a reset"
            % (host if isinstance(host, dict) else host.name,))


def test_the_fired_flag_is_not_serialised():
    connection = _conn(fire_once=True)
    connection._fired = True
    assert "_fired" not in connection.to_dict(), (
        "run-time fire_once state leaked into the saved map: %r"
        % (connection.to_dict(),))


# ---------------------------------------------------------------------------
# The revision counter
# ---------------------------------------------------------------------------

def test_adding_a_connection_moves_the_revision(entity):
    before = io.io_revision()
    io.add_connection(entity, _conn())
    assert io.io_revision() != before, (
        "io_revision stayed at %d after a connection was added; every cached "
        "lookup keyed off it would go stale" % before)


def test_removing_a_connection_moves_the_revision(entity):
    connection = _conn()
    io.add_connection(entity, connection)
    before = io.io_revision()
    io.remove_connection(entity, connection)
    assert io.io_revision() != before


def test_setting_connections_wholesale_moves_the_revision(entity):
    before = io.io_revision()
    io.set_connections(entity, [_conn()])
    assert io.io_revision() != before


def test_removing_a_connection_that_is_absent_does_not_move_the_revision(entity):
    io.add_connection(entity, _conn(output="Keep"))
    before = io.io_revision()
    io.remove_connection(entity, _conn(output="Stranger"))
    assert io.io_revision() == before, (
        "a no-op removal bumped the revision from %d to %d, invalidating every "
        "cached panel for nothing" % (before, io.io_revision()))


def test_bump_io_revision_is_available_for_edits_made_outside_the_helpers():
    """A map load or an undo rewrites ``_io_connections`` directly."""
    before = io.io_revision()
    io.bump_io_revision()
    assert io.io_revision() != before


# ---------------------------------------------------------------------------
# I/O definitions
# ---------------------------------------------------------------------------

def test_registering_io_defs_makes_them_queryable():
    io.register_io("test_widget",
                   inputs=[io.IODef("Poke", "poke it")],
                   outputs=[io.IODef("OnPoked", "it was poked")])
    assert io.get_input_names("test_widget") == ["Poke"]
    assert io.get_output_names("test_widget") == ["OnPoked"]


def test_io_defs_for_an_unknown_type_are_empty_not_an_error():
    assert io.get_inputs("no_such_entity_type") == []
    assert io.get_outputs("no_such_entity_type") == []
    assert io.get_input_names("no_such_entity_type") == []


def test_io_def_lookup_matches_the_registered_key_exactly():
    """The registry is keyed verbatim, so entity types must be spelt one way.

    Every ``Thing`` sets its ``type`` from its class name lower-cased (or an
    explicit lower-case default), and ``register_default_io`` registers the
    same strings, so exact matching is sufficient - but it does mean a plugin
    that registers "MyWidget" and an entity whose type is "mywidget" will not
    find each other.
    """
    io.register_io("casetest", inputs=[io.IODef("Go")], outputs=[])
    assert io.get_input_names("casetest") == ["Go"]
    assert io.get_input_names("CaseTest") == [], (
        "lookup is exact; a differently-cased key must miss rather than "
        "silently resolve")


def test_every_default_io_type_is_registered_in_lower_case():
    """What makes the exact-match lookup above safe in practice."""
    io.register_default_io()
    odd = [key for key in io.IO_REGISTRY if key != key.lower()]
    assert not odd, (
        "these registered entity types are not lower case and so can never be "
        "matched by an entity's 'type' property: %s" % (sorted(odd),))
