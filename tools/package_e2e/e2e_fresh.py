"""Fresh-instance process: a pristine install opens the .fiopak and plays it.

Run from the root of a clean checkout that has none of the author's maps,
assets or cutscenes:  xvfb-run -a python e2e_fresh.py <package> <outdir>
"""
import os
import sys

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PACKAGE, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])

import numpy as np                                    # noqa: E402

import e2e_common as common                           # noqa: E402

for absent in ("maps/e2e_world.json", "maps/e2e_annex.json", "assets/textures/e2e_wall.png",
               "assets/textures/E2E_Crate_Skin.png", "assets/models/e2e/crate.obj",
               "assets/sounds/e2e_tone.wav", "cutscenes/e2e_intro.json"):
    assert not os.path.exists(absent), "the fresh install is not fresh: %s" % absent

app, dialogs = common.qt_app()
from PyQt5.QtCore import Qt                           # noqa: E402
from tests.helpers.session import FioTestSession      # noqa: E402

session = FioTestSession(app, {"version": 3, "brushes": [], "things": []}, size=(320, 180))
window = session.window
package_bytes = open(PACKAGE, "rb").read()
if not window.config.has_section("Kiosk"):
    window.config.add_section("Kiosk")
window.config.set("Kiosk", "launch_in_editor", "True")   # stepped capture first; kiosk below
window.unsaved_changes = False
window.play_package_from_path(PACKAGE)
assert window._package is not None, "the package did not open"
assert window.file_path is None, "the package became the save target"
names = sorted(t.properties.get("name", "") for t in window.state.things)
print("fresh: opened package; %d brushes, %d things: %s" % (
    len(window.state.brushes), len(window.state.things), names))

data, dashboard = common.capture(session, "world", direct_out=os.path.join(OUT, "fresh_direct.npz"))
assert not common.tick_fault(session), common.tick_fault(session)
np.savez_compressed(os.path.join(OUT, "fresh.npz"), **data)
open(os.path.join(OUT, "fresh_dashboard.txt"), "w").write(dashboard)

# -- the package's own assets, cutscene and second level, while playing --------
from engine.asset_archives import asset_exists        # noqa: E402
checks = {}
for asset in ("assets/textures/e2e_wall.png", "assets/textures/E2E_Crate_Skin.png",
              "assets/models/e2e/crate.obj", "assets/models/e2e/crate.mtl",
              "assets/sounds/e2e_tone.wav"):
    checks[asset] = bool(asset_exists(asset))
cutscene = session.logic.cutscene_runtime._load_cutscene_file("cutscenes/e2e_intro.json")
checks["cutscene from package"] = isinstance(cutscene, dict) and len(cutscene["camera"]) == 2
print("checks:", checks)

# The trigger pad opened the door? (the scripted walk crosses it)
door = next(b for b in window.state.brushes if b.get("name") == "door")
print("door pos y=%.1f (authored 96.0)" % door["pos"][1])

# Level change inside the package: walk into the LevelChanger.
console = session.view.debug_console_window


def run(line):
    console.command_input.setText(line)
    console._on_command_entered()


assert session.playing
import math                                           # noqa: E402
from PyQt5.QtCore import QEvent                       # noqa: E402
from PyQt5.QtGui import QKeyEvent                     # noqa: E402
run("setpos -400 40 -340")
session.step(2)
session.logic.player_runtime.player.angle = math.pi   # face the LevelChanger at z=-400
session.step(1, use=True)
ui = session.logic.interaction_runtime.level_complete_ui
print("level complete prompt:", ui)
checks["level-complete prompt names the packaged map"] = bool(ui) and ui.get("target_map") == "maps/e2e_annex.json"
# The player presses E on the prompt (as tests/e2e/test_level_changer_spawns.py drives it).
session.publish()
session.game_state.try_swap()
session.view._cached_level_complete_ui = session.game_state.published("level_complete_ui")
session.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_E, Qt.NoModifier))
app.processEvents()
session.logic.session_runtime.stop_monster_ai()
names = sorted(t.properties.get("name", "") for t in window.state.things)
checks["level change to packaged annex"] = "AnnexStart" in names and "Start" not in names
checks["still playing after level change"] = bool(session.playing)
checks["package still the source"] = window._package is not None and window.file_path is None
if session.playing:
    session.step(60, keys={Qt.Key_W})
    with session.render_state() as frame:
        checks["annex player finite"] = bool(np.all(np.isfinite(frame.player_pos)))
assert not common.tick_fault(session), common.tick_fault(session)
print("after level change:", names)
session.stop_play()

# -- the shipping path: kiosk mode (what Tools > Play Game Package does) ------
window.config.set("Kiosk", "launch_in_editor", "False")
window.config.set("Kiosk", "window_mode", "Windowed")
window.unsaved_changes = False
window.play_package_from_path(PACKAGE)
app.processEvents()
checks["kiosk: playing"] = bool(session.playing and window.is_kiosk_mode)
session.logic.session_runtime.stop_monster_ai()
session.step(120, keys={Qt.Key_W}, mouse=(3.0, 0.0), shoot=True)
checks["kiosk: no tick fault"] = not common.tick_fault(session)
window.exit_kiosk_mode(confirm=False)
app.processEvents()
checks["kiosk: left cleanly"] = not session.playing and not window.is_kiosk_mode

checks["package file untouched"] = open(PACKAGE, "rb").read() == package_bytes
checks["nothing extracted into the install"] = not any(
    os.path.exists(p) for p in ("maps/e2e_world.json", "assets/textures/e2e_wall.png",
                                "cutscenes/e2e_intro.json"))
print("checks:", checks)
print("dialogs answered:", dialogs)
failed = [k for k, v in checks.items() if not v]
print("FRESH", "OK" if not failed else "FAILED: %s" % failed)
os._exit(1 if failed else 0)
