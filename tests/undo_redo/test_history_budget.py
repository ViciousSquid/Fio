"""The undo history is bounded by bytes, and holds each paint blob once (audit M2).

Each undo entry is the whole scene as JSON, and was capped only by count (50):
5.5 MB a checkpoint on a 20k-brush map, about 270 MB in all. Terrain paint
made it worse: every compressed paint chunk was encoded into every entry, so
50 entries held 50 copies of the same blobs.

Entries now hold paint chunks by reference -- one string per distinct blob,
shared by every entry -- and the oldest steps go once the history passes
``UNDO_BYTE_BUDGET``, keeping ``UNDO_MIN_STEPS``. Undo and redo put back
exactly what they put back before.
"""

import base64
import json
import os

import pytest

from editor.editor_state import EditorState

pytestmark = pytest.mark.qt

CHUNKS = 16
BLOB = 24 * 1024


def _blob(seed):
    return base64.b64encode(os.urandom(BLOB * 3 // 4) + bytes([seed % 256])).decode()


def _painted_state():
    state = EditorState()
    state.brushes.append({'id': 'wall', 'name': 'wall', 'pos': [0.0, 0.0, 0.0],
                          'size': [64.0, 64.0, 64.0]})
    state.terrain_data = {
        'seed': 1,
        'texture_paint': [{'coord': [i, 0], 'data': _blob(i)} for i in range(CHUNKS)],
        'texture_paint_base': [{'coord': [0, 0], 'data': _blob(99)}],
    }
    return state


def _distinct_blobs(state):
    held = {}
    for entry in list(state.undo_stack) + list(state.redo_stack):
        for blob in getattr(entry, 'blobs', ()):
            held[id(blob)] = blob
    return held


def test_fifty_checkpoints_hold_each_paint_blob_once():
    state = _painted_state()
    paint_bytes = sum(len(e['data']) for e in state.terrain_data['texture_paint'])
    for step in range(50):
        state.save_state()
        state.brushes[0]['pos'][0] = float(step)
    assert len(_distinct_blobs(state)) == CHUNKS + 1
    # Encoded into every entry, the paint alone was 50 copies.
    assert state.history_bytes < 2 * paint_bytes
    naive = sum(len(state.snapshot()) for _ in range(len(state.undo_stack)))
    assert state.history_bytes * 10 < naive


def test_a_re_encoded_chunk_is_matched_by_content():
    state = _painted_state()
    state.save_state()
    # A restore rebuilds the terrain's paint: same bytes, new strings.
    for entry in state.terrain_data['texture_paint']:
        entry['data'] = ''.join(list(entry['data']))
    state.save_state()
    assert len(_distinct_blobs(state)) == CHUNKS + 1
    # One repainted chunk is the one new blob.
    state.terrain_data['texture_paint'][3]['data'] = _blob(1003)
    state.save_state()
    assert len(_distinct_blobs(state)) == CHUNKS + 2


def test_undo_and_redo_restore_exactly_what_they_did():
    state = _painted_state()
    seen = [state.snapshot()]
    for step in range(6):
        state.save_state()
        state.brushes[0]['pos'][0] = float(step + 1)
        state.terrain_data['texture_paint'][step]['data'] = _blob(500 + step)
        seen.append(state.snapshot())
    for expected in reversed(seen[:-1]):
        assert state.undo()
        assert state.snapshot() == expected
    for expected in seen[1:]:
        assert state.redo()
        assert state.snapshot() == expected
    # The paint itself is back as data, not as references.
    assert all(isinstance(e['data'], str) and len(e['data']) > 1000
               for e in state.terrain_data['texture_paint'])


def test_the_oldest_steps_go_once_the_history_passes_its_budget():
    state = _painted_state()
    state.save_state()
    entry = len(state.undo_stack[-1])
    blobs = sum(len(b) for b in _distinct_blobs(state).values())
    state.UNDO_BYTE_BUDGET = blobs + 12 * entry
    state.UNDO_MIN_STEPS = 3
    for step in range(40):
        state.save_state()
        state.brushes[0]['pos'][0] = float(step)
    assert len(state.undo_stack) < 15
    assert state.history_bytes <= state.UNDO_BYTE_BUDGET
    # However tight the budget, a few steps are kept.
    state.UNDO_BYTE_BUDGET = 1
    state.save_state()
    assert len(state.undo_stack) == state.UNDO_MIN_STEPS + 1
    for _ in range(state.UNDO_MIN_STEPS):
        assert state.undo()
    assert not state.undo()


def test_a_history_entry_is_still_json_of_the_scene():
    state = _painted_state()
    state.save_state()
    checkpoint = json.loads(state.undo_stack[-1])
    assert checkpoint['brushes'][0]['name'] == 'wall'
    assert all('data' not in e for e in checkpoint['terrain_texture_paint'])
