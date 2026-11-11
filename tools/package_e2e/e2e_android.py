"""Android-runtime process: the staged Android engine + player open the same .fiopak.

Run from the root of the staged Android runtime (player.tools.stage_runtime):
    PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless python e2e_android.py <package> <outdir> [desktop_fresh.npz]
Uses a real OpenGL ES 3 context (Mesa), the player's FioPackage reader, PlayerWorld
(the shared engine as it ships in the APK) and the player's GLES renderer.
"""
import ctypes
import math
import os
import sys

sys.path.insert(0, os.getcwd())
PACKAGE, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
DESKTOP = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 else None

import numpy as np                                    # noqa: E402

assert not os.path.exists("editor/main_window.py"), "this is not the staged Android runtime"
checks = {}

from player.tools.verify_package import verify_package   # noqa: E402
maps, assets = verify_package(PACKAGE)
print("verify_package: %d maps, %d referenced assets" % (maps, assets))
checks["android reader: every referenced asset resolves"] = maps == 2

from OpenGL import EGL, GL                            # noqa: E402
display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
EGL.eglInitialize(display, ctypes.c_int(), ctypes.c_int())
attributes = (ctypes.c_int * 13)(EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT, EGL.EGL_RENDERABLE_TYPE, 0x40,
                                 EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8, EGL.EGL_BLUE_SIZE, 8,
                                 EGL.EGL_DEPTH_SIZE, 24, EGL.EGL_NONE)
config, count = EGL.EGLConfig(), ctypes.c_int()
EGL.eglChooseConfig(display, attributes, ctypes.byref(config), 1, count)
EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
context = EGL.eglCreateContext(display, config, EGL.EGL_NO_CONTEXT,
                               (ctypes.c_int * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION, 3, EGL.EGL_NONE))
W, H = 320, 180
surface = EGL.eglCreatePbufferSurface(display, config,
                                      (ctypes.c_int * 5)(EGL.EGL_WIDTH, W, EGL.EGL_HEIGHT, H, EGL.EGL_NONE))
EGL.eglMakeCurrent(display, surface, surface, context)
print("GL:", GL.glGetString(GL.GL_VERSION).decode())

from player.app import FioPlayerApp                   # noqa: E402
from player.input.state import ACTION_FIRE, ACTION_USE, InputState   # noqa: E402
from player.render.quality import resolve             # noqa: E402
from player.render.renderer import GLESRenderer       # noqa: E402
from player.runtime.fiopak import FioPackage          # noqa: E402

app = FioPlayerApp(FioPackage.open(PACKAGE))
renderer = GLESRenderer(gles=True, quality=resolve("mobile"))
renderer.on_gl_ready()
renderer.resize(W, H)
app.renderer = renderer
app._load_level(app.package.start_map_path())
world = app.world
names = sorted(t.properties.get("name", "") for t in world.state.things)
print("android: %d brushes, %d things: %s" % (len(world.state.brushes), len(world.state.things), names))


def columns(table):
    found = {}
    for klass in type(table).__mro__:
        for name in getattr(klass, "__slots__", ()):
            value = getattr(table, name, None)
            if isinstance(value, np.ndarray) and value.dtype != object:
                found[name] = value.copy()
    return found


# -- the dense data, as the engine itself projects it in this runtime ---------
# The engine's own dense tables, filled directly from the loaded world (the
# player's renderer fills the EntityTable the same way every frame:
# player/render/projection.py). The desktop side makes the same two calls.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_common                                     # noqa: E402
dense = e2e_common.direct_tables(world.state.brushes, world.state.things,
                                 world.logic.session_runtime.effect_store)
np.savez_compressed(os.path.join(OUT, "android_tick0.npz"), **dense)
if DESKTOP:
    desktop = np.load(DESKTOP)
    same, differ, missing = [], [], []
    for key, value in sorted(dense.items()):
        other = key
        if other not in desktop.files:
            missing.append(key)
        elif desktop[other].shape == value.shape and desktop[other].tobytes() == value.tobytes():
            same.append(key)
        else:
            differ.append(key)
    print("dense tables at tick 0, Android runtime vs desktop fresh instance: "
          "%d columns identical, %d differ, %d only on Android" % (len(same), len(differ), len(missing)))
    for key in differ:
        a, b = dense[key], desktop[key]
        note = ""
        if a.shape == b.shape and a.dtype.kind == "f":
            note = "max |delta| %.6g" % float(np.nanmax(np.abs(a.astype(np.float64) - b.astype(np.float64))))
        elif a.shape == b.shape:
            note = "%d of %d elements" % (int(np.sum(a != b)), a.size)
        print("   differs: %-40s %s vs %s  %s" % (key, a.shape, b.shape, note))
    checks["dense world data matches desktop"] = not [k for k in differ if not k.endswith(
        ("effect_spawn_time", "geo_epoch"))]


def shot(label):
    state = app._render_state()
    for _ in range(3):
        renderer.render(state)
    error = GL.glGetError()
    raw = GL.glReadPixels(0, 0, W, H, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
    image = np.frombuffer(raw, np.uint8).reshape(H, W, 4)[::-1, :, :3].copy()
    from PIL import Image
    Image.fromarray(image).save(os.path.join(OUT, "android_%s.png" % label))
    return image, error


def custom_texture_pixels(image):
    """Pixels of the author's magenta/green check texture: strongly magenta
    (R and B well above G) or strongly green, which no stock asset here is."""
    r, g, b = (image[:, :, i].astype(int) for i in range(3))
    magenta = (r > g * 2 + 20) & (b > g * 2 + 20)
    green = (g > r * 2 + 20) & (g > b * 2 + 20)
    return int(magenta.sum()), int(green.sum())


app.cam_yaw = 90 - math.degrees(world.player.angle)
app.cam_pos = [world.player.pos.x, world.player.pos.y + world.player.view_height, world.player.pos.z]
image, error = shot("start")
magenta, green = custom_texture_pixels(image)
print("GLES frame at spawn: %d distinct colours, custom texture pixels magenta=%d green=%d, GL error %d" % (
    len(np.unique(image.reshape(-1, 3), axis=0)), magenta, green, error))
checks["GLES: world drawn"] = len(np.unique(image.reshape(-1, 3), axis=0)) > 50
checks["GLES: package-only texture drawn"] = magenta > 200 and green > 200
checks["GLES: no GL error"] = error == GL.GL_NO_ERROR
checks["GLES: package-only model uploaded"] = any(s[0] == "model" for s in renderer._draw_sources.values())

# -- play: walk, strafe, turn, fire ------------------------------------------
start = tuple(world.player.pos)
inp = InputState()
for tick in range(600):
    inp.begin_frame()
    inp.add_move(0.6 if (tick // 60) % 2 else -0.6, 1.0)
    inp.set_button(ACTION_FIRE, tick % 15 < 2)
    app.cam_yaw += 0.8
    app.cam_pos = world.step(1 / 60, inp, app.cam_yaw, app.cam_pitch)
    assert all(math.isfinite(v) for v in app.cam_pos), (tick, app.cam_pos)
end = tuple(world.player.pos)
print("played 600 ticks: player %s -> %s, health %s" % (
    tuple(round(v, 1) for v in start), tuple(round(v, 1) for v in end), world.logic.player_runtime.player_health))
checks["android: simulation ran and the player moved"] = math.dist(start, end) > 50
image, error = shot("after_play")
checks["GLES: no GL error after play"] = error == GL.GL_NO_ERROR
cutscene = world.logic.cutscene_runtime._load_cutscene_file("cutscenes/e2e_intro.json")
checks["android: cutscene from package"] = isinstance(cutscene, dict) and len(cutscene["camera"]) == 2

# -- the packaged second level, through the LevelChanger ----------------------
import glm                                            # noqa: E402
world.player.pos = glm.vec3(-400.0, 40.0, -340.0)
app.cam_yaw = 90 - math.degrees(math.pi)              # face the LevelChanger at z=-400


for attempt in range(2):                              # Use: the prompt; Use again: continue
    use = InputState()
    use.begin_frame()
    use.set_button(ACTION_USE, True)
    world.step(1 / 60, use, app.cam_yaw, 0)
    world.step(1 / 60, InputState(), app.cam_yaw, 0)
print("level complete ui:", world.logic.interaction_runtime.level_complete_ui, "pending:", world.pending_level)
checks["android: LevelChanger names the packaged map"] = bool(world.pending_level) and \
    world.pending_level[0] == "maps/e2e_annex.json"
if world.pending_level:
    app._advance_level()
    names = sorted(t.properties.get("name", "") for t in app.world.state.things)
    print("after level change:", names, "map:", app.map_path)
    checks["android: second level loaded from the package"] = names == ["AnnexLight", "AnnexStart"]
    for _ in range(120):
        app.cam_pos = app.world.step(1 / 60, InputState(), app.cam_yaw, 0)
    image, error = shot("annex")
    checks["GLES: second level drawn, no GL error"] = error == GL.GL_NO_ERROR and \
        len(np.unique(image.reshape(-1, 3), axis=0)) > 20
print("checks:", checks)
failed = [k for k, v in checks.items() if not v]
print("ANDROID-RUNTIME", "OK" if not failed else "FAILED: %s" % failed)
os._exit(1 if failed else 0)
