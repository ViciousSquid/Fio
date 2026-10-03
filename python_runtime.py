"""Interpreter requirements shared by Fio entry points."""

from __future__ import annotations

import platform
import sys
import sysconfig


MINIMUM_PYTHON = (3, 14)


def require_supported_python() -> None:
    """Abort before startup when the interpreter is outside Fio's baseline."""
    if platform.python_implementation() != "CPython" or sys.version_info[:2] < MINIMUM_PYTHON:
        raise SystemExit("Fio requires CPython 3.14 or newer.")

    # Fio relies on normal GIL-enabled CPython threading semantics. Reject
    # free-threaded builds explicitly, including a 3.14t interpreter.
    if sysconfig.get_config_var("Py_GIL_DISABLED") in (1, "1") or not sys._is_gil_enabled():
        raise SystemExit(
            "Fio requires the normal GIL-enabled CPython build; "
            "free-threaded Python is not supported."
        )
