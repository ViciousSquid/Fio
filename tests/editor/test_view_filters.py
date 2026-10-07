"""Editor view filters (the Filter menu, GtkRadiant style).

Every brush and entity belongs to one filter group, decided by the same class
bits the dense tables carry. Turning a group off:

* leaves its rows out of what the logic thread publishes while editing --
  ``visible_brush_slots`` / ``all_brush_slots`` for brushes,
  ``visible_thing_slots`` for entities -- so no renderer is asked to draw them;
* hides them from the 2D views and from picking;
* drops the I/O and path-node connection lines that touch them;

and Play ignores the filters altogether.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor.things import (                                   # noqa: E402
    Light, LogicRelay, Monster, PathNode, PlayerStart, Portal, Speaker)
from engine import entity_table as et                         # noqa: E402
from engine import render_table as rt                         # noqa: E402
from engine.effect_entity import Effect                       # noqa: E402
from engine.prop_entity import Prop                           # noqa: E402
from engine.view_filters import KEYS, ViewFilters, brush_group, entity_group  # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt


def _brushes():
    return {
        "world": box_brush("crate", (0.0, 64.0, -200.0), (128.0, 128.0, 128.0)),
        "water": box_brush("pool", (300.0, 16.0, 0.0), (128.0, 32.0, 128.0), shader="Water"),
        "glass": box_brush("pane", (-300.0, 64.0, 0.0), (128.0, 128.0, 8.0), shader="Glass"),
        "fog": box_brush("mist", (0.0, 64.0, 300.0), (128.0, 128.0, 128.0), shader="Fog"),
        "triggers": box_brush("zone", (0.0, 64.0, 600.0), (64.0, 64.0, 64.0), is_trigger=True),
        "movers": box_brush("lift", (600.0, 64.0, 0.0), (64.0, 64.0, 64.0), is_mover=True),
        "door": box_brush("door", (-600.0, 64.0, 0.0), (64.0, 128.0, 16.0), is_door=True),
    }


def _things():
    a = make_thing(PathNode, "a", (0.0, 8.0, 100.0), next_node="b")
    b = make_thing(PathNode, "b", (100.0, 8.0, 100.0))
    return {
        "lights": make_thing(Light, "lamp", (0.0, 300.0, 0.0)),
        "pathnodes": a, "pathnodes_b": b,
        "monsters": make_thing(Monster, "grunt", (200.0, 0.0, 200.0)),
        "portals": make_thing(Portal, "door_portal", (-200.0, 64.0, 200.0)),
        "props": make_thing(Prop, "drum", (200.0, 0.0, -200.0), render_mode="model",
                            model_path=Prop.DEFAULT_MODEL_PATH),
        "effects": make_thing(Effect, "boom", (0.0, 96.0, -400.0), effect_type="FIRE"),
        "sounds": make_thing(Speaker, "radio", (50.0, 32.0, 50.0)),
        "player": make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=0.0),
        "logic": make_thing(LogicRelay, "relay", (80.0, 32.0, 80.0)),
    }


# -- grouping -----------------------------------------------------------------------

def test_every_kind_of_brush_has_its_group():
    for expected, brush in _brushes().items():
        group = brush_group(rt._brush_class_bits(brush))
        assert group == ("movers" if expected == "door" else expected), brush["name"]


def test_every_kind_of_entity_has_its_group():
    for expected, thing in _things().items():
        assert entity_group(thing) == expected.split("_")[0], type(thing).__name__


def test_the_menu_covers_what_was_asked_for():
    for key in ("world", "lights", "fog", "water", "glass", "movers", "triggers",
                "pathnodes", "monsters", "props", "effects", "portals", "terrain"):
        assert key in KEYS


def test_switching_a_group_off_and_on():
    filters = ViewFilters()
    assert not filters.active and filters.shows("water")
    assert filters.set_shown("water", False) and not filters.shows("water")
    assert not filters.set_shown("water", False)         # no change, no new version
    version = filters.version
    filters.show_all()
    assert filters.shows("water") and filters.version == version + 1
    with pytest.raises(KeyError):
        filters.set_shown("nonsense", False)


def test_dense_masks_agree_with_the_object_predicates():
    brushes = list(_brushes().values())
    table = rt.RenderTable()
    table.sync(brushes, 1)
    things = list(_things().values())
    etable = et.EntityTable()
    etable.begin_frame(things, epoch=1)
    filters = ViewFilters()
    for key in KEYS:
        filters.show_all()
        filters.set_shown(key, False)
        rows = filters.hidden_brush_rows(table.class_bits[:table.count])
        for brush in brushes:
            assert rows[table.slot_of_id[brush["id"]]] == filters.hides_brush(brush), (key, brush["name"])
        rows = filters.hidden_entity_rows(etable)
        for slot, thing in enumerate(etable.things[:etable.count]):
            assert rows[slot] == filters.hides_thing(thing), (key, type(thing).__name__)


# -- publication: what every renderer is given ----------------------------------------

def _level():
    return level_data(brushes=room(size=2048.0, height=512.0) + list(_brushes().values()),
                      things=list(_things().values()))


def _published(session):
    session.publish()
    with session.render_state() as frame:
        brushes = {frame.render_table.ids[int(s)] for s in frame.all_brush_slots}
        visible = {frame.render_table.ids[int(s)] for s in frame.visible_brush_slots}
        etable = frame.entity_table
        things = {etable.things[int(s)].properties["id"] for s in frame.visible_thing_slots}
    assert visible <= brushes
    return brushes, things


def test_filtered_rows_are_not_published_while_editing(fio_session):
    session = fio_session(_level())
    window = session.window
    brushes, things = _published(session)
    by_name = {b["name"]: b["id"] for b in window.state.brushes}
    assert by_name["pool"] in brushes and by_name["lift"] in brushes

    window.set_view_filter("water", False)
    window.set_view_filter("movers", False)
    window.set_view_filter("lights", False)
    window.set_view_filter("pathnodes", False)
    brushes, things = _published(session)
    assert by_name["pool"] not in brushes
    assert by_name["lift"] not in brushes and by_name["door"] not in brushes
    assert by_name["crate"] in brushes and by_name["pane"] in brushes
    hidden = {t.properties["id"] for t in window.state.things
              if isinstance(t, (Light, PathNode))}
    assert hidden and not hidden & things
    assert any(isinstance(t, Monster) and t.properties["id"] in things
               for t in window.state.things)

    window.show_all_view_filters()
    brushes, things = _published(session)
    assert by_name["pool"] in brushes and hidden <= things


def test_play_ignores_the_filters(fio_session):
    session = fio_session(_level())
    window = session.window
    for key in KEYS:
        window.set_view_filter(key, False)
    session.start_play()
    session.step(1)
    brushes, _things = _published(session)
    assert {b["id"] for b in window.state.brushes if b["name"] == "crate"} <= brushes


# -- the editor's own views ------------------------------------------------------------------

def test_the_filter_menu_drives_the_filters(main_window):
    menu = main_window.filter_menu
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Water" in labels and "Path nodes" in labels and "Show All" in labels
    water = main_window.filter_actions["water"]
    assert water.isChecked()
    water.setChecked(False)
    assert not main_window.state.view_filters.shows("water")
    main_window.show_all_view_filters()
    assert water.isChecked() and main_window.state.view_filters.shows("water")


def test_the_2d_views_and_picking_skip_filtered_objects(fio_session):
    session = fio_session(_level())
    window = session.window
    view = window.view_top
    pool = next(b for b in window.state.brushes if b["name"] == "pool")
    lamp = next(t for t in window.state.things if isinstance(t, Light))
    assert not view._hidden(pool) and not view._hidden(lamp)
    window.set_view_filter("water", False)
    window.set_view_filter("lights", False)
    assert view._hidden(pool) and view._hidden(lamp)
    # Hidden by the map still counts as hidden, filters or not.
    window.show_all_view_filters()
    pool["hidden"] = True
    assert view._hidden(pool)


def test_hiding_path_nodes_hides_their_connection_lines(fio_session):
    session = fio_session(_level())
    window, view3d = session.window, session.view
    olive = (128.0 / 255.0, 128.0 / 255.0, 0.0)
    assert any(line["color"] == olive for line in view3d._gather_io_connections())
    window.set_view_filter("pathnodes", False)
    assert not any(line["color"] == olive for line in view3d._gather_io_connections())


@pytest.mark.gl
def test_the_3d_view_draws_nothing_of_a_filtered_group(fio_session):
    """Every renderer gets fewer rows; here, the built-in one draws fewer pixels."""
    session = fio_session(level_data(
        brushes=[box_brush("crate", (0.0, 64.0, -200.0), (256.0, 256.0, 256.0))],
        things=[make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=0.0)]),
        size=(160, 120))
    session.view.sysmon.set_active(False)
    session.publish()
    shown = session.paint().astype(np.int16)
    session.window.set_view_filter("world", False)
    session.publish()
    filtered = session.paint().astype(np.int16)
    changed = float((np.abs(shown - filtered).max(axis=2) > 2).mean())
    assert changed > 0.02, f"the crate is still drawn ({changed:.4f} of the view changed)"
    session.window.set_view_filter("world", True)
    session.publish()
    assert np.abs(shown - session.paint().astype(np.int16)).max() <= 2
