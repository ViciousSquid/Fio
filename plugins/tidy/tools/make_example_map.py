"""
Generate maps/Tidy_Test.json — a small demo level for the Tidy plugin.

A closed room with a player start, a shelf (brush) fronted by a TidyReceptacle,
a grid of TidyObjects scattered on the floor, and a TidyGoal wired to a level
message. Things are serialised through the real entity classes so the file
always matches the current schema.

Run from the repo root:  python -m plugins.tidy.tools.make_example_map
"""

import json
import os
import sys
import uuid

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def brush(pos, size, tex="Dev/512.jpg"):
    return {
        "pos": [float(pos[0]), float(pos[1]), float(pos[2])],
        "size": [float(size[0]), float(size[1]), float(size[2])],
        "textures": {k: tex for k in ("north", "south", "east", "west", "top", "down")},
        "is_trigger": False,
        "id": str(uuid.uuid4()),
    }


def main():
    from plugins.manager import load_plugins
    load_plugins()
    from plugins.tidy.entities import TidyObject, TidyReceptacle, TidyGoal
    from editor.things import PlayerStart, Light

    HALF = 640.0        # room half-width (interior)
    WALL_H = 512.0
    WALL_T = 64.0
    FLOOR_TOP = 0.0

    brushes = []
    # Floor and ceiling
    brushes.append(brush([0, FLOOR_TOP - 32, 0], [HALF * 2 + WALL_T * 2, 64, HALF * 2 + WALL_T * 2]))
    brushes.append(brush([0, WALL_H + 32, 0], [HALF * 2 + WALL_T * 2, 64, HALF * 2 + WALL_T * 2]))
    # Four walls
    brushes.append(brush([0, WALL_H / 2, HALF + WALL_T / 2], [HALF * 2 + WALL_T * 2, WALL_H, WALL_T]))
    brushes.append(brush([0, WALL_H / 2, -HALF - WALL_T / 2], [HALF * 2 + WALL_T * 2, WALL_H, WALL_T]))
    brushes.append(brush([HALF + WALL_T / 2, WALL_H / 2, 0], [WALL_T, WALL_H, HALF * 2]))
    brushes.append(brush([-HALF - WALL_T / 2, WALL_H / 2, 0], [WALL_T, WALL_H, HALF * 2]))
    # A shelf ledge against the far (-Z) wall for objects to be placed on.
    shelf_top = 120.0
    brushes.append(brush([0, shelf_top - 12, -HALF + 40], [400, 24, 60]))

    things = []

    ps = PlayerStart(pos=[0.0, 40.0, HALF - 120.0], properties={"angle": 180.0})
    things.append(ps.to_dict())

    light = Light(pos=[0.0, WALL_H - 80.0, 0.0],
                  properties={"intensity": 1.1, "radius": 2000.0, "state": "on"})
    things.append(light.to_dict())

    # Receptacle sitting just above the shelf ledge, accepting anything.
    shelf = TidyReceptacle(
        pos=[0.0, shelf_top + 12.0, -HALF + 40.0],
        properties={
            "name": "Shelf",
            "accepts": "any",
            "capacity": 48,
            "slot_cols": 12,
            "slot_spacing": [30.0, 44.0, 0.0],
            "slot_offset": [0.0, 6.0, 0.0],
            "reach": 180.0,
        },
    )
    things.append(shelf.to_dict())

    # A 6x7 grid of books scattered on the floor to tidy away.
    idx = 0
    for row in range(7):
        for col in range(6):
            x = (col - 2.5) * 90.0
            z = (row - 1.0) * 90.0 + 40.0
            obj = TidyObject(
                pos=[x, 12.0, z],
                properties={"name": f"Book_{idx+1}", "category": "book"},
            )
            things.append(obj.to_dict())
            idx += 1

    # Goal: tidy everything. Wire OnComplete -> Shelf (as a harmless demo link;
    # mappers would target a LevelChanger, Speaker, etc.).
    goal = TidyGoal(pos=[0.0, 60.0, HALF - 60.0],
                    properties={"name": "TidyGoal", "target": "all", "category": "any"})
    goal.add_output_connection("OnComplete", "Shelf", "Disable",
                               target_id=shelf.properties["id"])
    things.append(goal.to_dict())

    doc = {"version": 3, "brushes": brushes, "things": things}
    out = os.path.join(_ROOT, "maps", "Tidy_Test.json")
    with open(out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"Wrote {out}: {len(brushes)} brushes, {len(things)} things "
          f"({idx} tidy objects)")


if __name__ == "__main__":
    main()
