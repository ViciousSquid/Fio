"""
Headless smoke tests for the refactored Tidy plugin.

Tidyable objects are core Props. Tidy owns only its metadata, receptacles,
placement, progress and goals; pickup/carry/drop are exercised through the
engine PropSession.
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeIO:
    def __init__(self):
        self.fired = []

    def fire_output(self, entity, output_name, value=None):
        self.fired.append((entity.properties.get("name", ""), output_name, value))


class FakePlayer:
    def __init__(self, pos, angle=0.0, pitch=0.0):
        self.pos = list(pos)
        self.angle = angle
        self.pitch = pitch
        self.camera_height = 0.0


class FakeLogic:
    def __init__(self, things):
        self.things = things
        self.io_manager = FakeIO()
        self.current_hud_message = ""
        self.player = FakePlayer([0, 40, 0])
        self._props = None
        self._physics_world = None


def test_plugin_loads_and_registers():
    print("[1] plugin loads and extends core Prop")
    try:
        from editor.things import ENTITY_TYPES
        from editor.io_system import get_input_names, get_output_names
    except Exception as exc:
        print(f"  skip: editor tier unavailable ({exc})")
        return

    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()
    names = [p.name for p in mgr.plugins]
    _check("tidy" in names, f"tidy plugin discovered ({names})")

    _check("TidyObject" not in ENTITY_TYPES, "TidyObject removed from editor entity types")
    _check("TidyReceptacle" in ENTITY_TYPES, "TidyReceptacle registered")
    _check("TidyGoal" in ENTITY_TYPES, "TidyGoal registered")
    _check("Reset" in get_input_names("prop"), "Tidy Reset added to core Prop I/O")
    _check("OnTidied" in get_output_names("prop"), "OnTidied added to core Prop I/O")

    menu_actions = [(label, callback) for plugin, label, callback, tooltip
                    in mgr.menu_actions() if plugin.name == "tidy"]
    _check(any(label == "Load Demo map" for label, _ in menu_actions),
           "Tidy registers Load Demo map in its plugin menu")

    demo = os.path.join(os.path.dirname(__file__), "..", "Tidy_Test.json")
    _check(os.path.isfile(demo), "bundled Tidy demo map exists")

    extras = mgr.extra_fields_for("prop")
    _check(any(getattr(spec, "name", "") == "tidy_category" for spec in extras),
           "tidy_category registered as a Prop extension")


def test_core_prop_pickup_and_tidy_place():
    print("[3] core PropSession handles pickup/drop while Tidy intercepts placement")
    from engine.prop_runtime import PropSession
    from plugins.entitybase import Prop
    from plugins.tidy.entities import TidyReceptacle, TidyGoal
    from plugins.tidy.runtime import TidySession

    prop = Prop(
        pos=[0, 40, 60],
        properties={
            "name": "book1",
            "tidy_category": "book",
            "pickup_enabled": True,
            "physics_enabled": False,
        },
    )
    recept = TidyReceptacle(
        pos=[0, 40, -60],
        properties={"name": "shelf", "accepts": "book"},
    )
    goal = TidyGoal(properties={"name": "goal", "target": "all"})

    logic = FakeLogic([prop, recept, goal])
    logic.player.angle = 0.0

    core = PropSession(logic)
    logic._props = core
    core.start()

    tidy = TidySession(logic)
    tidy.start()
    logic._prop_drop_interceptor = tidy.consume_drop

    core.tick(0.016, use_pressed=True)
    _check(core.held is prop, "core PropSession picked up the tidyable Prop")
    _check(("book1", "OnPickedUp", None) in logic.io_manager.fired,
           "core OnPickedUp fired")

    logic.player.angle = 3.141592653589793
    core.tick(0.016, use_pressed=True)

    _check(core.held is None, "core PropSession released the held Prop")
    _check(tidy.tidied == 1, "Tidy progress incremented")
    _check(id(prop) in tidy._tidied_ids, "Tidy owns stowed state")
    _check(("book1", "OnTidied", None) in logic.io_manager.fired,
           "Tidy OnTidied fired")
    _check(("shelf", "OnObjectPlaced", "1") in logic.io_manager.fired,
           "receptacle OnObjectPlaced fired")
    _check(("goal", "OnProgress", "1/1") in logic.io_manager.fired,
           "goal OnProgress fired")
    _check(("goal", "OnComplete", None) in logic.io_manager.fired,
           "goal OnComplete fired")

    tidy.reset_object(prop)
    _check(tidy.tidied == 0, "Reset removes Tidy progress")
    _check(prop.pos == [0, 40, 60], "Reset returns Prop to core authored position")
    _check(prop.properties["pickup_enabled"] is True,
           "Reset restores the Prop pickup setting")

    core.stop()
    tidy.stop()


def test_receptacle_slots_and_filtering():
    print("[4] receptacle slots and category filtering remain intact")
    from plugins.tidy.entities import TidyReceptacle, TidyGoal

    r = TidyReceptacle(
        pos=[0, 0, 0],
        properties={"slot_cols": 3, "slot_spacing": [10, 20, 0], "accepts": "book"},
    )
    p0 = r.slot_world_pos(0)
    p3 = r.slot_world_pos(3)
    _check(abs(p3[1] - (p0[1] + 20)) < 1e-6, "receptacle stacks rows in Y")
    _check(r.accepts_category("book") is True, "matching category accepted")
    _check(r.accepts_category("prop") is False, "non-matching category rejected")

    g = TidyGoal(properties={"target": "all"})
    _check(g.target_count(50) == 50, "goal 'all' resolves to total")
    g2 = TidyGoal(properties={"target": 5})
    _check(g2.target_count(50) == 5, "numeric goal target honoured")


def test_tidy_ignores_plain_props():
    print("[5] ordinary core Props are not silently converted into Tidy objects")
    from plugins.entitybase import Prop
    from plugins.tidy.runtime import TidySession
    prop = Prop(pos=[0, 0, 0], properties={"name": "ordinary"})
    logic = FakeLogic([prop])
    session = TidySession(logic)
    session.start()
    _check(session.total == 0, "plain Props are ignored without tidy_category")


def main():
    test_plugin_loads_and_registers()
    test_core_prop_pickup_and_tidy_place()
    test_receptacle_slots_and_filtering()
    test_tidy_ignores_plain_props()
    print("\nALL TIDY SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
