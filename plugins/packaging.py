"""
Making ``.fiopak`` packages plugin-aware.

A ``.fiopak`` is a zip of maps + referenced assets. When a map uses entities
that come from a plugin, the package must also carry that plugin's code and
assets, or it won't load anywhere but the machine that built it. This module
provides :func:`augment_fiopak`, which :class:`editor.package_exporter.PackageExporter`
calls natively as a first-class final step of ``export`` once the base package
is written: it inspects the bundled maps, works out which plugins they need, and
injects those plugins (plus the plugin-system core) into the archive, recording
them in ``metadata.json`` under ``"plugins"``.

Archive layout after augmentation (only the plugin bits shown)::

    metadata.json            # gains  "plugins": ["tidy", ...]
    plugins/__init__.py      # plugin-system core, so the package is loadable
    plugins/api.py
    plugins/manager.py
    plugins/integration.py
    plugins/packaging.py
    plugins/tidy/…           # each required plugin, verbatim (code + assets)

Because plugin assets keep their original repo-relative paths
(``plugins/tidy/assets/book.obj``, ``.../covers/cover_03.png``), a map's
``model_path`` / ``texture`` referencing them resolves straight out of the
package with no rewriting.

:func:`load_package_plugins` is the counterpart for a consumer (a player or an
editor opening a foreign package): point it at an extracted package root and it
loads the bundled plugins.
"""

from __future__ import annotations

import json
import os
import zipfile
from typing import Dict, Iterable, List, Optional, Set


# Top-level plugin-system core files that must travel with any bundled plugin
# so the package can load itself. (Per-plugin packages are added separately.)
_CORE_FILES = ("__init__.py", "api.py", "manager.py", "integration.py",
               "packaging.py")

_SKIP_DIRS = {"__pycache__", ".git"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _plugins_core_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    # plugins/ lives directly under the project root.
    return os.path.dirname(_plugins_core_dir())


def collect_entity_types(map_data: dict) -> Set[str]:
    """All entity ``type`` strings used by a map dict."""
    types: Set[str] = set()
    for thing in map_data.get("things", []):
        if isinstance(thing, dict):
            t = thing.get("type") or thing.get("properties", {}).get("type")
            if t:
                types.add(str(t))
    return types


def collect_required_plugin_names(map_data: dict) -> Set[str]:
    """Plugin names a map explicitly requires, independent of its entities.

    A *global* plugin (e.g. ``topdown``) changes how a map is viewed/played
    without placing any entities, so it can't be discovered from ``things``.
    A map opts in by listing plugin names under ``required_plugins``::

        {"version": …, "brushes": […], "things": […],
         "required_plugins": ["topdown"],
         "plugin_config": {"topdown": {"height": 900, "orientation": "player"}}}

    Returns the set of names (empty when the key is absent or malformed).
    """
    if not isinstance(map_data, dict):
        return set()
    raw = map_data.get("required_plugins", [])
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(n) for n in raw if n}


def enabled_global_plugins(mgr) -> List:
    """Currently-enabled plugins flagged ``global_plugin`` (order-stable).

    These are bundled into a package even when no map uses their entities,
    because enabling one in the editor is the author's way of saying "ship this
    view/mode with the game".
    """
    out = []
    for p in getattr(mgr, "plugins", []) or []:
        if getattr(p, "global_plugin", False) and mgr.is_enabled(p):
            out.append(p)
    return out


def _iter_package_files(pkg_dir: str) -> Iterable[str]:
    """Yield absolute paths of files under *pkg_dir*, skipping caches/tests junk."""
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(_SKIP_SUFFIXES):
                continue
            yield os.path.join(root, name)


def _archive_name(abs_path: str) -> str:
    return os.path.relpath(abs_path, _repo_root()).replace("\\", "/")


def required_plugin_files(type_names: Iterable[str],
                          extra_plugins: Optional[Iterable] = None) -> Dict[str, str]:
    """Map archive-path -> source-path for every file needed to bundle plugins.

    Covers the plugins that own the given entity *type_names* plus any
    *extra_plugins* (plugin objects) passed in — e.g. global plugins a map
    requires by name. Returns ``{}`` when nothing needs bundling. Includes the
    plugin-system core files only when at least one plugin is actually bundled.
    """
    try:
        from plugins.manager import get_manager
    except Exception:
        return {}

    mgr = get_manager()

    # De-duplicate plugins from both sources, preserving order.
    plugins: List = []
    seen = set()
    for plugin in list(mgr.required_plugins_for_types(type_names)) + list(extra_plugins or []):
        if plugin is not None and id(plugin) not in seen:
            seen.add(id(plugin))
            plugins.append(plugin)
    if not plugins:
        return {}

    files: Dict[str, str] = {}

    # Plugin-system core.
    core = _plugins_core_dir()
    for name in _CORE_FILES:
        src = os.path.join(core, name)
        if os.path.isfile(src):
            files[_archive_name(src)] = src

    # Each bundled plugin package, verbatim.
    for plugin in plugins:
        pkg_dir = mgr.plugin_package_dir(plugin)
        if not pkg_dir or not os.path.isdir(pkg_dir):
            continue
        for src in _iter_package_files(pkg_dir):
            files[_archive_name(src)] = src

    return files


def augment_fiopak(pak_path: str, log=None) -> dict:
    """Inject the plugins a package's maps depend on into an existing ``.fiopak``.

    Reads the archive at *pak_path*, works out required plugins from the bundled
    maps, and rewrites the archive with the plugin code/assets added and
    ``metadata.json`` updated. A no-op (leaving the file untouched) when the maps
    use no plugin entities.

    Returns a summary dict: ``{"plugins": [names], "added_paths": {archive…}}``.
    """
    def _say(msg):
        if log:
            log(msg)

    with zipfile.ZipFile(pak_path, "r") as zin:
        names = list(zin.namelist())
        entries: Dict[str, bytes] = {n: zin.read(n) for n in names}

    try:
        from plugins.manager import get_manager
        mgr = get_manager()
    except Exception:
        mgr = None

    # Parse every bundled map once: gather entity types, and the global plugins
    # each map should activate (declared ``required_plugins`` ∪ enabled globals).
    map_names = [n for n in entries
                 if n.startswith("maps/") and n.endswith(".json")]
    maps: Dict[str, dict] = {}
    types: Set[str] = set()
    declared_names: Set[str] = set()
    for name in map_names:
        try:
            data = json.loads(entries[name].decode("utf-8"))
        except Exception:
            continue
        maps[name] = data
        types |= collect_entity_types(data)
        declared_names |= collect_required_plugin_names(data)

    # Global plugins to ship: any the maps named, plus any enabled global plugin
    # (enabling one in the editor is the author opting the game into that mode).
    global_plugins: List = []
    if mgr is not None:
        seen = set()
        for p in enabled_global_plugins(mgr):
            if id(p) not in seen:
                seen.add(id(p))
                global_plugins.append(p)
        for nm in declared_names:
            p = mgr.find_plugin(nm)
            if p is not None and id(p) not in seen:
                seen.add(id(p))
                global_plugins.append(p)

    plugin_files = required_plugin_files(types, extra_plugins=global_plugins)
    if not plugin_files:
        return {"plugins": [], "added_paths": set()}

    # Names for the manifest, and the global-plugin activation baked into maps.
    entity_names: List[str] = []
    global_names: List[str] = []
    global_config: Dict[str, dict] = {}
    if mgr is not None:
        entity_names = [mgr.plugin_package_name(p)
                        for p in mgr.required_plugins_for_types(types)]
        for p in global_plugins:
            nm = mgr.plugin_package_name(p)
            global_names.append(nm)
            # Capture the plugin's current settings so the game ships as tuned.
            getter = getattr(p, "export_config", None)
            if callable(getter):
                try:
                    global_config[nm] = dict(getter())
                except Exception:
                    pass
    plugin_names = list(dict.fromkeys(entity_names + global_names))

    # Bake global activation into every bundled map, so the standalone player
    # (which only sees map data) enables them without needing entities. Existing
    # ``required_plugins`` / ``plugin_config`` are preserved and unioned.
    if global_names:
        for name, data in maps.items():
            existing = data.get("required_plugins")
            existing = list(existing) if isinstance(existing, (list, tuple)) else []
            merged = list(dict.fromkeys(existing + global_names))
            data["required_plugins"] = merged
            cfg = data.get("plugin_config")
            cfg = dict(cfg) if isinstance(cfg, dict) else {}
            for nm, c in global_config.items():
                cfg.setdefault(nm, c)
            if cfg:
                data["plugin_config"] = cfg
            entries[name] = json.dumps(data, indent=2).encode("utf-8")

    # Update metadata.
    meta: dict = {}
    if "metadata.json" in entries:
        try:
            meta = json.loads(entries["metadata.json"].decode("utf-8"))
        except Exception:
            meta = {}
    meta["plugins"] = plugin_names
    meta["requires_plugins"] = bool(plugin_names)
    if global_names:
        meta["global_plugins"] = global_names
    entries["metadata.json"] = json.dumps(meta, indent=2).encode("utf-8")

    # Add plugin files (dict de-dups against anything already present).
    added_paths: Set[str] = set()
    for arc, src in plugin_files.items():
        if arc in entries:
            continue
        try:
            with open(src, "rb") as f:
                entries[arc] = f.read()
            added_paths.add(arc)
        except Exception as exc:
            _say(f"[Plugins] could not bundle {arc}: {exc}")

    # Rewrite the archive.
    with zipfile.ZipFile(pak_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)

    _say(f"[Plugins] packaged {len(added_paths)} plugin file(s): "
         f"{', '.join(plugin_names) or '(none)'}")
    return {"plugins": plugin_names, "added_paths": added_paths}


def load_package_plugins(package_root: str, log=None) -> List[str]:
    """Load plugins bundled inside an extracted package at *package_root*.

    Adds *package_root* to ``sys.path`` (so the bundled ``plugins`` package is
    importable) and runs discovery. Intended for a player/editor opening a
    package built elsewhere. Returns the names of plugins now loaded.

    Note: this makes the plugins' entity types and I/O available. Executing a
    plugin's *gameplay* additionally requires the host to dispatch the play
    lifecycle (as the editor's play mode does via ``plugins.integration``).
    """
    import sys

    def _say(msg):
        if log:
            log(msg)

    plugins_dir = os.path.join(package_root, "plugins")
    if not os.path.isdir(plugins_dir):
        return []

    if package_root not in sys.path:
        sys.path.insert(0, package_root)

    try:
        from plugins.manager import get_manager, load_plugins
        load_plugins()
        mgr = get_manager()
        names = [p.name for p in mgr.plugins]
        _say(f"[Plugins] loaded from package: {', '.join(names) or '(none)'}")
        return names
    except Exception as exc:
        _say(f"[Plugins] failed to load package plugins: {exc}")
        return []


def package_plugin_names(pak_path: str) -> List[str]:
    """Read the ``plugins`` list declared in a package's ``metadata.json``."""
    try:
        with zipfile.ZipFile(pak_path, "r") as zf:
            for name in ("metadata.json", "manifest.json"):
                if name in zf.namelist():
                    meta = json.loads(zf.read(name).decode("utf-8"))
                    return list(meta.get("plugins", []) or [])
    except Exception:
        pass
    return []
