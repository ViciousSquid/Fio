"""``engine.spatial``: the one cell convention Fio partitions space with.

Both of Fio's spatial users — the collision ``SpatialGrid`` and Big World's
streaming lifecycle — bucket the world into 512-unit integer columns through
this module.  The point of it existing is that they cannot disagree about which
cell something is in, so these tests pin the convention itself: floor division
(so negative coordinates do not fold toward zero), inclusive on both edges (so
an object spanning a boundary is referenced from every cell it touches), and a
true circle of coverage for radius queries.

Deliberately stdlib-only, like the module: no NumPy, no Qt, no GL.
"""

import math

import pytest

from engine import spatial


CELL = spatial.CELL_SIZE


# ---------------------------------------------------------------------------
# Cell arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("x,z,expected", [
    (0.0, 0.0, (0, 0)),
    (511.9, 511.9, (0, 0)),
    (512.0, 0.0, (1, 0)),
    (-0.1, -0.1, (-1, -1)),
    (-512.0, -512.0, (-1, -1)),
    (-512.1, 0.0, (-2, 0)),
    (1536.0, -1024.0, (3, -2)),
])
def test_cell_of_point_floors_towards_negative_infinity(x, z, expected):
    """Truncation instead of flooring would make cell 0 twice as wide."""
    assert spatial.cell_of_point(x, z) == expected, (
        "point (%.1f, %.1f) landed in cell %s, expected %s"
        % (x, z, spatial.cell_of_point(x, z), expected))


def test_the_cell_boundary_belongs_to_the_higher_cell():
    assert spatial.cell_of_point(CELL, 0.0) == (1, 0)
    assert spatial.cell_of_point(CELL - 1e-9, 0.0) == (0, 0)


def test_cell_bounds_round_trip_through_cell_of_point():
    for cell in [(0, 0), (3, -2), (-7, 11), (-1, -1)]:
        min_x, min_z, max_x, max_z = spatial.cell_bounds(*cell)
        centre = ((min_x + max_x) * 0.5, (min_z + max_z) * 0.5)
        assert spatial.cell_of_point(*centre) == cell, (
            "the centre %s of cell %s maps back to %s"
            % (centre, cell, spatial.cell_of_point(*centre)))
        assert max_x - min_x == pytest.approx(CELL)
        assert max_z - min_z == pytest.approx(CELL)


def test_cells_for_aabb_is_inclusive_on_both_edges():
    """An object spanning a boundary must be referenced from every cell."""
    cells = spatial.cells_for_aabb(-10.0, -10.0, 10.0, 10.0)
    assert set(cells) == {(-1, -1), (-1, 0), (0, -1), (0, 0)}, (
        "a box straddling the origin touches four cells, got %s" % (sorted(cells),))


def test_cells_for_aabb_of_a_box_inside_one_cell_is_that_cell():
    assert spatial.cells_for_aabb(10.0, 10.0, 20.0, 20.0) == [(0, 0)]


def test_cells_for_aabb_covers_a_wide_span_without_gaps():
    cells = spatial.cells_for_aabb(-1500.0, 100.0, 1500.0, 200.0)
    xs = sorted({cx for cx, _ in cells})
    assert xs == [-3, -2, -1, 0, 1, 2], \
        "a 3000-unit span should cover cells -3..2, got %s" % (xs,)
    assert all(cz == 0 for _, cz in cells)


def test_cell_distance_sq_is_zero_inside_and_grows_outside():
    assert spatial.cell_distance_sq(0, 0, 256.0, 256.0) == 0.0
    # 100 units left of cell 0's left edge.
    assert spatial.cell_distance_sq(0, 0, -100.0, 256.0) == pytest.approx(100.0 ** 2)
    # Diagonally off the corner.
    assert spatial.cell_distance_sq(0, 0, -30.0, -40.0) == pytest.approx(30 ** 2 + 40 ** 2)


# ---------------------------------------------------------------------------
# CellIndex
# ---------------------------------------------------------------------------

def test_insert_files_an_object_under_every_cell_it_touches():
    index = spatial.CellIndex()
    obj = {"name": "wide"}
    index.insert(obj, -10.0, -10.0, 10.0, 10.0)
    touched = {coord for coord, bucket in index.cells.items() if obj in bucket}
    assert touched == {(-1, -1), (-1, 0), (0, -1), (0, 0)}, (
        "object filed under %s, expected the four cells it spans" % (sorted(touched),))


def test_the_index_stores_references_not_copies():
    """Identity is what makes the grid a view of the world, not a second copy."""
    index = spatial.CellIndex()
    obj = {"name": "a", "pos": [0, 0, 0]}
    index.insert_point(obj, 0.0, 0.0)
    stored = index.cell((0, 0))[0]
    assert stored is obj, "the index copied the object instead of referencing it"
    obj["pos"] = [1, 2, 3]
    assert stored["pos"] == [1, 2, 3]


def test_inserting_the_same_object_twice_files_it_twice():
    """The index does not deduplicate; its callers must.

    Pinned because the collision queries above it rely on knowing this - they
    de-duplicate by ``id`` when a query spans more than one cell.
    """
    index = spatial.CellIndex()
    obj = {"name": "a"}
    index.insert_point(obj, 0.0, 0.0)
    index.insert_point(obj, 0.0, 0.0)
    assert len(index.cell((0, 0))) == 2


def test_an_empty_cell_reads_as_an_empty_sequence():
    index = spatial.CellIndex()
    assert index.cell((99, 99)) == ()
    assert list(index.cell((99, 99))) == []


def test_clear_empties_every_bucket():
    index = spatial.CellIndex()
    index.insert_point({"name": "a"}, 0.0, 0.0)
    index.insert_point({"name": "b"}, 5000.0, -5000.0)
    index.clear()
    assert index.cells == {}


def test_cells_within_is_a_circle_not_a_square():
    """A cell counts the moment any part of it is inside the radius."""
    index = spatial.CellIndex()
    # One object per cell across a 3x3 block of cells around the origin.
    for cx in (-1, 0, 1):
        for cz in (-1, 0, 1):
            index.insert_point({"cell": (cx, cz)},
                               cx * CELL + 10.0, cz * CELL + 10.0)

    # A radius that reaches the axis-adjacent cells but not the diagonal
    # corners: the nearest point of cell (1, 1) is at (512, 512), which is
    # 724 units away, while (512, 0) is only 512.
    reached = index.cells_within(0.0, 0.0, 600.0)
    assert (1, 0) in reached and (0, 1) in reached, \
        "axis-adjacent cells within 600 units were missed: %s" % (sorted(reached),)
    assert (1, 1) not in reached, (
        "cell (1,1) is %.0f units away at its nearest corner but was included "
        "by a 600-unit query - the coverage is a square, not a circle"
        % math.hypot(CELL, CELL))


def test_cells_within_skips_cells_the_index_does_not_have():
    """Cost is proportional to occupied local cells, not to world size."""
    index = spatial.CellIndex()
    index.insert_point({"name": "lonely"}, 0.0, 0.0)
    assert index.cells_within(0.0, 0.0, 100000.0) == {(0, 0)}


def test_cells_within_of_an_empty_index_is_empty():
    assert spatial.CellIndex().cells_within(0.0, 0.0, 5000.0) == set()


def test_negative_coordinates_are_indexed_symmetrically():
    index = spatial.CellIndex()
    near = {"name": "near"}
    far = {"name": "far"}
    index.insert_point(near, -100.0, -100.0)
    index.insert_point(far, -100000.0, -100000.0)
    assert index.cell((-1, -1)) == [near]
    assert index.cell(spatial.cell_of_point(-100000.0, -100000.0)) == [far]


def test_a_custom_cell_size_is_honoured_everywhere():
    index = spatial.CellIndex(cell_size=64.0)
    index.insert({"name": "a"}, 0.0, 0.0, 200.0, 0.0)
    assert sorted(index.cells) == [(0, 0), (1, 0), (2, 0), (3, 0)], (
        "a 200-unit span in 64-unit cells should touch 4 cells, got %s"
        % (sorted(index.cells),))


# ---------------------------------------------------------------------------
# Parked ("streamed out") objects
# ---------------------------------------------------------------------------

def test_authored_hidden_reads_the_flag_for_an_untouched_object():
    assert spatial.authored_hidden({"hidden": True}) is True
    assert spatial.authored_hidden({"hidden": False}) is False
    assert spatial.authored_hidden({}) is False


def test_authored_hidden_sees_through_streaming():
    """Parking reuses ``hidden``; durable structures must not be fooled.

    The collision grid is built once and outlives a cell's activation state.
    If it confused "the mapper hid this" with "it is out of range this second",
    a parked brush would drop out of collision permanently.
    """
    parked_visible = {"hidden": True, spatial.PARKED_HIDDEN_KEY: False}
    parked_hidden = {"hidden": True, spatial.PARKED_HIDDEN_KEY: True}
    assert spatial.authored_hidden(parked_visible) is False, (
        "a brush parked by the streaming layer reads as authored-hidden; it "
        "would be dropped from collision for good")
    assert spatial.authored_hidden(parked_hidden) is True, (
        "a brush the mapper hid must stay hidden even while parked")


def test_the_authored_accessors_take_an_entity_as_well_as_a_brush():
    """Both halves of Fio's world, one question.

    Parking sets ``hidden`` on brushes and ``hidden`` + ``disabled`` on
    entities, so "what is this object authored as?" has to be answerable for an
    entity too.  ``tier_of`` next door already takes either shape; an accessor
    that raised ``TypeError`` on the entity half would push every caller into
    reaching past it and into ``properties`` by hand — which is how the two
    meanings of ``hidden`` get confused again.
    """
    class _Thing:
        def __init__(self, **props):
            self.properties = dict(props)

    assert spatial.authored_hidden(_Thing(hidden=True)) is True
    assert spatial.authored_disabled(_Thing(disabled=True)) is True
    parked = _Thing(hidden=True, disabled=True,
                    **{spatial.PARKED_HIDDEN_KEY: False,
                       spatial.PARKED_DISABLED_KEY: True})
    assert spatial.authored_hidden(parked) is False, (
        "a parked entity read as authored-hidden")
    assert spatial.authored_disabled(parked) is True, (
        "an entity the map disabled stopped reading as disabled while parked")
    # Anything without a property dict answers the default rather than raising.
    assert spatial.authored_hidden(object()) is False


def test_setting_an_authored_flag_on_a_parked_object_updates_the_stash():
    """The write half of ``authored_hidden``, and why it has to exist.

    Unparking restores the live flag *from* the stash, so a write that lands on
    the live flag of a parked object is discarded the instant its cell comes
    back.  Writing through the accessor puts the value where unparking will
    read it.
    """
    parked = {"hidden": True, spatial.PARKED_HIDDEN_KEY: False}
    spatial.set_authored_flag(parked, "hidden", True)
    assert parked[spatial.PARKED_HIDDEN_KEY] is True, (
        "the authored value went to the live flag, which parking owns; it "
        "would be thrown away on the next unpark")
    assert parked["hidden"] is True, "parking's own flag must not be disturbed"

    # Simulate the unpark the streaming layer performs.
    parked["hidden"] = parked.pop(spatial.PARKED_HIDDEN_KEY)
    assert spatial.authored_hidden(parked) is True


def test_setting_an_authored_flag_on_an_unparked_object_writes_the_flag():
    plain = {}
    spatial.set_authored_flag(plain, "hidden", True)
    assert plain == {"hidden": True}
    spatial.set_authored_flag(plain, "hidden", False)
    assert plain["hidden"] is False
    assert spatial.PARKED_HIDDEN_KEY not in plain, (
        "writing an authored value invented a parking marker on an object no "
        "streaming layer has touched")


def test_the_reader_and_the_writer_agree_on_every_parkable_flag():
    """Whatever is written through the pair must read back through it.

    The two functions share one table of parked flags precisely so a third
    parkable flag cannot be taught to one of them and not the other.
    """
    for flag in spatial._PARKED_FLAGS:
        for parked in (False, True):
            for value in (False, True):
                obj = {}
                if parked:
                    obj[flag] = True
                    obj[spatial._PARKED_FLAGS[flag]] = not value
                spatial.set_authored_flag(obj, flag, value)
                assert spatial.authored_flag(obj, flag) is value, (
                    "%s: wrote %r (parked=%s) and read back %r"
                    % (flag, value, parked, spatial.authored_flag(obj, flag)))
