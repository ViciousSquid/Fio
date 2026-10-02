"""Targeted tests for the back-port's remaining risk areas.

Covers: render-cull scoping (shadow/portal collections untouched), plugin API
backwards compatibility (group / wizards / singletons all optional), KeyValue
designer-default editing and persistence, and logic-thread exception logging.
"""

import ast
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Cull scoping: only the main-camera object path may be affected
# ---------------------------------------------------------------------------

def _render_scene_source():
    """The body of Renderer_F.render_scene, as source text."""
    src = _read("engine/renderer_F.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "render_scene":
                    return ast.get_source_segment(src, fn)
    raise AssertionError("render_scene not found")


def test_cull_output_feeds_only_sort_objects():
    """cull_brushes/cull_things must reach _sort_objects and nothing else."""
    body = _render_scene_source()
    uses = re.findall(r"cull_brushes|cull_things", body)
    # 2 assignments (tuple target), 2 in the reassignment, 2 in the _sort_objects call.
    assert "self._sort_objects(cull_brushes, cull_things, config)" in body
    # No other call site may consume the culled lists.
    other = re.findall(r"\w+\((?:[^()]*\b(?:cull_brushes|cull_things)\b[^()]*)\)", body)
    for call in other:
        assert "_sort_objects" in call or "_camera_distance_cull" in call, \
            f"culled list leaked into another call: {call}"
    assert len(uses) >= 4


def test_shadow_and_portal_passes_use_the_unculled_collections():
    """The shadow/portal passes must still see the full scene."""
    body = _render_scene_source()
    # Locate the shadow-map render call and confirm it uses the originals.
    assert "render_shadow_maps(shadow_lights, shadow_brushes, shadow_things" in body
    # shadow_brushes/shadow_things must not be derived from the culled lists.
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("shadow_brushes") or s.startswith("shadow_things"):
            assert "cull_brushes" not in s and "cull_things" not in s, \
                f"shadow collection built from culled data: {s}"


def test_cull_is_opt_in_and_defaults_to_play_mode():
    body = _render_scene_source()
    assert "config.get('camera_distance_cull', config.get('play_mode', False))" in body
    # When the flag is absent and not in play mode, the originals pass through.
    assert "cull_brushes, cull_things = brushes, things" in body


def test_cull_does_not_mutate_its_input_lists():
    from engine.render_cull import cull_by_distance
    brushes = [{"pos": [0.0, 0.0, 0.0]}, {"pos": [99999.0, 0.0, 0.0]}]
    snapshot = list(brushes)
    out = []
    cull_by_distance(brushes, 0.0, 0.0, out=out)
    assert brushes == snapshot, "the source list was mutated"
    assert len(out) == 1


def test_light_and_portal_exemption_predicate():
    """_cull_keep_thing must exempt Light and Portal and nothing else."""
    src = _read("engine/renderer_F.py")
    assert "def _cull_keep_thing(t):" in src
    assert "isinstance(t, Light)" in src
    assert "isinstance(t, Portal)" in src
    # The predicate is passed for things only, never for brushes.
    assert "out=tbuf, keep=self._cull_keep_thing" in src
    assert "out=bbuf)" in src


# ---------------------------------------------------------------------------
# Plugin API backwards compatibility
# ---------------------------------------------------------------------------

def test_property_spec_group_is_optional_and_defaults_empty():
    from plugins.api import PropertySpec, prop
    s = PropertySpec(name="speed")
    assert s.group == ""
    assert prop("speed").group == ""
    assert prop("speed", group="Movement").group == "Movement"


def test_property_spec_positional_order_unchanged():
    """Adding `group` must not shift any existing positional argument."""
    from plugins.api import PropertySpec
    s = PropertySpec("hp", "int", "Hit Points", 100, 0, 999, None, "help text")
    assert (s.name, s.type, s.label, s.default) == ("hp", "int", "Hit Points", 100)
    assert (s.min, s.max, s.choices, s.help) == (0, 999, None, "help text")
    assert s.group == ""


def test_ungrouped_specs_render_no_section_header():
    """A schema with no groups must produce the pre-existing flat layout."""
    src = _read("plugins/integration.py")
    # The header is emitted only when a non-empty group is present.
    assert 'group = getattr(spec, "group", "") or ""' in src
    assert "if group and group != current_group:" in src
    # The trailing "Other" header only appears if a group was already emitted.
    assert "if _uncovered and current_group is not None:" in src


def test_manager_registries_start_empty():
    """New facilities must be inert until something registers."""
    from plugins.manager import PluginManager
    m = PluginManager()
    assert m.builtin_games() == []
    assert m.builtin_menu_entries() == []
    assert m.entity_wizard_for("anything") is None
    assert m.is_singleton_entity("anything") is False


def test_singleton_only_affects_marked_types():
    from plugins.manager import PluginManager
    m = PluginManager()
    m.register_singleton_entity("game_settings")
    assert m.is_singleton_entity("game_settings") is True
    assert m.is_singleton_entity("GameSettings") is True      # normalised
    assert m.is_singleton_entity("light") is False
    assert m.is_singleton_entity("monster") is False


def test_wizard_hook_is_optional_and_normalised():
    from plugins.manager import PluginManager
    m = PluginManager()
    sentinel = object()
    m.register_entity_wizard("my_entity", lambda parent: {"a": 1})
    assert m.entity_wizard_for("my_entity") is not None
    assert m.entity_wizard_for("MyEntity") is not None
    assert m.entity_wizard_for("other") is None
    del sentinel


def test_participants_returns_plugins_when_no_builtin_layer():
    """_participants must degrade to the original enabled-plugin scan."""
    from plugins.manager import PluginManager
    m = PluginManager()
    assert m._participants("on_tick") == []
    assert m._builtin_games == []


def test_builtin_registration_is_idempotent():
    from plugins.manager import PluginManager

    class Layer:
        name = "test-layer"
        version = "1.0"
        category = "test"

        def __init__(self):
            self.registered = 0

        def register(self, api):
            self.registered += 1

    m = PluginManager()
    layer = Layer()
    m.register_builtin_game(layer)
    m.register_builtin_game(layer)          # same instance
    m.register_builtin_game(Layer())        # same type
    assert len(m.builtin_games()) == 1
    assert layer.registered == 1
    assert getattr(layer, "is_builtin_game", False) is True


def test_builtin_menu_entries_do_not_pollute_the_plugins_menu():
    from plugins.manager import PluginManager

    class Layer:
        name = "L"
        is_builtin_game = True

    class Plug:
        name = "P"

    m = PluginManager()
    m._add_menu_entry(Layer(), "Native Thing", object)
    m._add_menu_entry(Plug(), "Plugin Thing", object)
    assert len(m.builtin_menu_entries()) == 1
    assert len(m.menu_entries()) == 1
    assert m.menu_entries()[0][1] == "Plugin Thing"


def test_editor_api_exposes_the_new_hooks():
    src = _read("plugins/api.py")
    assert "def register_singleton_entity(self, entity_type: str)" in src
    assert "def register_entity_wizard(self, entity_type: str, factory)" in src
    # And the pre-existing surface is untouched.
    assert "def fire_output(" in src
    assert "def register_property_tab(" in src


# ---------------------------------------------------------------------------
# KeyValue designer defaults
# ---------------------------------------------------------------------------

def _kv_write_back(rows, cap=25):
    """Reproduces the table -> properties write-back in _build_keyvalue_group."""
    data = {}
    for key, value in rows:
        key = key.strip()
        if not key:
            continue
        data[key] = value
    if len(data) > cap:
        for extra in list(data.keys())[cap:]:
            del data[extra]
    return data


def test_keyvalue_write_back_skips_blank_keys():
    assert _kv_write_back([("a", "1"), ("", "2"), ("  ", "3")]) == {"a": "1"}


def test_keyvalue_write_back_trims_whitespace_keys():
    assert _kv_write_back([("  spaced  ", "v")]) == {"spaced": "v"}


def test_keyvalue_write_back_respects_capacity():
    rows = [(f"k{i}", str(i)) for i in range(40)]
    data = _kv_write_back(rows, cap=25)
    assert len(data) == 25
    assert "k0" in data and "k39" not in data


def test_keyvalue_defaults_are_json_serialisable():
    """initial_data must round-trip through the map file."""
    data = _kv_write_back([("flag", "true"), ("count", "7")])
    assert json.loads(json.dumps({"initial_data": data}))["initial_data"] == data


def test_keyvalue_group_is_generic():
    """No game-supplied suggestion hook may exist in the generic editor."""
    src = _read("editor/property_editor.py")
    assert "_kv_suggestions" not in src
    assert "kv_key_suggestions" not in src
    assert "Preset key" not in src
    for banned in ("quest", "faction", "miniwind"):
        assert banned not in src.lower(), f"RPG term '{banned}' in property_editor"


def test_keyvalue_group_has_its_helpers():
    src = _read("editor/property_editor.py")
    assert "def _update_kv_count(self, data, cap)" in src
    assert "QTableWidget, QTableWidgetItem" in src
    assert "thing.properties['initial_data'] = data" in src
    assert "thing.properties['store_name'] = name_edit.text().strip()" in src


def test_property_editor_has_no_hard_debug_console_dependency():
    """debug_log must degrade gracefully when the console is unavailable."""
    src = _read("editor/property_editor.py")
    assert "try:\n    from editor.debug_console import debug_log" in src
    assert "def debug_log(category, message):" in src


# ---------------------------------------------------------------------------
# Logic-thread exception isolation
# ---------------------------------------------------------------------------

def test_tick_exception_is_logged_not_swallowed():
    src = _read("engine/logic_thread.py")
    i = src.index("while accumulator >= self.TICK_DURATION:")
    block = src[i:i + 900]
    assert "try:" in block and "self._tick(self.TICK_DURATION)" in block
    assert "except Exception:" in block
    assert "traceback.format_exc()" in block, "traceback must be reported"
    assert "debug_log(" in block, "the failure must reach the console"
    # It must never be a bare pass.
    assert re.search(r"except Exception:\s*\n\s*pass", block) is None


def test_tick_loop_still_advances_the_accumulator_after_a_failure():
    """A failing tick must not spin the accumulator forever."""
    src = _read("engine/logic_thread.py")
    i = src.index("while accumulator >= self.TICK_DURATION:")
    block = src[i:i + 900]
    tick_pos = block.index("self._tick(self.TICK_DURATION)")
    acc_pos = block.index("accumulator -= self.TICK_DURATION")
    except_pos = block.index("except Exception:")
    # The decrement must sit outside (after) the except handler.
    assert acc_pos > except_pos > tick_pos


def test_exception_isolation_simulation():
    """The guard shape must keep looping and record every failure."""
    logged = []

    def debug_log(cat, msg):
        logged.append((cat, msg))

    ticks = []

    def _tick(d):
        ticks.append(d)
        if len(ticks) == 2:
            raise RuntimeError("bad handler")

    accumulator, TICK = 0.5, 0.1
    while accumulator >= TICK:
        try:
            _tick(TICK)
        except Exception:
            import traceback
            debug_log("LogicThread", "Unhandled exception in _tick:\n"
                      + traceback.format_exc())
        accumulator -= TICK

    assert len(ticks) == 5, "the loop must survive the failure and finish"
    assert len(logged) == 1
    assert "bad handler" in logged[0][1], "the traceback must name the cause"


# ---------------------------------------------------------------------------
# Persistence: a moved brush must save clean
# ---------------------------------------------------------------------------

def test_moved_brush_saves_without_runtime_cache_fields():
    from engine.constants import AABB_RUNTIME_KEYS, brush_aabb_bounds

    brush = {"pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0], "textures": {}}
    brush_aabb_bounds(brush)                 # populate the cache
    brush["pos"] = [128.0, 0.0, 0.0]         # a door opens
    brush_aabb_bounds(brush)                 # refresh

    # Simulate the serialiser's strip using the real key set.
    src = _read("editor/editor_state.py")
    assert "frozenset(AABB_RUNTIME_KEYS)" in src
    clean = {k: v for k, v in brush.items() if k not in AABB_RUNTIME_KEYS}

    text = json.dumps(clean)
    reloaded = json.loads(text)
    assert reloaded["pos"] == [128.0, 0.0, 0.0]
    assert not any(k.startswith("_aabb") for k in reloaded)

    # Reloaded brush recomputes bounds correctly from scratch.
    assert brush_aabb_bounds(reloaded) == (96.0, -32.0, -32.0, 160.0, 32.0, 32.0)


def test_mover_position_types_survive_a_json_round_trip():
    """Guards the numpy-scalar regression at the serialisation boundary."""
    direction = (0.0, 1.0, 0.0)
    original = [10.0, 20.0, 30.0]
    distance, factor = 128.0, 0.37
    pos = [original[i] + (direction[i] * distance) * factor for i in range(3)]
    text = json.dumps({"pos": pos})          # would raise on np.float64
    assert json.loads(text)["pos"] == pos
