"""Data assets owned by plugins installed with the player.

A ``.fiopak`` is a world container: it may not carry anything under
``plugins/``, so plugin-owned artwork never travels with a world. The player
ships the same plugins as desktop Fio (Bigworld, Deathmatch and Tidy), and
they draw their entities with files from their own directories: Tidy's book
model and covers (``plugins/tidy/assets/...``), Deathmatch's CTF flags and
navigation markers (named by the plugin's install path). This view of a
package serves exactly those files -- image, model and sound data inside the
installed ``plugins`` directory, never code -- and defers every other request
to the world's package, which always wins.
"""
from __future__ import annotations

from pathlib import Path

#: The only file types served from the plugin tree: images, models, sounds.
DATA_SUFFIXES = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tga', '.webp',
    '.obj', '.mtl', '.glb', '.gltf', '.bin',
    '.wav', '.ogg', '.mp3', '.flac',
})
#: Cap per file, far above any shipped plugin asset.
MAX_BYTES = 32 * 1024 * 1024


def plugins_root():
    import plugins
    return Path(plugins.__file__).resolve().parent


def plugin_asset(name, root=None):
    """The installed plugin data file *name* refers to, or None.

    *name* may be the absolute path a plugin computed from its own location
    or a project-relative ``plugins/...`` path. Anything outside the installed
    plugins directory, any non-data file and any symlink escape is refused.
    """
    if not name:
        return None
    root = Path(root or plugins_root()).resolve()
    text = str(name).replace('\\', '/')
    candidate = Path(text)
    if not candidate.is_absolute():
        if not text.startswith('plugins/'):
            return None
        candidate = root.parent / text
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if path.suffix.lower() not in DATA_SUFFIXES or not path.is_file():
        return None
    if not path.is_relative_to(root) or path.stat().st_size > MAX_BYTES:
        return None
    return path


class PluginAssetPackage:
    """A world package plus read-only access to installed plugin data files."""

    def __init__(self, package, root=None):
        self._package = package
        self._root = Path(root or plugins_root()).resolve()
        self._files = {}

    @property
    def package(self):
        return self._package

    def _plugin_bytes(self, name):
        if name in self._files:
            return self._files[name]
        path = plugin_asset(name, self._root)
        data = path.read_bytes() if path is not None else None
        self._files[name] = data
        return data

    def read_asset(self, name):
        data = self._package.read_asset(name) if self._package is not None else None
        return data if data is not None else self._plugin_bytes(name)

    def has_asset(self, name):
        if self._package is not None and self._package.has_asset(name):
            return True
        return self._plugin_bytes(name) is not None

    def __getattr__(self, attribute):
        return getattr(self._package, attribute)
