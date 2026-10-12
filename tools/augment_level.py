"""Add movers, doors, Effects, special volumes and angled brushes to a level.

``tools/procedural_map_gen.py`` makes rooms, lights, monsters and props; this
adds, at its rooms' light positions, what it does not make, so one random
level exercises the runtime paths a level can have:

* 12 linear movers (every third one spinning), the first six toggling a door
  each when fully open, one (fast) triggering an explosion every cycle;
* 6 doors; 3 fire, 2 explosion (one in editor preview) and 1 orb Effect;
* 2 each of water, glass, fog and glow volumes; 4 angled (clipped) brushes.

    python tools/procedural_map_gen.py -o level.json --size large --seed 359815 \
        --room-count 28 --monster-count 10 --random-wall-texture --random-floor-texture
    python tools/augment_level.py level.json level_aug.json 359815
"""
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers.worlds import angled_brush  # noqa: E402

src, dst, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
d = json.load(open(src))
lights = [t for t in d["things"] if t.get("type") == "light"]
rooms = [list(t["pos"]) for t in lights]
rng.shuffle(rooms)
tex = {f: "default.png" for f in ("north", "south", "east", "west", "top", "down")}
brushes, things = d["brushes"], d["things"]
uid = iter(range(10 ** 6))

def bid(prefix):
    return "%s-%06d" % (prefix, next(uid))

def at(room, dy):
    return [room[0] + rng.uniform(-60, 60), max(48.0, room[1] + dy), room[2] + rng.uniform(-60, 60)]

doors = []
for k in range(6):
    room = rooms[k % len(rooms)]
    name = "aug_door_%d" % k
    doors.append(name)
    brushes.append({"id": bid("door"), "name": name, "pos": at(room, -200), "size": [128.0, 160.0, 24.0],
                    "textures": dict(tex), "is_door": True, "door_direction": rng.choice(["up", "north", "east"]),
                    "door_distance": rng.choice([96.0, 128.0]), "door_speed": rng.choice([80.0, 160.0]),
                    "open_time": rng.choice([0.5, 1.0]), "door_lip": 8.0})
effects = []
for k, kind in enumerate(["FIRE", "FIRE", "FIRE", "EXPLOSION", "EXPLOSION", "ORB"]):
    room = rooms[(k + 6) % len(rooms)]
    name = "aug_effect_%d" % k
    effects.append((name, kind))
    things.append({"type": "effect", "pos": at(room, -150), "properties": {
        "type": "effect", "io_enabled": True, "name": name, "id": bid("effect"),
        "effect_type": kind, "preview": k == 4, "silent": True,
        "width": 96.0, "height": 128.0, "intensity": 1.5, "colour": [255, 110, 25],
        "lifetime": rng.choice([0.4, 0.8]), "light_enabled": True, "light_radius": 320.0,
        "light_intensity": 2.5, "light_colour": [255, 165, 70], "effect_seed": rng.randrange(10 ** 9)}})
for k in range(12):
    room = rooms[(k + 12) % len(rooms)]
    io = []
    if k < 6:
        io.append({"output": "OnFullyOpen", "target": doors[k], "input": "Toggle", "parameter": "",
                   "delay": 0.0, "fire_once": False})
    if k == 7:
        io.append({"output": "OnFullyClosed", "target": "aug_effect_3", "input": "Explode",
                   "parameter": "", "delay": 0.0, "fire_once": False})
    b = {"id": bid("mover"), "name": "aug_mover_%d" % k, "pos": at(room, -180), "size": [96.0, 24.0, 96.0],
         "textures": dict(tex), "is_mover": True, "start_on": True,
         "speed": rng.choice([48.0, 96.0, 160.0]), "distance": rng.choice([128.0, 256.0]),
         "direction": rng.choice([[0, 1, 0], [1, 0, 0], [0, 0, 1], [1, 1, 0]]),
         "rotate": k % 3 == 0}
    if k == 7:
        b.update(speed=256.0, distance=64.0)    # cycles in ~0.5 s: explodes often
    if io:
        b["io_connections"] = io
    brushes.append(b)
specials = [("Water", {"water_tint": [0.0, 0.4, 0.6], "water_opacity": 0.5}),
            ("Glass", {"glass_color": [0.9, 0.95, 1.0], "glass_opacity": 0.3}),
            ("Fog", {"is_fog": True, "fog_density": 2.0, "fog_color": [0.5, 0.6, 0.7]}),
            ("Glow", {"glow_intensity": 6.0, "glow_color": [0.1, 0.7, 0.7]})]
for k in range(8):
    shader, extra = specials[k % 4]
    room = rooms[(k + 24) % len(rooms)]
    b = {"id": bid(shader.lower()), "pos": at(room, -170), "size": [192.0, 96.0, 192.0],
         "textures": dict(tex), "shader": shader}
    b.update(copy.deepcopy(extra))
    brushes.append(b)
for k in range(4):
    room = rooms[(k + 3) % len(rooms)]
    a = angled_brush("aug_angled_%d" % k, pos=at(room, -160), size=(128, 128, 128),
                     clip_normal=rng.choice([(1, 1, 0), (0, 1, 1), (1, 0, 1)]))
    a = {k: v for k, v in a.items() if not k.startswith("_")}
    a["id"] = bid("angled")
    a["textures"] = dict(tex)
    brushes.append(a)
json.dump(d, open(dst, "w"), indent=1)
print("augmented:", len(brushes), "brushes", len(things), "things")
