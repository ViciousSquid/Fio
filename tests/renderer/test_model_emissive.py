"""glTF emissive factors survive material resolution and reach model shaders."""

import pytest

pytest.importorskip("OpenGL", reason="renderer resources require PyOpenGL")

from types import SimpleNamespace

from engine.renderer.core.resources import ResourcesMixin
from engine.shaders import DEFAULT_SHADERS


def _draw_group(material):
    model = SimpleNamespace(
        groups=[{"material": "sign", "start": 0, "count": 6, "indexed": False}],
        materials={"sign": material},
        ebo=None,
    )
    owner = SimpleNamespace(_model_draw_group_cache={})
    return ResourcesMixin._model_draw_groups(owner, model)[0]


def test_glb_emissive_factor_is_retained_in_the_cached_model_draw_group():
    group = _draw_group({
        "color": [0.2, 0.3, 0.4],
        "alpha": 1.0,
        "texture": None,
        "emissive": [0.05, 0.25, 1.0],
    })

    assert group.emissive == pytest.approx((0.05, 0.25, 1.0))


def test_materials_without_emissive_factor_remain_unlit_by_emission():
    # OBJ/MTL materials and older GLB data without emissiveFactor default to 0.
    group = _draw_group({"color": [0.8, 0.8, 0.8], "alpha": 1.0, "texture": None})

    assert group.emissive == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("name", (
    "lit.frag", "textured.frag", "lit_arm.frag", "textured_arm.frag",
))
def test_model_fragment_shaders_add_emissive_factor_independently_of_lighting(name):
    source = DEFAULT_SHADERS[name]

    assert "uniform vec3 emissive_factor;" in source
    assert "result += emissive_factor;" in source
    assert source.index("result += emissive_factor;") < source.index("FragColor =")
