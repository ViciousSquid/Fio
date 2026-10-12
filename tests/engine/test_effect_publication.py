"""Effect rows publish the store's execution columns when they change.

``EntityTable._advance_effects`` used to copy five columns of the
``EffectStore`` into the Effect rows every tick, pairing rows by position.
They are now published when the store's ``generation`` (or the table's Effect
rows) move, paired by object identity, and the tick does only the clock's
arithmetic.  What is pinned here:

* over a timed run with triggers, type changes, removal and insertion, in
  Play and in the editor preview, every column the tables publish is
  bit-identical to the per-tick copy (kept below, verbatim, as the
  reference) -- in both tables of the double buffer;
* a store column changed without the generation moving is *not* copied
  (fails before: the copy ran every tick);
* a store whose rows are out of world order still publishes each Effect's
  own state (fails before: rows were paired by position);
* ``EffectStore`` row order is defined: world order after a rebuild,
  first-synced order for rows appended in between;
* a trigger is published on the next frame, to both buffers.
"""

import itertools

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")
pytestmark = pytest.mark.qt

from editor.things import Effect                                # noqa: E402
import engine.entity_table as et                                # noqa: E402
from engine.effect_table import (                               # noqa: E402
    FAMILY_EXPLOSION, FAMILY_FIRE, FAMILY_ORB, EffectStore)
from engine.entity_table import EntityTable                     # noqa: E402


def _per_tick_copy_advance(self, effect_runtime, effect_store=None):
    """The previous _advance_effects, verbatim: copies every tick."""
    effect_ls = self.effect_slots
    now = et.time.perf_counter()

    if effect_store is not None:
        count = len(effect_ls)
        family = effect_store.family_id[:count]
        active = effect_store.active[:count]
        lifetime = np.maximum(effect_store.lifetime[:count], 0.01)
        # The animation's origin, as _resolve_row derives it: an Effect
        # with no playback start runs on the shared clock origin.
        spawn = effect_store.spawn_time[:count]
        spawn = np.where(spawn > 0.0, spawn, et._CLOCK_ORIGIN)
        phase = np.clip(effect_store.phase[:count], 0.0, 1.0)
        self.effect_type[effect_ls] = family
        self.effect_lifetime[effect_ls] = lifetime
        self.effect_spawn_time[effect_ls] = spawn
        self.effect_phase[effect_ls] = phase
        self.effect_active[effect_ls] = active
    else:
        family = self.effect_type[effect_ls]
        active = self.effect_active[effect_ls]
        lifetime = np.maximum(self.effect_lifetime[effect_ls], 0.01)
        spawn = self.effect_spawn_time[effect_ls]
        phase = self.effect_phase[effect_ls]

    explosion = family == 1
    fire = ~explosion

    if effect_runtime:
        elapsed = np.maximum(now - spawn, 0.0).astype(
            np.float32, copy=False
        )
        explosion_active = (
            explosion & active & (elapsed < lifetime)
        )
        preview_explosion = np.zeros_like(explosion)
    else:
        elapsed = np.where(
            fire,
            np.float32(now - et._CLOCK_ORIGIN),
            np.float32(0.0),
        ).astype(np.float32)
        explosion_active = np.zeros_like(explosion)
        preview_explosion = (
            explosion & self.effect_preview[effect_ls]
        )

    self.light_enabled[effect_ls] = self.effect_light_enabled[effect_ls]
    self.light_params[effect_ls, 0] = self.effect_params[effect_ls, 2]
    self.light_params[effect_ls, 1] = self.effect_params[effect_ls, 3]
    if explosion.any():
        explosion_slots = effect_ls[explosion]
        if effect_runtime:
            explosion_elapsed = elapsed[explosion]
            flash_active = (
                explosion_active[explosion]
                & (explosion_elapsed < np.minimum(
                    lifetime[explosion], 0.12))
            )
            decay = np.exp(-explosion_elapsed / 0.035).astype(
                np.float32
            )
            self.light_enabled[explosion_slots] = (
                self.effect_light_enabled[explosion_slots]
                & flash_active
            )
            self.light_params[explosion_slots, 0] = (
                self.effect_params[explosion_slots, 2]
                * (1.0 + 2.0 * decay)
            )
        else:
            self.light_enabled[explosion_slots] = False

    if preview_explosion.any():
        preview_t = (
            (float(et._EXPLOSION_PREVIEW_FRAME) - 0.5)
            / et._EXPLOSION_FRAME_COUNT
        )
        elapsed = np.where(
            preview_explosion,
            (lifetime * preview_t).astype(np.float32),
            elapsed,
        )
    self.effect_elapsed[effect_ls] = elapsed

    if fire.any():
        fire_slots = effect_ls[fire]
        flicker = et._effect_flicker(
            self.effect_seed[fire_slots],
            elapsed[fire] + phase[fire],
        )
        self.light_params[fire_slots, 0] = (
            self.effect_params[fire_slots, 2]
            * (0.78 + 0.38 * flicker)
        )

    self.effect_alive[effect_ls] = (
        fire | explosion_active | preview_explosion
    )


class _PerTickCopyTable(EntityTable):
    """An EntityTable advancing its Effects the way it used to."""
    _advance_effects = _per_tick_copy_advance


#: Every column the Effect pass writes, and the light columns it drives.
_COLUMNS = ("effect_type", "effect_lifetime", "effect_spawn_time",
            "effect_phase", "effect_active", "effect_elapsed", "effect_alive",
            "light_enabled", "light_params")


class _Clock:
    def __init__(self, monkeypatch):
        self.now = et._CLOCK_ORIGIN + 50.0
        monkeypatch.setattr(et.time, "perf_counter", lambda: self.now)

    def tick(self, seconds=1.0 / 60.0):
        self.now += seconds


def _effects():
    kinds = [("FIRE", {}), ("EXPLOSION", {"lifetime": 0.6}),
             ("ORB", {}), ("EXPLOSION", {"preview": True, "lifetime": 0.9}),
             ("FIRE", {"light_enabled": False}), ("CUSTOM", {}),
             ("EXPLOSION", {"light_intensity": 6.0, "lifetime": 0.2})]
    return [Effect(pos=(10.0 * i, 0.0, 0.0),
                   properties=dict({"effect_type": kind, "effect_seed": 7 + i}, **extra))
            for i, (kind, extra) in enumerate(kinds)]


def _assert_same(new, old, where):
    for name in _COLUMNS:
        a = getattr(new, name)[:new.count]
        b = getattr(old, name)[:old.count]
        assert a.tobytes() == b.tobytes(), (where, name, a, b)


@pytest.mark.parametrize("runtime", [True, False], ids=["play", "editor-preview"])
def test_published_columns_match_the_per_tick_copy(monkeypatch, runtime):
    clock = _Clock(monkeypatch)
    things = _effects()
    store = EffectStore()
    store.begin_session(things)
    # Both buffers of the double buffer, each against its reference.
    pairs = [(EntityTable(), _PerTickCopyTable()) for _ in range(2)]
    epoch = 1
    extra = Effect(pos=(1.0, 2.0, 3.0), properties={"effect_type": "EXPLOSION"})
    explosion_frames = 0
    for tick in range(240):
        clock.tick()
        if tick == 10:
            store.trigger_explosion(things[1], clock.now)
        if tick == 30:
            store.set_type(things[0], "EXPLOSION")
            store.trigger_explosion(things[0], clock.now)
        if tick == 45:
            store.trigger_explosion(things[6], clock.now - 0.05)
        if tick == 60:
            store.set_type(things[2], "FIRE")
        if tick == 90:                       # removal: the list changes
            things = things[:3] + things[4:]
            epoch += 1
        if tick == 120:                      # insertion mid-list
            things = things[:2] + [extra] + things[2:]
            epoch += 1
        if tick == 130:
            store.trigger_explosion(extra, clock.now)
        if tick == 200:
            store.phase[:store.count] = np.linspace(-0.5, 1.5, store.count)
            store.generation += 1            # a direct, announced write
        new, old = pairs[tick % 2]
        for table in (new, old):
            table.begin_frame(things, epoch, effect_runtime=runtime,
                              effect_store=store)
        _assert_same(new, old, (tick, runtime))
        explosion_frames += bool(
            (new.effect_alive[new.effect_slots]
             & (new.effect_type[new.effect_slots] == FAMILY_EXPLOSION)).any())
    # Not vacuous: explosions played (or previewed) for a good part of the run.
    assert explosion_frames > 20, explosion_frames


def test_a_store_change_that_is_not_announced_is_not_copied(monkeypatch):
    _Clock(monkeypatch)
    things = _effects()
    store = EffectStore()
    store.begin_session(things)
    table = EntityTable()
    table.begin_frame(things, 1, effect_runtime=True, effect_store=store)
    assert int(table.effect_type[2]) == FAMILY_ORB

    # Written behind the store's back: the tick must not pick it up, or it
    # is still copying every tick.
    store.family_id[2] = FAMILY_FIRE
    table.begin_frame(things, 1, effect_runtime=True, effect_store=store)
    assert int(table.effect_type[2]) == FAMILY_ORB

    store.generation += 1
    table.begin_frame(things, 1, effect_runtime=True, effect_store=store)
    assert int(table.effect_type[2]) == FAMILY_FIRE


def test_rows_out_of_world_order_publish_to_their_own_effect(monkeypatch):
    clock = _Clock(monkeypatch)
    things = _effects()
    store = EffectStore()
    store.begin_session(things)
    table = EntityTable()
    table.begin_frame(things, 1, effect_runtime=True, effect_store=store)

    # The store's rows reversed against the world, the table not reconciled.
    store.rebuild(list(reversed(things)))
    store.trigger_explosion(things[1], clock.now)
    clock.tick(0.01)
    table.begin_frame(things, 1, effect_runtime=True, effect_store=store)

    for slot, thing in enumerate(things):
        row = store.index_of(thing)
        assert row == len(things) - 1 - slot
        assert int(table.effect_store_index[slot]) == row
        assert int(table.effect_type[slot]) == int(store.family_id[row]), slot
        assert bool(table.effect_active[slot]) == bool(store.active[row]), slot
    assert bool(table.effect_alive[1]), "the triggered explosion is not playing"
    assert float(table.effect_spawn_time[1]) == pytest.approx(clock.now - 0.01)


def test_store_rows_have_a_defined_order():
    a, b, c, d = (Effect(properties={"effect_type": "FIRE"}) for _ in range(4))
    store = EffectStore()
    store.begin_session([a, b])
    assert [store.index_of(t) for t in (a, b)] == [0, 1]
    # Effects first seen between rebuilds follow, in first-synced order.
    assert store.sync_authored(d) == 2
    assert store.trigger_explosion(c, 5.0) == 3
    assert store.sync_authored(d) == 2              # already held: same row
    store.phase[2] = 0.25
    # A rebuild puts every row in world order, keeping execution state.
    store.rebuild([d, a, c, b])
    assert [store.index_of(t) for t in (d, a, c, b)] == [0, 1, 2, 3]
    assert float(store.phase[0]) == pytest.approx(0.25)
    assert int(store.family_id[2]) == FAMILY_EXPLOSION
    assert float(store.spawn_time[2]) == 5.0


def test_every_published_column_change_moves_the_generation():
    a, b = Effect(properties={"effect_type": "FIRE"}), Effect(properties={})
    store = EffectStore()
    seen = [store.generation]

    def moved():
        seen.append(store.generation)
        return seen[-1] != seen[-2]

    store.begin_session([a]);                         assert moved()
    store.sync_authored(a);                           assert moved()
    store.sync_authored(b);                           assert moved()   # append
    store.refresh_authored(0, a);                     assert moved()
    store.set_type(a, "ORB");                         assert moved()
    store.trigger_explosion(b, 1.0);                  assert moved()
    store.rebuild([b, a]);                            assert moved()
    store.set_position(a, (1.0, 2.0, 3.0));           assert not moved()
    store.clear();                                    assert moved()


def test_a_trigger_reaches_both_buffers_on_the_next_frame(monkeypatch):
    clock = _Clock(monkeypatch)
    effect = Effect(properties={"effect_type": "EXPLOSION", "lifetime": 0.5})
    store = EffectStore()
    store.begin_session([effect])
    tables = [EntityTable(), EntityTable()]
    for table in tables:
        table.begin_frame([effect], 1, effect_runtime=True, effect_store=store)
        assert not bool(table.effect_alive[0])
    store.trigger_explosion(effect, clock.now)
    for table, step in zip(tables, itertools.count()):
        clock.tick()
        table.begin_frame([effect], 1, effect_runtime=True, effect_store=store)
        assert bool(table.effect_active[0]) and bool(table.effect_alive[0]), step
        assert float(table.effect_spawn_time[0]) == float(store.spawn_time[0])
