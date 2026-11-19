"""Bookkeeping the physics contract asks for and a solver does not keep.

:class:`Body` ``resting_on``, waking what rests on a body that moves, and
:meth:`Physics.entities_that_changed_cell` are the contract's. Soma keeps
them in its own arrays; an implementation over a general solver (Bullet's
contact manifolds, say) feeds these instead:

* :class:`SupportGraph` -- each step, the contacts in which one body is held
  up by another; answers who rests on whom, and everything resting
  (directly or not) on a body that is about to move.
* :class:`CellTracker` -- each step, the bodies' positions; answers which
  crossed a grid column since the last time it was asked.

Stdlib only, as the contract is. Entities are held by identity.
"""

from __future__ import annotations

import math


class SupportGraph:
    """Who rests on whom, rebuilt from one step's contacts.

    Call :meth:`begin` at the start of a step, :meth:`support` for each
    contact in which *lower* holds *upper* up (its contact normal points up
    from lower to upper), then ask. Something resting on several bodies
    rests on the first reported.
    """

    def __init__(self):
        self._under = {}            # id(upper) -> lower
        self._riders = {}           # id(lower) -> [upper, ...]

    def begin(self):
        """A new step: forget last step's contacts."""
        self._under.clear()
        self._riders.clear()

    def support(self, upper, lower):
        """*lower* holds *upper* up this step."""
        if upper is lower or id(upper) in self._under:
            return
        self._under[id(upper)] = lower
        self._riders.setdefault(id(lower), []).append(upper)

    def resting_on(self, entity):
        """The entity whose body *entity*'s stands on, or None."""
        return self._under.get(id(entity))

    def riders(self, entity):
        """Everything resting on *entity*, directly or on something that
        does: what to wake when *entity* moves. Nearest first."""
        found, seen, queue = [], {id(entity)}, [entity]
        while queue:
            for upper in self._riders.get(id(queue.pop(0)), ()):
                if id(upper) not in seen:
                    seen.add(id(upper))
                    found.append(upper)
                    queue.append(upper)
        return found

    def forget(self, entity):
        """*entity*'s body is gone: it holds nothing up, and rests on nothing."""
        lower = self._under.pop(id(entity), None)
        if lower is not None:
            riders = self._riders.get(id(lower), [])
            self._riders[id(lower)] = [e for e in riders if e is not entity]
        for upper in self._riders.pop(id(entity), ()):
            self._under.pop(id(upper), None)


class CellTracker:
    """Which bodies crossed a grid column (``WORLD_QUERIES['cell_size']``).

    Call :meth:`place` with each body's position after a step; drain
    :meth:`changed` for :meth:`Physics.entities_that_changed_cell`.
    """

    def __init__(self, cell_size):
        self.cell_size = float(cell_size)
        self._cells = {}            # id(entity) -> (entity, (cx, cz))
        self._changed = []

    def _cell(self, pos):
        size = self.cell_size
        return (math.floor(float(pos[0]) / size), math.floor(float(pos[2]) / size))

    def place(self, entity, pos):
        """*entity*'s body is at *pos*; True if that is a new column. A body
        placed for the first time is not a change."""
        cell = self._cell(pos)
        held = self._cells.get(id(entity))
        self._cells[id(entity)] = (entity, cell)
        if held is not None and held[1] != cell:
            self._changed.append(entity)
            return True
        return False

    def changed(self):
        """The entities that changed column since the last call."""
        changed, self._changed = self._changed, []
        return changed

    def forget(self, entity):
        """*entity*'s body is gone."""
        self._cells.pop(id(entity), None)
        self._changed = [e for e in self._changed if e is not entity]
