"""A Terrain makes no GL call at all, and its renderer-side GL object does
not compile without a current context.

The editor builds a Terrain at map load and for the terrain panel, before a
view's context is current. Since M5 the terrain is GL-free (it publishes
tables; ``engine/renderer/core/terrain_gl.py`` owns the GL), so there is
nothing to guard there. The guard that mattered -- on macOS calling GL
without a context crashes the process, which took the native editor UI job
down -- now protects ``TerrainGL``'s own compile.
"""

import subprocess
import sys

import pytest

pytest.importorskip("OpenGL")

from engine.renderer.core import terrain_gl as gl_module  # noqa: E402

pytestmark = pytest.mark.qt


class _Platform:
    def __init__(self, context):
        self.context = context

    def GetCurrentContext(self):
        return self.context


@pytest.fixture
def mac(monkeypatch):
    from OpenGL import platform as gl_platform
    monkeypatch.setattr(gl_module.sys, "platform", "darwin")

    def use(context):
        monkeypatch.setattr(gl_platform, "PLATFORM", _Platform(context))
    return use


def test_building_and_advancing_a_terrain_loads_no_opengl():
    code = (
        "import sys, glm\n"
        "from engine.terrain import Terrain\n"
        "t = Terrain(); t.advance(glm.vec3(0, 0, 0)); t.publish()\n"
        "assert int(t.table.built.sum()) > 0\n"
        "assert not [m for m in sys.modules if m.split('.')[0] == 'OpenGL']\n")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_no_context_on_macos_skips_the_compile_without_touching_gl(mac, monkeypatch):
    mac(None)
    def explode(*_args, **_kwargs):
        raise AssertionError("GL was called without a current context")
    monkeypatch.setattr(gl_module, "compileShader", explode)
    held = gl_module.TerrainGL()
    held._init_shader()
    assert held.shader_program == 0
    assert not held._failed_programs             # retried at the next draw


def test_a_current_context_on_macos_still_compiles(mac, monkeypatch):
    mac(0x1234)
    calls = []
    def record(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("no context")         # stop before real GL work
    monkeypatch.setattr(gl_module, "compileShader", record)
    gl_module.TerrainGL()._init_shader()
    assert calls


def test_other_platforms_keep_their_own_error_handling(monkeypatch):
    monkeypatch.setattr(gl_module.sys, "platform", "linux")
    assert gl_module._no_current_gl_context() is False
