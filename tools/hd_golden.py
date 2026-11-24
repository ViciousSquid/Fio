"""Golden frames, shader sources and source lock of the Classic renderer.

    xvfb-run -a python tools/hd_golden.py --check          # compare with the stored set
    xvfb-run -a python tools/hd_golden.py --update         # store a new set
    xvfb-run -a python tools/hd_golden.py --base ../fio-base [--out DIR]
    python tools/hd_golden.py --lock-sources               # after a deliberate change to Classic

Classic (``engine/renderer/forward``) is the renderer Fio had before HD_High
existed beside it: the same pixels from the same shaders. The stored set
(``tests/visual/golden/``) was drawn by it before any HD code existed, and
``classic_sources.json`` there holds the digest of every file it is made of
(tests/visual/test_classic_lock.py fails when one changes; ``--lock-sources``
records a change that was meant). ``--check`` draws the same
frames (tests/helpers/golden.py: stock maps, fixed cameras, stepped clock) and
the lit programs' final fragment sources, and fails on any difference.

Stored frames are compared only on the driver that drew them (named in
``manifest.json``): another rasteriser rounds differently. ``--base`` needs no
stored frames: it draws another checkout (a ``git worktree`` of the commit to
compare with) and this one on the driver at hand and compares those.

The capture runs with ``PYTHONHASHSEED=0`` (this script re-executes itself to
get it): set iteration order decides the draw order of some coplanar
surfaces, and with it which one a seam pixel shows.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _capture(tree, renderer):
    """Frames, shader sources and the driver's name, drawn by the checkout
    at *tree*."""
    os.chdir(tree)
    sys.path.insert(0, tree)
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from tests.helpers import golden
    from tests.helpers import gl as glh
    frames = golden.capture_frames(app, renderer=renderer)
    with glh.GLTestContext(16, 16):
        import configparser
        config = configparser.ConfigParser()
        config.add_section("Renderer")
        config.set("Renderer", "lowpower_mode", "False")
        built = glh.make_renderer(config, name={"Classic": "Forward"}.get(renderer, renderer))
        try:
            sources = golden.capture_shader_sources(built)
            driver = golden.driver_name()
        finally:
            built.cleanup()
    glh.reset_texture_cache()
    return frames, sources, driver


def _save(directory, frames, sources, driver):
    from PIL import Image
    os.makedirs(directory, exist_ok=True)
    for name in os.listdir(directory):
        if name.endswith(".png"):
            os.remove(os.path.join(directory, name))
    for key, pixels in frames.items():
        Image.fromarray(pixels).save(os.path.join(directory, key.replace("/", "__") + ".png"))
    with open(os.path.join(directory, "shaders.json"), "w") as handle:
        json.dump(sources, handle, indent=1, sort_keys=True)
    with open(os.path.join(directory, "manifest.json"), "w") as handle:
        json.dump({"driver": driver, "frames": sorted(frames)}, handle, indent=1)


def _load(directory):
    import numpy as np
    from PIL import Image
    with open(os.path.join(directory, "manifest.json")) as handle:
        manifest = json.load(handle)
    with open(os.path.join(directory, "shaders.json")) as handle:
        sources = json.load(handle)
    frames = {key: np.asarray(Image.open(
        os.path.join(directory, key.replace("/", "__") + ".png")).convert("RGB"))
        for key in manifest["frames"]}
    return frames, sources, manifest["driver"]


def compare(expected, actual, *, frames=True):
    """Differences between two captures, as readable lines (empty: none)."""
    import numpy as np
    problems = []
    want_frames, want_sources, _ = expected
    got_frames, got_sources, _ = actual
    for name in sorted(set(want_sources) | set(got_sources)):
        if want_sources.get(name) != got_sources.get(name):
            problems.append("shader %s: fragment source differs" % name)
    if frames:
        for key in sorted(set(want_frames) | set(got_frames)):
            if key not in want_frames or key not in got_frames:
                problems.append("frame %s: only in one capture" % key)
                continue
            a, b = want_frames[key], got_frames[key]
            if a.shape != b.shape:
                problems.append("frame %s: %s against %s" % (key, a.shape, b.shape))
                continue
            differing = int((a != b).any(axis=-1).sum())
            if differing:
                worst = int(np.abs(a.astype(int) - b.astype(int)).max())
                problems.append("frame %s: %d pixels differ (largest step %d)"
                                % (key, differing, worst))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    mode.add_argument("--base", help="another checkout to compare this one with")
    mode.add_argument("--lock-sources", action="store_true",
                      help="record Classic's source digests as they are now")
    mode.add_argument("--dump", help=argparse.SUPPRESS)   # internal: capture to DIR
    parser.add_argument("--tree", default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--renderer", default="Classic",
                        help="the renderer to draw with (default Classic)")
    parser.add_argument("--out", help="keep what was drawn here (PNG)")
    parser.add_argument("--golden", default=os.path.join(ROOT, "tests", "visual", "golden"))
    args = parser.parse_args(argv)

    if args.lock_sources:
        sys.path.insert(0, ROOT)
        from tests.helpers import golden
        digests = golden.classic_source_digests()
        with open(os.path.join(args.golden, "classic_sources.json"), "w") as handle:
            json.dump(digests, handle, indent=1, sort_keys=True)
        print("locked %d Classic source files" % len(digests))
        return 0

    if os.environ.get("PYTHONHASHSEED") != "0":
        environment = dict(os.environ, PYTHONHASHSEED="0")
        return subprocess.call([sys.executable, os.path.abspath(__file__)]
                               + list(sys.argv[1:] if argv is None else argv),
                               env=environment)

    if args.dump:
        _save(args.dump, *_capture(os.path.abspath(args.tree), args.renderer))
        return 0

    if args.base:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="fio-golden-") as scratch:
            captures = []
            for label, tree in (("base", os.path.abspath(args.base)), ("this", ROOT)):
                directory = os.path.join(args.out or scratch, label)
                # Each checkout in a process of its own: its own engine.
                status = subprocess.call(
                    [sys.executable, os.path.abspath(__file__), "--dump", directory,
                     "--tree", tree, "--renderer", args.renderer], cwd=tree)
                if status:
                    return status
                captures.append(_load(directory))
        problems = compare(*captures)
    else:
        actual = _capture(ROOT, args.renderer)
        if args.out:
            _save(args.out, *actual)
        if args.update:
            _save(args.golden, *actual)
            print("stored %d frames and %d shader sources (%s)"
                  % (len(actual[0]), len(actual[1]), actual[2]))
            return 0
        expected = _load(args.golden)
        same_driver = expected[2] == actual[2]
        if not same_driver:
            print("frames not compared: stored by %r, this is %r "
                  "(use --base to compare two checkouts here)" % (expected[2], actual[2]))
        problems = compare(expected, actual, frames=same_driver)
    for line in problems:
        print("DIFFERENT  " + line)
    print("identical" if not problems else "%d differences" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    status = main()
    sys.stdout.flush()
    os._exit(status)
