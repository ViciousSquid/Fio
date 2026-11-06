"""Building a Terrain before any GL context exists must not call into GL.

The editor builds one at map load and for the terrain panel, before a view's
context is current. Elsewhere GL then raises and the compile is retried at the
first draw; on macOS calling GL without a context crashes the process, which
took the native macOS editor UI job down with it.
"""

import pytest

pytest.importorskip("OpenGL")

from engine import terrain as terrain_module  # noqa: E402

pytestmark = pytest.mark.qt


class _Platform:
    def __init__(self, context):
        self.context = context

    def GetCurrentContext(self):
        return self.context


@pytest.fixture
def mac(monkeypatch):
    from OpenGL import platform as gl_platform
    monkeypatch.setattr(terrain_module.sys, "platform", "darwin")

    def use(context):
        monkeypatch.setattr(gl_platform, "PLATFORM", _Platform(context))
    return use


def test_no_context_on_macos_skips_the_compile_without_touching_gl(mac, monkeypatch):
    mac(None)
    def explode(*_args, **_kwargs):
        raise AssertionError("GL was called without a current context")
    monkeypatch.setattr(terrain_module, "compileShader", explode)
    terrain = terrain_module.Terrain()
    assert terrain.shader_program == 0
    assert not terrain._failed_programs          # retried at the first draw


def test_a_current_context_on_macos_still_compiles(mac, monkeypatch):
    mac(0x1234)
    calls = []
    def record(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("no context")         # stop before real GL work
    monkeypatch.setattr(terrain_module, "compileShader", record)
    terrain_module.Terrain()
    assert calls


def test_other_platforms_keep_their_own_error_handling(monkeypatch):
    monkeypatch.setattr(terrain_module.sys, "platform", "linux")
    assert terrain_module._no_current_gl_context() is False
