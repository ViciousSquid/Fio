"""Buffer-view boundaries and draw offsets are local to each GLB primitive."""

import json
import struct

import numpy as np
import pytest

pytest.importorskip('OpenGL', reason='engine.glb_loader imports PyOpenGL')

from engine import glb_loader
from engine.glb_loader import GLB, GLBLoader

pytestmark = pytest.mark.qt


def _load(tmp_path, gltf, blob):
    encoded = json.dumps(gltf).encode('utf-8')
    encoded += b' ' * (-len(encoded) % 4)
    blob += b'\0' * (-len(blob) % 4)
    chunks = struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
    chunks += struct.pack('<II', len(blob), 0x004E4942) + blob
    path = tmp_path / 'bounds.glb'
    path.write_bytes(struct.pack('<III', 0x46546C67, 2, 12 + len(chunks)) + chunks)
    loader = GLBLoader()
    assert loader.load(str(path))
    return path, loader


def _accessor_document(blob, view, accessor):
    return {
        'asset': {'version': '2.0'},
        'buffers': [{'byteLength': len(blob)}],
        'bufferViews': [dict(buffer=0, **view)],
        'accessors': [dict(bufferView=0, componentType=5126, type='VEC3', **accessor)],
    }


@pytest.mark.parametrize('stride, accessor_offset, view_length', [(12, 0, 24), (16, 4, 32)])
@pytest.mark.parametrize('view_offset', [0, 8])
def test_accessor_cannot_read_following_view(tmp_path, stride, accessor_offset,
                                           view_length, view_offset):
    expected = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    blob = bytearray(view_offset + view_length + 64)
    for i, row in enumerate(expected):
        struct.pack_into('<3f', blob, view_offset + accessor_offset + i * stride, *row)
    # Adjacent data is readable as floats, so crossing the view boundary used
    # to silently append extra values instead of failing a buffer-length check.
    blob[view_offset + view_length:] = np.full(16, 999.0, dtype='<f4').tobytes()
    document = _accessor_document(blob, {
        'byteOffset': view_offset, 'byteLength': view_length, 'byteStride': stride,
    }, {'count': 1_000_000, 'byteOffset': accessor_offset})
    _, loader = _load(tmp_path, document, bytes(blob))
    result = loader._read_accessor(0)
    np.testing.assert_array_equal(result, expected)
    assert result.flags.owndata


@pytest.mark.parametrize('view_length, accessor_offset', [(0, 0), (11, 0), (12, 1), (12, 12), (12, 16)])
def test_accessor_without_a_whole_element_in_its_view_is_absent(tmp_path, view_length,
                                                              accessor_offset):
    blob = np.arange(24, dtype='<f4').tobytes()
    document = _accessor_document(blob, {'byteOffset': 8, 'byteLength': view_length},
                                    {'count': 1, 'byteOffset': accessor_offset})
    _, loader = _load(tmp_path, document, blob)
    assert loader._read_accessor(0) is None


@pytest.mark.parametrize('length', [None, -1, True, 24.0, '24'])
def test_invalid_view_lengths_are_rejected(tmp_path, length):
    blob = np.arange(24, dtype='<f4').tobytes()
    document = _accessor_document(blob, {'byteLength': length}, {'count': 2})
    _, loader = _load(tmp_path, document, blob)
    assert loader._read_accessor(0) is None


def test_missing_view_length_is_rejected(tmp_path):
    blob = np.arange(24, dtype='<f4').tobytes()
    _, loader = _load(tmp_path, _accessor_document(blob, {}, {'count': 2}), blob)
    assert loader._read_accessor(0) is None


def test_view_cannot_extend_accessor_past_real_buffer(tmp_path):
    expected = np.array([[1, 2, 3], [4, 5, 6]], dtype='<f4')
    blob = expected.tobytes()
    document = _accessor_document(blob, {'byteLength': 1_000_000}, {'count': 1_000_000})
    _, loader = _load(tmp_path, document, blob)
    np.testing.assert_array_equal(loader._read_accessor(0), expected)


@pytest.mark.parametrize('stride', [-1, True, False, None, 12.0, '12', 1, 4, 8, 11])
def test_invalid_or_overlapping_strides_are_rejected(tmp_path, stride):
    blob = np.arange(24, dtype='<f4').tobytes()
    document = _accessor_document(blob, {'byteLength': len(blob), 'byteStride': stride},
                                    {'count': 2})
    _, loader = _load(tmp_path, document, blob)
    assert loader._read_accessor(0) is None


def test_overlapping_matrix_stride_is_rejected(tmp_path):
    blob = np.arange(64, dtype='<f4').tobytes()
    document = _accessor_document(blob, {'byteLength': len(blob), 'byteStride': 1},
                                    {'count': 1_000_000})
    document['accessors'][0]['type'] = 'MAT4'
    _, loader = _load(tmp_path, document, blob)
    assert loader._read_accessor(0) is None


@pytest.mark.parametrize('stride', [0, 12, 16])
def test_implicit_packed_exact_and_padded_strides_still_decode(tmp_path, stride):
    expected = np.array([[1, 2, 3], [4, 5, 6]], dtype='<f4')
    actual_stride = stride or expected.shape[1] * expected.itemsize
    blob = bytearray(actual_stride * 2)
    for i, row in enumerate(expected):
        struct.pack_into('<3f', blob, i * actual_stride, *row)
    document = _accessor_document(blob, {'byteLength': len(blob), 'byteStride': stride},
                                    {'count': 2})
    _, loader = _load(tmp_path, document, bytes(blob))
    np.testing.assert_array_equal(loader._read_accessor(0), expected)


class _CaptureGL:
    GL_ARRAY_BUFFER = 1
    GL_ELEMENT_ARRAY_BUFFER = 2

    def __init__(self):
        self.uploads = {}

    def glBufferData(self, target, size, data, usage):
        assert size == data.nbytes
        self.uploads[target] = data.copy()

    def __getattr__(self, name):
        if name.startswith('GL_'):
            return 0
        return lambda *args: 1


@pytest.mark.parametrize('primitives', [
    [(True, 3), (False, 6)],
    [(False, 6), (True, 3)],
    [(True, 3), (False, 6), (True, 4), (False, 3)],
    [(False, 3), (True, 4), (False, 6), (True, 3)],
])
def test_mixed_primitives_keep_vertex_and_element_offsets_separate(tmp_path, monkeypatch,
                                                                  primitives):
    blob = bytearray()
    views, accessors, meshes, expected_groups, position_blocks, index_blocks = [], [], [], [], [], []
    vertex_start = element_start = 0

    def append_array(array, kind, component_type):
        blob.extend(b'\0' * (-len(blob) % 4))
        views.append({'buffer': 0, 'byteOffset': len(blob), 'byteLength': array.nbytes})
        blob.extend(array.tobytes())
        accessors.append({'bufferView': len(views) - 1, 'componentType': component_type,
                          'count': len(array), 'type': kind})
        return len(accessors) - 1

    for i, (indexed, count) in enumerate(primitives):
        positions = np.arange(count * 3, dtype='<f4').reshape(count, 3) + 100 * i
        position_blocks.append(positions)
        primitive = {'attributes': {'POSITION': append_array(positions, 'VEC3', 5126)}}
        if indexed:
            indices = np.array([count - 1, 0, 1], dtype='<u2')
            primitive['indices'] = append_array(indices, 'SCALAR', 5123)
            index_blocks.append(indices.astype(np.uint32) + vertex_start)
        expected_groups.append({'material': 'default', 'start': element_start if indexed else vertex_start,
                                'count': 3 if indexed else count, 'mode': 4, 'indexed': indexed})
        # Separate meshes also exercise the shared VBO/EBO offsets across meshes.
        meshes.append({'primitives': [primitive]})
        vertex_start += count
        element_start += 3 if indexed else 0

    document = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(blob)}],
                'bufferViews': views, 'accessors': accessors, 'meshes': meshes}
    path, _ = _load(tmp_path, document, bytes(blob))
    fake_gl = _CaptureGL()
    monkeypatch.setattr(glb_loader, 'gl', fake_gl)
    model = GLB(str(path))
    assert model.is_loaded
    assert model.groups == expected_groups
    assert model.vertex_count == vertex_start
    assert model.index_count == element_start
    np.testing.assert_array_equal(model.cpu_vertices, np.concatenate(position_blocks))
    np.testing.assert_array_equal(fake_gl.uploads[fake_gl.GL_ELEMENT_ARRAY_BUFFER],
                                  np.concatenate(index_blocks))
    # DrawArrays starts select each non-indexed primitive's own vertices.
    for group, positions in zip(model.groups, position_blocks):
        if not group['indexed']:
            start = group['start']
            np.testing.assert_array_equal(model.cpu_vertices[start:start + group['count']], positions)
