"""Build reproducible category .fiopak archives from loose development assets.

    python tools/pack_assets.py [--root PROJECT] [editor sprites ...]

Archives contain the original bytes and project-relative paths. Existing
archives are replaced only after verification; a category with no loose files
keeps its existing archive (notably the shipped sounds.fiopak).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.asset_archives import ASSET_CATEGORIES, category_member, _entries


def pack_category(root, category):
    root = Path(root).resolve()
    assets = root / 'assets'
    base = assets if category == 'editor' else assets / category
    candidates = base.glob('*') if category == 'editor' else base.rglob('*')
    files = sorted((p for p in candidates if p.is_file()
                    and category_member(p.relative_to(root).as_posix(), category)),
                   key=lambda p: p.relative_to(root).as_posix())
    if category == 'textures' and (assets / 'noise_3d.bin').is_file():
        files.append(assets / 'noise_3d.bin')
    target = assets / f'{category}.fiopak'
    if not files:
        return target, 0
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(assets.resolve()):
            raise ValueError(f'asset outside project: {path}')
    # An archive may be the only copy of some assets. Repacking a partial
    # loose override must not silently drop the other archived members.
    payloads = {}
    if target.exists():
        with zipfile.ZipFile(target) as archive:
            for info in _entries(archive, category):
                payloads[info.filename] = archive.read(info)
    payloads.update((p.relative_to(root).as_posix(), p.read_bytes()) for p in files)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=assets, delete=False) as temp:
            temp_name = temp.name
        with zipfile.ZipFile(temp_name, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for name, data in sorted(payloads.items()):
                info = zipfile.ZipInfo(name,
                                       date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data, compresslevel=9)
        with zipfile.ZipFile(temp_name) as archive:
            for name, data in payloads.items():
                if archive.read(name) != data:
                    raise ValueError(f'asset verification failed: {name}')
        Path(temp_name).replace(target)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
    return target, len(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default=Path(__file__).resolve().parents[1])
    parser.add_argument('categories', nargs='*', choices=ASSET_CATEGORIES)
    args = parser.parse_args(argv)
    for category in args.categories or ASSET_CATEGORIES:
        path, count = pack_category(args.root, category)
        print(f'{path.name}: {count} files packed' if count
              else f'{path.name}: no loose files; existing archive preserved')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
