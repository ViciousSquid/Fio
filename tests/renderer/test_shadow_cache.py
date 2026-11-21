"""The shadow pass keeps each light's casters while nothing they depend on moves.

A light's cube-map is valid while the casters in its reach have not moved or
changed shape (the caster signature). Finding those casters used to gather
every caster in the level, for every shadow light, every frame. Now a light's
casters are kept per shadow slot and table, and found again only when the
light moves, the published rows change, the table is replaced or renumbered,
or a written transform touches a row that was -- or now is -- in the light's
reach. These tests hold the second half of that bargain: the cube-map is
re-rendered exactly when it was before.

Driven through the real ``render_shadow_maps`` with OpenGL replaced by a
recorder: which cube-maps are re-rendered is decided before any GL call.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the lights are editor Things")
pytest.importorskip("OpenGL")

from editor.things import Light                               # noqa: E402
from engine.entity_table import EntityTable                   # noqa: E402
from engine.render_table import RenderTable                   # noqa: E402
from engine.renderer.api import RenderStats                   # noqa: E402
from engine.renderer.core.visibility import VisibilityMixin   # noqa: E402
from engine.renderer.forward import lighting                  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing        # noqa: E402

_REAL_GL = lighting.gl

pytestmark = pytest.mark.qt

#: Lights far enough apart that no caster is in reach of two of them.
LIGHT_SPACING = 8000.0
LIGHT_REACH = 600.0


class _GLRecorder:
    """Records every GL call and makes none; enums are OpenGL's own."""

    def __init__(self):
        self.calls = []

    def glGetIntegerv(self, enum):
        self.calls.append('glGetIntegerv')
        return [0, 0, 64, 64] if enum == _REAL_GL.GL_VIEWPORT else 0

    def __getattr__(self, name):
        if name.startswith('GL_'):
            return getattr(_REAL_GL, name)
        return lambda *args, **kwargs: self.calls.append(name)


class _ShadowPass(lighting.LightingMixin, VisibilityMixin):
    """The shadow pass's state, and a record of each cube-map it re-renders
    (its casters are prepared once per light re-rendered)."""

    def __init__(self):
        self.render_stats = RenderStats()
        self._shadow_cubemaps = list(range(1, self.MAX_SHADOW_LIGHTS + 1))
        self.shaders = {'depth_cube': 1}
        self.uniforms = {'depth_cube': {'model': 0, 'lightSpaceMatrix': 1,
                                        'lightPos': 2, 'far_plane': 3}}
        self.vaos = {'cube': 1}
        self._cube_vbo = None
        self._current_shader = None
        self._shadow_fbo = 1
        self.shadow_map_size = 64
        self._shadow_slot_owner = [None] * self.MAX_SHADOW_LIGHTS
        self._shadow_slot_sig = [None] * self.MAX_SHADOW_LIGHTS
        self._light_shadow_index = {}
        self.rendered = []
        self.scans = 0

    def _prepare_shadow_instances(self, table, in_brushes, instanced, key=('shadow', 0)):
        self.rendered.append(key[1])
        empty = np.empty(0, dtype=np.int32)
        return empty, empty, None

    def _casters_in_reach(self, *args):
        self.scans += 1
        return VisibilityMixin._casters_in_reach(*args)


def _world(lights=4, per_light=50):
    """*per_light* box casters clustered round each of *lights* lights."""
    brushes = []
    for light in range(lights):
        for i in range(per_light):
            brushes.append(box_brush(
                "b%d_%d" % (light, i),
                (light * LIGHT_SPACING + (i % 10) * 80.0 - 400.0, 0.0,
                 (i // 10) * 80.0 - 200.0),
                (48, 48, 48)))
    table = RenderTable()
    table.sync(brushes, 1)
    things = [make_thing(Light, "L%d" % light, (light * LIGHT_SPACING, 200.0, 0.0),
                         radius=LIGHT_REACH, state="on", casts_shadows=True)
              for light in range(lights)]
    entities = EntityTable()
    entities.begin_frame(things, epoch=1)
    return brushes, table, things, entities


def _config(table, entities):
    return {'render_table': table, 'all_brush_slots': table.shown()[1],
            'entity_table': entities, 'thing_hidden': None}


def _frame(renderer, table, entities):
    renderer.rendered.clear()
    renderer.scans = 0
    renderer.render_shadow_maps((entities, entities.light_slots), _config(table, entities))
    return sorted(renderer.rendered)


@pytest.fixture
def gl(monkeypatch):
    recorder = _GLRecorder()
    monkeypatch.setattr(lighting, 'gl', recorder)
    return recorder


def _shadow_slot_of(renderer, entities, light):
    return renderer._light_shadow_index[int(entities.light_slots[light])]


def test_the_first_frame_renders_every_cube_map_and_a_still_one_none(gl):
    _brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    assert _frame(renderer, table, entities) == [0, 1, 2, 3]
    gl.calls.clear()
    for _ in range(3):
        assert _frame(renderer, table, entities) == []
        assert renderer.scans == 0, "a still frame scanned the casters again"
    assert gl.calls == [], (
        "a frame with every cube-map current still saved and restored GL "
        "state: %s" % sorted(set(gl.calls)))
    assert len(renderer._light_shadow_index) == 4


@pytest.mark.perf
def test_still_frames_at_twenty_thousand_casters_scan_none(gl):
    """Eight shadow lights over 20 000 casters: 13 ms a still frame when every
    light gathered every caster; now no light looks at one."""
    _brushes, table, _things, entities = _world(lights=8, per_light=2500)
    renderer = _ShadowPass()
    assert _frame(renderer, table, entities) == list(range(8))
    assert renderer.scans == 8
    gl.calls.clear()
    for _ in range(5):
        assert _frame(renderer, table, entities) == []
        assert renderer.scans == 0
    assert gl.calls == []


def test_a_mover_re_renders_only_the_light_it_is_in_reach_of(gl):
    _brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    before = list(renderer._shadow_slot_sig)
    near = _shadow_slot_of(renderer, entities, 2)
    slot = table.slot_of_id[box_brush("b2_7")["id"]]

    table.write_transforms([slot], centre=table.center[[slot]] + (0.0, 32.0, 0.0))

    assert _frame(renderer, table, entities) == [near]
    assert renderer.scans == 1, "only the light the mover is near is scanned"
    for index, sig in enumerate(renderer._shadow_slot_sig):
        if index == near:
            assert sig != before[index]
        else:
            assert sig is before[index]


def test_a_caster_moving_into_reach_re_renders_that_light(gl):
    _brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    far = table.slot_of_id[box_brush("b0_0")["id"]]
    target = _shadow_slot_of(renderer, entities, 3)

    table.write_transforms([far], centre=[(3 * LIGHT_SPACING, 0.0, 0.0)])

    assert _frame(renderer, table, entities) == sorted(
        [_shadow_slot_of(renderer, entities, 0), target])
    in_reach, _sig = renderer._casters_in_reach(
        table, renderer._kept_shadow_casters(table, table.shown()[1]),
        3 * LIGHT_SPACING, 200.0, 0.0, LIGHT_REACH)
    assert far in in_reach


def test_a_rewritten_but_unmoved_caster_renders_nothing(gl):
    """A mover publishing the same position again is a written row, and the
    light it is near looks again -- but its signature is unchanged, so the
    cube-map stays, as it always did."""
    _brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    slot = table.slot_of_id[box_brush("b1_3")["id"]]
    table.write_transforms([slot], centre=table.center[[slot]].copy())
    assert _frame(renderer, table, entities) == []


def test_a_light_moving_re_renders_its_cube_map(gl):
    _brushes, table, things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    things[1].pos = [LIGHT_SPACING + 40.0, 200.0, 0.0]
    entities.begin_frame(things, epoch=2)
    assert _frame(renderer, table, entities) == [_shadow_slot_of(renderer, entities, 1)]


def test_a_reconciled_table_scans_again_and_keeps_unchanged_cube_maps(gl):
    brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    brushes[0]["pos"] = [brushes[0]["pos"][0], 64.0, brushes[0]["pos"][2]]
    table.sync(brushes, 2)                      # an editor transaction
    assert _frame(renderer, table, entities) == [_shadow_slot_of(renderer, entities, 0)]
    assert renderer.scans == 4, "a renumbered table must be scanned again"


def test_hiding_a_caster_re_renders_its_light(gl):
    brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    slot = table.slot_of_id[brushes[60]["id"]]
    brushes[60]["hidden"] = True
    table.refresh_transforms(brushes, [slot])   # a journalled hide
    assert _frame(renderer, table, entities) == [_shadow_slot_of(renderer, entities, 1)]


def test_the_two_render_buffers_tables_share_cube_maps_while_still(gl):
    """The two buffers' tables take turns frame by frame. Each keeps its own
    casters, and a still world re-renders nothing on either."""
    brushes, table, _things, entities = _world()
    other = RenderTable()
    other.sync(brushes, 1)
    renderer = _ShadowPass()
    assert _frame(renderer, table, entities) == [0, 1, 2, 3]
    assert _frame(renderer, other, entities) == []
    for _ in range(3):
        for current in (table, other):
            assert _frame(renderer, current, entities) == []
            assert renderer.scans == 0

    slot = table.slot_of_id[brushes[10]["id"]]
    moved = table.center[[slot]] + (16.0, 0.0, 0.0)
    table.write_transforms([slot], centre=moved)
    assert _frame(renderer, table, entities) == [_shadow_slot_of(renderer, entities, 0)]
    other.write_transforms([slot], centre=moved)
    assert _frame(renderer, other, entities) == []


def test_a_replaced_table_is_scanned_afresh(gl):
    brushes, table, _things, entities = _world()
    renderer = _ShadowPass()
    _frame(renderer, table, entities)
    brushes[0]["pos"] = [brushes[0]["pos"][0] + 24.0, 0.0, brushes[0]["pos"][2]]
    replacement = RenderTable()
    replacement.sync(brushes, 1)
    assert _frame(renderer, replacement, entities) == [_shadow_slot_of(renderer, entities, 0)]
    assert renderer.scans == 4


def test_kept_casters_match_a_fresh_scan_through_every_change():
    """The kept result is, every frame, what a fresh scan returns."""
    rng = np.random.default_rng(7)
    brushes, table, _things, _entities = _world(lights=2, per_light=40)
    selection = VisibilityMixin()
    light = (LIGHT_SPACING, 200.0, 0.0, LIGHT_REACH)
    for _ in range(40):
        rows = rng.choice(table.count, size=int(rng.integers(0, 4)), replace=False)
        if len(rows):
            table.write_transforms(rows, centre=table.center[rows] + rng.normal(
                0.0, 300.0, size=(len(rows), 3)))
        slots = selection._kept_shadow_casters(table, table.shown()[1])
        kept = selection._kept_casters_in_reach(0, table, slots, *light)
        fresh = VisibilityMixin._casters_in_reach(table, slots, *light)
        assert np.array_equal(kept[0], fresh[0])
        assert kept[1] == fresh[1]
