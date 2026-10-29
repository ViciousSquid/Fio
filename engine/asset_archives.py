"""Validated category ZIP assets with loose-file overrides and no extraction."""

from __future__ import annotations

import os
from pathlib import Path
import stat
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
_prepared = {}


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


def prepare_asset_archives(root_dir) -> tuple[str, ...]:
    """Validate and mount category archives; never extract their contents."""
    root = Path(root_dir).resolve()
    assets = root / 'assets'
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
    """Loose-file or archive-backed asset presence."""
    return os.path.isfile(path) or _archive_member(path, root_dir) is not None


def asset_bytes(path, root_dir=None, max_bytes=MAX_ENTRY_BYTES):
    """Read a loose override or validated ZIP member, without extracting."""
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
    loose = root / 'assets'
    if loose.is_dir():
        for path in loose.rglob('*'):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() != '.fiopak':
                name = path.relative_to(root).as_posix()
                if name.startswith(prefix):
                    result.add(name)
    return sorted(result, key=str.casefold)
