"""
Editor package initialiser.

Bootstraps the Fio plugin system as early as possible — before any map is
loaded or the main window is built — so plugin-provided entity types, I/O
definitions and editor integration are available everywhere they are needed.

This is deliberately tiny and fully guarded: a plugin (or the plugin system
itself) failing must never stop the editor from starting.
"""

try:
    from plugins.manager import load_plugins
    load_plugins()                       # register entity types + I/O defs
    from plugins import integration as _fio_integration
    _fio_integration.apply()             # wire hooks into editor + engine
except Exception as _fio_plugin_exc:      # pragma: no cover - defensive
    print(f"[Plugins] editor bootstrap skipped: {_fio_plugin_exc}")
