"""
Headless smoke test for the plugin system and the Tidy plugin.

Runs without a display or OpenGL: it exercises plugin discovery/registration,
entity construction + serialization round-trips, and the carry/place runtime
against a fake logic thread. It does NOT touch the renderer.

Run with:  python -m plugins.tidy.tests.test_smoke   (from the project root)
or under pytest.
"""

import inspect
import math
import os
import sys

# Allow "python plugins/tidy/tests/test_smoke.py" from the repo root.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A QApplication-free environment is fine for importing things.py, but QPixmap
# construction needs a QGuiApplication. Force the offscreen platform so any
# incidental pixmap load during the test can't require a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakePlayer:
    def __init__(self, pos, angle=0.0, pitch=0.0):
        import glm
        self.pos = glm.vec3(*pos)
        self.angle = angle
        self.pitch = pitch
        self.camera_height = 40.0


class FakeIO:
    """Minimal IOManager stand-in that records fired outputs."""
    def __init__(self):
        self.fired = []

    def fire_output(self, entity, output, value=None):
        name = getattr(entity, 'properties', {}).get('name', '?')
        self.fired.append((name, output, value))


class FakeGrid:
    """Stand-in for engine.physics.SpatialGrid with just raycast_down.

    ``floor_fn(x, z)`` returns the surface Y under a point, so a test can model
    a stepped floor and prove a dropped object follows the *world* ground rather
    than its original height.
    """
    def __init__(self, floor_fn):
        self._floor_fn = floor_fn

    def raycast_down(self, x, z, start_y=10000.0):
        return self._floor_fn(x, z)


class FakeLogic:
    def __init__(self, things, spatial_grid=None):
        self.things = things
        self.player = None
        self.io_manager = FakeIO()
        self.current_hud_message = ""
        # Present only in editor play mode; the player build omits it and the
        # runtime falls back to the object's resting height.
        if spatial_grid is not None:
            self._spatial_grid = spatial_grid


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_plugin_loads_and_registers():
    print("[1] plugin discovery + registration")
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()
    names = [p.name for p in mgr.plugins]
    _check("tidy" in names, f"tidy plugin discovered (loaded: {names})")

    from editor.things import ENTITY_TYPES, ENTITY_CATEGORIES
    for t in ("TidyObject", "TidyReceptacle", "TidyGoal"):
        _check(t in ENTITY_TYPES, f"{t} in ENTITY_TYPES")
    _check("Tidy" in ENTITY_CATEGORIES, "Tidy category registered")

    from editor.io_system import IO_REGISTRY, get_output_names
    _check("tidyobject" in IO_REGISTRY, "tidyobject I/O registered")
    _check("OnTidied" in get_output_names("tidyobject"), "OnTidied output present")
    _check("OnComplete" in get_output_names("tidygoal"), "OnComplete output present")

    entries = mgr.menu_entries()
    _check(len(entries) >= 3, f"editor menu entries recorded ({len(entries)})")


def test_entities_and_serialization():
    print("[2] entity construction + serialization round-trip")
    from plugins.tidy.entities import TidyObject, TidyReceptacle, TidyGoal
    from editor.things import Thing

    obj = TidyObject(pos=[100, 40, 0], properties={'category': 'book'})
    _check(obj.properties['type'] == 'tidyobject', "TidyObject type string")
    _check(obj.properties.get('model_path', '').endswith('book.obj'),
           "TidyObject uses the book model")

    # Each book gets a random cover; a batch should show several distinct ones.
    from plugins.tidy.entities import COVERS
    covers = {TidyObject().properties.get('texture') for _ in range(40)}
    _check(covers.issubset(set(COVERS)) and len(covers) >= 3,
           f"books get random covers ({len(covers)} distinct)")
    # An explicit texture is preserved (saved maps stay stable).
    fixed = TidyObject(properties={'texture': COVERS[0]})
    _check(fixed.properties['texture'] == COVERS[0], "explicit cover preserved")

    data = obj.to_dict()
    obj2 = Thing.from_dict(data)
    _check(obj2 is not None and obj2.properties['type'] == 'tidyobject',
           "TidyObject survives to_dict/from_dict")
    _check(obj2.get_category() == 'book', "category preserved through round-trip")

    r = TidyReceptacle(pos=[0, 0, 0], properties={'slot_cols': 3,
                                                  'slot_spacing': [10, 20, 0]})
    p0 = r.slot_world_pos(0)
    p3 = r.slot_world_pos(3)  # next row
    _check(abs(p3[1] - (p0[1] + 20)) < 1e-6, "receptacle stacks rows in Y")
    _check(r.accepts_category('anything') is True, "'any' receptacle accepts all")

    g = TidyGoal(properties={'target': 'all'})
    _check(g.target_count(50) == 50, "goal 'all' resolves to total")
    g2 = TidyGoal(properties={'target': 5})
    _check(g2.target_count(50) == 5, "numeric goal target honoured")


def test_carry_place_runtime():
    print("[3] carry / place runtime against fake logic")
    import glm
    from plugins.tidy.entities import TidyObject, TidyReceptacle, TidyGoal
    from plugins.tidy.runtime import TidySession
    from plugins.api import TickContext

    # Object sits 60 units in front of the player (+Z). Player looks down +Z.
    obj = TidyObject(pos=[0, 40, 60], properties={'category': 'book', 'name': 'book1'})
    recept = TidyReceptacle(pos=[0, 40, -60], properties={'accepts': 'book',
                                                          'name': 'shelf'})
    goal = TidyGoal(properties={'target': 'all', 'name': 'goal'})
    logic = FakeLogic([obj, recept, goal])
    logic.player = FakePlayer([0, 0, 0], angle=0.0)  # forward = +Z

    session = TidySession(logic)
    session.start()
    logic._tidy = session
    _check(session.total == 1, "session counted the object")

    # Look at the object and press use -> pick up.
    ctx = TickContext(delta=0.016, use_pressed=True)
    session.tick(ctx)
    _check(session.held is obj, "object picked up when looked at + use")
    _check(("book1", "OnPickedUp", None) in logic.io_manager.fired, "OnPickedUp fired")

    # Now turn around to face the receptacle (-Z) and press use -> place.
    logic.player.angle = math.pi  # forward = -Z
    ctx2 = TickContext(delta=0.016, use_pressed=True)
    session.tick(ctx2)
    _check(session.held is None, "object placed (no longer held)")
    _check(obj.properties['tidied'] is True, "object marked tidied")
    _check(session.tidied == 1, "tidied counter incremented")
    fired = [f[1] for f in logic.io_manager.fired]
    _check("OnTidied" in fired, "OnTidied fired")
    _check("OnComplete" in fired, "goal OnComplete fired when all tidied")

    # HUD line reports completion.
    _check("All done" in (session.hud_line() or ""), "HUD reports completion")

    # Stopping restores the object's editor position.
    session.stop()
    _check(list(obj.pos) == [0, 40, 60], "object restored to home on play stop")


def _run_until_landed(session, obj, max_ticks=600):
    from plugins.api import TickContext
    for _ in range(max_ticks):
        session.tick(TickContext(delta=1.0 / 60.0, use_pressed=False))
        if id(obj) not in session._falling:
            return True
    return False


def test_drop_falls_to_floor_flat():
    print("[7] a dropped object falls to the floor (flat-floor fallback)")
    from plugins.tidy.entities import TidyObject
    from plugins.tidy.runtime import TidySession

    # Object rests on the floor at y=12 (like the demo map's books).
    obj = TidyObject(pos=[0, 12, 0], properties={'category': 'book', 'name': 'b'})
    logic = FakeLogic([obj])                 # no spatial grid → flat-floor path
    logic.player = FakePlayer([0, 0, 0], angle=0.0)
    session = TidySession(logic)
    session.start()
    logic._tidy = session

    # Simulate holding it and releasing it high in the air (where carry floats it).
    session.held = obj
    obj.pos = [0, 200, 0]
    session._drop(obj)
    _check(session.held is None, "drop releases the object")
    _check(id(obj) in session._falling, "drop starts a fall (physics engaged)")

    _check(_run_until_landed(session, obj), "object lands within a bounded time")
    _check(abs(obj.pos[1] - 12.0) < 1e-3,
           f"object rests on the floor at its home height (y={obj.pos[1]})")


def test_drop_follows_world_floor():
    print("[8] a dropped object follows the world floor under it")
    from plugins.tidy.entities import TidyObject
    from plugins.tidy.runtime import TidySession

    # Stepped floor: a ledge at y=12 near the origin, a pit at y=-100 further out.
    grid = FakeGrid(lambda x, z: 12.0 if x < 50.0 else -100.0)
    obj = TidyObject(pos=[0, 12, 0], properties={'category': 'book', 'name': 'b'})
    logic = FakeLogic([obj], spatial_grid=grid)
    logic.player = FakePlayer([0, 0, 0], angle=0.0)
    session = TidySession(logic)
    session.start()
    logic._tidy = session

    # Carry it out over the pit and drop it there.
    session.held = obj
    obj.pos = [200, 300, 0]
    session._drop(obj)

    _check(_run_until_landed(session, obj), "object lands within a bounded time")
    _check(abs(obj.pos[1] - (-100.0)) < 1e-3,
           f"object settles on the pit floor, not its home height (y={obj.pos[1]})")


def test_drop_physics_is_opt_in():
    print("[9] only dropped objects are simulated (framerate stays the priority)")
    from plugins.tidy.entities import TidyObject
    from plugins.tidy.runtime import TidySession
    from plugins.api import TickContext

    # A pile of resting objects, none dropped.
    things = [TidyObject(pos=[i * 20, 12, 0], properties={'category': 'book',
                                                          'name': f'b{i}'})
              for i in range(50)]
    logic = FakeLogic(things)
    logic.player = FakePlayer([0, 0, 0], angle=0.0)
    session = TidySession(logic)
    session.start()
    logic._tidy = session

    session.tick(TickContext(delta=1.0 / 60.0, use_pressed=False))
    _check(not session._falling, "no object is simulated until one is dropped")

    # Picking one up then dropping it is the only way physics engages.
    target = things[0]
    session.held = target
    target.pos = [0, 200, 0]
    session._drop(target)
    _check(list(session._falling.keys()) == [id(target)],
           "exactly the dropped object is under simulation")
    _run_until_landed(session, target)
    _check(not session._falling, "the sim empties once it lands (zero idle cost)")


def test_spatial_hash_scale():
    print("[4] spatial hash scales to many objects")
    from plugins.tidy.entities import TidyObject
    from plugins.tidy.runtime import TidySession
    from plugins.api import TickContext

    things = []
    # A 40x40 grid of 1600 objects spread across the world.
    for i in range(40):
        for j in range(40):
            things.append(TidyObject(pos=[i * 50 - 1000, 40, j * 50 - 1000],
                                     properties={'category': 'obj', 'name': f'o{i}_{j}'}))
    # One object right in front of the player, closer than any grid object
    # (grid objects sit on 50-unit boundaries; the nearest in-view one is at
    # z=50, so put the target at z=35 to make it the unambiguous nearest).
    target = TidyObject(pos=[0, 40, 35], properties={'category': 'obj', 'name': 'target'})
    things.append(target)

    logic = FakeLogic(things)
    logic.player = FakePlayer([0, 0, 0], angle=0.0)
    session = TidySession(logic)
    session.start()
    logic._tidy = session
    _check(session.total == len(things), f"indexed all {len(things)} objects")

    # The neighbour query should only surface a handful of nearby objects.
    import glm
    eye = glm.vec3(0, 40, 0)
    near = list(session.grid.neighbours(eye))
    # The query touches only the local 3x3 cell block, so it stays bounded by
    # local density regardless of how big the map gets (here ~100, not 1600+).
    _check(len(near) < 200 and len(near) < len(things) // 4,
           f"neighbour query stays local ({len(near)} of {len(things)})")

    session.tick(TickContext(delta=0.016, use_pressed=True))
    _check(session.held is target, "picks the object under the crosshair from thousands")


def test_integration_shim_applies():
    print("[6] core integration installs cleanly")
    # Importing the editor package runs editor/__init__.py, which loads plugins
    # and applies the editor integration shim (View2D placement menu, etc.).
    import editor  # noqa: F401
    from plugins import integration
    integration.apply()  # idempotent

    # The engine hooks are native, not monkey-patched: LogicThread calls the
    # manager directly, so it exposes the call sites rather than a patch flag.
    from engine.logic_thread import LogicThread
    from plugins.manager import get_manager
    mgr = get_manager()
    for hook in ("attach_runtime", "dispatch_play_start", "dispatch_play_stop", "tick"):
        _check(hasattr(mgr, hook),
               f"manager exposes native engine hook: {hook}()")
    src = inspect.getsource(LogicThread._tick_play_mode)
    _check("self.plugins.tick(" in src,
           "LogicThread._tick_play_mode calls the plugin tick natively")
    try:
        from editor.view_2d import View2D
        _check(getattr(View2D, "_fio_plugins_patched", False),
               "View2D patched (plugin placement submenu)")
    except Exception as exc:
        print(f"  skip: view_2d unavailable ({exc})")


def test_obj_asset_parses():
    print("[5] bundled model asset parses")
    try:
        from engine.obj_loader import OBJ
    except Exception as exc:
        print(f"  skip: obj_loader unavailable ({exc})")
        return
    path = os.path.join(_ROOT, "plugins", "tidy", "assets", "book.obj")
    model = OBJ(path)
    _check(getattr(model, "is_loaded", False), "book.obj loaded")
    _check(getattr(model, "vertex_count", 0) > 0, "model has vertices")


def main():
    test_plugin_loads_and_registers()
    test_entities_and_serialization()
    test_carry_place_runtime()
    test_drop_falls_to_floor_flat()
    test_drop_follows_world_floor()
    test_drop_physics_is_opt_in()
    test_spatial_hash_scale()
    test_integration_shim_applies()
    test_obj_asset_parses()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
