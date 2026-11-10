"""What establishing "what changed" costs, site by site (M4).

A frame in which nothing changed should cost almost nothing to recognise as
such, whatever the size of the world: that is M4's invariant. To hold it to
that, every place that works out what changed -- a table comparing its row
set, the kept matrices comparing columns, a pass comparing its visible rows,
an instance stream comparing its items -- reports here how many rows it
examined and how long it took. Counted rows are deterministic (tests assert
them); the time is for the benchmarks, which report it apart from the
frame's rendering work.

Rendering work -- culling a moved camera's view, gathering and uploading
changed rows, submitting draws -- is not counted: it grows with what is
visible, as it should. Neither are the rows a change actually touches:
those are the change's own cost.

``ORACLE`` (set ``FIO_RENDER_ORACLE=1``, or set it in a test) makes every
site that trusts a change record also do the full comparison the record
replaced, and raise :class:`MissedChange` when the record missed something.
"""

import os
import time

now = time.perf_counter_ns


class MissedChange(AssertionError):
    """A change no record reported: a writer that did not mark what it
    wrote. Raised only in oracle mode."""


class ChangeDetection:
    """Rows examined and nanoseconds spent working out what changed, in total
    and per site, since the last :meth:`reset`."""

    __slots__ = ('rows', 'ns', 'calls', 'sites')

    def __init__(self):
        self.reset()

    def reset(self):
        self.rows = 0
        self.ns = 0
        self.calls = 0
        #: site -> [rows, ns, calls]
        self.sites = {}

    def add(self, site, rows, ns):
        self.rows += rows
        self.ns += ns
        self.calls += 1
        entry = self.sites.get(site)
        if entry is None:
            self.sites[site] = [rows, ns, 1]
        else:
            entry[0] += rows
            entry[1] += ns
            entry[2] += 1


#: The process's counters. Diagnostic: the logic and render threads both
#: add to them, and a benchmark or test reads them between frames.
DETECT = ChangeDetection()

#: Verify every change record against a full comparison (tests, debugging).
ORACLE = bool(os.environ.get('FIO_RENDER_ORACLE'))

#: What a miss does: raise :class:`MissedChange` where it is found, or --
#: ``FIO_RENDER_ORACLE=record`` -- append the message to :data:`MISSES` and
#: carry on. Raising in the middle of a GL frame (inside ``paintGL``, with
#: the HUD's painter active) can take the process down; the test suite
#: records instead and fails the test that left a miss behind (conftest.py).
RAISE = os.environ.get('FIO_RENDER_ORACLE') != 'record'
MISSES = []


def missed(message):
    """Report a change no record reported (oracle mode only)."""
    if RAISE:
        raise MissedChange(message)
    MISSES.append(message)
