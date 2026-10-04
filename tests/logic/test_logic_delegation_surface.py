"""AST audit for the extracted LogicThread delegation surface."""

import ast
import inspect
from pathlib import Path

from engine.logic_thread import LogicThread


ENGINE = Path(__file__).resolve().parents[2] / "engine"
RUNTIME_MODULES = sorted(ENGINE.glob("logic_*.py"))


def _logic_calls():
    calls = {}
    for path in RUNTIME_MODULES:
        if path.name == "logic_thread.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "logic":
                calls.setdefault(node.func.attr, []).append((path.name, node.lineno))
            elif isinstance(owner, ast.Attribute) and owner.attr == "logic":
                calls.setdefault(node.func.attr, []).append((path.name, node.lineno))
    return calls


def _signature_without_self(method):
    return inspect.signature(method)


def test_every_extracted_logic_call_has_a_matching_logic_thread_wrapper():
    missing = []
    mismatched = []

    for name, locations in sorted(_logic_calls().items()):
        wrapper = getattr(LogicThread, name, None)
        if wrapper is None or not callable(wrapper):
            missing.append((name, locations))
            continue

        # Compare the callable's public Python signature. This catches wrapper
        # argument drift while allowing annotations/defaults to evolve.
        signature = _signature_without_self(wrapper)
        source_methods = []
        for module_name, line in locations:
            source_methods.append((module_name, line))

        # The AST audit establishes existence. Signature compatibility is
        # checked conservatively: a wrapper must accept at least the number of
        # positional arguments used by every call site, while *args is naturally
        # accepted.
        params = list(signature.parameters.values())
        positional = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        variadic = any(p.kind == p.VAR_POSITIONAL for p in params)

        for module_name, line in locations:
            source = ast.parse(
                (ENGINE / module_name).read_text(encoding="utf-8"),
                filename=str(ENGINE / module_name),
            )
            for node in ast.walk(source):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == name
                    and (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "logic"
                        or isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "logic"
                    )
                    and node.lineno == line
                ):
                    positional_args = len(node.args)
                    if not variadic and positional_args > len(positional):
                        mismatched.append(
                            (
                                name,
                                module_name,
                                line,
                                positional_args,
                                str(signature),
                            )
                        )

    assert not missing, "Missing LogicThread wrappers:\n" + "\n".join(
        f"  {name}: {locations}" for name, locations in missing
    )
    assert not mismatched, "LogicThread wrapper signature mismatch:\n" + "\n".join(
        f"  {name} at {module}:{line}: call has {argc} positional args; "
        f"wrapper is {signature}"
        for name, module, line, argc, signature in mismatched
    )
