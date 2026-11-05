"""Spawn safety, variety and threat preference without a running renderer."""
import random
from types import SimpleNamespace

from plugins.deathmatch.spawning import choose_spawn


def match(spawns, *, seed=42, participants=(), team_mode=False, teams=None, blocked=()):
    fighter = SimpleNamespace(team="red", alive=False, spectator=False)
    game = SimpleNamespace(enabled=team_mode,
                           allies=lambda a, b: team_mode and a.team == b.team)
    session = SimpleNamespace(
        spawns=[(pos, 0.0) for pos in spawns], spawn_teams=teams or {}, game=game,
        fighters=dict(enumerate([fighter, *participants])), rng=random.Random(seed),
        clear=lambda a, b: a not in blocked, feet=lambda f: f.pos,
        eye=lambda f: (f.pos[0], f.pos[1] + 104, f.pos[2]),
        visible_points=lambda a, b: True)
    return session, fighter


def test_every_clear_empty_spawn_remains_eligible():
    positions = [(i * 100, 0, 0) for i in range(70)]
    session, fighter = match(positions)
    seen = {choose_spawn(session, fighter)[0] for _ in range(2000)}
    assert seen == set(positions)


def test_team_clearance_and_occupancy_constrain_selection():
    ally = SimpleNamespace(pos=(0, 0, 0), team="red", alive=True, spectator=False)
    positions = [(0, 0, 0), (200, 0, 0), (400, 0, 0), (600, 0, 0)]
    teams = dict(zip(positions, ["red", "red", "red", "blue"]))
    session, fighter = match(positions, participants=[ally], team_mode=True,
                             teams=teams, blocked=[positions[1]])
    assert {choose_spawn(session, fighter)[0] for _ in range(50)} == {positions[2]}


def test_spectators_do_not_occupy_spawns_and_exhausted_pool_has_a_fallback():
    spectator = SimpleNamespace(pos=(0, 0, 0), team="blue", alive=True, spectator=True)
    session, fighter = match([(0, 0, 0)], participants=[spectator], blocked=[(0, 0, 0)])
    assert choose_spawn(session, fighter)[0] == (0, 0, 0)


def test_cover_increases_selection_frequency_at_equal_enemy_distance():
    enemy = SimpleNamespace(pos=(0, 0, 0), team="blue", alive=True, spectator=False)
    session, fighter = match([(-400, 0, 0), (400, 0, 0)], participants=[enemy])
    session.visible_points = lambda a, b: a[0] > 0
    covered = sum(choose_spawn(session, fighter)[0][0] < 0 for _ in range(1000))
    assert covered > 650


def test_seed_reproduces_choices_without_forcing_one_location():
    a, fa = match([(0, 0, 0), (200, 0, 0), (400, 0, 0)])
    b, fb = match([(0, 0, 0), (200, 0, 0), (400, 0, 0)])
    choices = [choose_spawn(a, fa) for _ in range(30)]
    assert choices == [choose_spawn(b, fb) for _ in range(30)]
    assert len(set(choices)) == 3
