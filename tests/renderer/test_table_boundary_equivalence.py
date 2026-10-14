"""Exact numerical oracles for avoiding redundant native/table crossings."""

import math

import glm
import numpy as np
import pytest

from engine.logic_render import LogicRender
from engine.renderer.core.visibility import VisibilityMixin

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('count', [0, 1, 8, 32, 256, 4096])
def test_model_radius_views_match_the_original_packed_axes_bit_for_bit(count):
    rng = np.random.default_rng(4321)
    matrices = rng.normal(size=(count, 16)).astype(np.float32)
    slots = np.arange(count, dtype=np.int32)[::-1]
    basis = matrices[slots].astype(np.float64)
    packed = np.stack([basis[:, :3], basis[:, 4:7], basis[:, 8:11]], axis=1)
    expected = np.sqrt((packed ** 2).sum(axis=2)).max(axis=1)
    actual = VisibilityMixin._model_axis_scale(matrices[slots])
    assert actual.dtype == expected.dtype
    assert actual.tobytes() == expected.tobytes()



def test_frustum_column_reads_preserve_scalar_arithmetic_bits():
    rng = np.random.default_rng(817)
    logic = LogicRender(None)
    for matrix in [glm.mat4(0), glm.mat4(1)] + [glm.mat4(*rng.normal(size=16)) for _ in range(100)]:
        planes = []
        for axis in range(3):
            for sign in (1, -1):
                # Independent old scalar expression: column[row] crossings
                # and the same left-to-right normalization arithmetic.
                a, b, c, d = (matrix[col][3] + matrix[col][axis] if sign == 1
                              else matrix[col][3] - matrix[col][axis]
                              for col in range(4))
                length = math.sqrt(a * a + b * b + c * c)
                planes.append((0., 0., 0., 0.) if length < 1e-8 else
                              (a / length, b / length, c / length, d / length))
        expected = np.asarray(planes, dtype=np.float64).tobytes()
        assert np.asarray(VisibilityMixin._frustum_planes(matrix), dtype=np.float64).tobytes() == expected
        assert np.asarray(logic.extract_frustum_planes(matrix), dtype=np.float64).tobytes() == expected


def test_dense_classification_matches_every_scalar_bit_pattern_and_policy():
    from engine import entity_table as et

    def original_kind(bits, hidden, show):
        if bits & (et.ENT_SKIP | et.ENT_EFFECT):
            return 0
        if bits & et.ENT_ALWAYS_SPRITE:
            return 2
        has_model = bool(bits & et.ENT_HAS_MODEL)
        mode_model = bool(bits & et.ENT_MODE_MODEL)
        billboard = bool(bits & et.ENT_MODE_BILLBOARD)
        has_sprite = bool(bits & et.ENT_HAS_SPRITE)
        entity_sprite = bool(bits & et.ENT_ENTITY_SPRITE)
        if has_model and mode_model and not hidden:
            return 1
        if bits & et.ENT_PROP:
            return 2 if billboard and has_sprite and not entity_sprite else 0
        if entity_sprite:
            return 2
        if has_model and mode_model:
            return 0
        return 2 if (billboard and has_sprite) or show else 0

    words = np.arange(65536, dtype=np.uint16)
    for hidden in (False, True):
        for show in (False, True):
            expected = np.fromiter((original_kind(bits, hidden, show)
                                    for bits in range(65536)), dtype=np.uint8)
            actual = et._RENDER_KIND[int(show), int(hidden), words & et._CLASSIFY_BITS]
            assert actual.tobytes() == expected.tobytes()
