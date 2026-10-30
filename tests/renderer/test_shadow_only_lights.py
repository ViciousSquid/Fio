"""Shadow-only point lights reuse the existing cubemap without a hotspot."""

import pytest

from engine import shaders


RECEIVERS = (
    "lit.frag", "textured.frag", "lit_arm.frag",
    "textured_arm.frag", "terrain.frag",
)


@pytest.mark.parametrize("name", RECEIVERS)
def test_all_world_receivers_support_shadow_only_and_adjustable_strength(name):
    raw = shaders.DEFAULT_SHADERS[name]
    assert raw.count("float shadowTransmission = 1.0;") == 1
    assert "lights[i].shadowOnly > 0.5" in raw
    assert "lights[i].shadowStrength" in raw
    assert "shadowTransmission *= 1.0 -" in raw
    assert "result * shadowTransmission" in raw or "result *= shadowTransmission;" in raw

    packed = shaders.light_ubo_source(raw)
    assert "layout(std140) uniform FioLightBlock" in packed
    assert "lights[i].params.z" in packed
    assert "lights[i].params.w" in packed
    assert "lights[i].shadowOnly" not in packed
    assert "lights[i].shadowStrength" not in packed
    assert "uniform Light lights[" not in packed
    assert "float shadowTransmission = 1.0;" in packed


def test_water_ignores_shadow_only_illumination():
    raw = shaders.DEFAULT_SHADERS["water.frag"]
    assert "lights[i].shadowOnly < 0.5" in raw
    packed = shaders.light_ubo_source(raw)
    assert "lights[i].params.w < 0.5" in packed


def test_android_atlas_shader_keeps_compact_light_block():
    pytest.importorskip("player.gles_shaders")
    from player.render import world_shaders

    source = world_shaders.FRAGMENT
    assert "float shadowTransmission = 1.0;" in source
    assert "indices.y" in source and "indices.z" in source
    assert ".params.z" not in source and ".params.w" not in source
    assert "vec4 params;" not in source


def test_authored_light_shadow_only_projects_to_independent_dense_columns():
    pytest.importorskip("PyQt5")
    from editor.things import Light
    from engine.entity_table import EntityTable

    normal = Light(properties={"intensity": 0.5, "casts_shadows": True})
    shadow = Light(properties={"intensity": 0.0, "shadow_only": True,
                               "shadow_strength": 0.4})
    legacy = Light(properties={"casts_shadows": False})
    table = EntityTable()
    table.begin_frame([normal, shadow, legacy], epoch=1)

    assert table.light_shadow_only.tolist()[:3] == [False, True, False]
    assert table.light_casts_shadows.tolist()[:3] == [True, True, False]
    assert table.light_shadow_strength[:3].tolist() == pytest.approx([1.0, 0.4, 1.0])
    assert float(table.light_params[1, 0]) == 0.0
    assert bool(table.light_enabled[1])


def test_shadow_strength_is_clamped_at_projection_boundary():
    pytest.importorskip("PyQt5")
    from editor.things import Light
    from engine.entity_table import EntityTable

    table = EntityTable()
    table.begin_frame([
        Light(properties={"shadow_strength": -8}),
        Light(properties={"shadow_strength": 15}),
    ], epoch=1)
    assert table.light_shadow_strength[:2].tolist() == [0.0, 1.0]
