"""A list that says when its rows change (engine/row_list.py, M4)."""

import copy
import pickle

import pytest

from engine.row_list import RowList, row_list


@pytest.mark.parametrize("change", [
    lambda rows: rows.append(9),
    lambda rows: rows.extend([9]),
    lambda rows: rows.insert(0, 9),
    lambda rows: rows.remove(2),
    lambda rows: rows.pop(),
    lambda rows: rows.pop(0),
    lambda rows: rows.clear(),
    lambda rows: rows.sort(reverse=True),
    lambda rows: rows.reverse(),
    lambda rows: rows.__setitem__(1, 9),
    lambda rows: rows.__setitem__(slice(0, 2), [7, 8, 9]),
    lambda rows: rows.__delitem__(0),
    lambda rows: rows.__delitem__(slice(0, 2)),
    lambda rows: rows.__iadd__([9]),
    lambda rows: rows.__imul__(2),
])
def test_every_change_to_the_rows_moves_the_version(change):
    rows = RowList([1, 2, 3])
    before = rows.row_version
    change(rows)
    assert rows.row_version != before


def test_reading_does_not_move_it_and_versions_are_never_reused():
    rows = RowList([1, 2, 3])
    before = rows.row_version
    assert list(rows) == [1, 2, 3] and rows[1] == 2 and len(rows) == 3
    assert rows.row_version == before
    other = RowList([1, 2, 3])
    assert other.row_version != before
    assert row_list(rows) is rows and isinstance(row_list([1]), RowList)

    class Other(list):
        pass
    other_type = Other([1])
    assert row_list(other_type) is other_type        # left as it is


def test_copies_and_pickles_stay_row_lists():
    rows = RowList([{'a': 1}])
    for twin in (copy.copy(rows), copy.deepcopy(rows), pickle.loads(pickle.dumps(rows))):
        assert isinstance(twin, RowList) and twin == rows


def test_editor_state_holds_its_rows_as_row_lists():
    pytest.importorskip("PyQt5")
    from editor.editor_state import EditorState
    state = EditorState()
    state.brushes = [{'id': 'a'}]
    assert isinstance(state.brushes, RowList)
    before = state.brushes.row_version
    state.brushes.append({'id': 'b'})
    assert state.brushes.row_version != before
    state.things = state.things + []
    assert isinstance(state.things, RowList)


def test_a_table_recognises_an_unchanged_row_list_without_comparing_rows():
    from engine import change_detection
    from engine.render_table import RenderTable
    rows = RowList([{'id': 'b%d' % i, 'pos': [i * 80.0, 0.0, 0.0],
                     'size': [64.0, 64.0, 64.0]} for i in range(50)])
    table = RenderTable()
    table.begin_frame(rows, 1)
    change_detection.DETECT.reset()
    table.begin_frame(rows, 1)
    assert change_detection.DETECT.sites['render_table.rows'][0] == 0
    rows.append({'id': 'new', 'pos': [0.0, 0.0, 0.0], 'size': [8.0, 8.0, 8.0]})
    table.begin_frame(rows, 1)
    assert table.count == 51
    rows[3] = {'id': 'swapped', 'pos': [1.0, 0.0, 0.0], 'size': [8.0, 8.0, 8.0]}
    table.begin_frame(rows, 1)
    assert table.brushes[3]['id'] == 'swapped'
    plain = list(rows)
    table.begin_frame(plain, 1)                 # a plain list: compared
    assert table.count == 51
