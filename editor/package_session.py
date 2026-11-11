"""A played game package (``.fiopak``), read in place: never extracted.

Tools -> Play Game Package used to extract the package to a temporary folder
and load from the copy. A :class:`PackageSession` reads it where it is: the
start map and any map a level change names, cutscenes, and -- through
:func:`engine.asset_archives.set_package` -- the assets it carries, after
the project's own. The archive is opened per read and never held, so the
file stays free (and is never written: it is an input, not an output).

A package is checked when the session opens, before anything changes: the
player's own reader (``engine.fiopak.FioPackage``: no bundled plugin code, a
manifest that names a start map), member names that cannot climb out of the
package, and the player's size budget, per entry and in total -- the limits
extraction used to enforce on the bytes it would write.
"""

from __future__ import annotations

import json
import posixpath
import zipfile


class PackageSession:
    """One played ``.fiopak``: its members by name, read on demand."""

    #: Upper bound on entries in a package.
    MAX_ENTRIES = 20000

    def __init__(self, path):
        import os
        import engine.fiopak as fiopak
        self.path = os.path.abspath(path)
        with fiopak.FioPackage.open(self.path) as package:
            self.start_map = package.start_map_path()
            names = package.namelist()
        if len(names) > self.MAX_ENTRIES:
            raise ValueError(f"Package has too many files ({len(names)})")
        total = 0
        with zipfile.ZipFile(self.path) as archive:
            for info in archive.infolist():
                if _member(info.filename) != info.filename.rstrip('/') and not info.is_dir():
                    raise ValueError(f"Unsafe path in package: {info.filename}")
                if info.file_size > fiopak.MAX_ASSET_ENTRY_BYTES:
                    raise ValueError(f"Package file too large: {info.filename}")
                total += info.file_size
                if total > fiopak.MAX_TOTAL_ASSET_BYTES:
                    raise ValueError("Package expands past the size limit")
        self.names = frozenset(n for n in names if not n.endswith('/'))
        if self.start_map not in self.names:
            raise FileNotFoundError(f"Start map '{self.start_map}' missing from package")

    def member(self, relative_path):
        """The member *relative_path* names (``maps/next.json``), or None:
        absent, absolute, or climbing out of the package."""
        name = _member(relative_path)
        return name if name in self.names else None

    def read(self, member) -> bytes:
        """The bytes of *member* (a name from :attr:`names`)."""
        if member not in self.names:
            raise KeyError(member)
        with zipfile.ZipFile(self.path) as archive:
            return archive.read(member)

    def load_json(self, member):
        """*member* parsed as JSON (a map or a cutscene)."""
        import engine.fiopak as fiopak
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo(member)
            if info.file_size > fiopak.MAX_DOCUMENT_BYTES:
                raise ValueError(f"'{member}' is over the document size limit")
            return json.loads(archive.read(info).decode('utf-8-sig'))


def _member(path):
    """*path* as a package member name, or None when it is absolute or
    climbs out (``..``)."""
    raw = str(path or '').replace('\\', '/').strip()
    if not raw or raw.startswith('/') or ':' in raw.split('/')[0]:
        return None
    parts = raw.split('/')
    if any(part == '..' for part in parts):
        return None
    name = posixpath.normpath(raw)
    return None if name in ('.', '') or name.startswith('../') else name
