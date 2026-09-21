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
    "tests/persistence/test_legacy_map_compat.py": 4,
    "tests/persistence/test_map_round_trip.py": 10,
    "tests/plugins/test_registration_replay.py": 4,
    "tests/editor/test_property_editor_rebuilds.py": 10,
}


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
