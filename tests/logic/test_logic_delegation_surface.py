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
    """Verify existence and Python-call compatibility of every delegation site."""
    failures = []

    for name, locations in sorted(_logic_calls().items()):
        wrapper = getattr(LogicThread, name, None)
        if wrapper is None or not callable(wrapper):
            failures.append(f"{name}: missing LogicThread wrapper")
            continue

        signature = inspect.signature(wrapper)

        for module_name, line in locations:
            source = ast.parse(
                (ENGINE / module_name).read_text(encoding="utf-8"),
                filename=str(ENGINE / module_name),
            )
            call = next(
                node
                for node in ast.walk(source)
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
                )
            )

            args = [object()] * len(call.args)
            kwargs = {keyword.arg: object() for keyword in call.keywords if keyword.arg}

            try:
                # LogicThread methods are inspected unbound, so account for
                # the implicit self parameter when validating the call site.
                signature.bind(object(), *args, **kwargs)
            except TypeError as exc:
                failures.append(
                    f"{module_name}:{line}: logic.{name}(...): {exc}; "
                    f"wrapper signature is {signature}"
                )

    assert not failures, "LogicThread delegation contract failures:\n" + "\n".join(
        f"  {failure}" for failure in failures
    )
