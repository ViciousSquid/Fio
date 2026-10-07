"""The Benchmark plugin only calls methods its classes define.

Removing the monitor shims left calls to them behind (``self._stop_monitor()``
and others); ``BenchmarkRunner.__getattr__`` turned each into an
AttributeError, so every benchmark run failed before its first frame and left
the runner wedged. Static, so it covers paths no short run reaches.
"""

import ast
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "benchmark"


def _classes(tree):
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _defined(cls, classes):
    """Names a class defines: methods and attributes assigned anywhere on self."""
    names = set()
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"):
                        names.add(sub.attr)
                    elif isinstance(sub, ast.Name):
                        names.add(sub.id)
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in classes:
            names |= _defined(classes[base.id], classes)
    return names


@pytest.mark.parametrize("module", ["benchmark.py", "benchmark_tests.py"])
def test_every_self_method_call_names_a_defined_member(module):
    tree = ast.parse((PLUGIN / module).read_text(encoding="utf-8"))
    classes = _classes(tree)
    # BenchmarkRunner forwards what it lacks to its components (BenchmarkTests,
    # BenchmarkResults, its dialog), so a member of any plugin class counts.
    every = {}
    for path in sorted(PLUGIN.glob("*.py")):
        every.update(_classes(ast.parse(path.read_text(encoding="utf-8"))))
    pooled = set()
    for cls in every.values():
        pooled |= _defined(cls, every)
    missing = []
    for cls in classes.values():
        for node in ast.walk(cls):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
                    and node.func.attr not in pooled):
                missing.append(f"{cls.name}: self.{node.func.attr}() (line {node.lineno})")
    assert not missing, missing
