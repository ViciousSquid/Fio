"""Security regressions for bundled .fiopak plugin loading."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from player.plugin_host import PlayerPluginHost


class FakePackage:
    title = "Foreign Game"

    def __init__(self, entries):
        self._entries = dict(entries)

    def namelist(self):
        return list(self._entries)

    def read_asset(self, name):
        return self._entries.get(name)

    def _read_raw(self, name):
        return self._entries.get(name)

    def has_bundled_plugins(self):
        return any(
            str(name).replace("\\", "/").startswith("plugins/")
            for name in self._entries
        )


def test_plugin_path_validation_rejects_traversal_and_absolute_paths():
    safe = [
        "plugins/example/__init__.py",
        "plugins/example/plugin.py",
    ]
    unsafe = [
        "plugins/../evil.py",
        "plugins/foo/../../evil.py",
        r"plugins\..\evil.py",
        "/plugins/evil.py",
        r"C:\plugins\evil.py",
    ]

    for name in safe:
        assert PlayerPluginHost._safe_plugin_entry(name) is not None

    for name in unsafe:
        assert PlayerPluginHost._safe_plugin_entry(name) is None


def test_extract_plugins_writes_only_inside_destination(tmp_path):
    package = FakePackage({
        "plugins/example/__init__.py": b"",
        "plugins/example/plugin.py": b"VALUE = 1",
    })
    host = PlayerPluginHost()
    host._extract_plugins(package, str(tmp_path))

    assert (tmp_path / "plugins/example/plugin.py").read_bytes() == b"VALUE = 1"
    assert not (tmp_path / "evil.py").exists()


def test_extract_plugins_rejects_zip_slip_before_writing(tmp_path):
    package = FakePackage({
        "plugins/../evil.py": b"pwned",
        "plugins/example/plugin.py": b"safe",
    })
    host = PlayerPluginHost()

    with pytest.raises(ValueError, match="unsafe bundled plugin path"):
        host._extract_plugins(package, str(tmp_path))

    assert list(tmp_path.rglob("*")) == []


def test_load_does_not_extract_foreign_plugins_when_user_denies(monkeypatch):
    package = FakePackage({
        "plugins/example/__init__.py": b"",
    })
    host = PlayerPluginHost(lambda _package: False)
    called = []

    def forbidden_extract(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("foreign plugin extraction must not occur after denial")

    monkeypatch.setattr(host, "_extract_plugins", forbidden_extract)

    # The built-in plugin system may still load; the security property under
    # test is that untrusted package bytes are never extracted/imported.
    host.load(package)

    assert called == []
    assert host._extract_root is None

def test_player_prompt_requires_an_explicit_allow(monkeypatch):
    import sys
    import types

    calls = {}

    def fake_messagebox(title, message, **kwargs):
        calls["title"] = title
        calls["message"] = message
        calls["kwargs"] = kwargs
        return 0

    video = types.ModuleType("pygame._sdl2.video")
    video.messagebox = fake_messagebox
    sdl2 = types.ModuleType("pygame._sdl2")
    sdl2.video = video
    pygame = types.ModuleType("pygame")
    pygame._sdl2 = sdl2

    monkeypatch.setitem(sys.modules, "pygame", pygame)
    monkeypatch.setitem(sys.modules, "pygame._sdl2", sdl2)
    monkeypatch.setitem(sys.modules, "pygame._sdl2.video", video)

    from player.app import FioPlayerApp

    package = SimpleNamespace(title="Untrusted package")
    assert FioPlayerApp()._confirm_plugin_execution(package) is True
    assert calls["kwargs"]["buttons"] == ("Allow", "Deny")
    assert calls["kwargs"]["return_button"] == 1
    assert calls["kwargs"]["escape_button"] == 1


def test_player_prompt_denies_when_modal_ui_is_unavailable(monkeypatch):
    import sys

    for name in ("pygame._sdl2.video", "pygame._sdl2", "pygame"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    import player.app as player_app

    class BrokenImport:
        def __getattr__(self, _name):
            raise ImportError("no SDL message box")

    monkeypatch.setitem(sys.modules, "pygame", BrokenImport())
    monkeypatch.setattr(player_app, "is_android", lambda: True)

    package = SimpleNamespace(title="Untrusted package")
    assert player_app.FioPlayerApp()._confirm_plugin_execution(package) is False

def test_stop_does_not_delete_caller_owned_extract_directory(tmp_path):
    root = tmp_path / "caller-owned"
    root.mkdir()

    host = PlayerPluginHost()
    host._extract_root = str(root)
    host._extract_root_owned = False

    host.stop()

    assert root.exists()
