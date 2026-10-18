"""Crossfire fixtures and the CANARY sorted-query baseline (69f6fc8).

Keep the reference independent of the optimized kernels: it checks cache
semantics as well as the returned target and provides repeatable old timings.
"""
from types import SimpleNamespace

import numpy as np

from engine.monster_table import MonsterTable


def make_table(positions, **properties):
    monsters = [SimpleNamespace(pos=list(p), properties=dict(properties))
                for p in positions]
    table = MonsterTable()
    table.gather(monsters)
    return table


def sorted_crossfire(self, shooter, ray_start, ray_end, radius=80.0):
    """The first monster a shot from *shooter* at *ray_end* passes through.

    The per-monster walk this replaces, as one expression over the rows:
    same sphere (radius 80, 64 above the monster's origin), same
    exclusions (the shooter, dead or hidden monsters, the shooter's team)
    and the same answer -- the nearest entry point, the earliest row on a
    tie.

    Who is alive is read once per tick, not once per shot: when shooters'
    timers line up, hundreds of shots land in one tick, and reading every
    monster's flags for every shot was 300 ms of a single tick at 1000
    monsters. A shot earlier in the tick can still kill a monster, so the
    monster a shot would hit is re-read before it is returned, and one
    that has died since is struck off for the rest of the tick. (A monster
    that I/O *un*-hides mid-tick joins the crossfire test on the next
    tick.)
    """
    n = self.count
    start = np.asarray(ray_start, dtype=np.float64)
    direction = np.asarray(ray_end, dtype=np.float64) - start
    length = float(np.sqrt(direction @ direction))
    if length < 1.0 or not n:
        return None
    direction /= length
    props = self.props
    live = self._live
    if live is None:
        live = self._live = np.fromiter(
            (not (p.get('dead', False) or p.get('hidden', False)) for p in props),
            dtype=bool, count=n)
    candidates = live.copy()
    candidates[shooter] = False
    shooter_team = self.team[shooter]
    if shooter_team >= 0:
        candidates &= self.team[:n] != shooter_team
    rows = np.flatnonzero(candidates)
    if not len(rows):
        return None
    centre = self.pos[rows].copy()
    centre[:, 1] += 64.0
    oc = start - centre
    b = 2.0 * (oc @ direction)
    c = np.einsum('ij,ij->i', oc, oc) - radius * radius
    disc = b * b - 4.0 * c
    t = np.full(len(rows), np.inf)
    hit = disc >= 0.0
    t[hit] = (-b[hit] - np.sqrt(disc[hit])) / 2.0
    entered = np.flatnonzero((t > 0.0) & (t < length))
    # Nearest entry first, earliest row on a tie; almost always the first
    # candidate is still alive and the loop runs once.
    for i in entered[np.lexsort((rows[entered], t[entered]))]:
        row = int(rows[i])
        p = props[row]
        if p.get('dead', False) or p.get('hidden', False):
            live[row] = False
            continue
        return self.monsters[row]
    return None
