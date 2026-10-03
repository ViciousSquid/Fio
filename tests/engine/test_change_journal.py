"""The change journal: what keeps the dense tables current without polling.

The render tables used to re-read every entity's position, sprite state and
light settings, and every brush's ``hidden`` flag and (in the editor) its
transform, every frame, to find the few that had changed. Now the objects say
when they change, and a frame reads only those rows.

That trade is only sound if every writer the renderer depends on does say so.
These tests check the journal itself, that each kind of runtime writer reaches
both render buffers, and -- the property everything rests on -- that a table
kept current by the journal holds exactly what a table rebuilt from scratch
holds.
"""

import ast
import pathlib
import random

import numpy as np
import pytest

from engine import change_journal as cj
from engine.change_journal import ChangeJournal, MOVED, OVERFLOW, STATE, touch

pytest.importorskip("PyQt5", reason="the entity classes live in editor.things")

from editor.things import Light, LogicGate, Monster, Portal       # noqa: E402
from engine.effect_entity import Effect                           # noqa: E402
from engine.entity_table import ENT_EFFECT, EntityTable           # noqa: E402
from engine.prop_entity import Prop                               # noqa: E402
from engine.render_table import RenderTable                       # noqa: E402
from engine.spatial import set_authored_flag                      # noqa: E402
from tests.helpers.worlds import box_brush, make_thing            # noqa: E402

pytestmark = pytest.mark.qt


class _Subscriber:
    """Anything weak-referenceable can subscribe."""


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

def test_each_subscriber_sees_every_change_once():
    journal = ChangeJournal()
    a, b = _Subscriber(), _Subscriber()
    journal.subscribe(a)
    journal.subscribe(b)
    thing = object()

    journal.record(thing, MOVED)
    journal.record(thing, STATE)

    assert journal.drain(a) == {id(thing): MOVED | STATE}
    assert journal.drain(a) == {}
    assert journal.drain(b) == {id(thing): MOVED | STATE}, (
        "draining one buffer's table consumed the other's changes")


def test_a_subscriber_that_stops_draining_is_bounded(monkeypatch):
    monkeypatch.setattr(cj, "PENDING_LIMIT", 4)
    journal = ChangeJournal()
    idle = _Subscriber()
    journal.subscribe(idle)
    things = [object() for _ in range(10)]
    for thing in things:
        journal.record(thing, MOVED)

    assert journal.drain(idle) is OVERFLOW
    assert journal.drain(idle) == {}


def test_an_unknown_subscriber_is_told_to_refresh_everything():
    assert ChangeJournal().drain(_Subscriber()) is OVERFLOW


def test_a_dropped_table_stops_being_recorded_for():
    journal = ChangeJournal()
    table = _Subscriber()
    journal.subscribe(table)
    del table
    journal.record(object(), STATE)          # must not keep the table alive
    assert len(journal._sinks) == 0
    journal.subscribe(_Subscriber())         # and its sink leaves the walk
    assert sum(sink.alive for sink in journal._live) <= 1
    keep = _Subscriber()
    journal.subscribe(keep)
    journal.drain(keep)
    assert all(sink.alive for sink in journal._live)


def test_position_assignment_journals_and_normalises():
    lamp = make_thing(Light, "lamp")
    table = EntityTable()
    table.begin_frame([lamp], 1)

    lamp.pos = (1, 2, 3)

    assert lamp.pos == [1.0, 2.0, 3.0]
    table.begin_frame([lamp], 1)
    assert table.pos[0].tolist() == [1.0, 2.0, 3.0]
    assert table.rows_read == 1


def test_reading_a_position_is_still_a_plain_attribute_read():
    """Only the setter is Python: ``thing.pos`` stays an instance-dict hit."""
    lamp = make_thing(Light, "lamp", (4, 5, 6))
    assert "pos" in vars(lamp)
    assert not hasattr(type(vars(lamp)).__dict__, "__get__")
    assert not hasattr(cj.TrackedPosition, "__get__")


# ---------------------------------------------------------------------------
# Runtime writers reach both render buffers
# ---------------------------------------------------------------------------

def _two_buffers(rows, table_type=EntityTable):
    tables = [table_type(), table_type()]
    for table in tables:
        table.begin_frame(rows, 1)
    return tables


def test_an_io_tint_reaches_the_brush_table():
    """SetTint used to change the brush and never the table (#3)."""
    from editor.io_handlers import register_all_input_handlers
    from editor.io_system import IOManager

    wall = box_brush("wall")
    tables = _two_buffers([wall], RenderTable)
    before = tables[0].colour[0].copy()

    io = IOManager()
    register_all_input_handlers(io)
    io.set_entity_finder(lambda name: wall)
    io._execute_input("wall", "SetTint", "255 0 0", "test")
    for table in tables:
        table.begin_frame([wall], 1)

    for table in tables:
        assert table.colour[0].tolist() == [1.0, 0.0, 0.0]
    assert before.tolist() != [1.0, 0.0, 0.0]


def test_the_console_tint_reaches_the_brush_table():
    from types import SimpleNamespace
    from editor.console_commands import ConsoleCommandHandler

    wall = box_brush("wall")
    tables = _two_buffers([wall], RenderTable)
    console = ConsoleCommandHandler.__new__(ConsoleCommandHandler)
    console.editor_state = SimpleNamespace(find_entity_by_name=lambda name: wall)

    console.cmd_tint("wall 0 255 0")
    for table in tables:
        table.begin_frame([wall], 1)

    for table in tables:
        assert table.colour[0].tolist() == [0.0, 1.0, 0.0]


@pytest.mark.parametrize("change", ["park", "unpark"])
def test_big_world_parking_reaches_the_tables(change):
    from plugins.bigworld.runtime import BigWorldSession

    wall = box_brush("wall")
    lamp = make_thing(Light, "lamp")
    runtime = BigWorldSession.__new__(BigWorldSession)
    runtime._parked_brushes = {}
    runtime._parked_lights = {}
    if change == "unpark":
        runtime._set_brush_active(wall, False)
        runtime._set_light_active(lamp, False)
    brushes = _two_buffers([wall], RenderTable)
    things = _two_buffers([lamp])

    active = change == "unpark"
    runtime._set_brush_active(wall, active)
    runtime._set_light_active(lamp, active)

    for table in brushes:
        assert bool(table.begin_frame([wall], 1)[0]) is not active
    for table in things:
        assert bool(table.begin_frame([lamp], 1)[0]) is not active


def test_a_light_fading_at_runtime_is_re_resolved_each_step():
    from engine.logic_thread import LogicThread

    lamp = make_thing(Light, "lamp", intensity=2.0)
    table = EntityTable()
    table.begin_frame([lamp], 1)
    logic = LogicThread.__new__(LogicThread)
    logic.io_manager = None
    logic.light_fade_states = {id(lamp): {
        "entity": lamp, "from": 2.0, "to": 0.0,
        "elapsed": 0.0, "duration": 1.0, "end_off": True}}

    logic._update_light_fades(0.5)
    table.begin_frame([lamp], 1)
    assert table.light_params[0, 0] == pytest.approx(1.0)

    logic._update_light_fades(0.5)
    table.begin_frame([lamp], 1)
    assert table.light_params[0, 0] == pytest.approx(0.0)
    assert not table.light_enabled[0]


def test_a_monster_that_keeps_shooting_is_journalled_once():
    from engine.monster_ai import _set_render_flag

    grunt = make_thing(Monster, "grunt")
    table = EntityTable()
    table.begin_frame([grunt], 1)

    for _ in range(5):
        _set_render_flag(grunt, "is_shooting", True)
        table.begin_frame([grunt], 1)
        if _:
            assert table.rows_read == 0, "an unchanged flag re-resolved the row"


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

def _entity_world(rng):
    things = []
    for i in range(40):
        kind = rng.choice([Light, Monster, LogicGate, Prop, Portal, Effect])
        things.append(make_thing(kind, "e%d" % i, (i * 50.0, 0.0, 0.0)))
    return things


def _edit_entity(thing, rng):
    """One runtime change of the kind the engine journals."""
    kind = rng.randrange(6)
    if kind == 0:
        thing.pos = [rng.uniform(-500, 500), 0.0, rng.uniform(-500, 500)]
    elif kind == 1:
        set_authored_flag(thing, "hidden", rng.random() < 0.5)
    elif kind == 2 and isinstance(thing, Monster):
        thing.properties["dead"] = rng.random() < 0.5
        touch(thing)
    elif kind == 3 and isinstance(thing, Light):
        thing.properties["intensity"] = rng.uniform(0.0, 5.0)
        thing.properties["state"] = rng.choice(["on", "off"])
        touch(thing)
    elif kind == 4 and isinstance(thing, Prop):
        thing._respawn_fade_alpha = rng.random()
        thing._carry_sprite_yaw = rng.choice([None, rng.uniform(-3, 3)])
    elif kind == 5 and isinstance(thing, Effect):
        thing.trigger_explosion(123.0)
        touch(thing)


ENTITY_COLUMNS = ("pos", "class_bits", "hidden", "light_color", "light_params",
                  "light_casts_shadows", "sprite_size", "render_alpha",
                  "sprite_fixed_yaw", "effect_type", "effect_spawn_time",
                  "effect_active", "portal_active", "portal_fade",
                  "model_recipe_id")


@pytest.mark.parametrize("seed", range(8))
def test_a_journal_kept_table_matches_a_rebuild(seed):
    rng = random.Random(seed)
    things = _entity_world(rng)
    live = EntityTable()
    live.begin_frame(things, 1)

    for _ in range(20):
        for thing in rng.sample(things, rng.randint(1, 5)):
            _edit_entity(thing, rng)
        live.begin_frame(things, 1)

        fresh = EntityTable()
        fresh.begin_frame(things, 1)
        n = len(things)
        # An Effect's light flickers with the clock, and the fresh table
        # samples it a moment later; every other row must match exactly.
        steady = (fresh.class_bits[:n] & ENT_EFFECT) == 0
        for name in ENTITY_COLUMNS:
            got, want = getattr(live, name)[:n], getattr(fresh, name)[:n]
            if name == "light_params":
                got, want = got[steady], want[steady]
            np.testing.assert_array_equal(
                got, want, err_msg="column %r diverged from a rebuild" % name)
        # Recipe ids are table-local; compare the recipes they name.
        assert ([live.sprite_recipes()[i] if i >= 0 else None
                 for i in live.sprite_key_id[:n]]
                == [fresh.sprite_recipes()[i] if i >= 0 else None
                    for i in fresh.sprite_key_id[:n]])


# ---------------------------------------------------------------------------
# Writers that would bypass the journal
# ---------------------------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The player, the camera and ``self`` inside the player keep their own glm
#: positions; they are not table rows.
# ``table`` is a dense table (MonsterTable.pos is a NumPy column, not an entity).
_NOT_ENTITIES = {"player", "player2", "camera", "self", "table"}


def _in_place_position_writes(tree):
    """``x.pos[i] = ...`` / ``x.pos[i] += ...`` targets in a module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "pos"):
                base = target.value.value
                name = base.id if isinstance(base, ast.Name) else (
                    base.attr if isinstance(base, ast.Attribute) else "")
                if name not in _NOT_ENTITIES:
                    yield node.lineno


def test_no_engine_code_moves_an_entity_in_place():
    """``thing.pos[1] = y`` moves an entity without telling the renderer."""
    offenders = []
    for folder in ("engine", "editor", "plugins"):
        for path in (ROOT / folder).rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(), str(path))
            offenders += ["%s:%d" % (path.relative_to(ROOT), line)
                          for line in _in_place_position_writes(tree)]
    assert not offenders, (
        "assign a new list instead, so the move is journalled:\n  "
        + "\n  ".join(offenders))
