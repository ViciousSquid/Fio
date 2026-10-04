"""Editor package initialiser.

Loads the plugin registry before maps or the main window are built so plugin
entity types and I/O definitions are available to the real editor owners.
"""

try:
    from plugins.manager import load_plugins
    load_plugins()
except Exception as _fio_plugin_exc:      # pragma: no cover - defensive
    print(f"[Plugins] editor bootstrap skipped: {_fio_plugin_exc}")
