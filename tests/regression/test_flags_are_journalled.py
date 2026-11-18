"""``hidden`` and ``disabled`` are changed through the change journal.

Physics keeps its own account of which entities are hidden, disabled or past
live simulation, and hears of a change through the change journal and nowhere
else -- it used to look at every awake body's property dict every step, in
case something had written a flag without saying so. Now the writers say so:
``engine.spatial.set_live_flag`` / ``clear_live_flag`` / ``set_authored_flag``
(for plugins, ``plugins.api.set_flag`` / ``clear_flag``).

These tests are the enforcement. The first fails on any code in the engine,
the editor or a shipped plugin that writes one of those flags into an
entity's properties directly. The rest pin what happens to a writer that does
it anyway: physics does not hear it, and says so.
"""
import pathlib
import re

import pytest

from engine.physics.soma import Soma
from engine.spatial import (SIM_TIER_KEY, TIER_DISTANT, clear_live_flag,
                            set_authored_flag, set_live_flag)
from engine.spatial_grid import SpatialGrid
from tests.physics.test_dynamic_bodies import _drum_brush, _floor, _prop, _run, _world

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: A flag written or removed straight on an entity's property dict.
DIRECT_WRITE = re.compile(
    r"""(properties|props)\s*\[\s*["'](hidden|disabled)["']\s*\]\s*=(?!=)"""
    r"""|(properties|props)\.pop\(\s*["'](hidden|disabled)["']""")

#: Where a direct write is the implementation of the journalled one.
ALLOWED = {
    # The writers themselves.
    'engine/spatial.py',
    # The streaming layer: parking stashes the authored value and writes the
    # live one, and journals each (touch(..., VISIBILITY)) where it does.
    'plugins/bigworld/runtime.py',
    # Works on a deep copy of a saved level, never on a live entity.
    'plugins/bigworld/persistence.py',
    # Building an entity: its defaults, before it is in any world.
    'engine/prop_entity.py',
}


def _sources():
    for folder in ('engine', 'editor', 'plugins', 'player'):
        for path in sorted((ROOT / folder).rglob('*.py')):
            relative = path.relative_to(ROOT).as_posix()
            if '/tests/' in relative or relative in ALLOWED:
                continue
            yield relative, path.read_text(encoding='utf-8', errors='replace')


def test_no_code_writes_hidden_or_disabled_behind_the_journals_back():
    offenders = []
    for relative, source in _sources():
        for number, line in enumerate(source.splitlines(), 1):
            code = line.split('#', 1)[0]
            if '``' in code:
                continue                    # documentation showing the mistake
            if DIRECT_WRITE.search(code):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        "these write 'hidden'/'disabled' straight into an entity's properties; "
        "use engine.spatial.set_live_flag / clear_live_flag / set_authored_flag "
        "(plugins.api.set_flag / clear_flag):\n" + "\n".join(offenders))


def test_the_streaming_layer_journals_what_it_writes():
    """The one allowed direct writer on live entities does say so."""
    source = (ROOT / 'plugins/bigworld/runtime.py').read_text(encoding='utf-8')
    for function in ('_set_thing_active', '_set_light_active', '_restore_thing'):
        body = source[source.index(f"def {function}("):]
        body = body[:body.index("\n    def ", 1)]
        assert 'touch(' in body, function
    tiers = (ROOT / 'plugins/bigworld/tiers.py').read_text(encoding='utf-8')
    assert tiers.count('touch(obj)') >= 4          # every stamp it writes or drops


# -- what physics hears -------------------------------------------------------

def _falling():
    world = _world(_floor())
    prop = _prop((0.0, 600.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    _run(world, 5)
    return world, prop


@pytest.mark.parametrize('flag', ['hidden', 'disabled'])
def test_a_journalled_flag_stops_a_body_and_clearing_it_lets_it_go(flag):
    world, prop = _falling()
    set_live_flag(prop, flag, True)
    held = prop.pos[1]
    _run(world, 30)
    assert prop.pos[1] == held

    clear_live_flag(prop, flag)
    _run(world, 5)
    assert prop.pos[1] < held


def test_the_authored_writer_is_heard_too():
    world, prop = _falling()
    set_authored_flag(prop, 'disabled', True)
    held = prop.pos[1]
    _run(world, 30)
    assert prop.pos[1] == held


def test_a_flag_written_directly_is_not_heard_and_physics_says_so(capsys):
    """Not repaired: the body goes on as the journal last described it, so
    the mistake shows where it was made instead of costing every body a look
    every step."""
    world, prop = _falling()
    prop.name = 'crate_7'
    world.AUDIT_FLAGS_EVERY = world._audit_in = 10
    prop.properties['disabled'] = True                 # behind the journal's back
    before = prop.pos[1]
    _run(world, world.AUDIT_FLAGS_EVERY + 1)

    assert prop.pos[1] < before                        # still simulated
    said = capsys.readouterr().out
    assert "crate_7" in said and "set_live_flag" in said
    _run(world, world.AUDIT_FLAGS_EVERY * 2)
    assert capsys.readouterr().out.count("crate_7") == 0   # once, not every time


def test_a_tier_stamp_is_heard_through_the_journal():
    from engine.change_journal import touch

    world, prop = _falling()
    prop.properties[SIM_TIER_KEY] = TIER_DISTANT
    touch(prop)                                        # as the classifier does
    held = prop.pos[1]
    _run(world, 30)
    assert prop.pos[1] == held


def test_a_steps_look_at_flags_is_the_journal_not_the_bodies():
    """With nothing journalled, a step reads no entity's properties at all."""
    world = Soma(SpatialGrid())
    reads = []

    class Watched(dict):
        def get(self, key, default=None):
            reads.append(key)
            return dict.get(self, key, default)

    props = [_prop((60.0 * k, 600.0, 0.0)) for k in range(20)]
    world.rebuild([_drum_brush(prop) for prop in props])
    for prop in props:
        world.wake(prop)
        prop.properties = Watched(prop.properties)
    world.AUDIT_FLAGS_EVERY = 0
    world.step(1.0 / 60.0)
    reads.clear()
    _run(world, 30)

    assert [key for key in reads if key in ('hidden', 'disabled', SIM_TIER_KEY)] == []
