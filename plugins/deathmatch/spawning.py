# SPDX-License-Identifier: GPL-2.0-or-later
"""Spawn selection from occupancy and the combined threat of live enemies."""
from .navigation import distance


def choose_spawn(session, fighter):
    """Sample every usable spawn, with less weight near visible enemies.

    Team ownership and collision clearance constrain the pool first. Occupied
    locations are a last resort. Each remaining location receives an inverse
    threat weight, so several nearby opponents matter more than one distant
    opponent and every unoccupied location remains eligible.
    """
    candidates = list(session.spawns)
    if session.game.enabled:
        team_spawns = [spawn for spawn in candidates
                       if session.spawn_teams.get(spawn[0]) == fighter.team]
        candidates = team_spawns or candidates
    if not candidates:
        raise ValueError("A match needs at least one spawn position.")

    clear = [spawn for spawn in candidates if session.clear(spawn[0], spawn[0])]
    candidates = clear or candidates
    participants = [other for other in session.fighters.values()
                    if other is not fighter and other.alive and not other.spectator]
    positions = [(other, session.feet(other)) for other in participants]
    empty = [spawn for spawn in candidates
             if all(distance(spawn[0], pos) >= 80 for _, pos in positions)]
    candidates = empty or candidates

    weights = []
    for pos, _ in candidates:
        threat = 0.0
        eye = (pos[0], pos[1] + 104, pos[2])
        for other, other_pos in positions:
            if session.game.allies(fighter, other):
                continue
            separation = max(80.0, distance(pos, other_pos)) / 256.0
            exposure = 3.0 if session.visible_points(eye, session.eye(other)) else 1.0
            threat += exposure / (separation * separation)
        weights.append(1.0 / (0.05 + threat))
    return session.rng.choices(candidates, weights=weights, k=1)[0]
