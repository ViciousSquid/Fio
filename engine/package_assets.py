"""Portable asset discovery for project export and streamed package validation."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import posixpath
import struct
import zipfile

from engine.asset_archives import (ASSET_CATEGORIES, _entries, user_pack_entries,
                                   user_pack_paths)

ASSET_KEYS = {'texture', 'texture_path', 'sprite_2d', 'custom_idle', 'custom_shoot',
              'custom_dead', 'model_path', 'mesh', 'sound_file', 'sound', 'audio',
              'sprite_path', 'collect_custom_sprite', 'world_sprite', 'hud_sprite',
              'muzzle_flash', 'fire_texture', 'orb_texture', 'custom_gif', 'sky_image',
              'cookie_texture'}


def map_assets(node):
    result = set()
    def visit(value, key=''):
        if isinstance(value, dict):
            text = value.get('text')
            if value.get('type') == 'decal' and isinstance(text, str) and text.strip():
                # A sign is drawn in its font: the package carries the file.
                from engine.decal_text import font_file
                result.add('fonts/' + font_file(value.get('font')))
            for k, v in value.items():
                if k == 'textures' and isinstance(v, dict):
                    result.update(x for x in v.values() if isinstance(x, str) and x)
                visit(v, k)
        elif isinstance(value, list):
            for v in value:
                visit(v, key)
        elif isinstance(value, str) and value and key in ASSET_KEYS and not value.startswith('data:'):
            result.add(value)
    visit(node)
    return result


#: The asset types :func:`companion_assets` finds references in; any other
#: file names none, and need not be read to find out.
COMPANION_SUFFIXES = ('.obj', '.mtl', '.glb', '.gltf')

#: MTL statements that name a texture file (the loader reads map_Kd/map_Ka;
#: a package carries the rest too, for a player that reads them).
_MTL_MAP_KEYWORDS = ('bump', 'disp', 'decal', 'norm')


def companion_assets(path, data, errors=None):
    """OBJ materials, MTL maps and glTF external buffers/images, data only.

    Lines are read as :class:`engine.obj_loader.OBJLoader` reads them --
    split on whitespace, ``#`` only at the start of a line, an ``mtllib``
    name running to the end of the line -- so the package holds exactly the
    files the loader will ask for. (``shlex`` used to read them as shell
    words: an apostrophe in ``o Bob's_chair`` raised, a backslash path lost
    its separators, and a 10 MB OBJ took seconds.)

    A reference that cannot be packaged (absolute, a drive, another scheme)
    or a model whose header cannot be read is a problem with that one model:
    with an *errors* list it is reported there and skipped, so one odd model
    does not abort a whole export; without one it raises ``ValueError``.
    """
    def problem(message):
        if errors is None:
            raise ValueError(message)
        errors.append(message)

    suffix = posixpath.splitext(path)[1].lower()
    refs = []
    if suffix == '.obj':
        text = data.decode('utf-8-sig', 'replace')
        if 'mtllib' in text:
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith('mtllib'):
                    continue
                parts = line.split()
                if parts[0] == 'mtllib' and len(parts) > 1:
                    refs.append(' '.join(parts[1:]))
    elif suffix == '.mtl':
        # Keep the same option parser as the desktop OBJ loader.
        from engine.obj_loader import OBJLoader
        for line in data.decode('utf-8-sig', 'replace').splitlines():
            parts = line.split()
            if not parts or parts[0].startswith('#'):
                continue
            keyword = parts[0]
            if keyword.startswith('map_') or keyword in _MTL_MAP_KEYWORDS:
                name = OBJLoader._parse_texture_map(parts[1:])
                if name:
                    refs.append(name)
    elif suffix in ('.glb', '.gltf'):
        try:
            if suffix == '.glb':
                if len(data) < 20 or data[:4] != b'glTF':
                    raise ValueError(f'Invalid GLB: {path}')
                length, kind = struct.unpack_from('<II', data, 12)
                if kind != 0x4E4F534A or 20 + length > len(data):
                    raise ValueError(f'Invalid GLB JSON chunk: {path}')
                document = json.loads(data[20:20 + length])
            else:
                document = json.loads(data)
        except (ValueError, RecursionError) as exc:   # JSONDecodeError, UnicodeError too
            problem(str(exc) if str(exc).startswith('Invalid GLB') else f'Invalid glTF: {path}: {exc}')
            return []
        if not isinstance(document, dict):
            problem(f'Invalid glTF: {path}: not a JSON object')
            return []
        for group in ('buffers', 'images'):
            items = document.get(group)
            for item in items if isinstance(items, list) else ():
                uri = item.get('uri') if isinstance(item, dict) else None
                if isinstance(uri, str) and uri and not uri.startswith('data:'):
                    refs.append(uri)
    result = []
    for reference in refs:
        reference = reference.replace('\\', '/')
        if reference.startswith('/') or ':' in reference:
            problem(f'Model companion must be project-relative: {reference} (in {path})')
            continue
        result.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), reference)))
    return result


class ProjectAssets:
    """Loose-file overrides plus category archives and user packs, without
    extraction."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}
        self._stack = ExitStack()
        try:
            # Later sources win: user packs, then the built-in category
            # archives, then loose files (the development override).
            for path in user_pack_paths(self.root / 'assets'):
                try:
                    source = self._stack.enter_context(zipfile.ZipFile(path))
                    entries, _skipped = user_pack_entries(source)
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    print(f'[Assets] Cannot read {path.name}: {exc}')
                    continue
                for name, info in entries:
                    self.sources[name] = (source, info)
            for category in ASSET_CATEGORIES:
                archive = self.root / 'assets' / f'{category}.fiopak'
                if archive.is_file():
                    source = self._stack.enter_context(zipfile.ZipFile(archive))
                    for info in _entries(source, category):
                        self.sources[info.filename] = (source, info)
            directory = self.root / 'assets'
            if directory.is_dir():
                for path in directory.rglob('*'):
                    if path.is_file() and path.resolve().is_relative_to(directory.resolve()):
                        if path.suffix.lower() not in ('.py', '.pyc', '.fiopak'):
                            self.sources[path.relative_to(self.root).as_posix()] = path
        except BaseException:
            self.close()
            raise

    def resolve(self, reference):
        name = str(reference).replace('\\', '/')
        if name.startswith('/') or ':' in name or '..' in name.split('/'):
            return None
        name = posixpath.normpath(name)
        candidates = [name, 'assets/' + name]
        for folder in ('textures', 'models', 'sounds', 'music', 'sprites', 'fonts', 'materials', 'backgrounds'):
            candidates.append(f'assets/{folder}/{name}')
        for candidate in candidates:
            if candidate in self.sources:
                return candidate
        matches = [p for p in self.sources if posixpath.basename(p) == posixpath.basename(name)]
        return matches[0] if len(matches) == 1 else None

    def read(self, name):
        source = self.sources[name]
        if isinstance(source, Path):
            return source.read_bytes()
        archive, info = source
        return archive.read(info)

    def close(self):
        self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
