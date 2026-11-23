"""A Light's shadow filter and map size in the EntityTable's light_shadow column.

``(shadow_softness, shadow_bias, shadow_map_size)``; 0 in any of them is the
renderer's default, which is also what an unset, non-positive or unreadable
value becomes.
"""

import math

import pytest

pytest.importorskip("PyQt5", reason="Lights are editor Things")

from editor.things import Light                       # noqa: E402
from engine.entity_table import EntityTable            # noqa: E402
from engine.shaders import SHADOW_BIAS_DEFAULT, SHADOW_SOFTNESS_DEFAULT  # noqa: E402
from tests.helpers.worlds import make_thing            # noqa: E402

pytestmark = pytest.mark.qt


def _column(**props):
    light = make_thing(Light, 'lamp', (0, 64, 0), casts_shadows=True, **props)
    table = EntityTable()
    table.begin_frame([light], epoch=1)
    return tuple(float(v) for v in table.light_shadow[0])


def test_a_new_light_has_the_default_filter_and_the_settings_size():
    light = Light(pos=[0, 0, 0])
    assert light.properties['shadow_softness'] == SHADOW_SOFTNESS_DEFAULT
    assert light.properties['shadow_bias'] == SHADOW_BIAS_DEFAULT
    assert light.properties['shadow_map_size'] == 0
    assert _column() == pytest.approx((SHADOW_SOFTNESS_DEFAULT, SHADOW_BIAS_DEFAULT, 0.0))


def test_authored_values_reach_the_column():
    assert _column(shadow_softness=0.5, shadow_bias=3.0, shadow_map_size=768) == \
        pytest.approx((0.5, 3.0, 768.0))


@pytest.mark.parametrize("bad", [0, -2, 'soft', None, float('nan'), float('inf')])
def test_unreadable_or_non_positive_values_are_the_default(bad):
    assert _column(shadow_softness=bad, shadow_bias=bad, shadow_map_size=bad) == (0.0, 0.0, 0.0)


def test_values_are_clamped_to_a_sane_range():
    softness, bias, size = _column(shadow_softness=100, shadow_bias=0.001,
                                   shadow_map_size=100000)
    assert softness == 16.0 and math.isclose(bias, 0.05, rel_tol=1e-6) and size == 4096.0


def test_a_light_that_is_not_a_light_any_more_clears_its_values():
    light = make_thing(Light, 'lamp', (0, 64, 0), casts_shadows=True,
                       shadow_softness=2.0, shadow_bias=2.0, shadow_map_size=512)
    table = EntityTable()
    table.begin_frame([light], epoch=1)
    assert table.light_shadow[0].any()
    table.begin_frame([], epoch=2)
    assert not table.light_shadow[:table.count].any()
