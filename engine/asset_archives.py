"""Category .fiopak asset archives, read in place, with loose-file overrides.

``assets/<category>.fiopak`` (``editor``, ``sprites``, ``textures``,
``models``, ``fonts``, ``music``, ``sounds``) holds members under their
project-relative paths (``assets/textures/Dev/256.jpg``). ``editor`` holds
the root images (``assets/b_save.png``) and the editor's private artwork
under ``assets/editor/`` (``assets/editor/playerstart.obj``). Desktop
archiver litter (``__MACOSX/``, ``.DS_Store``, ``Thumbs.db``) is skipped. Nothing is
extracted: :func:`prepare_asset_archives` indexes each archive once, and a
loader asks for an asset by the path it always used --
:func:`asset_bytes`, :func:`asset_source` (a path or an in-memory file for
Pillow), :func:`asset_exists`, :func:`asset_listdir`, :func:`asset_files`;
Qt images, icons and fonts through :mod:`engine.asset_qt`. A loose file at
that path still wins (a development override), and a member is also found
by a case-insensitive match, as a Windows file system would. Audio keeps
its streaming reader (:mod:`engine.audio_archives`). Archives are opened per
read, never held open: a repack (``tools/pack_assets.py``) can replace one
while the editor runs, and the next read sees it.
"""

from __future__ import annotations

import os
from pathlib import Path
import io
import stat
import threading
import zipfile


# Root images belong to the editor; everything else has its own directory.
ASSET_CATEGORIES = ('editor', 'sprites', 'textures', 'models', 'fonts',
                    'music', 'sounds')
_ROOT_IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.icns',
                '.ppm', '.pbm', '.pgm', '.pnm'}
_DATA_SUFFIXES = _ROOT_IMAGES | {
    '.webp', '.tga', '.tif', '.tiff', '.dds', '.ktx', '.ktx2',
    '.ttf', '.otf', '.obj', '.mtl', '.glb', '.gltf', '.bin',
    '.json', '.txt', '.md', '.wav', '.ogg', '.mp3',
}
MAX_ENTRY_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 10000


def junk_member(member: str) -> bool:
    """Metadata a desktop archiver adds (macOS Finder's ``__MACOSX/`` and
    ``._name`` resource forks, ``.DS_Store``, Windows ``Thumbs.db``): never
    an asset, and no reason to refuse an otherwise good archive."""
    parts = member.replace('\\', '/').split('/')
    name = parts[-1]
    return (parts[0] == '__MACOSX' or name in ('.DS_Store', 'Thumbs.db', 'desktop.ini')
            or name.startswith('._'))


def category_member(member: str, category: str) -> bool:
    """Safe data-only project-relative member in exactly one category."""
    parts = member.split('/')
    if '\\' in member or any(p in ('', '.', '..') or ':' in p for p in parts):
        return False
    if not parts or parts[0] != 'assets':
        return False
    suffix = Path(member).suffix.lower()
    if category == 'textures' and member == 'assets/noise_3d.bin':
        return True
    if category == 'editor':
        if len(parts) >= 3 and parts[1] == 'editor':    # private artwork
            return suffix in _DATA_SUFFIXES
        return len(parts) == 2 and suffix in _ROOT_IMAGES
    return (category in ASSET_CATEGORIES and len(parts) >= 3
            and parts[1] == category
            and (suffix in _DATA_SUFFIXES or not suffix))


def _entries(archive, category):
    infos = [info for info in archive.infolist()
             if not info.is_dir() and not junk_member(info.filename)]
    if len(infos) > MAX_ENTRIES:
        raise ValueError('too many asset entries')
    total = 0
    names = set()
    for info in infos:
        if not category_member(info.filename, category):
            raise ValueError(f'invalid {category} asset path: {info.filename!r}')
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError('asset archive contains a symbolic link')
        if info.filename in names:
            raise ValueError('asset archive contains duplicate members')
        names.add(info.filename)
        if info.file_size > MAX_ENTRY_BYTES:
            raise ValueError('asset entry exceeds size limit')
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('asset archive exceeds total size limit')
    return infos


class _Archive:
    """One category archive's index: its members, and their folded names."""

    __slots__ = ('path', 'signature', 'members', 'folded', 'dirs')

    def __init__(self, path, signature, infos):
        self.path = path
        self.signature = signature
        self.members = {info.filename: info for info in infos}
        self.folded = {name.casefold(): name for name in self.members}
        dirs = set()
        for name in self.members:
            parts = name.split('/')
            for depth in range(1, len(parts)):
                dirs.add('/'.join(parts[:depth]))
        self.dirs = dirs


_lock = threading.Lock()
_state = {'root': None, 'archives': {}, 'package': None}


def set_package(path, names=()):
    """Serve a played game package's ``assets/...`` members (read in place,
    see ``editor/package_session.py``) after the loose files and the
    category archives; ``None`` ends it."""
    with _lock:
        if path is None:
            _state['package'] = None
        else:
            members = {n: n for n in names if n.startswith('assets/')}
            _state['package'] = (os.fspath(path), members,
                                 {n.casefold(): n for n in members})


def prepare_asset_archives(root_dir) -> tuple[str, ...]:
    """Index *root_dir*'s ``assets/*.fiopak``; write nothing.

    Called before the splash and icons and by MainWindow for alternative
    hosts. Repeated calls only stat the archives unless one changed. A bad
    archive is reported and ignored, so a working loose-file installation
    can still start. Returns the categories that have an archive.
    """
    root = Path(root_dir).resolve()
    assets = root / 'assets'
    found = tuple(c for c in ASSET_CATEGORIES
                  if (assets / f'{c}.fiopak').is_file())
    with _lock:
        if _state['root'] != root:
            _state['root'] = root
            _state['archives'] = {}
        archives = _state['archives']
        for category in list(archives):
            if category not in found:
                del archives[category]
        for category in found:
            _index(archives, category, assets / f'{category}.fiopak')
    return found


def _index(archives, category, path):
    """(Re)index *category*'s archive at *path* when it is new or changed."""
    try:
        stamp = path.stat()
        signature = (stamp.st_mtime_ns, stamp.st_size)
        held = archives.get(category)
        if held is not None and held.signature == signature:
            return held
        with zipfile.ZipFile(path) as archive:
            infos = _entries(archive, category)
        archives[category] = held = _Archive(path, signature, infos)
        return held
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        archives.pop(category, None)
        print(f'[Assets] Cannot read {path.name}: {exc}')
        return None


def _root():
    root = _state['root']
    if root is None:
        prepare_asset_archives(os.getcwd())
        root = _state['root']
    return root


def asset_member(path):
    """The archive member name (``assets/...``) of *path* -- absolute, or
    relative to the working directory, as ``open`` reads it -- or None when
    it is not inside the project's ``assets`` folder."""
    root = _root()
    try:
        rel = os.path.relpath(os.path.abspath(os.fspath(path)), root)
    except ValueError:                      # another drive (Windows)
        return None
    rel = rel.replace(os.sep, '/')
    if rel == 'assets' or not rel.startswith('assets/'):
        return None
    return rel


def _find(member):
    """``(archive, info)`` holding *member* (exact, then case-insensitive)."""
    if member is None:
        return None, None
    with _lock:
        archives = list(_state['archives'].items())
    for category, archive in archives:
        info = archive.members.get(member)
        if info is None:
            name = archive.folded.get(member.casefold())
            info = archive.members.get(name) if name is not None else None
        if info is not None:
            return (category, archive), info
    package = _state['package']
    if package is not None:
        path, members, folded = package
        name = members.get(member) or folded.get(member.casefold())
        if name is not None:
            return ('package', path), name
    return None, None


def _read(found, info):
    (category, archive) = found
    if category == 'package':
        with zipfile.ZipFile(archive) as handle:
            return handle.read(info)
    # Opened per read: never held, so a repack can replace the file. A
    # changed archive is re-indexed first (its members may have moved).
    with _lock:
        current = _index(_state['archives'], category, archive.path)
    if current is not archive:
        if current is None:
            return None
        info = current.members.get(info.filename)
        if info is None:
            return None
        archive = current
    with zipfile.ZipFile(archive.path) as handle:
        return handle.read(info)


def asset_bytes(path):
    """The bytes of the asset at *path*: the loose file if there is one,
    else the archive member, else None."""
    full = os.fspath(path)
    if os.path.isfile(full):
        with open(full, 'rb') as handle:
            return handle.read()
    found, info = _find(asset_member(full))
    if found is None:
        return None
    try:
        return _read(found, info)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        print(f'[Assets] Cannot read {getattr(info, "filename", info)}: {exc}')
        return None


def asset_source(path):
    """What a loader that takes a path or a file object (Pillow) opens:
    *path* itself when it is a loose file, an in-memory file of the archive
    member otherwise, None when the asset is in neither."""
    full = os.fspath(path)
    if os.path.isfile(full):
        return full
    data = asset_bytes(full)
    return None if data is None else io.BytesIO(data)


def asset_exists(path) -> bool:
    """Whether *path* names an asset file, loose or archived."""
    full = os.fspath(path)
    return os.path.isfile(full) or _find(asset_member(full))[0] is not None


def asset_isdir(path) -> bool:
    """Whether *path* is an asset folder, on disk or inside an archive."""
    full = os.fspath(path)
    if os.path.isdir(full):
        return True
    member = asset_member(full)
    if member is None:
        return False
    with _lock:
        archives = list(_state['archives'].values())
    folded = member.casefold()
    return any(member in a.dirs or any(d.casefold() == folded for d in a.dirs)
               for a in archives)


def _archive_children(member):
    """``(files, dirs)``: names directly inside archive folder *member*."""
    prefix = member.rstrip('/') + '/'
    files, dirs = set(), set()
    with _lock:
        archives = list(_state['archives'].values())
    for archive in archives:
        for name in archive.members:
            if name.startswith(prefix):
                head, sep, _rest = name[len(prefix):].partition('/')
                (dirs if sep else files).add(head)
    return files, dirs


def asset_listdir(path) -> list[str]:
    """Names inside folder *path*: the loose ones and the archived ones
    (``os.listdir`` over both, sorted; empty when it is neither)."""
    full = os.fspath(path)
    names = set()
    if os.path.isdir(full):
        try:
            names.update(os.listdir(full))
        except OSError:
            pass
    member = asset_member(full)
    if member is not None:
        files, dirs = _archive_children(member)
        names |= files | dirs
    names.discard('.gitkeep')
    return sorted(names)


def asset_files(path, suffixes=None) -> list[str]:
    """Every asset file under folder *path*, recursively, loose or archived,
    as paths under *path* (``os.path.join(path, 'sub', 'name')``), sorted.
    *suffixes*: keep only names ending so (lower-case, e.g. ``('.png',)``)."""
    full = os.fspath(path)
    found = set()
    if os.path.isdir(full):
        for folder, _dirs, files in os.walk(full):
            for name in files:
                found.add(os.path.relpath(os.path.join(folder, name), full))
    member = asset_member(full)
    if member is not None:
        prefix = member.rstrip('/') + '/'
        with _lock:
            archives = list(_state['archives'].values())
        for archive in archives:
            for name in archive.members:
                if name.startswith(prefix):
                    found.add(name[len(prefix):].replace('/', os.sep))
    out = []
    for rel in found:
        base = os.path.basename(rel)
        if base == '.gitkeep':
            continue
        if suffixes and not base.lower().endswith(tuple(suffixes)):
            continue
        out.append(os.path.join(full, rel))
    return sorted(out)
