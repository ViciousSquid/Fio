"""EXPLOSION: its instance rows, the shader layout they feed, and its light.

Pinned here, without a GPU (tests/visual/test_explosion_gl.py draws it):

* the row layout has one definition (engine.shaders.EXPLOSION_INSTANCE_LAYOUT)
  that effect.vert's attribute declarations are generated from, and the rows
  are filled by component name -- the hand-numbered columns put the height
  where the shader read "preview" and left every explosion invisible;
* each authored value lands in the component the shader reads for it;
* the rows' loop and NumPy paths agree to the bit, in the same back-to-front
  order, either side of their crossover, and the loop runs below it;
* the explosion light's loop and NumPy paths agree;
* the sprite sheet holds EXPLOSION_FRAME_COUNT frames, all of them used.
"""

import os
import re

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")
pytest.importorskip("OpenGL")
pytestmark = pytest.mark.qt

from editor.things import Effect                                    # noqa: E402
from engine import shaders                                          # noqa: E402
from engine.effect_entity import (                                  # noqa: E402
    EXPLOSION_FRAME_COUNT, EXPLOSION_PREVIEW_FRAME, EXPLOSION_PREVIEW_T,
    EXPLOSION_SHEET_COLUMNS, EXPLOSION_SHEET_ROWS, explosion_light_scale)
from engine.entity_table import (                                   # noqa: E402
    EXPLOSION_LIGHT_VECTOR_FROM, EntityTable, _CLOCK_ORIGIN)
from engine.renderer.forward import explosion_rows as er            # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COL = shaders.EXPLOSION_COLUMNS


def _table(things, runtime=False):
    table = EntityTable()
    table.begin_frame(list(things), epoch=1, effect_runtime=runtime)
    return table, np.asarray(table.effect_slots, dtype=np.int32)


# ---------------------------------------------------------------------------
# One layout
# ---------------------------------------------------------------------------

def test_the_shader_declares_exactly_the_layout():
    source = shaders.DEFAULT_SHADERS['effect.vert']
    declared = re.findall(r"layout \(location = (\d+)\) in (\w+) (\w+);", source)
    expected = [("0", "vec2", "aPos")] + [
        (str(i + 1), "vec4", name)
        for i, (name, _) in enumerate(shaders.EXPLOSION_INSTANCE_LAYOUT)]
    assert declared == expected


def test_every_column_is_named_once():
    assert sorted(COL.values()) == list(range(shaders.EXPLOSION_INSTANCE_FLOATS))
    assert shaders.EXPLOSION_INSTANCE_FLOATS == 16


def test_the_shader_reads_each_value_from_its_component():
    """The components effect.vert reads, by the layout's names: height is
    iSize.y, the Preview flag iMeta.y, the start iPosStart.w..."""
    source = shaders.DEFAULT_SHADERS['effect.vert']
    swizzle = "xyzw"

    def reads(name):
        vec, comp = divmod(COL[name], 4)
        attribute = shaders.EXPLOSION_INSTANCE_LAYOUT[vec][0]
        return f"{attribute}.{swizzle[comp]}"

    for name in ("width", "height", "lifetime", "start", "preview", "intensity"):
        assert reads(name) in source, name
    assert "iPosStart.xyz" in source and "iColor.rgb" in source


def test_each_authored_value_lands_in_its_column():
    effect = Effect(pos=(10.0, 20.0, 30.0), properties={
        "effect_type": "EXPLOSION", "preview": True, "width": 48.0,
        "height": 18.0, "lifetime": 0.7, "intensity": 1.5,
        "colour": [255, 0, 0]})
    table, slots = _table([effect])
    for rows in (er._rows_loop(table, slots, None, _CLOCK_ORIGIN),
                 er._rows_table(table, slots, None, _CLOCK_ORIGIN)):
        row = er.as_float32(rows)[0]
        assert tuple(row[COL['x']:COL['x'] + 3]) == (10.0, 20.0, 30.0)
        assert row[COL['width']] == 48.0
        assert row[COL['height']] == 18.0
        assert row[COL['lifetime']] == pytest.approx(0.7)
        assert row[COL['intensity']] == pytest.approx(
            float(table.effect_params[slots[0], 1]))
        assert row[COL['preview']] == 1.0
        assert row[COL['r']] == pytest.approx(1.0) and row[COL['g']] == 0.0
        assert row[COL['seed']] == np.float32(table.effect_seed[slots[0]])


def test_a_triggered_explosion_starts_on_the_render_clock():
    effect = Effect(properties={"effect_type": "EXPLOSION"})
    table, slots = _table([effect])
    table.effect_spawn_time[slots[0]] = _CLOCK_ORIGIN + 5000.25
    row = er.as_float32(er._rows_loop(table, slots, None, _CLOCK_ORIGIN))[0]
    # Wrapped as the shader's uTime is.
    assert row[COL['start']] == pytest.approx(5000.25 - shaders.EXPLOSION_TIME_WRAP)
    assert row[COL['preview']] == 0.0


# ---------------------------------------------------------------------------
# Rows: loop and NumPy agree
# ---------------------------------------------------------------------------

def _explosions(n, seed=0):
    rng = np.random.default_rng(seed)
    things = []
    for i in range(n):
        # A few on the same spot: ties in distance keep their order.
        x, z = (0.0, 0.0) if i % 5 == 0 else rng.uniform(-500, 500, 2)
        things.append(Effect(pos=(float(x), float(rng.uniform(0, 50)), float(z)),
                             properties={
            "effect_type": "EXPLOSION", "preview": bool(i % 2),
            "width": float(rng.uniform(8, 96)), "height": float(rng.uniform(8, 96)),
            "lifetime": float(rng.uniform(0.1, 3.0)),
            "colour": [int(c) for c in rng.integers(0, 256, 3)]}))
    table, slots = _table(things)
    table.effect_spawn_time[slots] = _CLOCK_ORIGIN + rng.uniform(0, 9000, n)
    return table, slots


def test_explosion_row_paths_agree():
    edge = er.EXPLOSION_ROWS_VECTOR_FROM
    for n in (1, 2, 5, edge - 1, edge, edge + 1, 3 * edge):
        table, slots = _explosions(n, seed=n)
        for camera in (None, (12.5, -40.0), (0.0, 0.0)):
            by_loop = er._rows_loop(table, slots, camera, _CLOCK_ORIGIN)
            by_table = er._rows_table(table, slots, camera, _CLOCK_ORIGIN)
            assert er.as_float32(by_loop).tobytes() == by_table.tobytes(), (n, camera)
            dispatched = er.as_float32(
                er.explosion_rows(table, slots, camera, _CLOCK_ORIGIN))
            assert dispatched.tobytes() == by_table.tobytes(), (n, camera)


def test_rows_are_back_to_front():
    table, slots = _explosions(9, seed=3)
    camera = (30.0, 10.0)
    rows = er.as_float32(er.explosion_rows(table, slots, camera, _CLOCK_ORIGIN))
    far = (rows[:, COL['x']] - camera[0]) ** 2 + (rows[:, COL['z']] - camera[1]) ** 2
    assert np.all(np.diff(far) <= 0.0)


def test_explosion_rows_loop_below_the_crossover(monkeypatch):
    def numpy_path(*_args, **_kwargs):
        raise AssertionError("NumPy path taken for a small count")

    monkeypatch.setattr(er, "_rows_table", numpy_path)
    for n in (1, 4, er.EXPLOSION_ROWS_VECTOR_FROM - 1):
        table, slots = _explosions(n)
        er.explosion_rows(table, slots, (1.0, 2.0), _CLOCK_ORIGIN)


def test_unchanged_rows_need_no_upload():
    table, slots = _explosions(3)
    first = er.explosion_rows(table, slots, (1.0, 2.0), _CLOCK_ORIGIN)
    again = er.explosion_rows(table, slots, (1.0, 2.0), _CLOCK_ORIGIN)
    assert er.same_rows(again, first)
    assert not er.same_rows(first, None)
    table.pos[slots[1], 1] += 1.0                      # one moved
    assert not er.same_rows(
        er.explosion_rows(table, slots, (1.0, 2.0), _CLOCK_ORIGIN), first)
    big, big_slots = _explosions(er.EXPLOSION_ROWS_VECTOR_FROM + 2)
    many = er.explosion_rows(big, big_slots, None, _CLOCK_ORIGIN)
    assert er.same_rows(er.explosion_rows(big, big_slots, None, _CLOCK_ORIGIN), many)


# ---------------------------------------------------------------------------
# The light: loop and NumPy agree
# ---------------------------------------------------------------------------

def _light_case(n, seed):
    rng = np.random.default_rng(seed)
    table, slots = _table([Effect(properties={"effect_type": "EXPLOSION"})
                           for _ in range(n)])
    life = rng.uniform(0.05, 3.0, n).astype(np.float32)
    elapsed = (life * rng.uniform(-0.1, 1.3, n)).clip(0.0).astype(np.float32)
    elapsed[::7] = 0.0
    playing = rng.random(n) < 0.8
    light_on = rng.random(n) < 0.9
    intensity = rng.uniform(0.0, 8.0, n).astype(np.float32)
    return table, slots, elapsed, life, playing, light_on, intensity


def test_explosion_light_paths_agree():
    edge = EXPLOSION_LIGHT_VECTOR_FROM
    for n in (1, 3, edge - 1, edge, edge + 1, 3 * edge):
        table, slots, *args = _light_case(n, seed=n)
        table._explosion_lights_loop(slots, *args)
        loop_on = table.light_enabled[slots].copy()
        loop_power = table.light_params[slots, 0].copy()
        table.light_enabled[slots] = True
        table.light_params[slots, 0] = -1.0
        table._explosion_lights_table(slots, *args)
        assert np.array_equal(table.light_enabled[slots], loop_on), n
        np.testing.assert_allclose(table.light_params[slots, 0], loop_power,
                                   rtol=1e-6, atol=1e-7)


def test_explosion_lights_loop_below_the_crossover(monkeypatch):
    def numpy_path(*_args, **_kwargs):
        raise AssertionError("NumPy path taken for a small count")

    monkeypatch.setattr(EntityTable, "_explosion_lights_table", numpy_path)
    for n in (1, 4, EXPLOSION_LIGHT_VECTOR_FROM - 1):
        table, slots, *args = _light_case(n, seed=1)
        table._explosion_lights(slots, *args)


def test_the_light_is_off_unless_playing_and_lit():
    table, slots, elapsed, life, _, _, intensity = _light_case(4, seed=2)
    elapsed[:] = 0.01
    playing = np.array([True, False, True, True])
    light_on = np.array([True, True, False, True])
    life[3] = 0.005                                   # already over
    table._explosion_lights(slots, elapsed, life, playing, light_on, intensity)
    assert table.light_enabled[slots].tolist() == [True, False, False, False]
    assert table.light_params[slots[0], 0] == pytest.approx(
        float(intensity[0]) * explosion_light_scale(0.01, float(life[0])), rel=1e-6)


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

def _sheet_alpha():
    image = pytest.importorskip("PIL.Image")
    return np.asarray(image.open(os.path.join(
        ROOT, "assets", "textures", "effects", "explosion.png")).convert("RGBA"))[..., 3]


def test_every_frame_lies_wholly_inside_its_cell():
    """The grid the shader and the player read is the sheet's own: all of a
    frame's art inside its cell, none of it outside every cell. Reading the
    sheet as rows filling its height cut frames through their art, and drew
    a sliver of the frame above at the top of the fireball."""
    from engine.effect_entity import explosion_cell_box
    alpha = _sheet_alpha() > 5
    height, width = alpha.shape
    claimed = np.zeros_like(alpha)
    cells = EXPLOSION_SHEET_COLUMNS * EXPLOSION_SHEET_ROWS
    for frame in range(cells):
        left, top, right, bottom = explosion_cell_box(frame, width, height)
        cell = alpha[top:bottom, left:right]
        claimed[top:bottom, left:right] = True
        if frame < EXPLOSION_FRAME_COUNT:
            assert cell.any(), frame
            # Clear all round its edge: the art does not run on past the cell.
            edge = np.concatenate((cell[0], cell[-1], cell[:, 0], cell[:, -1]))
            assert not edge.any(), frame
        else:
            assert not cell.any(), frame                  # past the last frame
    assert not (alpha & ~claimed).any()                 # nothing between cells


def test_the_shader_reads_the_sheet_grid():
    from engine.effect_entity import EXPLOSION_CELL_UV, EXPLOSION_SHEET_TOP_UV
    source = shaders.DEFAULT_SHADERS['effect.frag']
    assert "%.6f" % EXPLOSION_CELL_UV[0] in source
    assert "%.6f" % EXPLOSION_SHEET_TOP_UV in source
    assert "FRAME_COUNT = %.1f" % EXPLOSION_FRAME_COUNT in source


def test_the_preview_shows_its_frame_whole():
    # The shader blends frame p = t * FRAMES - 0.5: whole at the Preview's t.
    p = EXPLOSION_PREVIEW_T * EXPLOSION_FRAME_COUNT - 0.5
    assert p == pytest.approx(EXPLOSION_PREVIEW_FRAME - 1)
    # The player renderer picks int(t * FRAMES): the same frame.
    assert int(EXPLOSION_PREVIEW_T * EXPLOSION_FRAME_COUNT) == EXPLOSION_PREVIEW_FRAME - 1


# ---------------------------------------------------------------------------
# The sound
# ---------------------------------------------------------------------------

class _Sounds:
    def __init__(self):
        self.queued = []

    def queue_sound(self, sound):
        self.queued.append(sound)


@pytest.mark.parametrize("props, expected", [
    ({}, 1.0),
    ({"explosion_volume": 0.4}, 0.4),
    ({"explosion_volume": 7}, 1.0),
    ({"explosion_volume": -1}, 0.0),
    ({"explosion_volume": "nonsense"}, 1.0),
    ({"silent": True}, 0.0),                           # a map from before
    ({"silent": True, "explosion_volume": 0.5}, 0.5),  # the volume says
])
def test_explosion_volume(props, expected):
    from engine.explosions import explosion_volume
    assert explosion_volume(props) == pytest.approx(expected)


def test_the_sound_plays_from_the_explosion_at_its_volume():
    from engine.explosions import EXPLOSION_SOUND, queue_explosion_sound
    effect = Effect(pos=(10.0, 20.0, 30.0), properties={
        "effect_type": "EXPLOSION", "explosion_volume": 0.3})
    sounds = _Sounds()
    queue_explosion_sound(sounds, effect)
    assert sounds.queued == [{"action": "play", "file": EXPLOSION_SOUND,
                              "volume": pytest.approx(0.3),
                              "position": [10.0, 20.0, 30.0]}]
    assert os.path.exists(os.path.join(ROOT, EXPLOSION_SOUND))
    effect.properties["explosion_volume"] = 0.0
    queue_explosion_sound(sounds, effect)
    assert len(sounds.queued) == 1                     # 0 % is silent


# ---------------------------------------------------------------------------
# The blast's settings: finite, and no bigger than their maximums
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("props, expected", [
    ({}, (0.0, 192.0, 0.0)),
    ({"explosion_damage": 50, "explosion_radius": 300, "explosion_force": 900},
     (50.0, 300.0, 900.0)),
    # Not a finite number: the default (``"inf"`` used to pass, and fault the
    # tick in the radius query).
    ({"explosion_radius": "inf"}, (0.0, 192.0, 0.0)),
    ({"explosion_radius": float("nan")}, (0.0, 192.0, 0.0)),
    ({"explosion_damage": float("inf"), "explosion_force": "-inf"},
     (0.0, 192.0, 0.0)),
    ({"explosion_radius": "nonsense"}, (0.0, 192.0, 0.0)),
    # Past the maximum: the maximum.
    ({"explosion_damage": 1e30, "explosion_radius": 1e9, "explosion_force": 1e12},
     (1.0e6, 4096.0, 20000.0)),
    ({"explosion_radius": -5}, (0.0, 0.0, 0.0)),
])
def test_blast_settings_are_finite_and_bounded(props, expected):
    from engine.explosions import blast_settings
    assert blast_settings(props) == pytest.approx(expected)


def test_a_barrels_settings_are_bounded_the_same_way():
    from engine.explosions import BLAST_MAXIMUMS, _number
    props = {"explosion_radius": "inf", "explosion_force": 1e12, "explosive_health": "nan"}
    assert _number(props, "explosion_radius") == 192.0
    assert _number(props, "explosion_force") == BLAST_MAXIMUMS["explosion_force"]
    assert _number(props, "explosive_health") == 20.0
