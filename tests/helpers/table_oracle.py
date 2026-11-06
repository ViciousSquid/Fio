"""The table oracle: a published projection must equal a from-scratch rebuild.

Both dense projections state one invariant -- *a published
RenderTable/EntityTable equals a table rebuilt from scratch over the same
objects* -- and the Debug Tables instrument shows the same columns by hand.
This is that check, shared by the stability audit and by the oracle cross-check
(``tests/visual/test_oracle_crosscheck.py``), which pits it against the GL-level
draw oracle in ``tests/helpers/draw_oracle.py``.

Headless: RenderTable and EntityTable are GL-free.
"""

import numpy as np

from engine import entity_table as etm
from engine import render_table as rtm
from engine.entity_table import EntityTable
from engine.render_table import RenderTable

#: Entity columns that are a function of wall-clock time (the Effect clock).
CLOCK_COLUMNS = {"effect_elapsed", "effect_alive", "light_params",
                 "light_enabled", "portal_fade"}
INTERNED = {"tex_name_id": "texture_names", "sprite_key_id": "sprite_recipes",
            "sprite_direction_ids": "sprite_recipes",
            "model_recipe_id": "model_recipes"}


def named(table, name, ids):
    names = getattr(table, INTERNED[name])()
    return [tuple(names[int(i)] if i >= 0 else None for i in np.atleast_1d(row))
            for row in ids]


def diff_columns(pub, fresh, columns, skip=()):
    n = pub.count
    assert n == fresh.count
    bad = []
    for name, *_ in columns:
        if name in skip or not n:
            continue
        a, b = getattr(pub, name)[:n], getattr(fresh, name)[:n]
        if name in INTERNED:
            same = named(pub, name, a) == named(fresh, name, b)
        elif name == "geometry_id":
            same = ([pub.geometry_records[i].signature if i >= 0 else None for i in a]
                    == [fresh.geometry_records[i].signature if i >= 0 else None for i in b])
        elif a.dtype.kind == "f":
            same = np.allclose(a, b, atol=1e-4, equal_nan=True)
        else:
            same = np.array_equal(a, b)
        if not same:
            bad.append(name)
    return bad


def render_table_diff(table):
    """RenderTable columns in which *table* differs from a from-scratch build."""
    fresh = RenderTable()
    fresh.sync(list(table.brushes))
    return ["RT." + c for c in diff_columns(table, fresh, rtm._COLUMNS)]


def diff_rebuild(frame):
    """Columns in which the published frame differs from a from-scratch build."""
    fresh = RenderTable()
    fresh.sync(list(frame.render_table.brushes))
    bad = ["RT." + c for c in diff_columns(frame.render_table, fresh, rtm._COLUMNS)]
    efresh = EntityTable()
    efresh.sync(list(frame.entity_table.things))
    bad += ["ET." + c for c in diff_columns(frame.entity_table, efresh,
                                               etm._COLUMNS, CLOCK_COLUMNS)]
    return bad
