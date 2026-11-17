"""Soak: the placed-decal table across many map lifetimes.

``DecalTable`` outlives every Play session and map, while the ``Decal``
entities it draws belong to one map. 52a1bed made it hold them weakly; this
soak runs hundreds of "maps" through one table -- placing, moving through the
journal (the sparse path), swapping a decal out under an unchanged stamp, and
releasing -- and checks every finished map's decals are collectable, the
entry map tracks only live decals, and per-cycle cost stays flat.
"""

import gc
import weakref

import pytest

from editor.things import Decal
from engine.decal_table import DecalTable
from tests.helpers.soak import Soak, cycles
from tests.helpers.worlds import make_thing

# editor.things imports PyQt5 at module level.
pytestmark = [pytest.mark.qt, pytest.mark.soak, pytest.mark.slow]

TEXTURES = ('grime_01.png', 'scorch_01.png', 'wet_puddle_01.png', 'wet_damp_01.png')


def _decals(generation, count):
    return [make_thing(Decal, 'd%d_%d' % (generation, i),
                       (float(i * 48), 0.0, float(generation % 7) * 32.0),
                       texture=TEXTURES[i % len(TEXTURES)])
            for i in range(count)]


def test_one_table_across_many_maps_retains_no_finished_decals():
    table = DecalTable()
    previous = []
    total = cycles(300)
    with Soak("placed decals across maps", total, warmup=20) as soak:
        for generation in range(total):
            things = _decals(generation, 48)
            epoch = generation * 10
            table.place(things, show_selection=False, epoch=epoch)

            # Sparse path: moves journalled under the same stamp.
            for step in range(4):
                for decal in things[step::8]:
                    decal.pos = [decal.pos[0], decal.pos[1], decal.pos[2] + 4.0]
                table.place(things, show_selection=False, epoch=epoch)

            # A decal swapped out of the same list (same length, same stamp)
            # after it was journalled: the sparse update meets a dead weak
            # reference and must reconcile instead of drawing a stale row.
            gone = things[3]
            gone.pos = [gone.pos[0] + 1.0, gone.pos[1], gone.pos[2]]
            things[3] = make_thing(Decal, 'swap%d' % generation,
                                   (0.0, 0.0, 0.0), texture=TEXTURES[0])
            del gone
            table.place(things, show_selection=False, epoch=epoch)
            assert len(table._entries) == len(things)

            # The previous map is over: nothing of it may still be alive.
            gc.collect()
            leaked = sum(1 for ref in previous if ref() is not None)
            assert leaked == 0, "%d decals of a finished map are still alive" % leaked
            previous = [weakref.ref(d) for d in things]
            if generation % 5 == 4:
                table.release_placed()      # what Stop does
            del things
            soak.cycle_done(counters={'entries': len(table._entries)})

    soak.assert_heap_flat(per_cycle_bytes=256)
    soak.assert_time_flat(factor=2.0, slack_seconds=0.002)
    # The table holds rows for at most one map's decals at a time.
    assert max(v for _, v in soak.counters['entries']) <= 48
