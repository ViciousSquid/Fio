"""Per-pass renderer timings on a world dense in angled (convex) brushes.

A fixed camera over a deterministic field of clipped and rotated brushes --
varied textures, caulk and nodraw faces, authored UV scale/angle/shift and
natural-scale faces -- drawn through the production ``ForwardRenderer`` from a
production ``RenderTable``. Nothing moves, so every measured frame is a
steady-state frame: what is left is the per-frame submission cost.

Reported per mode (editor, play): median ``timed_pass`` CPU ms per pass, the
render's own thread CPU ms (llvmpipe rasterises on worker threads, so this is
the submission cost a hardware GPU would still leave on the CPU), Python
function calls per frame (machine independent -- the proxy for the low-power
ARM target, where per-call overhead dominates), ``draw_calls`` /
``visible_tris`` / ``batched_draws`` from ``RenderStats``, a census of the GL
calls actually made per frame (draws, texture/VAO/buffer binds, uniform
uploads through PyOpenGL and through the raw paths, total PyOpenGL
invocations), and cache-miss behaviour: angled-brush mesh builds and texture
loads during warm-up and during the measured steady-state frames.

Needs a display with OpenGL::

    xvfb-run -a python tools/bench_geo_passes.py [--grid 24] [--frames 200]
        [--json out.json] [--tree /path/to/other/checkout] [--save-map m.json]

``--tree`` measures another checkout (a ``git worktree`` at the base commit)
with the same generated world, which is how before/after numbers are taken.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import math
import os
import pstats
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: Textures that exist under assets/textures in every revision measured.
TEXTURES = (
    "Dev/128.jpg", "Dev/256.jpg", "Dev/512.jpg", "Metal_08-512x512.png",
    "Stone_09-512x512.png", "Tile_03-512x512.png", "WoodenCrate_512.jpg",
    "cobblestone.jpg", "concrete02.png", "sci_fi_metal_panel01.jpg",
    "sci_fi_mtlwall.jpg", "scifipnl01.jpeg",
)
FACE_TAGS = ("south", "north", "west", "east", "down", "top")


def angled_world(grid=24, spacing=160.0, seed=7):
    """``grid * grid`` angled brushes on a floor, deterministic from *seed*."""
    import random
    from engine import brush_geometry as bg

    rng = random.Random(seed)
    brushes = []
    half = (grid - 1) * spacing * 0.5
    floor_size = grid * spacing + 256.0
    brushes.append({
        "id": "bench-floor", "name": "floor",
        "pos": [0.0, -16.0, 0.0], "size": [floor_size, 32.0, floor_size],
        "textures": {tag: "concrete02.png" for tag in FACE_TAGS},
    })
    for i in range(grid):
        for k in range(grid):
            n = i * grid + k
            w = rng.uniform(60.0, 120.0)
            h = rng.uniform(48.0, 200.0)
            d = rng.uniform(60.0, 120.0)
            pos = [i * spacing - half, h * 0.5, k * spacing - half]
            textures = {tag: rng.choice(TEXTURES) for tag in FACE_TAGS}
            brush = {
                "id": "bench-%05d" % n, "name": "angled%d" % n,
                "pos": pos, "size": [w, h, d], "textures": textures,
            }
            # Natural scale on a convex face is read from the brush's per-tag
            # flags (a plane-level flag does not reach the renderer).
            natural = {tag: True for tag in FACE_TAGS if rng.random() < 0.15}
            if natural:
                brush["uv_natural"] = natural
            kind = n % 4
            if kind in (0, 1, 3):
                # One or two cuts through the upper half: ramps and wedges.
                cuts = 1 if kind == 0 else 2
                for _ in range(cuts):
                    nx = rng.uniform(-1.0, 1.0)
                    nz = rng.uniform(-1.0, 1.0)
                    normal = [nx, rng.uniform(0.6, 1.4), nz]
                    length = math.sqrt(sum(c * c for c in normal))
                    normal = [c / length for c in normal]
                    point = [pos[0], pos[1] + h * rng.uniform(0.05, 0.3), pos[2]]
                    offset = sum(a * b for a, b in zip(normal, point))
                    bg.clip_brush(brush, normal, offset,
                                  texture=rng.choice(TEXTURES))
            if kind in (2, 3):
                bg.rotate_brush(brush, rng.uniform(5.0, 80.0), (0.0, 1.0, 0.0))
            bg.box_to_geometry(brush)
            planes = brush["geometry"]["planes"]
            for p, plane in enumerate(planes):
                roll = rng.random()
                if roll < 0.08:
                    plane["texture"] = "caulk.jpg"
                elif roll < 0.14:
                    plane["texture"] = "nodraw.jpg"
                if rng.random() < 0.3:
                    plane["uv_scale"] = [rng.choice((0.5, 1.0, 2.0, 3.0)),
                                         rng.choice((0.5, 1.0, 2.0))]
                if rng.random() < 0.25:
                    plane["uv_angle"] = rng.choice((0.0, 15.0, 45.0, 90.0))
                if rng.random() < 0.25:
                    plane["uv_shift"] = [rng.uniform(0, 1), rng.uniform(0, 1)]
            bg.invalidate_geometry_cache(brush)
            bg.sync_brush_bounds(brush)
            brushes.append(brush)
    return brushes


def _context(width, height):
    from PyQt5.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    from PyQt5.QtWidgets import QApplication
    import OpenGL.GL as gl

    app = QApplication.instance() or QApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create() or not context.makeCurrent(surface):
        raise SystemExit("no OpenGL 3.3 context (run under xvfb-run -a)")
    colour = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, colour)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
    depth = gl.glGenRenderbuffers(1)
    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth)
    gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8,
                             width, height)
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                              gl.GL_TEXTURE_2D, colour, 0)
    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER,
                                 gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                 gl.GL_RENDERBUFFER, depth)
    gl.glViewport(0, 0, width, height)
    return app, surface, context


def _config(brushes, table, play_mode):
    import numpy as np
    from engine.constants import RENDER_MODE_LIT
    from engine.entity_table import EntityTable

    etable = EntityTable()
    hidden = etable.begin_frame([], epoch=1)
    _mask, slots = table.shown()
    return {
        "render_mode": RENDER_MODE_LIT, "brush_display_mode": "Textured",
        "play_mode": play_mode, "grid_visible": False,
        "camera_distance_cull": False, "shadows_enabled": False,
        "render_table": table, "all_brush_slots": slots.astype(np.int32),
        "entity_table": etable, "visible_thing_slots":
            np.arange(etable.count, dtype=np.int32),
        "thing_hidden": hidden, "all_brushes": brushes, "all_things": [],
    }


#: GL entry points grouped for the census. Everything else that is called
#: counts towards the total only.
_CENSUS_GROUPS = {
    "draws": ("glDrawArrays", "glDrawArraysInstanced", "glDrawElements",
              "glDrawElementsInstanced", "glMultiDrawArrays"),
    "texture_binds": ("glBindTexture",),
    "vao_binds": ("glBindVertexArray",),
    "buffer_binds": ("glBindBuffer",),
    "buffer_uploads": ("glBufferData", "glBufferSubData"),
    "program_binds": ("glUseProgram",),
}


def _census(frame, frames=5):
    """GL calls per frame, by kind, counted at the Python/GL boundary.

    Every ``gl*`` function in ``OpenGL.GL`` is wrapped (the renderer calls
    them as ``gl.glX``, looked up per call), and so are the raw entry points
    the renderer resolves for itself: ``uniform_rows``' native uniform uploads
    and the instancing pass's raw ``glVertexAttribPointer``. ``pyopengl``
    counts calls through PyOpenGL's wrappers; ``raw`` those that bypass them.
    """
    import OpenGL.GL as gl
    from collections import Counter
    from engine.renderer.core import uniform_rows as ur
    try:
        from engine.renderer.forward import instancing
    except ImportError:            # pragma: no cover - older trees
        instancing = None
    counts = Counter()
    saved = []                     # (owner, name, original)

    def counting(name, fn, raw=False):
        def call(*args, **kwargs):
            counts[name] += 1
            counts["raw" if raw else "pyopengl"] += 1
            return fn(*args, **kwargs)
        return call

    for name in dir(gl):
        if name.startswith("gl") and callable(getattr(gl, name)):
            saved.append((gl, name, getattr(gl, name)))
            setattr(gl, name, counting(name, saved[-1][2]))
    for entry, *_ in ur._KINDS.values():
        fn = ur._raw_entry(entry)
        if fn is not None:
            saved.append((ur._resolved, entry, fn))
            ur._resolved[entry] = counting("raw:" + entry, fn, raw=True)
    raw_vap = getattr(instancing, "_raw_vertex_attrib_pointer", None)
    if raw_vap is not None:
        saved.append((instancing, "_raw_vertex_attrib_pointer", raw_vap))
        instancing._raw_vertex_attrib_pointer = counting(
            "raw:glVertexAttribPointer", raw_vap, raw=True)
    try:
        for _ in range(frames):
            frame()
    finally:
        for owner, name, fn in saved:
            if isinstance(owner, dict):
                owner[name] = fn
            else:
                setattr(owner, name, fn)
    per_frame = {key: value / frames for key, value in counts.items()}
    result = {group: round(sum(per_frame.get(n, 0) for n in names), 1)
              for group, names in _CENSUS_GROUPS.items()}
    result["uniform_uploads_pyopengl"] = round(sum(
        v for k, v in per_frame.items() if k.startswith("glUniform")), 1)
    result["uniform_uploads_raw"] = round(sum(
        v for k, v in per_frame.items() if k.startswith("raw:glUniform")), 1)
    result["pyopengl_calls"] = round(per_frame.get("pyopengl", 0), 1)
    result["raw_calls"] = round(per_frame.get("raw", 0), 1)
    result["gl_calls_total"] = round(result["pyopengl_calls"]
                                     + result["raw_calls"], 1)
    return result


def _measure(renderer, brushes, play_mode, frames, width, height):
    import glm
    import OpenGL.GL as gl
    from engine.render_table import RenderTable

    # Cache-miss behaviour: every angled-brush mesh build and every texture
    # resolution that reaches the loader, counted separately for the warm-up
    # (where they belong) and the measured steady-state frames (where a
    # still world should have none).
    misses = {"mesh_builds": 0, "texture_loads": 0}
    build, load = renderer._build_geo_mesh, renderer.load_texture_callback

    def counted_build(*args, **kwargs):
        misses["mesh_builds"] += 1
        return build(*args, **kwargs)

    def counted_load(*args, **kwargs):
        misses["texture_loads"] += 1
        return load(*args, **kwargs)

    renderer._build_geo_mesh = counted_build
    renderer.load_texture_callback = counted_load
    table = RenderTable()
    table.begin_frame(brushes, epoch=1)
    config = _config(brushes, table, play_mode)
    eye = glm.vec3(0.0, 900.0, 1900.0)
    projection = glm.perspective(glm.radians(70.0), width / height, 1.0, 20000.0)
    view = glm.lookAt(eye, glm.vec3(0.0, 0.0, 0.0), glm.vec3(0, 1, 0))
    slots = config["all_brush_slots"]

    def frame():
        table.begin_frame(brushes, epoch=1)      # a still world: reads nothing
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=slots)

    for _ in range(10):                          # warm-up: loads and uploads
        frame()
    gl.glFinish()
    warm = dict(misses)
    misses.update(mesh_builds=0, texture_loads=0)
    passes, cpu, wall = {}, [], []
    stats = renderer.render_stats
    for _ in range(frames):
        c0, t0 = time.thread_time(), time.perf_counter()
        frame()
        c1, t1 = time.thread_time(), time.perf_counter()
        gl.glFinish()
        cpu.append((c1 - c0) * 1000.0)
        wall.append((t1 - t0) * 1000.0)
        for name, ms in stats.pass_ms.items():
            passes.setdefault(name, []).append(ms)
    counters = {name: int(getattr(stats, name)) for name in (
        "draw_calls", "visible_tris", "batched_draws", "visible_brushes")}
    profile = cProfile.Profile()
    calls_frames = 20
    profile.enable()
    for _ in range(calls_frames):
        frame()
    profile.disable()
    calls = sum(row[1] for row in pstats.Stats(profile).stats.values())
    census = _census(frame)
    steady = dict(misses)
    renderer._build_geo_mesh = build
    renderer.load_texture_callback = load
    return {
        "cache_misses_warmup": warm,
        "cache_misses_steady_state": steady,
        "steady_frames": frames + calls_frames + 5,
        "gl_census_per_frame": census,
        "render_cpu_ms_median": round(statistics.median(cpu), 3),
        "render_wall_ms_median": round(statistics.median(wall), 3),
        "python_calls_per_frame": round(calls / calls_frames),
        "pass_ms_median": {name: round(statistics.median(v), 4)
                           for name, v in sorted(passes.items())},
        "counters": counters,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grid", type=int, default=24)
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--size", default="960x540")
    parser.add_argument("--json")
    parser.add_argument("--tree", default=os.path.dirname(HERE))
    parser.add_argument("--save-map", help="also write the world as a map")
    parser.add_argument("--batching", default="elements",
                        help="geo_batching for a renderer that has it "
                             "(elements, multidraw, off)")
    args = parser.parse_args(argv)
    # main.py's PyOpenGL settings: no glGetError after every call unless
    # FIO_GL_DEBUG=1 asks for it. Decided before anything imports OpenGL.GL.
    if os.environ.get("FIO_GL_DEBUG") != "1":
        os.environ.setdefault("PYOPENGL_ERROR_CHECKING", "0")
    tree = os.path.abspath(args.tree)
    out = os.path.abspath(args.json) if args.json else None
    save = os.path.abspath(args.save_map) if args.save_map else None
    os.chdir(tree)
    sys.path.insert(0, tree)
    width, height = (int(v) for v in args.size.split("x"))

    brushes = angled_world(args.grid)
    if save:
        from engine import brush_geometry as bg
        keep = [{k: v for k, v in b.items() if k not in bg.GEO_RUNTIME_KEYS}
                for b in brushes]
        with open(save, "w") as handle:
            json.dump({"version": 3, "brushes": keep, "things": [],
                       "created": "tools/bench_geo_passes.py"}, handle)

    _app, _surface, _context_ = _context(width, height)
    from engine.renderer.forward import ForwardRenderer
    renderer = ForwardRenderer(None)
    renderer.shadows_enabled = False
    if hasattr(renderer, "geo_batching"):
        renderer.geo_batching = args.batching
    renderer.set_grid(4096, 64)
    renderer.set_sprite_textures({})
    result = {"tree": tree, "grid": args.grid, "brushes": len(brushes),
              "batching": getattr(renderer, "geo_batching", None),
              "angled": sum(1 for b in brushes if b.get("geometry")),
              "frames": args.frames, "size": [width, height]}
    for mode, play in (("editor", False), ("play", True)):
        result[mode] = _measure(renderer, brushes, play, args.frames,
                                width, height)
    renderer.cleanup()
    text = json.dumps(result, indent=2)
    print(text)
    if out:
        with open(out, "w") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
