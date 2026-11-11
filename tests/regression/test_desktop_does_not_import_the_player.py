"""A desktop build leaves the ``player`` package out (Nuitka:
``--nofollow-import-to=player``), so nothing the desktop runs may import it.

The package reader and the export's asset collector are shared with the
player and live in the engine (``engine.fiopak``, ``engine.package_assets``);
the player reaches them through aliases. ``main.py`` may name the player only
inside its guarded FIO_PLAYER / Android branch.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _player_imports(path):
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        found += [(node.lineno, name) for name in names
                  if name == "player" or name.startswith("player.")]
    return found


def test_editor_engine_and_plugins_never_import_the_player():
    offenders = []
    for top in ("editor", "engine", "plugins"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "tests" in path.parts:
                continue
            offenders += ["%s:%d imports %s" % (path.relative_to(ROOT), line, name)
                          for line, name in _player_imports(path)]
    assert not offenders, "\n".join(offenders)


def test_main_names_the_player_only_in_its_guarded_branch():
    assert _player_imports(ROOT / "main.py") == [
        (line, "player.main") for line, name in _player_imports(ROOT / "main.py")]
    assert len(_player_imports(ROOT / "main.py")) == 1


def test_every_release_build_leaves_the_player_out():
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        builds = text.count("--standalone")
        assert text.count("--nofollow-import-to=player") == builds, workflow.name
