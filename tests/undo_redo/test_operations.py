"""Every major editor mutation, as ``perform -> undo -> verify -> redo -> verify``.

The companion suite (``test_history_object_lifetime``) covers what still points
at a live object after a checkpoint is restored.  This one covers the mutations
themselves: property edits, geometry edits, texture changes, I/O changes,
entity creation and deletion, and multi-object operations — and, for each, that
the caches keyed on the replaced objects were invalidated.

The checkpoint discipline Fio uses is unusual and worth restating, because the
tests are written against it: tools call ``save_state()`` at the *start* of a
gesture, so the top of the undo stack is the state to go back *to*, and the
operation's result only ever exists in the live scene.
"""

import copy

import pytest

pytest.importorskip("PyQt5", reason="EditorState builds editor.things entities")

from editor import io_system as io                  # noqa: E402
from editor.editor_state import EditorState         # noqa: E402
from editor.io_system import OutputConnection       # noqa: E402
from editor.things import Light, LogicRelay         # noqa: E402
from engine import brush_geometry as bg             # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def state():
    editor = EditorState()
    editor.brushes = [box_brush("wall", (0, 0, 0), (64, 64, 64))]
    editor.things = [make_thing(Light, "lamp", (0, 100, 0), intensity=1.0)]
    editor.save_state()
    return editor


def _brush(state, name):
    for brush in state.brushes:
        if brush.get("name") == name:
            return brush
    return None


def _thing(state, name):
    for thing in state.things:
        if thing.name == name:
            return thing
    return None


# ---------------------------------------------------------------------------
# Property changes
# ---------------------------------------------------------------------------

def test_a_brush_property_change_undoes_and_redoes(state):
    state.save_state()
    _brush(state, "wall")["pos"] = [500.0, 0.0, 0.0]

    assert state.undo() is True
    assert _brush(state, "wall")["pos"] == [0.0, 0.0, 0.0], (
        "undo left the brush at %s, expected the original origin"
        % (_brush(state, "wall")["pos"],))

    assert state.redo() is True
    assert _brush(state, "wall")["pos"] == [500.0, 0.0, 0.0], (
        "redo left the brush at %s, expected the moved position"
        % (_brush(state, "wall")["pos"],))


def test_an_entity_property_change_undoes_and_redoes(state):
    state.save_state()
    _thing(state, "lamp").properties["intensity"] = 9.0

    state.undo()
    assert _thing(state, "lamp").properties["intensity"] == 1.0

    state.redo()
    assert _thing(state, "lamp").properties["intensity"] == 9.0


def test_renaming_an_entity_undoes_and_redoes(state):
    state.save_state()
    _thing(state, "lamp").name = "spotlight"

    state.undo()
    assert _thing(state, "lamp") is not None, (
        "after undo the entity should answer to its original name again; "
        "the scene holds %s" % ([t.name for t in state.things],))

    state.redo()
    assert _thing(state, "spotlight") is not None


def test_a_texture_change_undoes_and_redoes(state):
    brush = _brush(state, "wall")
    original = brush["textures"]["top"]
    state.save_state()
    brush["textures"]["top"] = "Dev/checker.png"

    state.undo()
    assert _brush(state, "wall")["textures"]["top"] == original, (
        "the texture is %r after undo, expected %r"
        % (_brush(state, "wall")["textures"]["top"], original))

    state.redo()
    assert _brush(state, "wall")["textures"]["top"] == "Dev/checker.png"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def test_a_clip_undoes_and_redoes(state):
    brush = _brush(state, "wall")
    state.clip_brush(brush, (0, 1, 0), 0.0)
    assert bg.get_convex(_brush(state, "wall")).bounds[1][1] == pytest.approx(0.0)

    state.undo()
    assert not bg.brush_has_geometry(_brush(state, "wall")), (
        "undo left the brush carrying a plane set; it should be a plain box")

    state.redo()
    clipped = _brush(state, "wall")
    assert bg.brush_has_geometry(clipped), "redo did not restore the clip"
    assert bg.get_convex(clipped).bounds[1][1] == pytest.approx(0.0)


def test_a_rotation_undoes_and_redoes(state):
    brush = _brush(state, "wall")
    state.rotate_brush(brush, 45.0, (0.0, 1.0, 0.0))
    rotated_extent = bg.get_convex(_brush(state, "wall")).extents()[0]

    state.undo()
    assert not bg.brush_has_geometry(_brush(state, "wall"))
    assert _brush(state, "wall")["size"] == [64.0, 64.0, 64.0]

    state.redo()
    assert bg.get_convex(_brush(state, "wall")).extents()[0] == \
        pytest.approx(rotated_extent, abs=1e-6)


def test_a_component_edit_undoes_and_redoes(state):
    brush = _brush(state, "wall")
    state.save_state()
    points = bg.brush_points(brush).copy()
    points[0] = points[0] + [0.0, 20.0, 0.0]
    bg.rebuild_brush_from_points(brush, points)
    edited_bounds = tuple(bg.get_shape(_brush(state, "wall")).bounds[0])

    state.undo()
    assert tuple(bg.get_shape(_brush(state, "wall")).bounds[0]) != edited_bounds, (
        "undo did not restore the corner that was dragged")

    state.redo()
    assert tuple(bg.get_shape(_brush(state, "wall")).bounds[0]) == \
        pytest.approx(edited_bounds)


# ---------------------------------------------------------------------------
# Creation and deletion
# ---------------------------------------------------------------------------

def test_creating_a_brush_undoes_and_redoes(state):
    state.save_state()
    state.brushes.append(box_brush("new_wall", (300, 0, 0)))

    state.undo()
    assert _brush(state, "new_wall") is None, (
        "the created brush survived an undo; the scene holds %s"
        % ([b.get("name") for b in state.brushes],))

    state.redo()
    assert _brush(state, "new_wall") is not None


def test_deleting_a_brush_undoes_and_redoes(state):
    state.save_state()
    state.brushes.remove(_brush(state, "wall"))

    state.undo()
    assert _brush(state, "wall") is not None, "undo did not bring the brush back"

    state.redo()
    assert _brush(state, "wall") is None


def test_creating_an_entity_undoes_and_redoes(state):
    state.save_state()
    state.things.append(make_thing(LogicRelay, "relay"))

    state.undo()
    assert _thing(state, "relay") is None

    state.redo()
    assert _thing(state, "relay") is not None


def test_deleting_an_entity_undoes_and_redoes(state):
    state.save_state()
    state.things.remove(_thing(state, "lamp"))

    state.undo()
    lamp = _thing(state, "lamp")
    assert lamp is not None, "undo did not restore the deleted entity"
    assert lamp.properties["intensity"] == 1.0, (
        "the restored entity lost its properties: %s" % (lamp.properties,))

    state.redo()
    assert _thing(state, "lamp") is None


def test_a_cloned_brush_keeps_its_own_identity_across_undo_and_redo(state):
    original = _brush(state, "wall")
    state.save_state()
    clone = copy.deepcopy({k: v for k, v in original.items()
                           if k not in bg.GEO_RUNTIME_KEYS})
    clone["id"] = "clone-id"
    clone["name"] = "wall (copy)"
    state.brushes.append(clone)

    state.undo()
    state.redo()

    ids = [b["id"] for b in state.brushes]
    assert len(set(ids)) == len(ids), (
        "the clone and its original share an id after a redo: %s" % (ids,))
    assert sorted(b["name"] for b in state.brushes) == ["wall", "wall (copy)"]


# ---------------------------------------------------------------------------
# I/O changes
# ---------------------------------------------------------------------------

def test_adding_a_connection_undoes_and_redoes(state):
    button = box_brush("button", (200, 0, 0), is_trigger=True)
    state.brushes.append(button)
    state.save_state()
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="wall", input_name="Hide",
        target_id=_brush(state, "wall")["id"]))

    state.undo()
    assert io.get_connections(_brush(state, "button")) == [], (
        "the connection survived an undo: %s"
        % (io.get_connections(_brush(state, "button")),))

    state.redo()
    restored = io.get_connections(_brush(state, "button"))
    assert len(restored) == 1, "redo did not restore the connection"
    assert restored[0].output_name == "OnTrigger"
    assert restored[0].input_name == "Hide"


def test_a_restored_connection_is_a_real_connection_object_not_a_dict(state):
    """The runtime calls ``conn.output_name``; a dict would break dispatch."""
    button = box_brush("button", (200, 0, 0), is_trigger=True)
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="wall", input_name="Hide"))
    state.brushes.append(button)
    state.save_state()
    state.brushes.remove(button)

    state.undo()

    restored = io.get_connections(_brush(state, "button"))
    assert all(isinstance(c, OutputConnection) for c in restored), (
        "restored connections are %s, not OutputConnection instances"
        % ([type(c).__name__ for c in restored],))


def test_undo_invalidates_the_io_reverse_index(state):
    """The index is keyed partly on list identity; undo swaps the lists."""
    state.save_state()                      # checkpoint before adding the source
    button = box_brush("button", (200, 0, 0), is_trigger=True)
    state.brushes.append(button)
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="wall", input_name="Hide"))
    # Warm the cache, then delete the source as its own gesture.
    io.target_index(state.brushes, state.things)
    state.save_state()
    state.brushes.remove(button)

    before = io.io_revision()
    state.undo()

    assert io.io_revision() != before, (
        "undo left the I/O revision at %d, so a cached 'targeted by' panel "
        "would go on describing the pre-undo scene" % before)
    sources = io.find_targeting_sources(state.brushes, state.things,
                                        target_name="wall")
    assert [name for name, _label in sources] == ["button"], (
        "the rebuilt index does not see the restored source; it reports %s"
        % (sources,))


# ---------------------------------------------------------------------------
# Multi-object operations
# ---------------------------------------------------------------------------

def test_a_multi_object_move_undoes_as_one_step(state):
    state.save_state()                      # checkpoint before adding wall2
    state.brushes.append(box_brush("wall2", (100, 0, 0)))
    state.save_state()                      # checkpoint before the move
    for brush in state.brushes:
        brush["pos"][0] += 64.0

    state.undo()

    assert [b["pos"][0] for b in state.brushes] == [0.0, 100.0], (
        "the group move did not undo as one step; positions are %s"
        % ([b["pos"][0] for b in state.brushes],))

    state.redo()
    assert [b["pos"][0] for b in state.brushes] == [64.0, 164.0]


def test_deleting_a_multi_selection_undoes_as_one_step(state):
    state.save_state()
    state.brushes.append(box_brush("wall2", (100, 0, 0)))
    state.things.append(make_thing(LogicRelay, "relay"))
    state.save_state()                      # checkpoint before the delete
    state.brushes.clear()
    state.things.clear()

    state.undo()

    assert sorted(b["name"] for b in state.brushes) == ["wall", "wall2"]
    assert sorted(t.name for t in state.things) == ["lamp", "relay"]


# ---------------------------------------------------------------------------
# History mechanics
# ---------------------------------------------------------------------------

def test_a_mutation_made_without_a_checkpoint_is_lost_on_undo(state):
    """Fio's checkpoint discipline, stated outright.

    ``save_state()`` is called at the *start* of a gesture, so the top of the
    stack is the scene as it stands.  Undo pops that and restores the entry
    below - which is the pre-gesture state only if the gesture checkpointed
    itself first.  A tool that mutates the scene without calling
    ``save_state()`` therefore loses that mutation on the next undo, silently.
    """
    state.save_state()
    state.brushes.append(box_brush("checkpointed", (100, 0, 0)))
    state.brushes.append(box_brush("not_checkpointed", (200, 0, 0)))

    state.undo()

    names = sorted(b.get("name") for b in state.brushes)
    assert names == ["wall"], (
        "both brushes were added after a single checkpoint, so one undo drops "
        "both; the scene holds %s" % (names,))


def test_undo_on_a_fresh_scene_does_nothing(state):
    while state.undo():
        pass
    assert state.undo() is False, (
        "undo kept reporting success with %d checkpoints left"
        % len(state.undo_stack))


def test_redo_with_nothing_to_redo_does_nothing(state):
    assert state.redo() is False


def test_the_undo_stack_is_bounded(state):
    limit = state.undo_stack.maxlen
    assert limit is not None, "the undo stack is unbounded; memory grows for ever"
    for index in range(limit + 20):
        state.save_state()
        state.brushes[0]["pos"][0] = float(index)
    assert len(state.undo_stack) == limit, (
        "the undo stack holds %d entries, its documented cap is %d"
        % (len(state.undo_stack), limit))


def test_a_long_sequence_undoes_and_redoes_back_to_the_same_scene(state):
    def _fingerprint():
        return sorted((b.get("name"), tuple(b["pos"])) for b in state.brushes)

    start = _fingerprint()
    steps = 6
    for index in range(steps):
        state.save_state()
        state.brushes.append(box_brush("extra_%d" % index, (index * 100, 0, 0)))
    end = _fingerprint()

    for _ in range(steps):
        state.undo()
    assert _fingerprint() == start, (
        "%d undos did not return to the starting scene:\n  now %s\n  was %s"
        % (steps, _fingerprint(), start))

    for _ in range(steps):
        state.redo()
    assert _fingerprint() == end, (
        "%d redos did not return to the final scene:\n  now %s\n  was %s"
        % (steps, _fingerprint(), end))


def test_a_checkpoint_never_carries_a_runtime_cache(state):
    """A ConvexGeometry is not JSON-serialisable and must never be checkpointed."""
    brush = _brush(state, "wall")
    bg.get_shape(brush)
    bg.box_to_geometry(brush)
    bg.get_convex(brush)

    snapshot = state.snapshot()

    for key in bg.GEO_RUNTIME_KEYS:
        assert '"%s"' % key not in snapshot, (
            "the runtime key %r was serialised into the undo checkpoint" % key)
