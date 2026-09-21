"""A `.fiopak` carries the plugins its maps need, and the importer loads them.

The documented contract is that packages are self-contained: a package built on
a machine with the Tidy plugin plays on a machine without it, because the
plugin's code travels inside the archive. `plugins.packaging.augment_fiopak`
writes it in; this is the host side that reads it back out.

It had never worked. `load_package_plugins` existed with zero callers, and
would not have loaded anything if called: `discover_and_load` returns
immediately once the manager has loaded, which it always has by the time the
editor opens a package.
"""

import json
import os
import sys
import textwrap

import pytest

from plugins.manager import PluginManager           # noqa: E402
from plugins.packaging import load_package_plugins  # noqa: E402


def make_extracted_package(root, plugin_name, entity_type="widget"):
    """An extracted .fiopak carrying one plugin of its own."""
    os.makedirs(os.path.join(root, "maps"), exist_ok=True)
    with open(os.path.join(root, "metadata.json"), "w") as f:
        json.dump({"title": "Demo", "plugins": [plugin_name],
                   "requires_plugins": True}, f)

    pkg = os.path.join(root, "plugins", plugin_name)
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(root, "plugins", "__init__.py"), "a").close()
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write(textwrap.dedent('''
            from plugins.api import FioPlugin

            class _Packaged(FioPlugin):
                name = "%s"
                version = "1.0.0"
                description = "Shipped inside a .fiopak"
                category = "Packaged"
                enabled = True

                def register(self, api):
                    pass

            PLUGIN = _Packaged()
        ''' % plugin_name))
    return root


@pytest.fixture
def clean_sys_path():
    before = list(sys.path)
    before_modules = set(sys.modules)
    yield
    sys.path[:] = before
    for name in set(sys.modules) - before_modules:
        if name.startswith("plugins."):
            sys.modules.pop(name, None)


def test_a_packages_plugins_are_loaded_into_a_running_session(tmp_path, clean_sys_path, monkeypatch):
    """The regression: a live manager has already loaded, and must still look."""
    root = make_extracted_package(str(tmp_path / "pak"), "packagedemo")

    manager = PluginManager()
    manager.discover_and_load()          # the editor's own startup load
    assert manager._loaded, "fixture wrong: the manager should be loaded already"
    before = {p.name for p in manager.plugins}
    assert "packagedemo" not in before

    monkeypatch.setattr("plugins.manager.get_manager", lambda: manager)
    added = load_package_plugins(root)

    assert added == ["packagedemo"], (
        "the package's own plugin was not loaded: %r" % (added,))
    assert "packagedemo" in {p.name for p in manager.plugins}


def test_a_plugin_the_session_already_has_is_not_swapped(tmp_path, clean_sys_path, monkeypatch):
    """First one loaded wins; live entity classes are never hot-swapped."""
    manager = PluginManager()
    manager.discover_and_load()
    installed = [p.name for p in manager.plugins]
    if not installed:
        pytest.skip("no plugins installed in this build to collide with")

    root = make_extracted_package(str(tmp_path / "pak"), installed[0])
    monkeypatch.setattr("plugins.manager.get_manager", lambda: manager)
    added = load_package_plugins(root)

    assert added == [], (
        "a package replaced a plugin the session was already running: %r"
        % (added,))
    names = [p.name for p in manager.plugins]
    assert names.count(installed[0]) == 1, "the plugin was loaded twice"


def test_a_package_with_no_plugins_is_a_no_op(tmp_path, clean_sys_path):
    root = str(tmp_path / "bare")
    os.makedirs(root)
    assert load_package_plugins(root) == []


def test_both_import_paths_load_package_plugins_before_parsing_the_map():
    """Order is load-bearing: Thing.from_dict resolves classes at parse time.

    Read as source — driving the importers needs a live editor window.
    """
    source = open("editor/main_window.py", encoding="utf-8").read()
    for func in ("play_game_package", "play_package_from_path"):
        start = source.index("def %s(self" % func)
        end = source.index("\n    def ", start + 1)
        body = source[start:end]
        assert "_load_package_plugins" in body, (
            "%s never loads the package's bundled plugins" % func)
        assert body.index("_load_package_plugins") < body.index("load_from_data"), (
            "%s loads the package's plugins after parsing the map; every "
            "plugin entity would already have failed to resolve" % func)


def test_a_packaged_plugins_entity_type_resolves_after_import(tmp_path, clean_sys_path, monkeypatch):
    """The point of the whole exercise: the package's entities are real.

    Without this, a map built with a plugin the destination Fio lacks loads
    every one of that plugin's entities as an unresolved record -- preserved
    by the 2.5 unknown-entity policy, but not playable.
    """
    root = str(tmp_path / "pak")
    os.makedirs(os.path.join(root, "plugins", "gadgetry"))
    open(os.path.join(root, "plugins", "__init__.py"), "a").close()
    with open(os.path.join(root, "plugins", "gadgetry", "__init__.py"), "w") as f:
        f.write(textwrap.dedent('''
            from plugins.api import FioPlugin
            from plugins.entitybase import Thing

            class Gadget(Thing):
                map_type = "gadget"

                def __init__(self, pos=None, properties=None):
                    super().__init__(pos=pos, properties=properties)
                    self.properties.setdefault("type", "gadget")

            class _Gadgetry(FioPlugin):
                name = "gadgetry"
                version = "1.0.0"
                description = "Ships inside the package"
                category = "Packaged"
                enabled = True

                def register(self, api):
                    api.register_entity(Gadget, menu_label="Gadget")

            PLUGIN = _Gadgetry()
        '''))

    manager = PluginManager()
    manager.discover_and_load()
    assert manager.entity_class_for_type("gadget") is None, (
        "fixture wrong: this build already knows what a gadget is")

    monkeypatch.setattr("plugins.manager.get_manager", lambda: manager)
    assert load_package_plugins(root) == ["gadgetry"]

    cls = manager.entity_class_for_type("gadget")
    assert cls is not None, (
        "the package's plugin loaded but its entity type is still unknown, so "
        "every gadget in the map would load as an unresolved record")
    instance = cls(pos=[1.0, 2.0, 3.0], properties={})
    assert instance.properties["type"] == "gadget"
