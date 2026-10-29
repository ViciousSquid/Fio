"""Validated category ZIP assets with loose-file overrides and no extraction."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import zipfile


# Root images belong to the editor; everything else has its own directory.
ASSET_CATEGORIES = ('editor', 'sprites', 'textures', 'models', 'fonts',
                    'music', 'sounds')
# The editor exposes writable loose-asset directories alongside the ZIPs.
# 'editor' is intentionally omitted: its members live at assets/ itself.
ASSET_FOLDERS = tuple(c for c in ASSET_CATEGORIES if c != 'editor')
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
        return len(parts) == 2 and suffix in _ROOT_IMAGES
    return (category in ASSET_CATEGORIES and len(parts) >= 3
            and parts[1] == category
            and (suffix in _DATA_SUFFIXES or not suffix))


def _entries(archive, category):
    infos = [info for info in archive.infolist() if not info.is_dir()]
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


# Indexed archive members, per project root. Values are archive categories.
_prepared = {}
# One active portable world package in the desktop editor. The ZIP is reopened
# only for resource cache misses and never extracted.
_game_package = None
_game_members = {}


def ensure_asset_directories(root_dir):
    """Create empty loose-asset folders without extracting archive members.

    Existing authored files are never touched. A read-only packaged install
    still loads normally: lack of write permission is not a mount failure.
    """
    assets = Path(root_dir).resolve() / 'assets'
    for category in ASSET_FOLDERS:
        path = assets / category
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f'[Assets] Cannot prepare loose-asset folder {path}: {exc}')


def prepare_asset_archives(root_dir) -> tuple[str, ...]:
    """Prepare loose directories and mount ZIPs without extracting their files."""
    root = Path(root_dir).resolve()
    assets = root / 'assets'
    ensure_asset_directories(root)
    found = tuple(c for c in ASSET_CATEGORIES if (assets / f'{c}.fiopak').is_file())
    index = {}
    for category in found:
        path = assets / f'{category}.fiopak'
        try:
            with zipfile.ZipFile(path) as archive:
                index.update((info.filename, category)
                             for info in _entries(archive, category))
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError,
                NotImplementedError) as exc:
            print(f'[Assets] Cannot mount {path.name}: {exc}')
    _prepared[str(root)] = index
    return found


def mount_game_package(path):
    """Validate and mount a portable world .fiopak, without unpacking it.

    Validate the *entire* archive first. A failed mount leaves any previously
    mounted world unchanged; no untrusted ZIP entry is written to disk.
    """
    from player.fiopak import (FioPackage, MAX_ASSET_ENTRY_BYTES,
                               MAX_TOTAL_ASSET_BYTES)
    root = str(Path(path).resolve())
    members = {}
    total = 0
    with FioPackage.open(root) as package:
        package.start_map_path()
        with zipfile.ZipFile(root) as archive:
            infos = archive.infolist()
            if len(infos) > 20000:
                raise ValueError('Package contains too many files')
            for info in infos:
                name = info.filename
                if (name.startswith('/') or '\\' in name or
                        any(p in ('', '.', '..') or ':' in p for p in name.split('/'))):
                    raise ValueError(f'Unsafe package entry: {name!r}')
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError('Package contains a symbolic link')
                if name in members:
                    raise ValueError(f'Duplicate package entry: {name!r}')
                if info.file_size > MAX_ASSET_ENTRY_BYTES:
                    raise ValueError(f'Package entry too large: {name!r}')
                total += info.file_size
                if total > MAX_TOTAL_ASSET_BYTES:
                    raise ValueError('Package expands past the size limit')
                if not info.is_dir():
                    members[name] = info.file_size
    global _game_package, _game_members
    _game_package, _game_members = root, members
    return root


def unmount_game_package():
    """Return to normal project assets after a package session ends."""
    global _game_package, _game_members
    _game_package, _game_members = None, {}


def mounted_game_package():
    return _game_package


def package_document(name):
    """Read an explicitly named bounded cutscene JSON in the active world."""
    from player.fiopak import MAX_DOCUMENT_BYTES
    if (_game_package is None or not name.startswith('cutscenes/') or
            name not in _game_members or
            _game_members[name] > MAX_DOCUMENT_BYTES):
        return None
    with zipfile.ZipFile(_game_package) as archive:
        return archive.read(name)


def _package_asset(name, max_bytes=MAX_ENTRY_BYTES):
    if (_game_package is None or name not in _game_members or
            not name.startswith('assets/') or _game_members[name] > max_bytes):
        return None
    try:
        with zipfile.ZipFile(_game_package) as archive:
            data = archive.read(name)
        return data if len(data) <= max_bytes else None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
        return None


def _asset_key(path, root_dir=None):
    """The owning project and an asset-relative ZIP member, if safe."""
    name = os.fspath(path)
    root = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
    if os.path.isabs(name):
        absolute = Path(name).resolve()
        roots = (root, *(Path(r) for r in _prepared if r != str(root)))
        for candidate in roots:
            try:
                rel = absolute.relative_to(candidate).as_posix()
            except ValueError:
                continue
            if rel.startswith('assets/'):
                return str(candidate), rel
        return str(root), None
    rel = name.replace('\\', '/')
    if not rel.startswith('assets/'):
        return str(root), None
    if any(p in ('', '.', '..') or ':' in p for p in rel.split('/')):
        return str(root), None
    return str(root), rel


def _archive_member(path, root_dir=None):
    root, member = _asset_key(path, root_dir)
    if member is None:
        return None
    if root not in _prepared:
        prepare_asset_archives(root)
    category = _prepared[root].get(member)
    if category is None:
        return None
    return Path(root) / 'assets' / f'{category}.fiopak', member


def asset_exists(path, root_dir=None) -> bool:
    """Package, loose-file, or category archive-backed asset presence."""
    _, name = _asset_key(path, root_dir)
    if name is not None and name in _game_members and name.startswith('assets/'):
        return True
    return os.path.isfile(path) or _archive_member(path, root_dir) is not None


def asset_bytes(path, root_dir=None, max_bytes=MAX_ENTRY_BYTES):
    """Read from active game package, loose override or category ZIP."""
    _, member = _asset_key(path, root_dir)
    if member is not None and member in _game_members and member.startswith('assets/'):
        return _package_asset(member, max_bytes)
    if os.path.isfile(path):
        try:
            with open(path, 'rb') as stream:
                result = stream.read(max_bytes + 1)
            return result if len(result) <= max_bytes else None
        except OSError:
            return None
    item = _archive_member(path, root_dir)
    if item is None:
        return None
    archive_path, member = item
    try:
        with zipfile.ZipFile(archive_path) as archive:
            info = archive.getinfo(member)
            if info.file_size > max_bytes:
                return None
            data = archive.read(info)
            return data if len(data) <= max_bytes else None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError,
            NotImplementedError) as exc:
        print(f'[Assets] Cannot read {member}: {exc}')
        return None


def asset_names(prefix='assets/', root_dir=None):
    """Return sorted virtual asset names, including loose overrides."""
    root = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
    key = str(root)
    if key not in _prepared:
        prepare_asset_archives(root)
    result = {name for name in _prepared[key] if name.startswith(prefix)}
    result.update(name for name in _game_members if name.startswith(prefix)
                  and name.startswith('assets/'))
    loose = root / 'assets'
    if loose.is_dir():
        for path in loose.rglob('*'):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() != '.fiopak':
                name = path.relative_to(root).as_posix()
                if name.startswith(prefix):
                    result.add(name)
    return sorted(result, key=str.casefold)
