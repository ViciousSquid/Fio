"""Distance fades keep streaming membership and authored light data intact."""

from types import SimpleNamespace

import numpy as np
import pytest

from engine.renderer.core.light_stream import (
    ACTIVE_LIGHT_RANGE, FADE_DISTANCE, LightStream,
)

pytestmark = pytest.mark.qt


def table_at(*xs):
    count = len(xs)
    return SimpleNamespace(
        generation=1,
        pos=np.array([(x, 0.0, 0.0) for x in xs]),
        light_slots=np.arange(count, dtype=np.int32),
        light_enabled=np.ones(count, dtype=bool),
        light_color=np.ones((count, 3), dtype=np.float32),
        light_params=np.array([(2.0, 512.0)] * count, dtype=np.float32),
    )


@pytest.mark.parametrize('reach', [ACTIVE_LIGHT_RANGE, 128.0])
def test_light_fades_before_cutoff_without_changing_hysteresis(reach):
    table = table_at(0.0)
    stream = LightStream()
    width = min(FADE_DISTANCE, reach)
    for distance, weight in [(0, 1), (reach - width, 1),
                             (reach - width * .75, .84375),
                             (reach - width * .5, .5),
                             (reach - width * .25, .15625),
                             (reach, 0), (reach + 1, 0),
                             (reach + 256, 0)]:
        slots = stream.select(table, table.light_slots, (distance, 0, 0), reach=reach)
        assert slots.tolist() == [0]
        assert stream.weights.tolist() == pytest.approx([weight])
    slots = stream.select(table, table.light_slots, (reach + 257, 0, 0), reach=reach)
    assert slots.size == stream.weights.size == 0


def test_fade_survives_render_table_swaps_and_row_generation_changes():
    stream = LightStream()
    for table in [table_at(0), table_at(0), table_at(0)]:
        # Each render buffer owns a different table; none can depend on the
        # previous table's hysteresis to finish fading before it drops a row.
        stream.select(table, table.light_slots, (ACTIVE_LIGHT_RANGE - 1, 0, 0))
        assert 0 < stream.weights[0] < .0001
        table.generation += 1
        stream.select(table, table.light_slots, (ACTIVE_LIGHT_RANGE + 1, 0, 0))
        assert stream.weights.size == 0


@pytest.mark.parametrize('world_reach', [512.0, 1024.0, 8192.0])
def test_earlier_world_parking_boundary_only_changes_intensity(world_reach):
    end = min(ACTIVE_LIGHT_RANGE, world_reach)
    table = table_at(end - FADE_DISTANCE / 2, end)
    stream = LightStream()
    assert stream.select(table, table.light_slots, (0, 0, 0),
                         fade_reach=world_reach).tolist() == [0, 1]
    assert stream.weights.tolist() == [.5, 0]


def test_weights_follow_nearest_order_and_change_without_gathering_again():
    table = table_at(ACTIVE_LIGHT_RANGE, 10)
    stream = LightStream(clock=lambda: 0)
    slots = stream.select(table, table.light_slots, (0, 0, 0))
    assert slots.tolist() == [1, 0]
    assert stream.weights.tolist() == [1, 0]
    stream.select(table, table.light_slots, (128, 0, 0))
    assert stream.weights.tolist() == [1, .5]
    assert stream.gathers == 1
    stream.select(table, table.light_slots, (128, 0, 0),
                  usable=lambda slots: np.zeros(len(slots), dtype=bool))
    assert stream.weights.size == 0
    stream.reset()
    assert stream.weights.size == 0


def test_shared_gpu_payload_fades_live_without_mutating_authored_lights(monkeypatch):
    pytest.importorskip('OpenGL')
    from engine.renderer.core.visibility import VisibilityMixin
    from engine.renderer.forward import lighting

    class Renderer(lighting.LightingMixin, VisibilityMixin):
        def __init__(self):
            self._light_ubo = 1
            self._light_ubo_key = None
            self._light_ubo_data = np.zeros(4, dtype=self.LIGHT_UBO_DTYPE)
            self._light_shadow_index = {}

        def _ensure_light_ubo(self, count):
            pass

    uploads = []
    monkeypatch.setattr(lighting.gl, 'glBindBuffer', lambda *args: None)
    monkeypatch.setattr(lighting.gl, 'glBufferData',
                        lambda target, size, data, usage: uploads.append(data.copy()))
    renderer = Renderer()
    table = table_at(ACTIVE_LIGHT_RANGE)
    original = table.light_params.copy()
    config = dict(entity_table=table, light_focus=(128, 0, 0), play_mode=True)
    packet = renderer._get_active_lights(config)
    renderer._upload_light_ubo(packet, 1)
    assert uploads[-1]['params'][0, :2].tolist() == [1, 512]
    # Same slots/generation, different player position: cache must not reuse
    # the old intensity. Every lit shader consumes this shared payload.
    config['light_focus'] = (256, 0, 0)
    renderer._upload_light_ubo(renderer._get_active_lights(config), 1)
    assert len(uploads) == 2
    assert uploads[-1]['params'][0, :2].tolist() == [2, 512]
    np.testing.assert_array_equal(table.light_params, original)
    # Big World can fit its residency inside the renderer's fixed range.
    config.update(light_focus=(ACTIVE_LIGHT_RANGE - 384, 0, 0), light_fade_range=512)
    renderer._upload_light_ubo(renderer._get_active_lights(config), 1)
    assert uploads[-1]['params'][0, 0] == 1
    # Editor has no player-distance fade.
    del config['light_focus']
    renderer._upload_light_ubo(renderer._get_active_lights(config), 1)
    assert uploads[-1]['params'][0, 0] == 2
    # Explicitly hidden/disabled lights still leave immediately.
    config.update(light_focus=(128, 0, 0), thing_hidden=np.array([True]))
    assert renderer._get_active_lights(config)[1].size == 0
    config['thing_hidden'][0] = False
    table.light_enabled[0] = False
    assert renderer._get_active_lights(config)[1].size == 0
