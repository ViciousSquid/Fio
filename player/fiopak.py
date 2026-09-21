"""Streaming ``.fiopak`` package reader for the Fio Player.

A ``.fiopak`` is an ordinary ZIP archive produced by the editor's
``editor/package_exporter.py``. Its layout is::

    metadata.json          # package manifest (title, start map, author, ...)
    maps/<name>.json       # one or more map files
    assets/textures/...    # referenced textures
    assets/models/...      # referenced .obj/.glb models
    assets/sounds/...      # referenced .wav/.ogg sounds

On the desktop the editor extracts the package to a temp directory before
playing. On mobile that is wasteful (storage + startup latency), so this reader
streams every entry **directly out of the ZIP** and never touches disk. It is
pure-stdlib so it can be unit-tested without a GPU or any third-party package.

The path-resolution fallbacks intentionally mirror both
``engine/resource_manager.py`` and ``editor/package_exporter.py`` so that any
package the editor can export, the player can open — including older packages
that used the ``manifest.json`` name or stored assets without the ``assets/``
prefix.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import BinaryIO, Dict, List, Optional


# Manifest file names tried in order (newer exporter writes ``metadata.json``,
# an older assembly path wrote ``manifest.json``).
_MANIFEST_NAMES = ("metadata.json", "manifest.json")

# Manifest keys that may hold the entry-point map, in order of preference.
_START_MAP_KEYS = ("map_path", "start_map", "main_map")

# Sub-directories an asset might live under when only a bare filename is known.
_ASSET_SUBDIRS = ("", "textures", "models", "sounds", "sprites", "materials")


class PackageError(Exception):
    """Raised when a ``.fiopak`` is missing, corrupt, or has no usable map."""


class FioPackage:
    """Read-only, streaming view over a ``.fiopak`` archive.

    Use as a context manager so the underlying ZIP handle is always closed::

        with FioPackage.open("game.fiopak") as pak:
            level = pak.load_start_map()
            png_bytes = pak.read_asset("floor.png")

    Instances are **not** thread-safe: a single :class:`zipfile.ZipFile` handle
    backs every read. The player touches the package only from the loader thread
    (assets are decoded into GPU/audio objects once at load), so a lock is not
    required; wrap calls in a lock if you stream lazily from multiple threads.
    """

    def __init__(self, zf: zipfile.ZipFile, source: str = "<memory>"):
        self._zf = zf
        self._source = source
        self._names = set(zf.namelist())
        # Index entries by basename so a map that references an asset by bare
        # filename ("floor.png") resolves to wherever the exporter filed it
        # ("assets/textures/floor.png"). First match wins deterministically.
        self._by_basename: Dict[str, str] = {}
        for name in sorted(self._names):
            self._by_basename.setdefault(_basename(name), name)
        # Small byte cache so repeated reads of the same asset (e.g. a texture
        # shared by many faces) hit memory instead of re-inflating the entry.
        self._cache: Dict[str, bytes] = {}
        self._manifest: Dict = self._load_manifest()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str) -> "FioPackage":
        """Open a ``.fiopak`` from a filesystem path."""
        try:
            zf = zipfile.ZipFile(path, "r")
        except FileNotFoundError as exc:
            raise PackageError(f"Package not found: {path}") from exc
        except zipfile.BadZipFile as exc:
            raise PackageError(f"Not a valid .fiopak (bad zip): {path}") from exc
        try:
            return cls(zf, source=path)
        except Exception:
            zf.close()
            raise

    @classmethod
    def from_bytes(cls, data: bytes, source: str = "<bytes>") -> "FioPackage":
        """Open a ``.fiopak`` already resident in memory.

        Android content-provider URIs (e.g. a package shared into the app) are
        most easily read as a byte blob, so accept that directly.
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(data), "r")
        except zipfile.BadZipFile as exc:
            raise PackageError("Not a valid .fiopak (bad zip)") from exc
        return cls(zf, source=source)

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._cache.clear()
        try:
            self._zf.close()
        except Exception:
            pass

    def __enter__(self) -> "FioPackage":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    def _load_manifest(self) -> Dict:
        for name in _MANIFEST_NAMES:
            if name in self._names:
                raw = self._read_raw(name)
                if raw is None:
                    continue
                try:
                    data = json.loads(_decode(raw))
                except json.JSONDecodeError as exc:
                    raise PackageError(f"Corrupt manifest '{name}': {exc}") from exc
                if isinstance(data, dict):
                    return data
        # A package may legitimately ship without a manifest; fall back to an
        # empty one and let start-map auto-detection carry it.
        return {}

    @property
    def metadata(self) -> Dict:
        """The parsed package manifest (may be an empty dict)."""
        return self._manifest

    @property
    def title(self) -> str:
        return str(self._manifest.get("title") or "Untitled")

    @property
    def source(self) -> str:
        return self._source

    @property
    def required_plugins(self) -> List[str]:
        """Names of plugins this package depends on (from the manifest).

        Populated by the exporter when a map uses plugin-provided entities; the
        plugins' code/assets are bundled under ``plugins/`` in the archive. Use
        :func:`plugins.packaging.load_package_plugins` on the extracted package
        root to make them available.
        """
        return list(self._manifest.get("plugins", []) or [])

    def has_bundled_plugins(self) -> bool:
        return any(n.startswith("plugins/") for n in self._names)

    # ------------------------------------------------------------------
    # Map discovery
    # ------------------------------------------------------------------
    def list_maps(self) -> List[str]:
        """All map JSON entries in the archive, sorted, manifest excluded.

        Bundled plugin files (under ``plugins/``) are excluded so a plugin that
        happens to ship a ``.json`` is never mistaken for a level.
        """
        maps = [
            n
            for n in self._names
            if n.lower().endswith(".json")
            and _basename(n) not in _MANIFEST_NAMES
            and not _normalize(n).startswith("plugins/")
        ]
        return sorted(maps)

    def start_map_path(self) -> str:
        """Resolve the entry-point map's archive path.

        Prefers the manifest's ``map_path``/``start_map``/``main_map`` key,
        verifying the entry actually exists; otherwise auto-detects the first
        map file (mirroring ``ResourceManager._ensure_start_map_exists``).
        """
        for key in _START_MAP_KEYS:
            raw = self._manifest.get(key)
            if raw:
                candidate = _normalize(raw)
                if candidate in self._names:
                    return candidate
                # Manifest points somewhere stale — try matching by basename.
                base = _basename(candidate)
                for n in self.list_maps():
                    if _basename(n) == base:
                        return n
        maps = self.list_maps()
        if not maps:
            raise PackageError(f"No map (.json) found in package: {self._source}")
        return maps[0]

    def load_map(self, archive_path: str) -> Dict:
        """Parse a specific map entry into a Python dict."""
        raw = self._read_raw(_normalize(archive_path))
        if raw is None:
            raise PackageError(f"Map not found in package: {archive_path}")
        try:
            return json.loads(_decode(raw))
        except json.JSONDecodeError as exc:
            raise PackageError(f"Corrupt map '{archive_path}': {exc}") from exc

    def load_start_map(self) -> Dict:
        """Parse the entry-point map into a Python dict (ready for the engine)."""
        return self.load_map(self.start_map_path())

    # ------------------------------------------------------------------
    # Asset access (streamed straight from the ZIP)
    # ------------------------------------------------------------------
    def read_asset(self, relative_path: str) -> Optional[bytes]:
        """Return raw bytes for an asset, or ``None`` if it cannot be found.

        Resolution order (first hit wins):
          1. the path exactly as given
          2. with an ``assets/`` prefix stripped (packages sometimes store
             files relative to ``assets/``) or added
          3. by basename across the conventional asset sub-directories

        The multi-step search matches how the editor writes packages and how
        maps reference assets by bare filename.
        """
        norm = _normalize(relative_path)
        if norm in self._cache:
            return self._cache[norm]

        resolved = self._resolve(norm)
        if resolved is None:
            return None
        data = self._read_raw(resolved)
        if data is not None:
            self._cache[norm] = data
        return data

    def _resolve(self, norm: str) -> Optional[str]:
        """Return the archive entry name that satisfies ``norm``, or ``None``.

        Tries the explicit candidate paths first (exact, assets/ prefix toggled,
        conventional sub-dirs), then falls back to a whole-archive basename
        match so bare filenames always resolve.
        """
        for candidate in self._resolution_candidates(norm):
            if candidate in self._names:
                return candidate
        return self._by_basename.get(_basename(norm))

    def open_asset(self, relative_path: str) -> Optional[BinaryIO]:
        """Return a seekable in-memory stream for an asset (or ``None``).

        Handy for consumers that expect a file object, e.g.
        ``pygame.mixer.Sound(pak.open_asset("shoot.wav"))`` or
        ``PIL.Image.open(pak.open_asset("floor.png"))``.
        """
        data = self.read_asset(relative_path)
        return io.BytesIO(data) if data is not None else None

    def read_text(self, relative_path: str) -> Optional[str]:
        data = self.read_asset(relative_path)
        return _decode(data) if data is not None else None

    def has_asset(self, relative_path: str) -> bool:
        return self._resolve(_normalize(relative_path)) is not None

    def _resolution_candidates(self, norm: str) -> List[str]:
        candidates: List[str] = [norm]
        if norm.startswith("assets/"):
            candidates.append(norm[len("assets/") :])
        else:
            candidates.append("assets/" + norm)
        base = _basename(norm)
        if base != norm:
            for sub in _ASSET_SUBDIRS:
                prefix = f"assets/{sub}/" if sub else "assets/"
                candidates.append(prefix + base)
        # De-duplicate while preserving order.
        seen = set()
        ordered = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                ordered.append(c)
        return ordered

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _read_raw(self, name: str) -> Optional[bytes]:
        if name not in self._names:
            return None
        try:
            return self._zf.read(name)
        except (KeyError, zipfile.BadZipFile):
            return None

    def namelist(self) -> List[str]:
        """All entry names in the archive (debugging / diagnostics)."""
        return sorted(self._names)


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------
def _normalize(path: str) -> str:
    """Forward slashes, no leading slash, no ``./`` — ZIP-internal form."""
    p = str(path).replace("\\", "/").lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _basename(path: str) -> str:
    return _normalize(path).rsplit("/", 1)[-1]


def _decode(data: bytes) -> str:
    """Decode text with UTF-8, UTF-8-BOM, then latin-1 fallback (never fails)."""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
