"""Portable asset discovery for project export and streamed package validation."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import posixpath
import shlex
import struct
import zipfile

from engine.asset_archives import ASSET_CATEGORIES, _entries

ASSET_KEYS = {'texture', 'texture_path', 'sprite_2d', 'custom_idle', 'custom_shoot',
              'custom_dead', 'model_path', 'mesh', 'sound_file', 'sound', 'audio',
              'sprite_path', 'collect_custom_sprite', 'world_sprite', 'hud_sprite',
              'muzzle_flash', 'fire_texture', 'orb_texture', 'custom_gif'}


def map_assets(node):
    result = set()
    def visit(value, key=''):
        if isinstance(value, dict):
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


def companion_assets(path, data):
    """OBJ materials, MTL maps and glTF external buffers/images, data only."""
    suffix = posixpath.splitext(path)[1].lower()
    refs = []
    if suffix in ('.obj', '.mtl'):
        for line in data.decode('utf-8-sig', 'replace').splitlines():
            parts = shlex.split(line, comments=True, posix=True)
            if not parts:
                continue
            if suffix == '.obj' and parts[0] == 'mtllib':
                refs.extend(parts[1:])
            elif suffix == '.mtl' and (parts[0].startswith('map_') or parts[0] in ('bump', 'disp', 'decal', 'norm')):
                # Keep the same option parser as the desktop OBJ loader.
                from engine.obj_loader import OBJLoader
                name = OBJLoader._parse_texture_map(parts[1:])
                if name:
                    refs.append(name)
    elif suffix in ('.glb', '.gltf'):
        if suffix == '.glb':
            if len(data) < 20 or data[:4] != b'glTF':
                raise ValueError(f'Invalid GLB: {path}')
            length, kind = struct.unpack_from('<II', data, 12)
            if kind != 0x4E4F534A or 20 + length > len(data):
                raise ValueError(f'Invalid GLB JSON chunk: {path}')
            document = json.loads(data[20:20 + length])
        else:
            document = json.loads(data)
        refs = [item['uri'] for group in ('buffers', 'images') for item in document.get(group, [])
                if isinstance(item.get('uri'), str) and not item['uri'].startswith('data:')]
    result = []
    for reference in refs:
        reference = reference.replace('\\', '/')
        if reference.startswith('/') or ':' in reference:
            raise ValueError(f'Model companion must be project-relative: {reference}')
        result.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), reference)))
    return result


class ProjectAssets:
    """Loose-file overrides plus category archives, without extraction."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}
        self._stack = ExitStack()
        try:
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
        for folder in ('textures', 'models', 'sounds', 'music', 'sprites', 'fonts', 'materials'):
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
