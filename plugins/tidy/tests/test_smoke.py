"""Integration smoke tests for the Tidy plugin on real Fio hosts."""

import os

import glm
import pytest

pytest.importorskip("PyQt5", reason="Tidy integration uses the real editor/logic tier")

from editor.editor_state import EditorState
from engine.prop_entity import Prop
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


@pytest.fixture
def logic(request):
    value = LogicThread(ThreadedGameState(), EditorState())
    request.addfinalizer(value.stop)
    return value


def test_plugin_loads_and_registers():
    from editor.things import ENTITY_TYPES
    from editor.io_system import get_input_names, get_output_names
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

    menu_actions = [
        (label, callback)
        for plugin, label, callback, tooltip in mgr.menu_actions()
        if plugin.name == "tidy"
    ]
    _check(
        any(label == "Load Demo map" for label, _ in menu_actions),
        "Tidy registers Load Demo map in its plugin menu",
    )

    demo = os.path.join(os.path.dirname(__file__), "..", "Tidy_Test.json")
    _check(os.path.isfile(demo), "bundled Tidy demo map exists")

    receptacle_schema = {
        spec.name: spec for spec in mgr.property_schema_for("tidyreceptacle")
    }
    goal_schema = {spec.name: spec for spec in mgr.property_schema_for("tidygoal")}
    _check(
        {
            "accepts",
            "capacity",
            "slot_cols",
            "slot_spacing",
            "slot_offset",
            "reach",
            "disabled",
        }.issubset(receptacle_schema),
        "Tidy Receptacle exposes its gameplay properties",
    )
    _check(
        {"target", "category", "show_hud", "disabled"}.issubset(goal_schema),
        "Tidy Goal exposes its gameplay properties",
    )

    extras = mgr.extra_fields_for("prop")
    _check(
        any(getattr(spec, "name", "") == "tidy_category" for spec in extras),
        "tidy_category registered as a Prop extension",
    )


def test_demo_loader_respects_unsaved_changes(main_window, monkeypatch):
    from plugins.tidy.plugin import PLUGIN

    checked = []
    loaded = []
    real_load = main_window.load_level_file
    allow = {"value": False}

    def check_unsaved():
        checked.append(True)
        return allow["value"]

    def record_load(path):
        loaded.append(path)
        return real_load(path)

    monkeypatch.setattr(main_window, "check_unsaved_changes", check_unsaved)
    monkeypatch.setattr(main_window, "load_level_file", record_load)

    PLUGIN._load_demo_map(main_window)
    _check(checked == [True], "unsaved-change check was shown")
    _check(len(loaded) == 0, "demo did not replace unsaved work")

    allow["value"] = True
    PLUGIN._load_demo_map(main_window)

    _check(checked == [True, True], "existing dialog was still used")
    _check(loaded, "demo loaded through the real MainWindow")
    _check(
        loaded[-1].endswith(os.path.join("plugins", "tidy", "Tidy_Test.json")),
        "demo loaded from the plugin folder",
    )


def test_core_prop_carry_and_tidy_place(logic, monkeypatch):
    from plugins.tidy.entities import TidyGoal, TidyReceptacle
    from plugins.tidy.runtime import TidySession

    prop = Prop(
        pos=[0, 40, 60],
        properties={
            "name": "book1",
            "tidy_category": "book",
            "carry_enabled": True,
            "physics_enabled": False,
        },
    )
    recept = TidyReceptacle(
        pos=[0, 40, -60],
        properties={"name": "shelf", "accepts": "book"},
    )
    goal = TidyGoal(properties={"name": "goal", "target": "all"})

    logic.editor_state.things[:] = [prop, recept, goal]
    logic.editor_state.brushes[:] = []
    logic.player_runtime.player.pos = glm.vec3(0, 40, 0)
    logic.player_runtime.player.angle = 0.0
    logic.player_runtime.player.pitch = 0.0
    logic.world_runtime.build_entity_caches()

    fired = []
    real_fire_output = logic.io_manager.fire_output

    def record_fire(entity, output_name, value=None):
        fired.append((entity.properties.get("name", ""), output_name, value))
        return real_fire_output(entity, output_name, value)

    monkeypatch.setattr(logic.io_manager, "fire_output", record_fire)

    core = logic.prop_runtime
    core.start()

    tidy = TidySession(logic)
    tidy.start()
    logic.prop_runtime.drop_interceptor = tidy.consume_drop

    core.tick(0.016, use_pressed=True)
    _check(core.held is prop, "core PropSession carried the tidyable Prop")
    _check(("book1", "OnCarried", None) in fired, "core OnCarried fired")

    logic.player_runtime.player.angle = glm.pi
    core.tick(0.016, use_pressed=True)

    _check(core.held is None, "core PropSession released the held Prop")
    _check(tidy.tidied == 1, "Tidy progress incremented")
    _check(id(prop) in tidy._tidied_ids, "Tidy owns stowed state")
    _check(("book1", "OnTidied", None) in fired, "Tidy OnTidied fired")
    _check(("shelf", "OnObjectPlaced", "1") in fired, "receptacle OnObjectPlaced fired")
    _check(("goal", "OnProgress", "1/1") in fired, "goal OnProgress fired")
    _check(("goal", "OnComplete", None) in fired, "goal OnComplete fired")

    tidy.reset_object(prop)
    _check(tidy.tidied == 0, "Reset removes Tidy progress")
    _check(list(prop.pos) == [0, 40, 60], "Reset returns Prop to core authored position")
    _check(
        prop.properties["carry_enabled"] is True,
        "Reset restores the Prop carry setting",
    )

    tidy.stop()


def test_receptacle_slots_and_filtering():
    from plugins.tidy.entities import TidyGoal, TidyReceptacle

    recept = TidyReceptacle(
        pos=[0, 0, 0],
        properties={
            "slot_cols": 3,
            "slot_spacing": [10, 20, 0],
            "accepts": "book",
        },
    )
    p0 = recept.slot_world_pos(0)
    p3 = recept.slot_world_pos(3)
    _check(abs(p3[1] - (p0[1] + 20)) < 1e-6, "receptacle stacks rows in Y")
    _check(recept.accepts_category("book") is True, "matching category accepted")
    _check(recept.accepts_category("prop") is False, "non-matching category rejected")

    goal = TidyGoal(properties={"target": "all"})
    _check(goal.target_count(50) == 50, "goal 'all' resolves to total")
    goal_five = TidyGoal(properties={"target": 5})
    _check(goal_five.target_count(50) == 5, "numeric goal target honoured")


def test_tidy_ignores_plain_props(logic):
    from plugins.tidy.runtime import TidySession

    prop = Prop(pos=[0, 0, 0], properties={"name": "ordinary"})
    logic.editor_state.things[:] = [prop]
    session = TidySession(logic)
    session.start()
    try:
        _check(session.total == 0, "plain Props are ignored without tidy_category")
    finally:
        session.stop()
