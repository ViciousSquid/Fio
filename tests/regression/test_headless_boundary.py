"""The headless boundary, enforced.

Fio's core is meant to be dependency-light: the geometry, the spatial index, the
I/O and logic systems, the plugin API and Big World all run in the standalone
player, where neither the editor package nor PyQt5 nor PyOpenGL exists.  That
property is easy to lose by accident — one convenience import of a Qt-backed
logger is enough, and it was exactly how ``engine.monster_ai`` came to require
PyQt5 (see ``test_fixed_defects``).

So the boundary is asserted rather than assumed.  Each module below is imported
in a fresh interpreter with the heavy packages made unimportable; a module that
has grown a dependency fails here, by name, instead of turning up later as a
crash in the player.
"""

import subprocess
import sys

import pytest

from tests.helpers.paths import REPO_ROOT

pytestmark = pytest.mark.slow

#: Modules that must import with no PyQt5, no PyOpenGL and no editor package.
#: This list *is* the headless boundary; adding to it is a claim, and removing
#: from it should be a deliberate decision with a reason.
DEPENDENCY_LIGHT_MODULES = [
    # Geometry and spatial maths
    "engine.brush_geometry",
    "engine.spatial",
    "engine.constants",
    "engine.physics",
    # Simulation
    "engine.monster_ai",
    "engine.monster_constants",
    "engine.threaded_game_state",
    # The plugin surface
    "plugins.api",
    "plugins.host",
    "plugins.manager",
    "plugins.entitybase",
    # Big World's streaming core
    "plugins.bigworld.cell",
    "plugins.bigworld.manager",
    "plugins.bigworld.persistence",
    "plugins.bigworld.streaming",
    # The editor's Qt-free logic layers
    "editor.component_edit",
    # The player
    "player.render.glmath",
    "player.render.scene",
    "player.fiopak",
]

BLOCKED = ("PyQt5", "OpenGL")

_SCRIPT = """
import sys
sys.path.insert(0, {root!r})


class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {blocked!r}:
            raise ModuleNotFoundError("blocked: " + name)
        return None


sys.meta_path.insert(0, Block())
import {module}
leaked = [m for m in {blocked!r} if m in sys.modules]
assert not leaked, "importing {module} pulled in " + repr(leaked)
print("OK")
"""


def _import_in_isolation(module):
    script = _SCRIPT.format(root=REPO_ROOT, blocked=BLOCKED, module=module)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("module", DEPENDENCY_LIGHT_MODULES)
def test_a_core_module_imports_without_qt_or_opengl(module):
    result = _import_in_isolation(module)
    assert result.returncode == 0 and "OK" in result.stdout, (
        "%s no longer imports without PyQt5/PyOpenGL. Either the dependency is "
        "genuinely needed - in which case move it out of "
        "DEPENDENCY_LIGHT_MODULES and say why - or guard the import the way "
        "engine.logic_thread does.\n%s%s"
        % (module, result.stdout[-600:], result.stderr[-900:]))


def test_the_boundary_check_actually_detects_a_dependency():
    """The guard above is only worth having if it can fail."""
    result = _import_in_isolation("editor.things")
    assert result.returncode != 0, (
        "editor.things is PyQt-backed and should have been blocked; the "
        "isolation harness is not blocking anything")
