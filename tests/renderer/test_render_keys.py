"""Render keys and the runs they sort into.

The key is what says "this cannot vary within one draw". Getting a field's
width wrong, or its position in the layout, silently merges runs that should
be separate or splits ones that need not be -- neither of which shows up as a
crash, so both are pinned here.
"""

import numpy as np
import pytest

from engine.render_keys import KeyLayout, MAX_KEY_BITS, sort_into_runs


def test_fields_pack_and_read_back():
    layout = KeyLayout([('texture', 20), ('face', 3)])
    keys = layout.pack(texture=np.array([5, 900_000, 0]),
                       face=np.array([7, 0, 3]))
    assert layout.field(keys, 'texture').tolist() == [5, 900_000, 0]
    assert layout.field(keys, 'face').tolist() == [7, 0, 3]


def test_the_first_field_is_the_coarsest_grouping():
    """Most significant first, because that is the order keys sort in."""
    layout = KeyLayout([('texture', 8), ('face', 3)])
    keys = layout.pack(texture=np.array([2, 1, 1]), face=np.array([0, 7, 0]))
    order, _ = sort_into_runs(keys)
    # Both texture-1 entries come before texture 2, whatever their face.
    assert layout.field(keys[order], 'texture').tolist() == [1, 1, 2]


def test_a_value_too_wide_for_its_field_is_refused():
    """Silently colliding with the field above is the bug worth preventing."""
    layout = KeyLayout([('texture', 4), ('face', 3)])
    with pytest.raises(ValueError, match='does not fit'):
        layout.pack(texture=np.array([16]), face=np.array([0]))


def test_a_layout_wider_than_an_int64_is_refused():
    with pytest.raises(ValueError, match='int64'):
        KeyLayout([('a', 40), ('b', 40)])


def test_every_field_must_be_supplied():
    layout = KeyLayout([('texture', 8), ('face', 3)])
    with pytest.raises(ValueError, match='missing key fields'):
        layout.pack(texture=np.array([1]))
    with pytest.raises(ValueError, match='unknown key fields'):
        layout.pack(texture=np.array([1]), face=np.array([0]), fog=np.array([0]))


def test_runs_cover_each_stretch_of_equal_keys():
    keys = np.array([7, 2, 7, 2, 2], dtype=np.int64)
    order, starts = sort_into_runs(keys)
    assert starts.tolist() == [0, 3, 5]
    assert keys[order].tolist() == [2, 2, 2, 7, 7]
    for run in range(len(starts) - 1):
        stretch = keys[order][starts[run]:starts[run + 1]]
        assert len(set(stretch.tolist())) == 1


def test_the_sort_is_stable_so_depth_order_survives_within_a_run():
    """A depth-sorted input must stay depth-sorted inside its own run."""
    keys = np.array([1, 1, 1, 1], dtype=np.int64)
    order, starts = sort_into_runs(keys)
    assert order.tolist() == [0, 1, 2, 3]
    assert starts.tolist() == [0, 4]


def test_runs_can_use_a_secondary_depth_key_without_reordering_runs():
    textures = np.array([2, 1, 2, 1], dtype=np.int32)
    depth_sq = np.array([5.0, 4.0, 3.0, 2.0])
    order, starts = sort_into_runs(textures, secondary=-depth_sq)

    # Texture is the primary key, depth is only the stable order inside it.
    assert textures[order].tolist() == [1, 1, 2, 2]
    assert depth_sq[order].tolist() == [4.0, 2.0, 5.0, 3.0]
    assert starts.tolist() == [0, 2, 4]


def test_secondary_key_must_match_primary_length():
    with pytest.raises(ValueError, match='same length'):
        sort_into_runs(np.array([1, 2]), secondary=np.array([1.0]))


def test_an_empty_input_yields_no_runs():
    order, starts = sort_into_runs(np.empty(0, dtype=np.int64))
    assert len(order) == 0
    assert starts.tolist() == [0]
    assert len(starts) - 1 == 0


def test_a_single_key_is_a_single_run():
    order, starts = sort_into_runs(np.array([4], dtype=np.int64))
    assert starts.tolist() == [0, 1]


def test_the_widest_usable_layout_is_accepted():
    layout = KeyLayout([('a', MAX_KEY_BITS)])
    keys = layout.pack(a=np.array([(1 << MAX_KEY_BITS) - 1]))
    assert keys[0] > 0, "the key reached the sign bit and would sort first"
