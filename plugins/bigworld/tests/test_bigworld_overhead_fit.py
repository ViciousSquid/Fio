"""Big World with an overhead camera: residency and tiers sized from the screen.

While the host's camera is overhead, residency is a circle just past the
screen's farthest corner (never larger than authored), NEAR is the screen's
rectangle (in cells), and both follow the player's own movement instead of
waiting for a cell crossing. The session publishes the rectangle as
``sim_view_rect`` so a host knows what is on screen.

With a first-person camera -- the host's footprint is None -- or a host with no
footprint at all, nothing here applies: the authored radii, the near circle and
the view-distance horizon stand exactly as before.
"""

import math

from engine.spatial import TIER_ACTIVE, TIER_NEAR, tier_of
from engine.view_distance import ViewDistance
from plugins.bigworld.runtime import BigWorldSession
from plugins.bigworld.tiers import TierClassifier

from .test_bigworld_tiers import FakePlayer, grid_world


class _CameraFixture:
    def __init__(self, logic):
        self.logic = logic
        self.overhead_height = 800.0
        self.overhead_height_limit = None

    def overhead_ground_footprint(self):
        return self.logic.footprint

    def effective_overhead_height(self):
        height = float(self.overhead_height)
        if self.overhead_height_limit is not None:
            height = min(height, float(self.overhead_height_limit))
        return height


class OverheadLogic:
    """A streaming host whose overhead camera shows +/- (hx, hz) of ground."""

    def __init__(self, things, footprint=(1400.0, 800.0), at=(0.0, 0.0)):
        self.editor_state = type('State', (), {'things': things, 'brushes': []})()
        self.player = FakePlayer(*at)
        self.view_distance = ViewDistance()
        self.footprint = footprint
        self.camera = _CameraFixture(self)



def fitted_session(things, **kw):
    logic = OverheadLogic(things, **kw)
    session = BigWorldSession(logic, activation_radius=2048.0,
                              deactivation_radius=2304.0, sim_near_radius=1024.0)
    session.start()
    return logic, session


def test_residency_is_sized_from_the_screen_not_the_authored_radius():
    logic, session = fitted_session(grid_world(), footprint=(900.0, 500.0))
    corner = (900.0 ** 2 + 500.0 ** 2) ** 0.5
    act = session.manager.activation_radius
    # Past the corners by the refresh distance, and well inside the authored 2048.
    assert corner + session.FIT_REFRESH <= act < corner + session.FIT_REFRESH + session.FIT_QUANTUM
    assert act < 2048.0
    assert session.manager.deactivation_radius > act
    assert logic.sim_view_rect == session.tiers.near_rect
    # The camera's far plane follows residency.
    assert logic.view_distance.limit is not None


def test_near_is_the_screen_rectangle_and_the_rest_resident_is_active():
    logic, session = fitted_session(grid_world(cells_each_way=8), footprint=(1400.0, 500.0))
    rx, rz = session.tiers.near_rect
    assert rx > rz
    near = [t for t in logic.editor_state.things if tier_of(t) == TIER_NEAR]
    active = [t for t in logic.editor_state.things if tier_of(t) == TIER_ACTIVE]
    assert near and active
    cell = session.manager.cell_size
    # NEAR: in a cell meeting the rectangle. ACTIVE: outside it on some axis.
    for t in near:
        assert abs(t.pos[0]) <= rx + cell and abs(t.pos[2]) <= rz + cell
    assert all(abs(t.pos[2]) > rz or abs(t.pos[0]) > rx for t in active)


def test_tiers_follow_the_player_between_cell_crossings():
    logic, session = fitted_session(grid_world(cells_each_way=8), footprint=(600.0, 400.0))
    before = session._tier_pos
    # Inside the same 512 cell, but past the re-tier step.
    logic.player.pos[0] += session.FIT_RETIER + 10.0
    session.tick()
    assert session._tier_pos != before


def _authored(session, logic):
    assert session.manager.activation_radius == 2048.0
    assert session.manager.deactivation_radius == 2304.0
    assert session.tiers.near_rect is None
    assert session.tiers.near_radius == 1024.0
    assert getattr(logic, "sim_view_rect", None) is None


def test_a_first_person_camera_keeps_the_authored_radii():
    logic, session = fitted_session(grid_world(), footprint=None)
    _authored(session, logic)
    logic.player.pos[0] += 300.0
    session.tick()
    _authored(session, logic)


def test_a_host_without_a_footprint_keeps_the_authored_radii():
    from .test_bigworld_tiers import FakeLogic
    logic = FakeLogic(things=grid_world(), player=FakePlayer(),
                      view_distance=ViewDistance())
    session = BigWorldSession(logic, activation_radius=2048.0,
                              deactivation_radius=2304.0, sim_near_radius=1024.0)
    session.start()
    _authored(session, logic)


def test_the_classifier_rectangle_is_per_cell():
    class Manager:
        cell_size = 512.0

        def __init__(self, coords):
            self.active_cells = set(coords)
            self.cells = {}

    coords = [(x, z) for x in range(-4, 4) for z in range(-4, 4)]
    tiers = TierClassifier(near_radius=1024.0, active_radius=4096.0)
    tiers.set_near_rect((700.0, 100.0))
    tiers.update(Manager(coords), 256.0, 256.0)
    near = {c for c in coords if tiers.cell_tier(c) == TIER_NEAR}
    # The box spans x -444..956 and z 156..356: cells -1, 0 and 1 across,
    # only the player's row down.
    assert {z for _, z in near} == {0}
    assert {x for x, _ in near} == {-1, 0, 1}


def test_filled_terrain_keeps_streaming_at_the_fitted_radius():
    class Terrain:
        chunk_size = 2048.0
        min_chunk_x, max_chunk_x, min_chunk_z, max_chunk_z = -2, 2, -2, 2
        streaming = False
        stream_radius = 1536.0
        stream_evict_padding = 512.0
        offset_x = offset_z = 0.0

        def set_streaming(self, on, radius=None):
            self.streaming = on
            if radius:
                self.stream_radius = radius

        def set_world_extent(self, *a, **k):
            pass

        def set_bounds(self, *a, **k):
            pass

    logic = OverheadLogic(grid_world(), footprint=(900.0, 500.0))
    logic.terrain = Terrain()
    session = BigWorldSession(logic, activation_radius=2048.0,
                              deactivation_radius=2304.0, terrain_fill=True)
    session.start()
    session.tick()            # fit is applied before the terrain is set up
    assert logic.terrain.streaming is True
    assert logic.terrain.stream_radius == session.manager.activation_radius < 2048.0
    session.stop()
    assert logic.terrain.streaming is False


def test_leaving_the_overhead_camera_restores_the_authored_radii():
    logic, session = fitted_session(grid_world(), footprint=(900.0, 500.0))
    assert session.manager.activation_radius < 2048.0
    logic.footprint = None                    # switched to first person
    session.tick()
    _authored(session, logic)


def test_stopping_hands_the_fit_back():
    logic, session = fitted_session(grid_world(), footprint=(900.0, 500.0))
    assert logic.sim_view_rect is not None
    session.stop()
    assert logic.sim_view_rect is None
    assert session.tiers.near_rect is None


def test_a_screen_past_the_authored_radius_keeps_the_authored_residency():
    """A raked or high camera can show tens of thousands of units of ground;
    residency and the camera's reach stay what the map authored."""
    logic, session = fitted_session(grid_world(), footprint=(57000.0, 45000.0, 72600.0))
    assert session.manager.activation_radius == 2048.0
    assert session.manager.deactivation_radius == 2304.0
    # The camera sees down to the residency edge at the player's ground.
    assert logic.view_distance.limit == 64.0 * math.ceil(math.hypot(2048.0, 800.0) / 64.0)
    # The screen's box is still published, for whoever throttles off screen.
    assert logic.sim_view_rect is not None


def test_turning_the_camera_retiers_but_leaves_residency_alone():
    logic, session = fitted_session(grid_world(cells_each_way=8),
                                    footprint=(900.0, 500.0, 1030.0))
    residency = (session.manager.activation_radius, logic.view_distance.limit)
    forced = []
    original = session.manager.update

    def update(pos, force=False):
        forced.append(force)
        return original(pos, force=force)
    session.manager.update = update
    logic.footprint = (700.0, 800.0, 1030.0)        # same reach, turned box
    session.tick()
    assert (session.manager.activation_radius, logic.view_distance.limit) == residency
    assert True not in forced                       # no forced residency pass
    assert logic.sim_view_rect == session.tiers.near_rect
    assert session.tiers.near_rect[1] > session.tiers.near_rect[0]


def test_the_published_radii_do_not_depend_on_when_the_fit_arrived():
    at_start, s1 = fitted_session(grid_world(), footprint=(1000.0, 500.0))
    later, s2 = fitted_session(grid_world(), footprint=None)
    later.footprint = (1000.0, 500.0)
    s2.tick()
    assert (at_start.sim_near_radius, at_start.sim_active_radius) == \
        (later.sim_near_radius, later.sim_active_radius)
    assert at_start.sim_view_rect == later.sim_view_rect


def test_the_overhead_camera_is_held_under_the_activation_radius():
    logic, session = fitted_session(grid_world(), footprint=(900.0, 500.0))
    assert logic.camera.overhead_height_limit == 2048.0
    session.stop()
    assert logic.camera.overhead_height_limit is None


def test_a_camera_at_the_ceiling_still_sees_the_player():
    """With the screen past residency, the camera reaches down to the
    residency edge at the player's ground -- past the camera's own height, so
    the player is never beyond the far plane."""
    logic, session = fitted_session(grid_world(), footprint=(4000.0, 2048.0, 4500.0))
    logic.camera.overhead_height = 2048.0
    logic.footprint = (4100.0, 2048.0, 4600.0)      # re-fit at the new height
    session.tick()
    assert session.manager.activation_radius == 2048.0
    assert logic.view_distance.limit > 2048.0 * 1.4
