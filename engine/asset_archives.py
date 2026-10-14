"""Startup discovery of category .fiopak assets, with loose-file overrides.

Qt, Pillow and model loaders keep their existing paths. Non-audio archives
materialise only missing files at those paths; sounds/music remain streamed
by audio_archives. No archive is consulted in a rendering hot path.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
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


def prepare_asset_archives(root_dir) -> tuple[str, ...]:
    """Discover supported assets/*.fiopak; preserve every existing loose file.

    Called before the splash/icons and by MainWindow for alternative hosts.
    Repeated calls only stat the archives unless an archive changed. Failed
    preparations are never cached. A bad archive is reported and ignored so
    a working loose-file installation can still start.
    """
    root = Path(root_dir).resolve()
    assets = root / 'assets'
    found = tuple(c for c in ASSET_CATEGORIES
                  if (assets / f'{c}.fiopak').is_file())
    for category in found:
        path = assets / f'{category}.fiopak'
        if category in ('sounds', 'music'):
            continue  # Native readers use the archive without extraction.
        try:
            stamp = path.stat()
            key = (str(root), category)
            signature = (stamp.st_mtime_ns, stamp.st_size)
            if _prepared.get(key) == signature:
                continue
            with zipfile.ZipFile(path) as archive:
                infos = _entries(archive, category)
                # Validate all destinations before writing anything.
                for info in infos:
                    target = root / info.filename
                    if not target.resolve().is_relative_to(assets.resolve()):
                        raise ValueError('asset destination escapes assets directory')
                    if assets.is_symlink() or target.is_symlink():
                        raise ValueError('asset destination is a symbolic link')
                for info in infos:
                    target = root / info.filename
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Read fully (including ZIP CRC validation) before exposing
                    # the file to a loader. Link is atomic and never overwrites.
                    data = archive.read(info)
                    temp_name = None
                    try:
                        with tempfile.NamedTemporaryFile(
                                dir=target.parent, delete=False) as temp:
                            temp_name = temp.name
                            temp.write(data)
                        try:
                            os.link(temp_name, target)
                        except FileExistsError:
                            pass  # A loose override appeared during startup.
                    finally:
                        if temp_name is not None:
                            os.unlink(temp_name)
            _prepared[key] = signature
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            print(f'[Assets] Cannot prepare {path.name}: {exc}')
    return found
