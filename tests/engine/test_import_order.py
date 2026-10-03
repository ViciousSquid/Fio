"""Every entity module can be the first thing a process imports.

``engine.effect_entity`` subclasses ``editor.things.Thing`` and
``editor.things`` registers ``Effect`` in ``ENTITY_TYPES``.  The registration
used to import ``engine.effect_entity`` eagerly at module level, so a process
whose first import was ``engine.effect_entity`` (a tool, a plugin, a test run
on its own) died with "partially initialized module ... has no attribute
'Effect'".  Each case needs a fresh interpreter, so these run as subprocesses.
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PyQt5", reason="the editor entity tier needs PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.slow]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHECK = """
import {first}
from editor.things import ENTITY_TYPES, Thing
from engine.effect_entity import Effect
assert ENTITY_TYPES['Effect'] is Effect, ENTITY_TYPES.get('Effect')
assert issubclass(Effect, Thing)
"""


@pytest.mark.parametrize("first", ["engine.effect_entity", "engine.prop_entity",
                                   "editor.things", "editor.editor_state"])
def test_entity_types_resolve_whichever_module_is_imported_first(first):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=ROOT)
    result = subprocess.run([sys.executable, "-c", CHECK.format(first=first)],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
