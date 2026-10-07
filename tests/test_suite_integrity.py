"""The suite checking on itself.

Two defects in this repository's history were invisible for exactly one
reason: a test file existed, so it read as coverage, while never executing.

* ``tests/renderer/test_light_budget.py`` carried literal ``\\n\\n`` escapes
  instead of newlines and was a ``SyntaxError`` -- the whole light-UBO rewrite,
  eleven repair commits long, had no working coverage.
* ``tests/io/test_io_contract.py`` had a dedented function tail and raised
  ``NameError`` at import.

Either one aborts the *entire* pytest run (``Interrupted: N errors during
collection``), so nothing else ran either and the cause was a traceback rather
than a named failure.

These checks turn that class of breakage into an ordinary test failure that
names the file, while the rest of the suite still runs. They are static: every
module is compiled, not imported, so they cost almost nothing and have no
side effects.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The roots pytest.ini lists in `testpaths`.
TEST_ROOTS = ("tests", "plugins", "player")

#: Directories pytest.ini excludes via `norecursedirs`.
SKIP_DIRS = {".git", "build", "dist", "__pycache__", "assets", "maps"}


def _test_modules():
    for root in TEST_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            if SKIP_DIRS.intersection(path.parts):
                continue
            yield path


ALL_MODULES = list(_test_modules())


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def test_the_scan_finds_the_suite():
    """A guard on the guard: an empty scan would make everything below vacuous."""
    assert len(ALL_MODULES) > 50, (
        "only %d test modules found under %s -- the scan is wrong, and every "
        "other check in this file is silently passing on nothing"
        % (len(ALL_MODULES), ", ".join(TEST_ROOTS)))


@pytest.mark.parametrize("path", ALL_MODULES, ids=_rel)
def test_every_test_module_compiles(path):
    """The literal-escape defect, caught as a named failure.

    Compiled rather than imported: this is about the file being valid Python,
    and compiling has no import side effects.
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        pytest.fail(
            "%s is not valid Python, so pytest cannot collect it and every "
            "test in it is silently absent: line %s: %s"
            % (_rel(path), exc.lineno, exc.msg))


def _module_skips_without_pyqt5(tree):
    """True when the module body itself calls ``pytest.importorskip("PyQt5")``."""
    for node in tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "PyQt5"):
            return True
    return False


_DESKTOP_TIERS = ("qt", "gl")


def _module_is_in_a_desktop_tier(tree):
    """True when ``pytestmark`` names the qt or gl tier, or every test does."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark"
                   for t in node.targets):
            continue
        values = (node.value.elts if isinstance(node.value, (ast.List, ast.Tuple))
                  else [node.value])
        if any(isinstance(v, ast.Attribute) and v.attr in _DESKTOP_TIERS
               for v in values):
            return True
    tests = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name.startswith("test")]
    return bool(tests) and all(
        any(isinstance(d, ast.Attribute) and d.attr in _DESKTOP_TIERS
            for d in test.decorator_list)
        for test in tests)


@pytest.mark.parametrize("path", ALL_MODULES, ids=_rel)
def test_a_module_that_needs_pyqt5_is_in_the_qt_tier(path):
    """Skipped without PyQt5 and selected only by ``-m "not qt"``, an unmarked
    module runs in no tier: the fast tier lacks PyQt5 and the Qt tier
    deselects it.  Twenty modules sat there, 112 of their tests broken."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    if _module_skips_without_pyqt5(tree):
        assert _module_is_in_a_desktop_tier(tree), (
            "%s calls pytest.importorskip('PyQt5') at module level but is not "
            "marked `pytestmark = pytest.mark.qt` (or gl), so no test tier "
            "runs it"
            % _rel(path))


@pytest.mark.parametrize("path", ALL_MODULES, ids=_rel)
def test_every_test_module_defines_at_least_one_test(path):
    """A module that parses but declares nothing is coverage that is not there."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                found.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            found.extend(
                child.name for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test")
            )
        elif isinstance(node, ast.Assign):
            # A module may build its cases dynamically (parametrize tables).
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("test"):
                    found.append(target.id)

    assert found, (
        "%s declares no test functions at all. Either it is dead and should "
        "go, or its tests were renamed/indented out of collection."
        % _rel(path))


#: Modules covering behaviour this audit found broken. If one of these stops
#: existing or empties out, the regression it guards has lost its guard.
CRITICAL_MODULES = {
    "tests/logic/test_trigger_filters.py": 10,
    "tests/physics/test_dynamic_bodies.py": 15,
    "tests/io/test_io_contract.py": 10,
    "tests/renderer/test_light_budget.py": 10,
    "tests/renderer/test_render_cull.py": 10,
    "tests/persistence/test_unknown_entity_preservation.py": 1,
    "tests/persistence/test_map_round_trip.py": 10,
    "tests/plugins/test_registration_replay.py": 4,
    "tests/editor/test_property_editor_rebuilds.py": 10,
}


def _owner_double_classes(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    names = {"FakeHost", "FakeEditorWindow", "_MainWindow", "InspectorHost"}
    return [
        (node.name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in names
    ]


EDITOR_TEST_ROOT = ROOT / "tests" / "editor"

# Removed by the 2.6 ownership migration. These names must not reappear in
# production code, including through getattr/hasattr/string-based compatibility
# probes. The authoritative selection is EditorState.selected_objects.
LEGACY_SELECTION_NAMES = frozenset({
    "selected_object",
    "set_selected_object",
    "selected_type",
    "selected_index",
})

PRODUCTION_SOURCE_ROOTS = ("editor", "engine", "plugins", "player")


def _production_python_files():
    """Yield production Python files, excluding tests and generated trees."""
    for root in PRODUCTION_SOURCE_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "tests" in path.parts or SKIP_DIRS.intersection(path.parts):
                continue
            yield path
    main = ROOT / "main.py"
    if main.is_file():
        yield main


def _legacy_selection_uses(tree):
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in LEGACY_SELECTION_NAMES:
            offenders.append(node)
        elif isinstance(node, ast.Name) and node.id in LEGACY_SELECTION_NAMES:
            offenders.append(node)
        elif isinstance(node, ast.Constant) and node.value in LEGACY_SELECTION_NAMES:
            offenders.append(node)
    return offenders


def test_production_code_has_no_removed_selection_api():
    """The pre-2.6 singular selection surface is a deleted API, not fallback."""
    offenders = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
        for node in _legacy_selection_uses(tree):
            offenders.append("%s:%d" % (_rel(path), node.lineno))
    assert not offenders, (
        "removed 2.6 selection API has returned to production code; use the "
        "authoritative EditorState.selected_objects path instead:\n  "
        + "\n  ".join(offenders)
    )


def test_logic_thread_does_not_reintroduce_authoritative_world_aliases():
    """LogicThread must orchestrate EditorState, never own mirror lists."""
    path = ROOT / "engine" / "logic_thread.py"
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    logic_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LogicThread"
    )
    offenders = []
    for node in ast.walk(logic_class):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"brushes", "things"}:
            offenders.append("%s:%d (method/property %s)" % (_rel(path), node.lineno, node.name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self" and target.attr in {"brushes", "things"}:
                        offenders.append("%s:%d (self.%s)" % (_rel(path), node.lineno, target.attr))
    assert not offenders, (
        "LogicThread has regained world-state ownership aliases; EditorState is "
        "the sole authoritative owner:\n  " + "\n  ".join(offenders)
    )


MACHINERY_TEST_ROOTS = (
    "tests",
    "plugins/bigworld/tests",
    "plugins/tidy/tests",
)

#: These names have appeared as substitutes for production owners in behavioural
#: tests. They belong in narrow leaf/unit tests only, never in the machinery tiers.
MACHINERY_OWNER_DOUBLES = {
    "FakeHost", "FakeEditorWindow", "_MainWindow", "InspectorHost",
    "HostStub", "SaveStub", "PlayStub", "FakePhysics", "FakeGL",
    "FakeRenderer", "_FakeRenderer", "FakeLogicThread",
    "FakeLogic", "_FakeLogic", "FakeIO", "_FakeIO",
}


def _simple_namespace_aliases(tree):
    """Resolve common SimpleNamespace import aliases without importing tests."""
    aliases = {"SimpleNamespace", "types.SimpleNamespace"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "types":
            for alias in node.names:
                if alias.name == "SimpleNamespace":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "types":
                    prefix = alias.asname or "types"
                    aliases.add(prefix + ".SimpleNamespace")
    return aliases


def _assigned_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_machinery_tests_do_not_use_namespace_production_owners():
    """A fake namespace must not stand in for a production subsystem owner.

    Resolve aliases and attribute assignments too, so qualified imports and
    host.view_3d = SimpleNamespace(...) cannot evade the guard.
    """
    owner_names = {
        "logic", "host", "renderer", "physics", "ai", "main_window",
        "window", "view", "view_3d", "logic_thread", "game_state",
        "plugins", "editor", "state",
    }
    offenders = []
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            tree = ast.parse(
                path.read_text(encoding="utf-8", errors="replace"), str(path)
            )
            namespace_names = _simple_namespace_aliases(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, ast.Call):
                    continue
                if _dotted_name(node.value.func) not in namespace_names:
                    continue
                for target in node.targets:
                    assigned = _assigned_name(target)
                    if assigned in owner_names:
                        offenders.append("%s:%d (%s=SimpleNamespace)" % (
                            _rel(path), node.lineno, assigned))
    assert not offenders, (
        "machinery tests construct production owners with SimpleNamespace; use the real "
        "Fio owner fixture/object instead:\n  " + "\n  ".join(offenders)
    )



PRODUCTION_OWNER_NAMES = {
    "LogicThread",
    "MainWindow",
    "QtGameView",
    "ForwardRenderer",
    "RendererCore",
    "Terrain",
    "PhysicsWorld",
    "PropSession",
    "PluginHost",
    "DiskStreamingSession",
    "TidySession",
}


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return "%s.%s" % (prefix, node.attr) if prefix else node.attr
    return None


def test_machinery_tests_do_not_use_object_new_as_a_constructor_bypass():
    """Never allocate an object with object.__new__ in a machinery test.

    object.__new__ deliberately skips the class constructor. That is exactly
    how a behavioural test can appear to exercise production code while
    silently omitting the owner's real initialization.
    """
    offenders = []
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _dotted_name(node.func) != "object.__new__":
                    continue
                offenders.append("%s:%d" % (_rel(path), node.lineno))
    assert not offenders, (
        "machinery tests bypass constructors with object.__new__; use the real "
        "production constructor instead:\\n  " + "\\n  ".join(offenders)
    )


def test_machinery_tests_do_not_construct_production_owners_by_bypassing_init():
    """Production owners must execute their constructors in machinery tests."""
    offenders = []
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = _dotted_name(node.func)
                if not (func_name == "__new__" or (func_name is not None and func_name.endswith(".__new__"))):
                    continue
                if not node.args:
                    continue
                owner = _dotted_name(node.args[0])
                if owner in PRODUCTION_OWNER_NAMES:
                    offenders.append("%s:%d (%s)" % (
                        _rel(path), node.lineno, func_name + "(" + owner + ")"))
    assert not offenders, (
        "machinery tests bypass production-owner constructors; exercise the real "
        "constructor instead:\n  " + "\n  ".join(offenders)
    )


def test_machinery_tests_do_not_patch_production_owner_classes():
    """Do not replace production-owner methods before the code under test runs."""
    offenders = []
    patch_names = {"monkeypatch.setattr", "mock.patch.object", "patch.object"}
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func_name = _dotted_name(node.func)
                if func_name not in patch_names and not (func_name is not None and func_name.endswith(".patch.object")):
                    continue
                target = _dotted_name(node.args[0])
                if target in PRODUCTION_OWNER_NAMES:
                    offenders.append("%s:%d (%s)" % (_rel(path), node.lineno, func_name))
    assert not offenders, (
        "machinery tests patch production-owner classes instead of exercising their "
        "methods directly:\n  " + "\n  ".join(offenders)
    )


def test_machinery_tests_do_not_import_owner_fakes():
    """Owner doubles must not leak back into behavioural machinery suites.

    Small instrumentation helpers such as OrderRecordingDict are legitimate;
    substitutes for LogicThread, MainWindow, ForwardRenderer, AI, or IO ownership
    are not. This catches accidental reintroduction even when the fake class
    lives in tests/helpers and therefore evades the local-class check above.
    """
    forbidden = {
        "FakeLogicThread", "FakeLogic", "FakeGameState", "FakePlayer",
        "FakeIO", "FakeRenderer", "FakePhysics", "FakeHost",
    }
    allowed = {"tests/threading/test_publication_order.py"}
    offenders = []
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            rel = _rel(path)
            if rel in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [alias.asname or alias.name for alias in node.names]
                elif isinstance(node, ast.Import):
                    names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
                for name in names:
                    if name in forbidden or name.startswith("FakeLogic"):
                        offenders.append("%s:%d (%s)" % (rel, node.lineno, name))
    assert not offenders, (
        "machinery tests import production-owner fakes; exercise the real owner "
        "instead:\\n  " + "\\n  ".join(offenders)
    )


def test_machinery_tests_do_not_replace_production_owners():
    """Behavioural machinery tests must call the real subsystem owners."""
    offenders = []
    for root_name in MACHINERY_TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in MACHINERY_OWNER_DOUBLES:
                    offenders.append("%s:%d (%s)" % (_rel(path), node.lineno, node.name))
    assert not offenders, (
        "machinery tests contain production-owner doubles; use the real Fio owner "
        "fixture/object instead:\n  " + "\n  ".join(offenders)
    )



def test_editor_tests_do_not_reintroduce_fake_owner_windows():
    """Editor tests must cross the real MainWindow ownership boundary.

    Narrow leaf doubles (painters, dialogs, file selectors, etc.) are fine;
    these names specifically identify an object pretending to own editor
    state while production methods are exercised against it.
    """
    offenders = []
    for path in sorted(EDITOR_TEST_ROOT.rglob("test_*.py")):
        for name, line in _owner_double_classes(path):
            offenders.append("%s:%d (%s)" % (_rel(path), line, name))

    assert not offenders, (
        "editor tests contain fake owner windows; drive the real MainWindow "
        "fixture instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rel,minimum", sorted(CRITICAL_MODULES.items()))
def test_a_module_guarding_a_known_regression_still_has_tests(rel, minimum):
    path = ROOT / rel
    assert path.is_file(), (
        "%s has gone. It guards a regression this audit fixed; removing it "
        "removes the guard." % rel)

    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    count = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )
    assert count >= minimum, (
        "%s declares %d tests, expected at least %d -- coverage for a known "
        "regression has been removed." % (rel, count, minimum))
