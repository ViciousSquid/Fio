"""A solid model with Rotate on collides where it is drawn in Play: its
collision proxy turns with its Rotate angle (engine.logic_groups.CarriedModels),
about its own axis through its origin, as the renderer draws it."""
import math

import numpy as np
import pytest

from engine import brush_placement

pytestmark = pytest.mark.qt

DRUM = "assets/models/Oil_Drum.obj"
#: The drum squashed into a slab like a door: 182 wide (X), 65 high, 11 deep.
SLAB = [4.0, 1.0, 0.25]


def _level(*brushes):
    floor = {"id": "floor", "name": "floor", "pos": [0, -8, 0], "size": [2048, 16, 2048]}
    return {
        "version": 5,
        "brushes": [floor, *brushes],
        "things": [{"type": "playerstart", "pos": [0, 40, 600],
                    "properties": {"type": "player_start", "name": "start",
                                   "id": "ps-1", "primary": True}}],
    }


def _with_things(level, *things):
    level["things"].extend(things)
    return level


def _lift(**extra):
    brush = {"id": "lift", "name": "lift", "pos": [0, 100, 0], "size": [256, 16, 256],
             "is_mover": True, "start_on": True, "direction": [0, 1, 0],
             "distance": 200.0, "speed": 100.0, "brush_group": "lift"}
    brush.update(extra)
    return brush


def _prop(name="slab", pos=(0, 0, 0), scale=SLAB, shape="mesh", **props):
    properties = {"type": "prop", "name": name, "id": "prop-" + name,
                  "model_path": DRUM, "collision_shape": shape, "scale": list(scale),
                  "no_collision": False, "physics_enabled": False,
                  "rotate_enabled": True, "rotate_mode": "turn", "rotate_axis": "y",
                  "rotate_angle": 90.0, "rotate_speed": 360.0, "rotate_ease": 0.0,
                  "rotate_autostart": False}
    properties.update(props)
    return {"type": "prop", "pos": list(pos), "properties": properties}


def _thing(session, name):
    return next(t for t in session.state.things if t.properties.get("name") == name)


def _proxy(session, thing):
    found = [b for b in session.logic.collision_runtime._model_collision_brushes
             if b.get("_physics_entity") is thing]
    assert len(found) == 1, "the model should have one collision proxy"
    return found[0]


def _proxies_of(session, thing):
    return [b for b in session.logic.collision_runtime._model_collision_brushes
            if b.get("_physics_entity") is thing]


def _column(session, proxy, x, z):
    """``(bottom, top)`` of the proxy's solid over ``(x, z)`` as the spatial
    grid's broad and narrow phases answer it (what physics stands bodies
    on); NaN where the grid does not find it there."""
    grid = session.logic.session_runtime.spatial_grid
    query, row, rows = grid.box_pairs(np.array([[x - 1.0, -1e4, z - 1.0]]),
                                      np.array([[x + 1.0, 1e4, z + 1.0]]))
    mine = [r for r in row.tolist() if rows.brushes[r] is proxy]
    if not mine:
        return float("nan"), float("nan")
    bottom, top = grid.columns(rows, np.array(mine[:1]), np.array([[x]]), np.array([[z]]))
    return float(bottom[0, 0]), float(top[0, 0])


def _solid(session, proxy, x, z):
    return not math.isnan(_column(session, proxy, x, z)[1])


def _found_near(session, proxy, x, y, z, reach=4.0):
    """Whether the player's broad phase hands the proxy over around a point."""
    import glm
    grid = session.logic.session_runtime.spatial_grid
    near = grid.get_potential_colliders(glm.vec3(x - reach, y - reach, z - reach),
                                        glm.vec3(x + reach, y + reach, z + reach))
    return any(b is proxy for b in near)


def _drawn_basis(thing):
    """The rotation/scale the renderer draws *thing* with now, as a 3x3."""
    from engine.entity_table import _model_transform_columns
    model, _normal = _model_transform_columns(thing)
    m = np.asarray(model, dtype=np.float64).reshape(4, 4).T     # column-major
    return m[:3, :3]


def _turn(session, thing, ticks=60):
    assert session.logic.prop_runtime.start_rotate(thing)
    session.step(ticks)


def _assert_mesh_where_drawn(proxy, built_pos, built_basis, thing):
    """Every collision triangle is the built one carried by the drawn
    transform: ``pos + M_now M_built^-1 (b - pos_built)``."""
    planes, triangles, bounds, frame = brush_placement.collision_shape(proxy)
    assert frame is None, "a model's mesh is given in world space"
    move = _drawn_basis(thing) @ np.linalg.inv(built_basis)
    pos = np.asarray([float(v) for v in thing.pos], dtype=np.float64)
    built = np.asarray([tri for tri, _n in proxy["_mesh_triangles"]], dtype=np.float64)
    expected = (built - built_pos) @ move.T + pos
    got = np.asarray([tri for tri, _n in triangles], dtype=np.float64)
    assert got.shape == expected.shape
    assert np.allclose(got, expected, atol=1e-2), "collision is not where the model is drawn"
    flat = expected.reshape(-1, 3)
    assert list(bounds[0]) + list(bounds[1]) == pytest.approx(
        list(flat.min(axis=0)) + list(flat.max(axis=0)), abs=1e-2)
    # The broad phase's box holds the whole turned mesh.
    box = brush_placement.world_bounds(proxy)
    assert all(box[i] <= flat[:, i].min() + 1e-3 for i in range(3))
    assert all(box[i + 3] >= flat[:, i].max() - 1e-3 for i in range(3))


# ---------------------------------------------------------------------------
# Turn: a door-like model turned 90 degrees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["mesh", "aabb"])
def test_a_turned_model_collides_at_its_turned_orientation(fio_session, monkeypatch, shape):
    session = fio_session(_with_things(_level(), _prop(shape=shape)))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    slab = _thing(session, "slab")
    session.start_play()
    proxy = _proxy(session, slab)
    assert proxy["_collision_mode"] == shape
    home_box = brush_placement.world_bounds(proxy)
    built_basis = _drawn_basis(slab)
    built_pos = np.asarray([float(v) for v in slab.pos])
    # As placed: long along X, thin along Z.
    home_top = _column(session, proxy, 70.0, 0.0)[1]
    assert home_top == home_top
    assert not _solid(session, proxy, 0.0, 70.0)
    assert _found_near(session, proxy, 70.0, 30.0, 0.0)

    _turn(session, slab)

    assert slab._rotate_angle == pytest.approx(90.0)
    # Turned: long along Z, and its old footprint clear.
    assert not _solid(session, proxy, 70.0, 0.0), "collision was left where it was placed"
    assert _solid(session, proxy, 0.0, 70.0), "no collision where the model is drawn"
    # Its own +X end, turned 90 degrees about +Y, is over world -Z.
    assert _column(session, proxy, 0.0, -70.0)[1] == pytest.approx(home_top, abs=1e-3)
    assert _found_near(session, proxy, 0.0, 30.0, 70.0)
    box = brush_placement.world_bounds(proxy)
    assert box[2] < -80.0 and box[5] > 80.0 and box[0] > -20.0 and box[3] < 20.0
    shape_now = brush_placement.collision_shape(proxy)
    if shape == "mesh":
        _assert_mesh_where_drawn(proxy, built_pos, built_basis, slab)
    else:
        # The box, turned with the model: its planes in a turned frame, whose
        # world AABB is the broad phase's.
        planes, _triangles, _bounds, frame = shape_now
        assert len(planes) == 6 and frame is not None and frame.r is not None
        x, y, z = frame.to_shape(0.0, 30.0, 70.0)
        assert all(p[0] * x + p[1] * y + p[2] * z <= p[3] + 1e-6 for p in planes)
        x, y, z = frame.to_shape(70.0, 30.0, 0.0)
        assert any(p[0] * x + p[1] * y + p[2] * z > p[3] for p in planes)

    session.stop_play()
    assert not proxy["is_mover"] and brush_placement.runtime_of(proxy) is None
    assert brush_placement.world_bounds(proxy) == pytest.approx(home_box)
    assert id(proxy) not in brush_placement.off_range
    for key in ("_carried_travel", "_travel_filed", "_turned_mesh", "_aabb_turn",
                "_collision_entry", "_mesh_columns_turned"):
        assert key not in proxy, key
    stopped = brush_placement.collision_shape(proxy)
    if shape == "mesh":
        assert stopped[1] is proxy["_mesh_triangles"], "Stop must restore the authored mesh"
    else:
        assert stopped is None, "Stop must restore the authored box"
    from engine.logic_groups import CarriedModels
    assert not any(isinstance(s, CarriedModels) for s in brush_placement.runtime_sources())
    session.window.unsaved_changes = False


def test_a_model_tipped_about_an_axis_off_its_centre_swings_about_its_origin(
        fio_session, monkeypatch):
    """A drum (origin at its base) tipped 90 degrees about its own X axis --
    which a yaw of 90 points along world -Z -- lies on its side: its centre
    swings about its origin, exactly as it is drawn."""
    session = fio_session(_with_things(_level(), _prop(
        "drum", pos=(300, 0, 0), scale=[1, 1, 1], rotate_axis="x",
        rotation=[0.0, 90.0, 0.0])))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    drum = _thing(session, "drum")
    session.start_play()
    proxy = _proxy(session, drum)
    built_basis = _drawn_basis(drum)
    built_pos = np.asarray([float(v) for v in drum.pos])
    home_box = brush_placement.world_bounds(proxy)
    assert home_box[4] == pytest.approx(65.2, abs=0.5)

    _turn(session, drum)

    _assert_mesh_where_drawn(proxy, built_pos, built_basis, drum)
    box = brush_placement.world_bounds(proxy)
    assert box[4] < 30.0, "a tipped drum is no longer standing"
    # Lying along world X from its base at x = 300, one way or the other.
    assert max(abs(box[0] - 300.0), abs(box[3] - 300.0)) > 60.0
    assert id(proxy) not in brush_placement.off_range
    session.stop_play()
    assert brush_placement.world_bounds(proxy) == pytest.approx(home_box)


# ---------------------------------------------------------------------------
# Continuous: a spinning model
# ---------------------------------------------------------------------------

def test_a_spinning_model_stays_filed_and_its_collision_turns(fio_session, monkeypatch):
    session = fio_session(_with_things(_level(), _prop(
        rotate_mode="continuous", rotate_speed=120.0, rotate_autostart=True)))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    slab = _thing(session, "slab")
    session.start_play()
    proxy = _proxy(session, slab)
    assert proxy["is_mover"] and proxy["_travel_filed"][2], "filed as one that turns"
    built_basis = _drawn_basis(slab)
    built_pos = np.asarray([float(v) for v in slab.pos])
    boxes = set()
    for _ in range(14):                     # most of two turns, checked as it goes
        session.step(17)
        angle = math.radians(slab._rotate_angle)
        assert id(proxy) not in brush_placement.off_range, "a turn took it off range"
        box = tuple(round(v, 3) for v in brush_placement.world_bounds(proxy))
        boxes.add(box)
        # The slab's long axis (its X) turned about Y: Ry(a) . (1, 0, 0).
        tip = (70.0 * math.cos(angle), -70.0 * math.sin(angle))
        assert _solid(session, proxy, *tip), "no collision under the slab's end"
        assert _found_near(session, proxy, tip[0], 30.0, tip[1])
        across = (70.0 * math.sin(angle), 70.0 * math.cos(angle))
        assert not _solid(session, proxy, *across)
    assert len(boxes) > 6, "the collision bounds should change as it spins"
    _assert_mesh_where_drawn(proxy, built_pos, built_basis, slab)
    session.stop_play()


def test_a_player_on_a_turntable_goes_round_with_it(fio_session, monkeypatch):
    session = fio_session(_with_things(_level(), _prop(
        "table", scale=[3.0, 0.3, 3.0], rotate_mode="continuous", rotate_speed=90.0,
        rotate_autostart=True)))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    table = _thing(session, "table")
    session.start_play()
    proxy = _proxy(session, table)
    deck = _column(session, proxy, 40.0, 0.0)[1]
    assert 10.0 < deck < 25.0, "the test needs the turntable's deck under (40, 0)"
    player = session.logic.player_runtime.player
    player.pos.x, player.pos.y, player.pos.z = 40.0, deck + 60.0, 0.0
    player.velocity.x = player.velocity.y = player.velocity.z = 0.0
    session.step(20)
    assert player.ground_object is proxy, "the test needs the player on the turntable"
    start = math.atan2(-float(player.pos.z), float(player.pos.x))
    turned = table._rotate_angle

    session.step(40)

    swept = (table._rotate_angle - turned) % 360.0
    assert swept > 30.0
    now = math.atan2(-float(player.pos.z), float(player.pos.x))
    assert math.degrees(now - start) % 360.0 == pytest.approx(swept, abs=3.0), (
        "the player was not carried round by the surface under them")
    assert math.hypot(float(player.pos.x), float(player.pos.z)) == pytest.approx(
        math.hypot(40.0, 0.0), abs=2.0)
    assert player.ground_object is proxy
    session.stop_play()


# ---------------------------------------------------------------------------
# Grouped and turning
# ---------------------------------------------------------------------------

def test_a_grouped_model_turning_on_a_lift_is_carried_and_turned(fio_session, monkeypatch):
    session = fio_session(_with_things(
        _level(_lift(speed=300.0)),
        _prop("drum", pos=(0, 108, 0), scale=[1, 1, 1], rotate_axis="x",
              rotation=[0.0, 90.0, 0.0], rotate_speed=180.0, brush_group="lift")))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    drum = _thing(session, "drum")
    session.start_play()
    proxy = _proxy(session, drum)
    built_basis = _drawn_basis(drum)
    built_pos = np.asarray([float(v) for v in drum.pos])
    filed = proxy["_travel_filed"]
    assert filed[2], "filed as one that turns"

    session.logic.prop_runtime.start_rotate(drum)
    for _ in range(4):
        session.step(10)
        assert id(proxy) not in brush_placement.off_range
    risen = float(drum.pos[1]) - 108.0
    assert risen > 50.0, "the lift should have carried the drum up"
    assert drum._rotate_angle == pytest.approx(90.0)
    _assert_mesh_where_drawn(proxy, built_pos, built_basis, drum)
    session.stop_play()
    assert not proxy["is_mover"] and brush_placement.runtime_of(proxy) is None
    assert list(drum.pos) == pytest.approx([0.0, 108.0, 0.0])


# ---------------------------------------------------------------------------
# What is left alone
# ---------------------------------------------------------------------------

def test_models_that_do_not_turn_keep_still_collision(fio_session, monkeypatch):
    """A model with Rotate off, a physics Prop and a Prop with no collision,
    each set rotating: none of their collision is a mover, and with nothing
    to carry or turn no runtime source is registered."""
    session = fio_session(_with_things(
        _level(),
        _prop("still", pos=(300, 0, 0), rotate_enabled=False, rotate_autostart=True),
        _prop("barrel", pos=(-300, 0, 0), scale=[1, 1, 1], physics_enabled=True,
              shape="aabb", rotate_mode="continuous", rotate_autostart=True),
        _prop("ghost", pos=(0, 0, -300), no_collision=True,
              rotate_mode="continuous", rotate_autostart=True)))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    still, barrel, ghost = (_thing(session, n) for n in ("still", "barrel", "ghost"))
    session.start_play()
    from engine.logic_groups import CarriedModels
    assert not any(isinstance(s, CarriedModels) for s in brush_placement.runtime_sources())
    still_proxy = _proxy(session, still)
    still_box = brush_placement.world_bounds(still_proxy)
    body = _proxy(session, barrel)
    assert body["_physics_body"]
    assert not _proxies_of(session, ghost)

    session.step(40)

    assert ghost._rotate_angle != 0.0, "the test needs the ghost turning"
    assert not still_proxy["is_mover"] and "_carried_travel" not in still_proxy
    assert brush_placement.world_bounds(still_proxy) == pytest.approx(still_box)
    assert brush_placement.collision_shape(still_proxy)[1] is still_proxy["_mesh_triangles"]
    assert not body["is_mover"] and brush_placement.runtime_of(body) is None
    assert "_carried_travel" not in body
    assert not _proxies_of(session, ghost)
    session.stop_play()


# ---------------------------------------------------------------------------
# The player's headroom test reads the mesh where it is now
# ---------------------------------------------------------------------------

def _clear(player, proxy, centre, half=(16.0, 28.0, 16.0)):
    """Player._has_headroom for a standing hull centred at *centre*."""
    import glm
    lo = glm.vec3(centre[0] - half[0], centre[1] - half[1], centre[2] - half[2])
    hi = glm.vec3(centre[0] + half[0], centre[1] + half[1], centre[2] + half[2])
    return player._has_headroom(proxy, lo, hi)


def test_headroom_reads_a_carried_mesh_where_it_has_been_carried(fio_session, monkeypatch):
    session = fio_session(_with_things(
        _level(_lift(speed=400.0)),
        _prop("drum", pos=(0, 108, 0), scale=[1, 1, 1], rotate_enabled=False,
              brush_group="lift")))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    drum = _thing(session, "drum")
    session.start_play()
    proxy = _proxy(session, drum)
    player = session.logic.player_runtime.player
    authored = (20.0, 140.0, 0.0)               # inside the drum as placed
    assert not _clear(player, proxy, authored)

    session.step(30)

    risen = float(drum.pos[1]) - 108.0
    assert risen > 80.0
    assert _clear(player, proxy, authored), "the drum's vacated place still blocks"
    assert not _clear(player, proxy, (authored[0], authored[1] + risen, authored[2])), (
        "the drum where it has been carried leaves headroom")
    session.stop_play()


def test_headroom_reads_a_turned_mesh_where_it_has_turned(fio_session, monkeypatch):
    session = fio_session(_with_things(_level(), _prop()))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    slab = _thing(session, "slab")
    session.start_play()
    proxy = _proxy(session, slab)
    player = session.logic.player_runtime.player
    placed, turned = (70.0, 32.0, 0.0), (0.0, 32.0, -70.0)
    assert not _clear(player, proxy, placed)
    assert _clear(player, proxy, turned)

    _turn(session, slab)

    assert _clear(player, proxy, placed), "the slab's old footprint still blocks"
    assert not _clear(player, proxy, turned), "the turned slab leaves headroom"
    session.stop_play()
    assert not _clear(player, proxy, placed), "Stop must put the authored mesh back"
