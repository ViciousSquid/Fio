"""Golden frames of the stock maps, and the lit programs' final sources.

The Classic renderer (``engine/renderer/forward``) is, by contract, the
renderer Fio had before HD_High existed beside it: the same pixels from the
same shaders. This module draws what that contract is checked against:

* :func:`capture_frames` -- each map in :data:`MAPS`, loaded by the real
  editor (``FioTestSession``: ``MainWindow``, the logic thread's projection,
  ``QtGameView.paintGL``), drawn from a fixed editor camera and then in Play
  from its PlayerStart, on a stepped clock with seeded RNGs so a frame is the
  same picture every run;
* :func:`capture_shader_sources` -- the fragment source every program of a
  fresh ``ForwardRenderer`` was finally compiled from, read back from the
  driver (``glGetShaderSource``), so a rewrite anywhere on the way is seen.

``tools/hd_golden.py`` stores both under ``tests/visual/golden/`` and
``tests/visual/test_classic_golden.py`` compares against them. Rasterisers differ
in their last bit, so the stored frames are only compared on the driver that
drew them (the manifest names it); ``tools/hd_golden.py --base`` compares two
checkouts on whatever driver is present.
"""

import hashlib
import os
import random
import time

from tests.helpers.paths import REPO_ROOT

SIZE = (320, 180)

#: map -> (editor camera (x, y, z, yaw, pitch) or None for "above the
#: PlayerStart / the brushes", play ticks at which a frame is taken).
MAPS = {
    "Office_Corridor": (None, (2, 24)),
    "Fog_Test": (None, (2, 24)),
    "Light_Test": ((0.0, 220.0, 520.0, -90.0, -18.0), ()),
    "Glass_Test": ((0.0, 180.0, 420.0, -90.0, -15.0), ()),
    "Portal_Test": (None, (2, 24)),
    "Terrain_Test_small": (None, (2, 24)),      # terrain, no sky
    "Terrain_Test_small2": (None, (2, 24)),     # terrain under a sky panorama
    "_SHOWCASE": (None, (2, 24)),               # sky, movers, water, decals, models
    "dLight_Test": (None, (2, 40)),             # a mover and a light that rides it
    "MonsterTest": (None, (2, 24)),             # a door, a monster, water
    "Wet_Decal_Test": (None, (2,)),
    "DevTest": (None, (2,)),
}

GOLDEN_DIR = os.path.join(REPO_ROOT, "tests", "visual", "golden")


def _default_camera(state):
    """At the PlayerStart, a head above it, looking across the room (or
    above the brushes' middle, looking down)."""
    import numpy as np
    target = None
    for thing in state.things:
        if type(thing).__name__ == "PlayerStart":
            x, y, z = (float(v) for v in thing.pos)
            return (x, y + 90.0, z, 35.0, -12.0)
    if target is None:
        pos = np.array([b.get("pos", (0, 0, 0)) for b in state.brushes] or [(0, 0, 0)],
                       dtype=float)
        target = pos.mean(axis=0)
    return (float(target[0]) + 180.0, float(target[1]) + 200.0, float(target[2]) + 320.0,
            -120.0, -25.0)


def capture_frames(app, names=None, size=SIZE, renderer=None, sky=None):
    """``{"<map>/<shot>": (h, w, 3) uint8}`` for each map in *names* (default
    all of :data:`MAPS`). Needs a display with OpenGL.

    *renderer* starts the editor with that renderer (``"HD_High"``) in place
    of the shipped default (Classic); *sky* (a dict of ``hd_*`` properties)
    is put on each map's primary PlayerStart first -- for looking at
    HD_High, not
    for the stored set."""
    import glm
    import numpy as np
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    import engine.entity_table as entity_table
    import engine.terrain
    from tests.helpers.session import FioTestSession

    # A level that cannot enter Play is captured in the editor view only,
    # without a modal box waiting for a click.
    boxes = {name: getattr(QMessageBox, name)
             for name in ("warning", "information", "critical", "question")}
    for name in boxes:
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
    # Terrain streams under a wall-clock budget per paint; lifted, a paint
    # streams what it needs and the picture does not depend on timing.
    terrain_limits = (engine.terrain.Terrain.UPDATE_BUDGET_MS,
                      engine.terrain.Terrain.MAX_UPDATES_PER_FRAME)
    engine.terrain.Terrain.UPDATE_BUDGET_MS = 1e9
    engine.terrain.Terrain.MAX_UPDATES_PER_FRAME = 1 << 30
    origin = entity_table._CLOCK_ORIGIN
    real_clock = time.perf_counter
    frames = {}
    try:
        for name in (names or MAPS):
            camera, ticks = MAPS[name]
            # Before the map loads too: effects pick their phases as they are made.
            random.seed(1234)
            np.random.seed(1234)
            settings = None
            if renderer:
                # The editor reads its settings when the window is built.
                import configparser
                import tempfile
                from tests.helpers import settings_isolation
                settings_isolation.install()
                handle, settings = tempfile.mkstemp(prefix="fio-golden-", suffix=".ini")
                os.close(handle)
                settings_isolation.fresh_copy(settings)
                parser = configparser.ConfigParser()
                parser.read(settings)
                if not parser.has_section("Renderer"):
                    parser.add_section("Renderer")
                parser.set("Renderer", "renderer", renderer)
                parser.set("Renderer", "lowpower_mode", "False")
                with open(settings, "w") as out:
                    parser.write(out)
                settings_isolation.use(settings)
            session = FioTestSession(app, "maps/%s.json" % name, size=size)
            try:
                view = session.view
                if sky:
                    start = session.state.resolve_player_start()
                    if start is not None:
                        start.properties.update(sky)
                        session.window.sync_sky_image_from_map()
                view.sysmon.set_active(False)
                # Occlusion results are read when the driver has them ready,
                # which is a matter of timing: what is hidden would differ
                # from run to run.
                if getattr(view, "renderer", None) is not None:
                    view.renderer.occlusion_culling = False
                config = view.editor.config
                if not config.has_section("Display"):
                    config.add_section("Display")
                config.set("Display", "show_fps", "False")
                clock = [origin + 1000.0]
                time.perf_counter = lambda: clock[0]
                view.start_time = origin
                random.seed(1234)
                np.random.seed(1234)
                # The grass sways on a clock of its own that starts at its
                # first draw: a paint Qt delivered while the map loaded (on
                # the real clock) must not have started it.
                held = getattr(getattr(view, "renderer", None), "_terrain_gl_state", None)
                if held is not None:
                    held[1].grass_time = 0.0
                    held[1]._grass_last_time = clock[0]

                x, y, z, yaw, pitch = camera or _default_camera(session.state)
                session.logic.camera.set_editor_camera(glm.vec3(x, y, z), yaw, pitch, 90.0)
                # Twice: the first paint loads textures and streams terrain.
                for _ in range(2):
                    session.publish()
                    pixels = session.paint()
                frames["%s/editor" % name] = pixels

                if ticks:
                    try:
                        session.start_play()
                    except AssertionError:
                        ticks = ()
                for tick in range(1, (max(ticks) if ticks else 0) + 1):
                    clock[0] += 1.0 / 60.0
                    session.step(1, keys={Qt.Key_W} if tick > 4 else (),
                                 mouse=(4.0, 0.0) if tick > 12 else (0.0, 0.0))
                    if tick in ticks:
                        session.paint()
                        frames["%s/play%02d" % (name, tick)] = session.paint()
            finally:
                time.perf_counter = real_clock
                try:
                    session.close()
                except AssertionError:
                    pass    # a window still reachable: the suite reports that
                if settings is not None:
                    from tests.helpers import settings_isolation
                    settings_isolation.use(None)
                    try:
                        os.remove(settings)
                    except OSError:
                        pass
    finally:
        time.perf_counter = real_clock
        (engine.terrain.Terrain.UPDATE_BUDGET_MS,
         engine.terrain.Terrain.MAX_UPDATES_PER_FRAME) = terrain_limits
        for name, function in boxes.items():
            setattr(QMessageBox, name, function)
    return frames


def capture_shader_sources(renderer):
    """``{program name: fragment source}`` for every program of *renderer*,
    as the driver holds it (after every rewrite on the way to the compiler)."""
    import OpenGL.GL as gl
    sources = {}
    for name, program in sorted(renderer.shaders.items()):
        if not program:
            continue
        for shader in gl.glGetAttachedShaders(int(program)):
            if gl.glGetShaderiv(shader, gl.GL_SHADER_TYPE) == gl.GL_FRAGMENT_SHADER:
                source = gl.glGetShaderSource(shader)
                if isinstance(source, bytes):
                    source = source.decode("utf-8", "replace")
                sources[name] = source
    return sources


def source_digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def driver_name():
    """The current GL context's renderer and version, as one string."""
    import OpenGL.GL as gl

    def text(enum):
        value = gl.glGetString(enum)
        return value.decode("utf-8", "replace") if value else "?"
    return "%s | %s" % (text(gl.GL_RENDERER), text(gl.GL_VERSION))


#: The Classic renderer's source: its own package and the shared
#: infrastructure it is built on.
CLASSIC_SOURCE_DIRS = ("engine/renderer/forward", "engine/renderer/core")
CLASSIC_SOURCE_FILES = ("engine/shaders.py",)


def classic_source_digests():
    """``{repo-relative path: sha256}`` of every Python file Classic is made
    of (line endings normalised, so a checkout's setting does not matter)."""
    digests = {}
    paths = list(CLASSIC_SOURCE_FILES)
    for directory in CLASSIC_SOURCE_DIRS:
        for folder, _dirs, files in os.walk(os.path.join(REPO_ROOT, directory)):
            for name in files:
                if name.endswith(".py"):
                    paths.append(os.path.relpath(os.path.join(folder, name), REPO_ROOT))
    for path in sorted(p.replace(os.sep, "/") for p in paths):
        with open(os.path.join(REPO_ROOT, path), "rb") as handle:
            text = handle.read().replace(b"\r\n", b"\n")
        digests[path] = hashlib.sha256(text).hexdigest()
    return digests
