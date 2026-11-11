"""Author process: build a world in the editor, save, play+capture, export .fiopak.

Run from the root of the AUTHOR install (a checkout with extra assets that the
fresh install does not have):  xvfb-run -a python e2e_author.py <outdir>
"""
import json
import os
import struct
import sys
import wave

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.abspath(sys.argv[1])
os.makedirs(OUT, exist_ok=True)

import numpy as np                                    # noqa: E402
from PIL import Image                                 # noqa: E402

import e2e_common as common                           # noqa: E402

# -- assets that exist only in the author's install ---------------------------
os.makedirs("assets/textures", exist_ok=True)
os.makedirs("assets/models/e2e", exist_ok=True)
os.makedirs("assets/sounds", exist_ok=True)
os.makedirs("cutscenes", exist_ok=True)
tile = np.zeros((64, 64, 3), np.uint8)
tile[:, :, 0] = 255                                   # unmistakable: magenta/green checks
tile[:, :, 2] = 255
tile[:32, :32] = tile[32:, 32:] = (0, 255, 0)
Image.fromarray(tile).save("assets/textures/e2e_wall.png")
Image.fromarray(tile[:, ::-1].copy()).save("assets/textures/E2E_Crate_Skin.png")   # mixed case on purpose
with wave.open("assets/sounds/e2e_tone.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(b"".join(struct.pack("<h", int(8000 * np.sin(i / 3.0))) for i in range(800)))
with open("assets/models/e2e/crate.obj", "w", encoding="utf-8") as fh:
    fh.write("mtllib crate.mtl\n"
             "v -24 0 -24\nv 24 0 -24\nv 24 0 24\nv -24 0 24\nv -24 48 -24\nv 24 48 -24\nv 24 48 24\nv -24 48 24\n"
             "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nusemtl skin\n"
             "f 1/1 2/2 6/3 5/4\nf 2/1 3/2 7/3 6/4\nf 3/1 4/2 8/3 7/4\nf 4/1 1/2 5/3 8/4\nf 5/1 6/2 7/3 8/4\nf 4/1 3/2 2/3 1/4\n")
with open("assets/models/e2e/crate.mtl", "w", encoding="utf-8") as fh:
    fh.write("newmtl skin\nKd 1 1 1\nmap_Kd ../../textures/E2E_Crate_Skin.png\n")
with open("cutscenes/e2e_intro.json", "w", encoding="utf-8") as fh:
    json.dump({"version": 2, "id": "e2e-intro", "name": "e2e_intro", "actors": [],
               "camera": [{"time": 0.0, "pos": [0, 120, -300], "yaw": 0.0, "pitch": 0.0, "fov": 90.0},
                          {"time": 1.0, "pos": [0, 160, -200], "yaw": 20.0, "pitch": 5.0, "fov": 90.0}],
               "actor_tracks": {}, "events": [], "map": {}, "settings": {}}, fh)

app, dialogs = common.qt_app()
from PyQt5.QtWidgets import QFileDialog               # noqa: E402
from editor.io_system import OutputConnection, add_connection   # noqa: E402
from editor.things import (Light, LevelChanger, LogicCamera, Monster,      # noqa: E402
                           PlayerStart, Prop, Speaker)
from engine import brush_geometry as bg               # noqa: E402
from tests.helpers.session import FioTestSession      # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush, make_thing, room   # noqa: E402

session = FioTestSession(app, {"version": 3, "brushes": [], "things": []}, size=(320, 180))
window = session.window


def add_brush(brush):
    window.save_state()
    window.state.brushes.append(brush)
    return brush


def add_thing(thing):
    window.save_state()
    window.state.things.append(thing)
    return thing


def save_as(relative):
    window.file_path = os.path.join(ROOT, relative)
    window.save_level()
    assert not window.unsaved_changes and os.path.isfile(window.file_path), relative


# -- the annex: the level a LevelChanger in the main world leads to -----------
window.unsaved_changes = False        # the author answers "Don't save"
window.new_map()
for brush in room(size=512.0, height=192.0):
    add_brush(brush)
add_thing(make_thing(PlayerStart, "AnnexStart", (0.0, 40.0, 0.0), angle=0.0))
add_thing(make_thing(Light, "AnnexLight", (0.0, 150.0, 0.0), colour=[80, 255, 120], radius=700.0))
window.update_all_ui()
save_as("maps/e2e_annex.json")

# -- the main world -----------------------------------------------------------
window.unsaved_changes = False        # the author answers "Don't save"
window.new_map()
for brush in room(size=1024.0, height=256.0):
    add_brush(brush)
floor = window.state.brushes[0]
for brush in window.state.brushes:                    # custom texture on every room surface
    for tag in list(brush.get("textures", {})):
        brush["textures"][tag] = "e2e_wall.png"
pillar = add_brush(angled_brush("pillar", (200.0, 96.0, 250.0), (96.0, 192.0, 96.0),
                                clip_normal=(1.0, 0.0, 1.0)))
door = add_brush(box_brush("door", (-200.0, 96.0, 300.0), (128.0, 192.0, 24.0),
                           is_door=True, door_direction="up", door_distance=160.0, door_lip=8.0,
                           door_speed=80.0, original_pos=[-200.0, 96.0, 300.0]))
trigger = add_brush(box_brush("pad", (0.0, 32.0, 150.0), (160.0, 64.0, 96.0),
                              is_trigger=True, trigger_type="Once", trigger_activation="touch"))
add_connection(trigger, OutputConnection("OnTrigger", "door", "Open", target_id=door["id"]))
pool = add_brush(box_brush("pool", (300.0, 8.0, -250.0), (256.0, 16.0, 256.0), is_water=True))
add_thing(make_thing(PlayerStart, "Start", (0.0, 40.0, -200.0), angle=0.0))
add_thing(make_thing(Light, "WarmLight", (0.0, 200.0, 0.0), colour=[255, 180, 90], radius=900.0,
                     casts_shadows=True))
add_thing(make_thing(Light, "BlueLight", (-300.0, 120.0, -300.0), colour=[60, 90, 255], radius=500.0))
add_thing(make_thing(Monster, "Grunt", (150.0, 40.0, 380.0)))
add_thing(make_thing(Prop, "Crate", (-120.0, 0.0, 120.0), render_mode="model",
                     model_path="assets/models/e2e/crate.obj"))
add_thing(make_thing(Prop, "Shotgun", (60.0, 16.0, 40.0), collect_enabled=True, collect_type="item",
                     collect_item="gun2"))
add_thing(make_thing(Speaker, "Tone", (0.0, 60.0, 0.0), sound_file="assets/sounds/e2e_tone.wav",
                     looping=True, play_on_start=True, state="on"))
add_thing(make_thing(LogicCamera, "Intro", (0.0, 120.0, -300.0), cutscene_file="cutscenes/e2e_intro.json"))
add_thing(make_thing(LevelChanger, "ToAnnex", (-400.0, 40.0, -400.0), target_map="maps/e2e_annex.json",
                     destination_spawn="AnnexStart", radius=96.0, usable=True))
window.update_all_ui()
# undo/redo must leave the authored world as built
before = window.state.snapshot()
for _ in range(5):
    window.undo()
for _ in range(5):
    window.redo()
assert window.state.snapshot() == before, "undo/redo changed the authored world"
save_as("maps/e2e_world.json")

# reload from disk: what a user gets after closing and reopening the editor
assert window.load_level_file(os.path.join(ROOT, "maps/e2e_world.json"))
saved = json.load(open("maps/e2e_world.json", encoding="utf-8"))
print("authored: %d brushes, %d things, version %s, convex=%d" % (
    len(saved["brushes"]), len(saved["things"]), saved.get("version"),
    sum(1 for b in saved["brushes"] if b.get("geometry"))))

data, dashboard = common.capture(session, "world", direct_out=os.path.join(OUT, "author_direct.npz"))
assert not common.tick_fault(session), common.tick_fault(session)
np.savez_compressed(os.path.join(OUT, "author.npz"), **data)
open(os.path.join(OUT, "author_dashboard.txt"), "w").write(dashboard)
session.stop_play()
# Play left the world as authored? (restore_world_on_stop is on in settings.ini)
assert window.load_level_file(os.path.join(ROOT, "maps/e2e_world.json"))

# -- export through the editor's own dialog -----------------------------------
package = os.path.join(OUT, "e2e_world.fiopak")
from unittest.mock import patch                        # noqa: E402
from editor.package_exporter import PackageExporter   # noqa: E402
errors = []
original = PackageExporter.export


def checked(*a, **k):
    result = original(*a, **k)
    errors.extend(result[1])
    return result


window.unsaved_changes = False
window.export_game_package()
dialog = window._export_dialog
dialog.title_edit.setText("E2E World")
dialog.author_edit.setText("e2e")
dialog.desc_edit.setPlainText("Authored by the end-to-end check")
with patch.object(QFileDialog, "getSaveFileName", return_value=(package, "Game Packages (*.fiopak)")), \
        patch.object(PackageExporter, "export", checked):
    dialog.export_btn.click()
print("export label:", dialog.dep_label.text().replace("\n", " | "))
print("export errors:", errors)
assert "Export successful!" in dialog.dep_label.text() and not errors
window._close_current_overlay()

import zipfile                                        # noqa: E402
with zipfile.ZipFile(package) as zf:
    names = zf.namelist()
    print("package: %d entries, %d bytes" % (len(names), os.path.getsize(package)))
    for name in names:
        print("   ", name)
    assert all("\\" not in n and not n.startswith("/") and ".." not in n.split("/") for n in names)
    print("metadata:", zf.read("metadata.json").decode()[:400].replace("\n", " "))
print("dialogs answered:", dialogs)
session.close()
print("AUTHOR OK")
