"""Big World with an overhead camera: residency and tiers sized from the screen.

While the host's camera is overhead, residency is a circle just past the
screen's corners, NEAR is the screen's rectangle (in cells), and both follow
the player's own movement instead of waiting for a cell crossing. A host reads
``sim_tiers_fit_view`` to know that ACTIVE now means off screen.

With a first-person camera -- the host's footprint is None -- or a host with no
footprint at all, nothing here applies: the authored radii, the near circle and
the view-distance horizon stand exactly as before.
"""

from engine.spatial import TIER_ACTIVE, TIER_NEAR, tier_of
from engine.view_distance import ViewDistance
from plugins.bigworld.runtime import BigWorldSession
from plugins.bigworld.tiers import TierClassifier

from .test_bigworld_tiers import FakePlayer, grid_world


class OverheadLogic:
    """A streaming host whose overhead camera shows +/- (hx, hz) of ground."""

    def __init__(self, things, footprint=(1400.0, 800.0), at=(0.0, 0.0)):
        self.brushes = []
        self.things = things
        self.player = FakePlayer(*at)
        self.view_distance = ViewDistance()
        self.footprint = footprint
        self.overhead_height = 800.0

    def overhead_ground_footprint(self):
        return self.footprint


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
    assert logic.sim_tiers_fit_view is True
    # The camera's far plane follows residency.
    assert logic.view_distance.limit is not None


def test_near_is_the_screen_rectangle_and_the_rest_resident_is_active():
    logic, session = fitted_session(grid_world(cells_each_way=8), footprint=(1400.0, 500.0))
    rx, rz = session.tiers.near_rect
    assert rx > rz
    near = [t for t in logic.things if tier_of(t) == TIER_NEAR]
    active = [t for t in logic.things if tier_of(t) == TIER_ACTIVE]
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
    assert not getattr(logic, "sim_tiers_fit_view", False)


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
    assert logic.sim_tiers_fit_view is True
    session.stop()
    assert logic.sim_tiers_fit_view is False
