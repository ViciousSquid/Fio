"""Dense movement preserves object values and immutable publication snapshots."""

import numpy as np
import pytest

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('dtype', [np.float32, np.float64, np.int32, np.uint64])
@pytest.mark.parametrize('count', [1, 8, 256])
def test_native_conversion_matches_scalar_values_and_journal_bytes(dtype, count):
    from editor.things import Thing
    from engine.change_journal import JOURNAL, MOVED, set_positions
    from engine.entity_table import EntityTable
    from tests.helpers.worlds import make_thing

    positions = np.arange(count * 6, dtype=dtype).reshape(count * 2, 3)[::2]
    expected = [[float(x), float(y), float(z)] for x, y, z in positions]
    objects = [make_thing(Thing, f'moving-{i}') for i in range(count)]
    table = EntityTable()
    table.begin_frame(objects, epoch=1)
    set_positions(objects, positions)
    assert all(type(obj.pos) is list for obj in objects)
    assert all(type(value) is float for obj in objects for value in obj.pos)
    assert np.asarray([obj.pos for obj in objects]).tobytes() == np.asarray(expected).tobytes()
    positions.fill(999)  # Reusing the caller's numerical scratch must be safe.
    changes = JOURNAL.drain(table)
    snapshot = JOURNAL.drain_positions(table)
    assert all(changes[id(obj)] & MOVED for obj in objects)
    assert all(type(snapshot[id(obj)]) is tuple for obj in objects)
    assert np.asarray([snapshot[id(obj)] for obj in objects]).tobytes() == np.asarray(expected).tobytes()
