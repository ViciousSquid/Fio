"""Interleaved model batches using the engine's streaming CPU parsers."""
import base64
import posixpath
import numpy as np

from player.runtime.model_source import load_model


def batches(path, package):
    loader = load_model(path, package)
    if loader is None:
        raise ValueError(f'Cannot load packaged model: {path}')
    textures = {}
    groups = {}
    if hasattr(loader, 'faces'):
        for face in loader.faces:
            material = loader.materials.get(face['material'], {})
            texture = material.get('texture') or ''
            if texture:
                texture = posixpath.normpath(posixpath.join(material.get('mtl_dir', posixpath.dirname(path)), texture).replace('\\', '/'))
            tint = (*material.get('color', (1, 1, 1)), material.get('alpha', 1))
            output = groups.setdefault((texture, tint), [])
            corners = face['vertices']
            for j in range(1, len(corners) - 1):
                triangle = [corners[i] for i in (0, j, j + 1)]
                if any(v < 0 or v >= len(loader.vertices) for v, _, _ in triangle):
                    continue
                points = np.asarray([loader.vertices[v] for v, _, _ in triangle])
                normal = np.cross(points[1] - points[0], points[2] - points[0])
                normal /= max(float(np.linalg.norm(normal)), 1e-9)
                for point, (_, uv, n) in zip(points, triangle):
                    output.extend((*point, *(loader.normals[n] if 0 <= n < len(loader.normals) else normal),
                                   *(loader.texcoords[uv] if 0 <= uv < len(loader.texcoords) else (0, 0))))
    else:
        for primitive in loader._primitives():
            if primitive['mode'] != 4:
                continue
            material = loader.materials.get(primitive['material'], {})
            texture = material.get('texture') or ''
            if texture.startswith('__glb_embedded__'):
                index = int(texture.split('__')[2])
                view = loader.buffer_views[index]
                start = view.get('byteOffset', 0)
                raw = loader.buffers[view.get('buffer', 0)][start:start + view['byteLength']]
                texture = f'{path}#image{index}'
                textures[texture] = raw
            elif texture.startswith('data:image/'):
                key = f'{path}#material{primitive["material"]}'
                textures[key] = base64.b64decode(texture.split(',', 1)[1])
                texture = key
            elif texture:
                texture = posixpath.join(posixpath.dirname(path), texture)
            indices = primitive['indices']
            indices = np.arange(len(primitive['positions'])) if indices is None else indices
            indices = indices[:len(indices) // 3 * 3].reshape(-1, 3)
            tint = (*material.get('color', (1, 1, 1)), material.get('alpha', 1))
            output = groups.setdefault((texture, tint), [])
            for triangle in indices:
                points = primitive['positions'][triangle]
                normal = np.cross(points[1] - points[0], points[2] - points[0])
                normal /= max(float(np.linalg.norm(normal)), 1e-9)
                for index, point in zip(triangle, points):
                    n = primitive['normals'][index] if primitive['normals'] is not None else normal
                    uv = primitive['uvs'][index] if primitive['uvs'] is not None else (0, 0)
                    output.extend((*point, *n, *uv))
    return {name: np.asarray(values, dtype=np.float32) for name, values in groups.items() if values}, textures
