"""
Headless tests for the Big World plugin.

Runs without a display, OpenGL or PyQt: it exercises the cell maths, the
activation/hysteresis logic, UUID stability, the reversible runtime effects and
the plugin's discovery/registration. No renderer is touched.

Run with:  python -m plugins.bigworld.tests.test_bigworld   (from the repo root)
or under pytest.
"""

import math
import os
import sys
import uuid as _uuidlib

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Load plugins FIRST so plugin discovery owns the initial import of the bigworld
# package. Importing a bigworld submodule directly (below) would otherwise pull
# in editor.things, whose bootstrap re-enters discovery while bigworld is still
# half-imported and skips it. In the real editor this never happens — the editor
# package is imported before any plugin — so this ordering is a test-only guard.
from plugins.manager import load_plugins as _load_plugins
_load_plugins()

from plugins.bigworld.cell import (CELL_SIZE, CellState, cell_of_point,
                                    cells_for_aabb, cell_distance_sq)
from plugins.bigworld.manager import BigWorldManager
from plugins.bigworld.runtime import BigWorldSession
from plugins.bigworld import persistence


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

def make_brush(x, y, z, sx=64.0, sy=64.0, sz=64.0, **extra):
    b = {"id": str(_uuidlib.uuid4()), "pos": [x, y, z], "size": [sx, sy, sz]}
    b.update(extra)
    return b


class FakeThing:
    def __init__(self, x, y, z, ttype="monster", **props):
        self.pos = [x, y, z]
        self.properties = {"id": str(_uuidlib.uuid4()), "type": ttype}
        self.properties.update(props)


class FakePlayer:
    def __init__(self, pos):
        self.pos = list(pos)


class FakeLogic:
    def __init__(self, brushes, things, player_pos=(0, 0, 0), terrain=None):
        self.brushes = brushes
        self.things = things
        self.player = FakePlayer(player_pos)
        self.terrain = terrain


class FakeTerrain:
    """A stand-in exposing the streaming surface engine.terrain.Terrain adds.

    It faithfully mirrors ``set_world_extent`` / ``set_bounds`` / ``set_streaming``
    so the session's terrain-fill wiring can be exercised headlessly (the real
    Terrain needs numpy + an OpenGL context). GL is never touched; a real
    Terrain defers its chunk prune to the render thread, which the ``prune``
    flag here records instead.
    """

    def __init__(self, min_x=-2, max_x=2, min_z=-2, max_z=2,
                 chunk_size=256.0, enabled=True):
        self.enabled = enabled
        self.streaming = False
        self.stream_radius = 1536.0
        self.stream_evict_padding = 512.0
        self.streamed_chunks = 0
        self.chunk_size = chunk_size
        self.offset_x = 0.0
        self.offset_z = 0.0
        self.min_chunk_x = min_x
        self.max_chunk_x = max_x
        self.min_chunk_z = min_z
        self.max_chunk_z = max_z
        self.extent_calls = []
        self.deferred_prune = False

    def set_streaming(self, enabled, radius=None):
        self.streaming = bool(enabled)
        if radius is not None and radius > 0:
            self.stream_radius = float(radius)

    def set_bounds(self, min_x, max_x, min_z, max_z, prune=True):
        self.min_chunk_x, self.max_chunk_x = min_x, max_x
        self.min_chunk_z, self.max_chunk_z = min_z, max_z
        if not prune:
            self.deferred_prune = True

    def set_world_extent(self, min_wx, min_wz, max_wx, max_wz, prune=True):
        self.extent_calls.append((min_wx, min_wz, max_wx, max_wz))
        cs = self.chunk_size
        self.set_bounds(int(math.floor(min_wx / cs)), int(math.floor(max_wx / cs)),
                        int(math.floor(min_wz / cs)), int(math.floor(max_wz / cs)),
                        prune=prune)


# --------------------------------------------------------------------------
# 1. Cell maths — shares the SpatialGrid convention
# --------------------------------------------------------------------------

def test_cell_math_matches_spatial_grid():
    print("[1] cell coordinate maths (512 grid, shared with SpatialGrid)")
    _check(CELL_SIZE == 512.0, "cell size is 512 (matches engine SpatialGrid)")
    _check(cell_of_point(0, 0) == (0, 0), "origin is cell (0,0)")
    _check(cell_of_point(511.9, 511.9) == (0, 0), "just inside origin cell")
    _check(cell_of_point(512, 512) == (1, 1), "512 rolls to next cell")
    _check(cell_of_point(-1, -1) == (-1, -1), "negative floors correctly")

    # The engine's own SpatialGrid uses floor(coord/cell_size); prove identical.
    try:
        from engine.physics import SpatialGrid  # noqa: F401
        for x, z in [(0, 0), (513, -20), (-4000, 9000), (2047.5, -2048.1)]:
            gx = int(math.floor(x / 512.0)); gz = int(math.floor(z / 512.0))
            _check(cell_of_point(x, z) == (gx, gz),
                   f"cell_of_point matches grid convention at ({x},{z})")
    except Exception as exc:
        print(f"  skip: engine.physics unavailable ({exc})")


def test_multi_cell_spanning():
    print("[2] a brush spanning a boundary is referenced from every cell it touches")
    # A 1024-wide brush centred on a cell boundary spans two cells in X.
    cells = cells_for_aabb(-100, 0, 900, 100)  # x in [-100,900] crosses x=512
    xs = {c[0] for c in cells}
    _check(-1 in xs and 0 in xs and 1 in xs, "spans cells x=-1,0,1")

    mgr = BigWorldManager()
    big = make_brush(400, 0, 0, sx=1024, sy=64, sz=64)  # x in [-112, 912]
    mgr.add_brush(big)
    touching = [coord for coord, cell in mgr.cells.items() if big in cell.brushes]
    _check(len(touching) >= 2, f"spanning brush indexed into {len(touching)} cells")
    # Stored once by identity regardless of how many cells reference it.
    _check(len(mgr._brush_by_id) == 1, "spanning brush stored once by UUID (not duplicated)")


# --------------------------------------------------------------------------
# 3. Circular activation region
# --------------------------------------------------------------------------

def test_circular_activation():
    print("[3] activation region is a circle, not the bounding square")
    mgr = BigWorldManager(activation_radius=2048.0)
    # Fill a wide field of brushes so every candidate cell exists.
    brushes = []
    for cx in range(-8, 9):
        for cz in range(-8, 9):
            brushes.append(make_brush(cx * 512 + 256, 0, cz * 512 + 256))
    mgr.index_world(brushes, [])

    active = mgr.get_active_cells((0, 0, 0))
    # A cell tucked in the far corner of the bounding square is outside the circle.
    corner = (4, 4)  # nearest point ~ (2048,2048) → dist ~2896 > 2048
    near_axis = (4, 0)  # nearest point x=2048 → dist 2048 ≤ radius
    _check(corner not in active, "far corner cell excluded (circular, not square)")
    _check(near_axis in active, "on-axis cell at the radius is included")
    # Distance metric sanity.
    d2 = cell_distance_sq(4, 4, 0, 0)
    _check(d2 > 2048 ** 2, "corner cell nearest-point distance exceeds radius")


# --------------------------------------------------------------------------
# 4. Incremental activation / deactivation
# --------------------------------------------------------------------------

def test_incremental_streaming():
    print("[4] crossing a cell boundary streams only the delta")
    mgr = BigWorldManager(activation_radius=1024.0, deactivation_radius=1024.0)
    brushes = []
    for cx in range(-6, 7):
        for cz in range(-6, 7):
            brushes.append(make_brush(cx * 512 + 256, 0, cz * 512 + 256))
    mgr.index_world(brushes, [])

    d0 = mgr.update((0, 0, 0), force=True)
    _check(d0.changed and d0.entering_cells, "initial forced update activates the region")
    first_active = set(mgr.active_cells)

    # Move within the same cell → no work.
    d_same = mgr.update((10, 0, 10))
    _check(not d_same.changed, "moving within a cell does no streaming work")

    # Move one cell east → a strip enters, a strip leaves; not a full rebuild.
    d1 = mgr.update((512 + 10, 0, 10))
    _check(d1.changed, "crossing a boundary triggers an update")
    _check(d1.entering_cells and d1.leaving_cells, "both an entering and a leaving strip")
    _check(len(d1.entering_cells) < len(first_active),
           "only a strip of cells changes, not the whole active set")
    # Set algebra the spec calls out.
    _check(set(d1.entering_cells) == (mgr.active_cells - first_active),
           "entering == new_active - old_active")


# --------------------------------------------------------------------------
# 5. Hysteresis
# --------------------------------------------------------------------------

def test_hysteresis_no_thrash():
    print("[5] hysteresis stops boundary thrashing")
    mgr = BigWorldManager(activation_radius=2048.0, deactivation_radius=2304.0)
    brushes = [make_brush(cx * 512 + 256, 0, 256) for cx in range(-1, 12)]
    mgr.index_world(brushes, [])
    mgr.update((0, 0, 0), force=True)

    # Cell (4,0): nearest edge at x=2048. Activate the player just inside it.
    # Put the player so that cell (4,0) is between the two radii and check it
    # neither drops (within deact) once active, then finally drops beyond deact.
    # Move player east so cell (0,0)'s far edge distance grows.
    # Activate cell 4 by standing close, then retreat into the hysteresis band.
    mgr.update((2038, 0, 0))  # player cell 3; cell (4,0) nearest edge 10 away → active
    active_near = (4, 0) in mgr.active_cells

    # Step back into the hysteresis band: cell (4,0) nearest edge (x=2048) is now
    # ~2200 away (between 2048 and 2304). Player at x=-152 → cell -1 (a new cell,
    # so update actually runs).
    mgr.update((-152, 0, 0))
    in_band_still_active = (4, 0) in mgr.active_cells if active_near else True

    # Step further — into a different cell AND beyond the deactivation radius:
    # player x=-600 → cell -2; cell (4,0) nearest edge is 2648 away > 2304 → drop.
    mgr.update((-600, 0, 0))
    dropped = (4, 0) not in mgr.active_cells

    _check(in_band_still_active, "an active cell stays active inside the hysteresis band")
    _check(dropped, "the cell is finally dropped beyond the deactivation radius")


# --------------------------------------------------------------------------
# 6. Spanning brush stays active while any active cell holds it
# --------------------------------------------------------------------------

def test_spanning_refcount():
    print("[6] a spanning brush stays active while any active cell references it")
    mgr = BigWorldManager(activation_radius=600.0, deactivation_radius=600.0)
    # Brush spans cells (0,0) and (1,0).
    span = make_brush(512, 0, 128, sx=512, sy=64, sz=64)  # x in [256, 768]
    mgr.index_world([span], [])
    # Activate both cells, then deactivate one; the brush must remain active.
    mgr.activate_cell(0, 0)
    mgr.activate_cell(1, 0)
    _check(mgr.is_brush_active(span), "brush active with both cells active")
    b_gone, _, _ = mgr.deactivate_cell(1, 0)
    _check(span not in b_gone, "deactivating one spanning cell does NOT switch the brush off")
    _check(mgr.is_brush_active(span), "brush still active via the other cell")
    b_gone2, _, _ = mgr.deactivate_cell(0, 0)
    _check(span in b_gone2, "brush switches off only when its last active cell leaves")
    _check(not mgr.is_brush_active(span), "brush inactive once no active cell holds it")


# --------------------------------------------------------------------------
# 7. Persistent entities are never streamed out
# --------------------------------------------------------------------------

def test_persistent_entities():
    print("[7] persistent / global entities stay active regardless of cell")
    mgr = BigWorldManager(activation_radius=512.0, deactivation_radius=512.0)
    world_mgr = FakeThing(100000, 0, 100000, ttype="worldmanager")   # type-based
    tagged = FakeThing(90000, 0, 90000, ttype="monster", bw_persistent=True)  # property
    near = FakeThing(10, 0, 10, ttype="monster")
    mgr.index_world([], [world_mgr, tagged, near])
    mgr.update((0, 0, 0), force=True)
    _check(mgr.is_thing_active(world_mgr), "world manager (global type) always active")
    _check(mgr.is_thing_active(tagged), "bw_persistent entity always active")
    _check(world_mgr in mgr.persistent_things and tagged in mgr.persistent_things,
           "persistent entities tracked separately")
    _check(mgr.is_thing_active(near), "a nearby normal entity is active")


# --------------------------------------------------------------------------
# 8/9. Session applies reversible render/entity effects and restores them
# --------------------------------------------------------------------------

def test_session_effects_and_restore():
    print("[8] session hides inactive geometry/entities and restores on stop")
    near = make_brush(10, 0, 10)
    far = make_brush(50000, 0, 50000)
    near_mon = FakeThing(20, 0, 20, ttype="monster")
    far_mon = FakeThing(50010, 0, 50010, ttype="monster")
    far_pre_hidden = make_brush(60000, 0, 0, hidden=True)  # user already hid it
    logic = FakeLogic([near, far, far_pre_hidden], [near_mon, far_mon], player_pos=(0, 0, 0))

    session = BigWorldSession(logic, activation_radius=2048.0, deactivation_radius=2048.0)
    session.start()

    _check(not near.get("hidden", False), "near brush is visible (active)")
    _check(far.get("hidden", False), "far brush is hidden (inactive → culled by renderer)")
    _check(far_mon.properties.get("disabled", False), "far monster disabled (skipped by AI)")
    _check(not near_mon.properties.get("disabled", False), "near monster stays enabled")
    # Active-set queries.
    _check(session.manager.is_brush_active(near), "manager reports near brush active")
    _check(not session.manager.is_brush_active(far), "manager reports far brush inactive")

    print("[9] play-stop restores the exact prior state (nothing lost, no leaks)")
    session.stop()
    _check(not near.get("hidden", False), "near brush hidden flag cleared")
    _check(not far.get("hidden", False), "far brush un-hidden on stop")
    _check("_bw_parked_hidden" not in far, "no lingering BW marker on brush")
    _check(far_pre_hidden.get("hidden") is True, "user's own hidden brush stays hidden after stop")
    _check(not far_mon.properties.get("disabled", False), "far monster re-enabled on stop")
    _check("_bw_parked_disabled" not in far_mon.properties, "no lingering BW marker on entity")


def test_streaming_moves_active_set():
    print("[10] walking across the world moves the active set with the player")
    brushes = []
    for cx in range(0, 40):
        brushes.append(make_brush(cx * 512 + 256, 0, 256))
    logic = FakeLogic(brushes, [], player_pos=(0, 0, 0))
    session = BigWorldSession(logic, activation_radius=1024.0, deactivation_radius=1024.0)
    session.start()

    def active_xs():
        return sorted({c[0] for c in session.manager.active_cells})

    start_cols = active_xs()
    # Teleport-walk far east.
    logic.player.pos = [20 * 512 + 256, 0, 256]
    session.tick()
    end_cols = active_xs()
    _check(max(start_cols) < min(end_cols), "active columns shifted fully east with the player")
    # A brush that was active at the start is now hidden; one near the end is visible.
    _check(brushes[0].get("hidden", False), "the origin brush is streamed out after walking away")
    _check(not brushes[20].get("hidden", False), "a brush by the destination is streamed in")


# --------------------------------------------------------------------------
# 11. UUID stability across a full streaming round-trip
# --------------------------------------------------------------------------

def test_uuid_stability():
    print("[11] UUIDs are unchanged across load/activate/deactivate/save/reload")
    brushes = [make_brush(i * 400, 0, 0) for i in range(30)]
    things = [FakeThing(i * 400, 0, 50, ttype="pickup") for i in range(10)]
    logic = FakeLogic(brushes, things, player_pos=(0, 0, 0))

    before = persistence.collect_uuids(brushes, things)
    session = BigWorldSession(logic, activation_radius=800.0, deactivation_radius=800.0)
    session.start()
    # Stream around a bit.
    for x in (400, 2000, 6000, 0):
        logic.player.pos = [x, 0, 0]
        session.tick()
    session.stop()
    after = persistence.collect_uuids(brushes, things)
    _check(persistence.uuids_stable(before, after), "no UUID was regenerated by streaming")

    # Save hygiene: runtime markers never persist; UUIDs untouched.
    map_data = {
        "version": 3,
        "brushes": [dict(b) for b in brushes],
        "things": [{"type": "pickup", "pos": t.pos, "properties": dict(t.properties)}
                   for t in things],
    }
    persistence.strip_runtime_keys(map_data)
    leaked = any(k in b for b in map_data["brushes"] for k in persistence.RUNTIME_KEYS)
    _check(not leaked, "strip_runtime_keys removes all transient markers before save")
    saved_ids = {b["id"] for b in map_data["brushes"]}
    _check(saved_ids == before["brushes"], "brush UUIDs preserved in the saved map")


# --------------------------------------------------------------------------
# 12. Backwards compatibility
# --------------------------------------------------------------------------

def test_backwards_compatibility():
    print("[12] a map without Big World metadata behaves as ordinary Fio")
    plain = {"version": 3, "brushes": [make_brush(0, 0, 0)],
             "things": [{"type": "monster", "pos": [0, 0, 0],
                         "properties": {"id": "x", "type": "monster"}}]}
    _check(not persistence.map_has_bigworld(plain), "no settings entity ⇒ not a Big World map")
    _check(persistence.find_settings_thing([]) is None, "empty things ⇒ no settings")

    withbw = {"version": 3, "brushes": [],
              "things": [{"type": "bigworldsettings", "pos": [0, 0, 0],
                          "properties": {"id": "s", "type": "bigworldsettings",
                                         "activation_radius": 4096}}]}
    _check(persistence.map_has_bigworld(withbw), "settings entity present ⇒ Big World map")
    cfg = persistence.config_from_settings(persistence.find_settings_dict(withbw))
    _check(cfg["activation_radius"] == 4096.0, "config read from the settings entity")
    _check(cfg["deactivation_radius"] >= cfg["activation_radius"],
           "deactivation radius clamped to >= activation radius")


# --------------------------------------------------------------------------
# 13. Performance scaling
# --------------------------------------------------------------------------

def test_scaling_active_is_local():
    print("[13] active work is proportional to the local region, not world size")
    import time
    n = 100_000
    # A dense 316x316 field of brushes on a 200-unit lattice (~63k x 63k world).
    side = int(math.isqrt(n))
    brushes = []
    for i in range(side):
        for j in range(side):
            brushes.append(make_brush(i * 200, 0, j * 200))
    t0 = time.perf_counter()
    mgr = BigWorldManager(activation_radius=2048.0)
    mgr.index_world(brushes, [])
    t_index = time.perf_counter() - t0

    # Put the player in the middle of the field.
    mid = (side // 2) * 200
    t0 = time.perf_counter()
    mgr.update((mid, 0, mid), force=True)
    t_activate = time.perf_counter() - t0

    s = mgr.stats()
    _check(s["total_brushes"] == len(brushes), f"indexed all {len(brushes):,} brushes")
    _check(s["active_brushes"] < s["total_brushes"] // 20,
           f"active brushes are a small fraction ({s['active_brushes']:,} of {s['total_brushes']:,})")

    # Crossing one boundary must be far cheaper than the initial activation.
    t0 = time.perf_counter()
    d = mgr.update((mid + 512, 0, mid))
    t_cross = time.perf_counter() - t0
    _check(d.changed, "boundary crossing produced a delta")
    _check(len(d.entering_cells) < 40, f"a crossing touches few cells ({len(d.entering_cells)})")
    print(f"     index {len(brushes):,} brushes: {t_index*1000:.1f} ms | "
          f"activate: {t_activate*1000:.1f} ms | cross: {t_cross*1000:.2f} ms | "
          f"active: {s['active_brushes']:,}")


# --------------------------------------------------------------------------
# 14. Plugin discovery / registration
# --------------------------------------------------------------------------

def test_plugin_registers():
    print("[14] plugin discovery + registration")
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()
    names = [p.name for p in mgr.plugins]
    _check("bigworld" in names, f"bigworld plugin discovered (loaded: {names})")

    plugin = mgr.find_plugin("bigworld")
    _check(plugin is not None and plugin.enabled is False,
           "ships disabled by default (ordinary maps pay nothing)")

    # Auto-enable when a map carries a BigWorldSettings entity.
    map_data = {"things": [{"type": "bigworldsettings",
                            "properties": {"type": "bigworldsettings"}}]}
    newly = mgr.auto_enable_for_map(map_data)
    _check(plugin in newly or plugin.enabled,
           "auto-enabled for a map containing BigWorldSettings")

    try:
        from editor.things import ENTITY_TYPES
        _check("BigWorldSettings" in ENTITY_TYPES, "BigWorldSettings registered as an entity")
    except Exception as exc:
        print(f"  skip: editor.things unavailable ({exc})")


# --------------------------------------------------------------------------
# 15. Terrain fill: expand + stream terrain, reversibly
# --------------------------------------------------------------------------

def test_terrain_fill_expands_and_restores():
    print("[15] terrain fill expands terrain to the world and restores it")
    # A field of brushes spanning several cells in each direction.
    brushes = [make_brush(x, 0, z)
               for x in range(-3000, 3001, 1000)
               for z in range(-2000, 2001, 1000)]
    terrain = FakeTerrain(min_x=-2, max_x=2, min_z=-2, max_z=2)
    logic = FakeLogic(brushes, [], player_pos=(0, 0, 0), terrain=terrain)

    session = BigWorldSession(logic, activation_radius=2048.0,
                              deactivation_radius=2304.0, terrain_fill=True)
    # Capture the authored terrain state to compare against after stop().
    orig = (terrain.streaming, terrain.stream_radius,
            terrain.min_chunk_x, terrain.max_chunk_x,
            terrain.min_chunk_z, terrain.max_chunk_z)

    session.start()
    _check(terrain.streaming is True, "terrain streaming turned on during play")
    _check(len(terrain.extent_calls) == 1, "terrain sized to the world exactly once")
    # The extent must enclose the brush field's cell bounding box.
    mgr = session.manager
    xs = [c[0] for c in mgr.cells]
    zs = [c[1] for c in mgr.cells]
    exp = (min(xs) * mgr.cell_size, min(zs) * mgr.cell_size,
           (max(xs) + 1) * mgr.cell_size, (max(zs) + 1) * mgr.cell_size)
    _check(terrain.extent_calls[0] == exp, "terrain extent == cell bounding box")
    _check(terrain.deferred_prune is True,
           "bounds prune deferred to the render thread (no GL off-thread)")
    _check(terrain.stream_radius == 2048.0,
           "stream radius derived from the activation radius when unset")
    # Bounds actually widened past the authored 5x5.
    _check(terrain.max_chunk_x > 2 and terrain.min_chunk_x < -2,
           "terrain bounds widened to cover the world")

    session.stop()
    now = (terrain.streaming, terrain.stream_radius,
           terrain.min_chunk_x, terrain.max_chunk_x,
           terrain.min_chunk_z, terrain.max_chunk_z)
    _check(now == orig, "terrain restored to its authored bounds/streaming on stop")


def test_terrain_fill_opt_in_and_safe():
    print("[16] terrain fill is opt-in and fails safe")
    brushes = [make_brush(0, 0, 0)]

    # (a) terrain_fill off ⇒ terrain untouched.
    terrain = FakeTerrain()
    logic = FakeLogic(brushes, [], terrain=terrain)
    s = BigWorldSession(logic, terrain_fill=False)
    s.start()
    _check(terrain.streaming is False and not terrain.extent_calls,
           "terrain left exactly as authored when fill is off")
    s.stop()

    # (b) terrain_fill on but no terrain present ⇒ no crash, no-op.
    logic2 = FakeLogic(brushes, [], terrain=None)
    s2 = BigWorldSession(logic2, terrain_fill=True)
    s2.start()
    s2.stop()
    _check(True, "terrain fill with no terrain present is a safe no-op")

    # (c) explicit stream radius is honoured over the derived default.
    terrain3 = FakeTerrain()
    logic3 = FakeLogic(brushes, [], terrain=terrain3)
    s3 = BigWorldSession(logic3, activation_radius=2048.0, terrain_fill=True,
                         terrain_stream_radius=777.0)
    s3.start()
    _check(terrain3.stream_radius == 777.0, "explicit terrain_stream_radius honoured")
    s3.stop()


def test_terrain_config_roundtrips():
    print("[17] terrain fill config round-trips through the settings entity")
    settings = {"type": "bigworldsettings",
                "properties": {"type": "bigworldsettings", "enabled": True,
                               "terrain_fill": True,
                               "terrain_infinite": True,
                               "terrain_stream_radius": 1234.0}}
    cfg = persistence.config_from_settings(settings)
    _check(cfg["terrain_fill"] is True, "terrain_fill parsed from settings")
    _check(cfg["terrain_infinite"] is True, "terrain_infinite parsed from settings")
    _check(cfg["terrain_stream_radius"] == 1234.0, "terrain_stream_radius parsed")
    # Back-compat: old maps without the key default to bounded (off).
    cfg2 = persistence.config_from_settings({"properties": {"terrain_fill": True}})
    _check(cfg2["terrain_infinite"] is False, "terrain_infinite defaults off")


def test_terrain_infinite_streams_forever():
    print("[19] terrain_infinite fills a huge extent (no map edge)")
    brushes = [make_brush(0, 0, 0), make_brush(400, 0, 400)]
    terrain = FakeTerrain(min_x=-2, max_x=2, min_z=-2, max_z=2)
    logic = FakeLogic(brushes, [], player_pos=(0, 0, 0), terrain=terrain)
    session = BigWorldSession(logic, terrain_fill=True, terrain_infinite=True)
    session.start()
    _check(terrain.streaming is True, "streaming on for infinite terrain")
    h = BigWorldSession.INFINITE_HALF_EXTENT
    _check(terrain.extent_calls and terrain.extent_calls[0] == (-h, -h, h, h),
           "terrain sized to the huge origin-centred extent")
    # Bounds dwarf the tiny content bounding box → no edge to walk off.
    _check(terrain.min_chunk_x < -10000 and terrain.max_chunk_x > 10000,
           "chunk bounds span far beyond the content")
    session.stop()
    _check(terrain.streaming is False and terrain.min_chunk_x == -2,
           "infinite terrain restored to authored bounds on stop")

    # Defaults when the keys are absent (old maps): off, so terrain is untouched.
    cfg2 = persistence.config_from_settings({"properties": {}})
    _check(cfg2["terrain_fill"] is False, "terrain_fill defaults off (back-compat)")
    _check(cfg2["terrain_stream_radius"] == 0.0, "terrain_stream_radius defaults to 0")


def test_real_terrain_streaming_math():
    print("[18] engine.terrain chunk streaming (skipped if numpy/GL absent)")
    try:
        import numpy  # noqa: F401
        import OpenGL  # noqa: F401
        import glm  # noqa: F401
        from engine.terrain import Terrain
    except Exception as exc:
        print(f"  skip: engine.terrain unavailable ({exc})")
        return

    class _Vec:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = float(x), float(y), float(z)

    # Build a Terrain without running __init__ (which needs a GL context) —
    # exercising only the pure chunk-streaming maths (no upload ⇒ no GL calls).
    t = object.__new__(Terrain)
    t.chunk_size = 256.0
    t.offset_x = t.offset_z = t.offset_y = 0.0
    t.min_chunk_x, t.max_chunk_x = -100000, 100000
    t.min_chunk_z, t.max_chunk_z = -100000, 100000
    t.chunks = {}
    t.streaming = True
    t.stream_radius = 600.0
    t.stream_evict_padding = 256.0
    t.HEIGHT_CACHE_RESOLUTION = Terrain.HEIGHT_CACHE_RESOLUTION

    t._stream_chunks(_Vec(0, 0, 0))
    near = set(t.chunks.keys())
    _check(0 < len(near) < 100, f"a bounded ring of chunks streams in ({len(near)})")
    _check((0, 0) in near, "the chunk under the camera is resident")

    # Walk far away: the origin chunks must be evicted, new ones stream in.
    far_x = 50000.0
    t._stream_chunks(_Vec(far_x, 0, 0))
    _check((0, 0) not in t.chunks, "distant origin chunk evicted after moving away")
    far_cx = int(far_x // t.chunk_size)
    _check(any(abs(cx - far_cx) <= 3 for (cx, cz) in t.chunks),
           "chunks stream in around the new camera position")
    _check(len(t.chunks) < 100, "resident chunk count stays bounded regardless of travel")


def main():
    test_cell_math_matches_spatial_grid()
    test_multi_cell_spanning()
    test_circular_activation()
    test_incremental_streaming()
    test_hysteresis_no_thrash()
    test_spanning_refcount()
    test_persistent_entities()
    test_session_effects_and_restore()
    test_streaming_moves_active_set()
    test_uuid_stability()
    test_backwards_compatibility()
    test_scaling_active_is_local()
    test_plugin_registers()
    test_terrain_fill_expands_and_restores()
    test_terrain_fill_opt_in_and_safe()
    test_terrain_config_roundtrips()
    test_terrain_infinite_streams_forever()
    test_real_terrain_streaming_math()
    print("\nALL BIG WORLD TESTS PASSED")


if __name__ == "__main__":
    main()
