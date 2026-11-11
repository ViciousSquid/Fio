"""Blood: projected decals thrown when an actor is shot.

From the actor's centre, away from the shooter, onto the world behind and
under it; a little for the pistol, a lot for the shotgun; none with gore
off, more at the higher levels. The renderer is handed two arrays (boxes and
their parameters) and paints each box onto whatever lies inside it -- a
wall, the floor under it and the corner between."""

import math
import time

import numpy as np
import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

MAP = "maps/Simple_Map_Test_Extended.json"


def _human(session):
    from editor.things import Monster
    monster = next(t for t in session.logic.editor_state.things
                   if isinstance(t, Monster) and t.properties.get('monster_type') == 'human')
    monster.properties['health'] = 100000
    return monster


def _shoot(session, weapon_id, level, shots=1):
    """Shoot the human monster from 300 units, a wall behind it; returns
    (combat runtime, the monster's centre, the shot's direction)."""
    import glm
    session.start_play()
    session.step(2)
    combat = session.logic.combat_runtime
    combat.set_gore_level(level)
    monster = _human(session)
    height = float(monster.properties.get('sprite_height', 128.0))
    centre = glm.vec3(monster.pos[0], monster.pos[1] + height * 0.45, monster.pos[2])
    rows = session.logic.collision_runtime.collision_rows()
    best = None
    for k in range(16):
        a = k * math.pi / 8
        d = glm.vec3(math.sin(a), 0.0, math.cos(a))
        behind, _ = combat._trace_brushes(rows, centre, d, 2000.0)
        front, _ = combat._trace_brushes(rows, centre, -d, 2000.0)
        if front > 330.0 and (best is None or behind < best[0]):
            best = (behind, d)
    direction = best[1]
    origin = centre - direction * 300.0
    weapon = combat.items.resolve(weapon_id).weapon
    for _ in range(shots):
        assert combat._fire_rays(weapon, origin, [direction]) == [True]
    return combat, centre, direction


def _centres(combat):
    frame = combat.blood.frame(time.perf_counter(), combat.BLOOD_LIFETIME, combat.BLOOD_FADE_TIME)
    if frame is None:
        return np.zeros((0, 3)), np.zeros((0, 4)), np.zeros((0, 16))
    models, params = frame
    return models[:, 12:15].astype(np.float64), params, models


def test_blood_lands_behind_the_actor_away_from_the_shooter(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, centre, direction = _shoot(session, "gun2", level=1)
    points, params, _ = _centres(combat)
    assert len(points) == combat.BLOOD_HEAVY[0]
    away = (points - np.array([centre.x, centre.y, centre.z])) @ np.array(
        [direction.x, direction.y, direction.z])
    assert np.all(away > 0.0), "blood was thrown back towards the shooter"
    assert np.all(params[:, 1] == 1.0)


def test_the_pistol_throws_little_and_the_shotgun_a_lot(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, "gun1", level=1)
    light = _centres(combat)[2]
    session.stop_play()
    combat, _, _ = _shoot(session, "gun2", level=1)
    heavy = _centres(combat)[2]
    width = lambda models: np.linalg.norm(models[:, 0:3], axis=1)
    assert len(light) < len(heavy)
    assert width(light).max() < width(heavy).min() + 1e-6 or width(light).mean() < width(heavy).mean()
    assert width(light).max() <= combat.BLOOD_LIGHT[1][1] + 1e-3
    assert width(heavy).max() <= combat.BLOOD_HEAVY[1][1] + 1e-3


def test_gore_off_throws_and_traces_nothing(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, "gun2", level=0, shots=3)
    assert combat.blood.count == 0
    with session.render_state() as frame:
        assert frame.decals is None


def test_the_levels_escalate_and_turning_gore_off_clears_it(fio_session):
    session = fio_session(MAP, size=(200, 160))
    counts = []
    for level in (1, 2, 3):
        combat, _, _ = _shoot(session, "gun2", level=level)
        counts.append(len(_centres(combat)[0]))
        session.stop_play()
    assert counts == sorted(counts) and counts[0] < counts[1] < counts[2]
    combat, _, _ = _shoot(session, "gun2", level=3)
    combat.set_gore_level(0)
    assert len(_centres(combat)[0]) == 0


def test_decals_vary_in_size_shape_and_kind(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, "gun2", level=3, shots=3)
    _, params, models = _centres(combat)
    widths = np.linalg.norm(models[:, 0:3], axis=1)
    heights = np.linalg.norm(models[:, 4:7], axis=1)
    assert len(set(np.round(widths, 2))) > 10
    proportions = heights / widths
    assert proportions.min() >= 0.69 and proportions.max() <= 1.41
    assert proportions.max() - proportions.min() > 0.3
    assert set(params[:, 3].tolist()) == {0.0, 1.0}             # some mirrored
    assert len(set(params[:, 0].tolist())) == len(params)       # each its own image pick


def test_a_new_session_starts_clean(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, "gun2", level=2)
    assert len(_centres(combat)[0])
    session.stop_play()
    session.start_play()
    assert len(_centres(session.logic.combat_runtime)[0]) == 0


def test_one_decal_paints_the_wall_the_floor_and_the_corner_between(fio_session):
    import glm
    from engine.decal_table import WALL
    session = fio_session("maps/Office_Corridor.json", size=(320, 240))
    view = session.view
    if view.renderer is None or not view.renderer.ready:
        pytest.skip("no OpenGL context")
    view.sysmon.set_active(False)
    session.start_play()
    session.step(3)
    logic = session.logic
    combat, player = logic.combat_runtime, logic.player_runtime.player
    rows = logic.collision_runtime.collision_rows()
    origin, forward = combat._aim()
    left = glm.vec3(forward.z, 0.0, -forward.x)
    ahead = glm.vec3(player.pos.x, origin.y, player.pos.z) + forward * 420.0
    _, wall = combat._trace_brushes(rows, ahead, left, 2000.0)
    _, floor = combat._trace_brushes(rows, ahead, glm.vec3(0, -1, 0), 2000.0)
    eye = glm.vec3(wall.x, 0, wall.z) - left * 230.0 - forward * 150.0
    player.pos.x, player.pos.z = eye.x, eye.z
    to = glm.normalize(glm.vec3(wall.x - eye.x, 0.0, wall.z - eye.z))
    player.angle, player.pitch = math.atan2(to.x, to.z), -0.38

    def frame():
        session.step(2)
        return session.paint().astype(int)

    for _ in range(40):                 # let the scene's own fades settle
        session.step(2)
    # A box straddling the corner: centred on the wall, just above the floor.
    combat.blood.add((wall.x, floor.y + 12.0, wall.z), (left.x, 0.0, left.z),
                     230.0, 230.0, 4, time.perf_counter(), kind=WALL)
    stained = frame()
    assert view.renderer.decals_drawn == 1
    combat.set_gore_level(0)
    clean = frame()
    assert view.renderer.decals_drawn == 0
    # Stained pixels: redder than the same pixel without the decal.
    redness = lambda image: image[:, :, 0] - image[:, :, 1]
    changed = (redness(stained) - redness(clean)) > 12
    assert changed.sum() > 150, "the decal painted nothing"
    # A stain: it adds no light.
    assert stained[changed].sum(axis=1).mean() <= clean[changed].sum(axis=1).mean() + 6
    # Both sides of the corner took it, with no gap where they meet: the
    # stained rows run on from the wall, above, onto the floor, below.
    rows_hit = np.flatnonzero(changed.sum(axis=1) >= 2)
    assert rows_hit[-1] - rows_hit[0] > 30
    assert np.all(np.diff(rows_hit) <= 6), "a gap in the decal at the corner"


def test_the_gore_setting_reads_levels_and_old_on_off_values():
    import configparser
    from editor.main_window import gore_level_from
    from engine.logic_combat import LogicCombat
    config = configparser.ConfigParser()
    assert gore_level_from(config) == 1                         # the default
    config.add_section('Settings')
    for raw, level in (('0', 0), ('2', 2), ('3', 3), ('99', 3), ('True', 1),
                       ('False', 0), ('nonsense', 1)):
        config.set('Settings', 'gore', raw)
        assert gore_level_from(config) == level, raw
    assert len(LogicCombat.GORE_NAMES) == len(LogicCombat.GORE_LEVELS) == 4
    assert LogicCombat.GORE_NAMES[0] == "Off" and LogicCombat.GORE_NAMES[3] == "Verhoeven"


def test_a_hit_puts_blood_on_the_actors_sprite(fio_session):
    """Wounds: steps on the entity, published as the sprite_wound column."""
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, 'gun1', 1)
    monster = _human(session)
    light, heavy = combat.WOUND_PER_HIT[1]
    assert monster.properties['_wounds'] == light
    session.step(2)
    table = session.view.game_state.published('entity_table')
    slot = table.things.index(monster) if hasattr(table.things, 'index') else None
    packed = float(table.sprite_wound[slot])
    # Even (it rides on the opacity), and the steps are its low four bits.
    assert packed % 2.0 == 0.0 and int(packed / 2) % 16 == light
    others = [float(v) for i, v in enumerate(table.sprite_wound[:len(table.things)]) if i != slot]
    assert not any(others)


def test_wounds_stop_at_the_most_there_is_and_gore_off_leaves_none(fio_session):
    session = fio_session(MAP, size=(200, 160))
    combat, _, _ = _shoot(session, 'gun2', 3, shots=3)
    assert _human(session).properties['_wounds'] == combat.WOUND_STEPS
    session.stop_play()
    combat, _, _ = _shoot(session, 'gun2', 0)
    assert not _human(session).properties.get('_wounds')
