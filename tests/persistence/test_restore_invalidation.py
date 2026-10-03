"""Restoring a save must leave no stale visibility or spatial state behind.

A save records what every object *is*: hidden or not, disabled or not, where it
stands.  Restoring it is an overlay onto a live play session — the scene is not
rebuilt, which is what keeps identities and caches valid — and that is exactly
what makes the invalidation question sharp.  Two kinds of derived state are
built from an object's ``hidden``/``disabled`` flags and must be rebuilt when a
restore moves them:

* **per-frame** state (the render cull buffers), which re-reads ``hidden``
  every frame and therefore heals itself; and
* **durable** state — the collision grid above all, which files a brush by
  :func:`engine.spatial.authored_hidden` *once* on play-mode enter and then
  outlives every frame.  Nothing re-reads it, so nothing repairs it.

The sequence these tests walk is the one a player actually performs::

    entity -> save -> change/unload state -> restore -> visibility + spatial
    state rebuilt

They drive the real ``LogicThread``, the real ``SpatialGrid`` and the real
``engine.savegame``, because the defect they cover lives precisely in the seam
between them: a restore that announced itself through a hook the engine host
never implemented.
"""

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState             # noqa: E402
from editor.things import Monster, PlayerStart          # noqa: E402
from engine import savegame                             # noqa: E402
from engine.logic_thread import LogicThread             # noqa: E402
from engine.player import Player                        # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "restore_invalidation.json"


@pytest.fixture
def session():
    """``(state, thread)`` in play mode over a small world, always shut down."""
    made = []

    def _build(brushes=(), things=()):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state)
        made.append(thread)
        thread.player = Player(0.0, 0.0)
        thread.set_play_mode(True)
        return state, thread

    yield _build

    for thread in made:
        thread.set_play_mode(False)
        thread.stop()


def _in_grid(thread, brush):
    grid = thread._spatial_grid
    return any(b is brush for b in grid._all_solid)


# ---------------------------------------------------------------------------
# The engine host implements the invalidation contract at all
# ---------------------------------------------------------------------------

def test_the_logic_thread_implements_both_visibility_notifications(session):
    """The hooks a restore (and a streaming layer) announce through.

    ``engine.savegame`` has always announced a restore through a named hook on
    its host.  For as long as ``LogicThread`` did not implement one, that
    announcement went nowhere and every durable cache below stayed stale.
    """
    _state, thread = session(brushes=[box_brush("floor", (0, -16, 0), (1024, 32, 1024))])
    for hook in ("notify_visibility_changed", "notify_authored_visibility_changed"):
        assert callable(getattr(thread, hook, None)), (
            "LogicThread is the streaming host and the savegame host; it must "
            "implement %s(), or the announcement has no receiver" % hook)


def test_the_cheap_notification_does_not_rebuild_the_collision_grid(session):
    """The two notifications are two different costs, and must stay so.

    A streaming layer calls the cheap one on every cell crossing.  If that
    rebuilt the collision grid it would be an O(total brushes) pass per
    crossing — the exact cost streaming exists to abolish.
    """
    floor = box_brush("floor", (0, -16, 0), (1024, 32, 1024))
    _state, thread = session(brushes=[floor])

    before = list(thread._spatial_grid._all_solid)
    floor["hidden"] = True                      # as a parking pass would not
    thread.notify_visibility_changed()
    assert [id(b) for b in thread._spatial_grid._all_solid] == \
           [id(b) for b in before], (
        "the cheap drawable-set notification rebuilt the collision grid")
    assert thread.visibility_changes > 0, "the drawable-set counter did not move"


# ---------------------------------------------------------------------------
# entity -> save -> change -> restore -> spatial state rebuilt
# ---------------------------------------------------------------------------

def test_restoring_a_visible_brush_puts_it_back_into_collision(session):
    """The defect this file exists for: a wall you can see and walk through.

    Saved while solid, hidden during play (an I/O ``Hide``), then restored.  The
    render path re-reads ``hidden`` and draws it again; the collision grid does
    not, so without invalidation the player walks straight through it.
    """
    wall = box_brush("wall", (0, 64, 0), (128, 128, 128))
    _state, thread = session(brushes=[wall],
                             things=[make_thing(PlayerStart, "spawn", (0, 64, 0))])
    assert _in_grid(thread, wall), "fixture: the wall should start solid"

    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    wall["hidden"] = True
    thread._spatial_grid.populate(thread.brushes)   # as a Hide would
    assert not _in_grid(thread, wall), "fixture: the wall should now be gone"

    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert not wall.get("hidden"), "the restore did not un-hide the wall"
    assert _in_grid(thread, wall), (
        "the wall is visible again but absent from the collision grid: the "
        "player would walk through a wall that is drawn in front of them")


def test_restoring_a_hidden_brush_takes_it_out_of_collision(session):
    """The same defect the other way round: an invisible wall you bump into."""
    wall = box_brush("wall", (0, 64, 0), (128, 128, 128), hidden=True)
    _state, thread = session(brushes=[wall],
                             things=[make_thing(PlayerStart, "spawn", (0, 64, 0))])
    assert not _in_grid(thread, wall), "fixture: an authored-hidden wall is not solid"

    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    wall["hidden"] = False
    thread._spatial_grid.populate(thread.brushes)   # as a Show would
    assert _in_grid(thread, wall)

    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert wall.get("hidden") is True, "the restore did not re-hide the wall"
    assert not _in_grid(thread, wall), (
        "the wall is hidden again but still collidable: an invisible wall")


def test_the_announcement_happens_after_the_state_it_describes_moved(session):
    """Ordering, not just presence.

    Announced *before* the overlay, the notification makes a host rebuild its
    caches from the pre-restore world — which is worse than not announcing at
    all, because the cache then looks fresh.  The grid content observed at
    notification time is what pins the order.
    """
    wall = box_brush("wall", (0, 64, 0), (128, 128, 128))
    _state, thread = session(brushes=[wall],
                             things=[make_thing(PlayerStart, "spawn", (0, 64, 0))])
    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    wall["hidden"] = True
    thread._spatial_grid.populate(thread.brushes)

    seen = []
    real = thread.notify_authored_visibility_changed

    def _record():
        seen.append(bool(wall.get("hidden")))
        real()

    thread.notify_authored_visibility_changed = _record
    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert seen == [False], (
        "the host was told the world's visibility changed while the world "
        "still held its pre-restore value (observed hidden=%r)" % (seen,))


def test_a_delta_restore_invalidates_too(session):
    """Every restore path funnels through one announcement, not just ``full``.

    A delta carries only what differs from the base map, so the wall has to be
    hidden *before* the save for the save to have an opinion about it at all.
    """
    wall = box_brush("wall", (0, 64, 0), (128, 128, 128))
    state, thread = session(brushes=[wall],
                            things=[make_thing(PlayerStart, "spawn", (0, 64, 0))])
    base_level = state.get_level_data()

    wall["hidden"] = True                       # the gameplay change being saved
    snapshot = savegame.build_snapshot(thread, map_name=MAP,
                                       save_mode=savegame.SAVE_MODE_DELTA,
                                       base_level=base_level)
    assert snapshot["save_mode"] == savegame.SAVE_MODE_DELTA
    assert snapshot["delta"]["level"]["brushes"], (
        "fixture: the hidden wall should be in the delta")

    # Back to the base map's state, as loading it fresh would leave it.
    wall["hidden"] = False
    thread._spatial_grid.populate(thread.brushes)
    assert _in_grid(thread, wall)

    report = savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert report["mode"] == savegame.SAVE_MODE_DELTA
    assert wall.get("hidden") is True, "the delta did not re-hide the wall"
    assert not _in_grid(thread, wall), (
        "a delta restore left the collision grid describing the world as it "
        "was before the load")


# ---------------------------------------------------------------------------
# Entity visibility / simulation state
# ---------------------------------------------------------------------------

def test_restoring_clears_an_entity_flag_the_save_does_not_carry(session):
    """Absence in a save is a restored value, not "leave it alone".

    A serialized level drops a false ``hidden``, so an entity hidden *after* the
    save was taken has to come back visible.  Treating the missing key as "no
    opinion" is how a monster stays invisible for the rest of the session.
    """
    monster = make_thing(Monster, "grunt", (0, 64, 0))
    _state, thread = session(things=[make_thing(PlayerStart, "spawn", (0, 64, 0)),
                                     monster])
    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    monster.properties["hidden"] = True
    monster.properties["disabled"] = True

    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert not monster.properties.get("hidden"), (
        "the monster was hidden after the save was taken and stayed hidden "
        "through the load")
    assert not monster.properties.get("disabled"), (
        "the monster's simulation stayed switched off through the load")


def test_restoring_reinstates_an_entity_flag_the_save_does_carry(session):
    monster = make_thing(Monster, "grunt", (0, 64, 0))
    monster.properties["hidden"] = True
    _state, thread = session(things=[make_thing(PlayerStart, "spawn", (0, 64, 0)),
                                     monster])
    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    monster.properties["hidden"] = False

    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert monster.properties.get("hidden") is True, (
        "a hidden entity came back visible")


def test_a_restore_never_invents_a_flag_the_map_never_had(session):
    """Round-trip hygiene: restoring must not grow the saved document."""
    brush = box_brush("plain", (0, 64, 0))
    monster = make_thing(Monster, "grunt", (0, 64, 0))
    _state, thread = session(brushes=[brush],
                             things=[make_thing(PlayerStart, "spawn", (0, 64, 0)),
                                     monster])
    snapshot = savegame.build_snapshot(thread, map_name=MAP)

    savegame.restore_auto(thread, snapshot, current_map_name=MAP)

    assert "hidden" not in brush, "the restore added 'hidden' to a plain brush"
    assert "disabled" not in monster.properties, (
        "the restore added 'disabled' to an entity that never carried it")
