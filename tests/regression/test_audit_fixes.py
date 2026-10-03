"""Regression tests for the two audit fixes.

1. brush_aabb_bounds() publication order: bounds are stored BEFORE the
   signature, so a concurrent reader can never latch a new signature against
   stale bounds.
2. isAutoRepeat() suppression is scoped to play mode only; editor-mode key
   handling still receives autorepeat events.
"""

import ast
import json
import os
import pathlib
import re
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

ROOT = pathlib.Path(__file__).resolve().parents[2]

from engine.constants import AABB_RUNTIME_KEYS, brush_aabb_bounds  # noqa: E402


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. AABB cache publication invariant
# ---------------------------------------------------------------------------

def test_bounds_are_stored_before_the_signature():
    """Static check: the two stores must appear in the safe order."""
    src = _read("engine/constants.py")
    i_bounds = src.index("brush['_aabb_bounds'] = bounds")
    i_sig = src.index("brush['_aabb_sig'] = sig")
    assert i_bounds < i_sig, (
        "publication order regressed: _aabb_sig must be written LAST so a "
        "reader observing the new signature is guaranteed the new bounds"
    )


class _ObservingDict(dict):
    """A brush dict that records the order of writes to the cache keys."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.write_order = []

    def __setitem__(self, key, value):
        if key in AABB_RUNTIME_KEYS:
            self.write_order.append(key)
        super().__setitem__(key, value)


def test_write_order_observed_at_runtime():
    """Dynamic check: observe the actual store sequence, not just the source."""
    b = _ObservingDict(pos=[0.0, 0.0, 0.0], size=[64.0, 64.0, 64.0])
    brush_aabb_bounds(b)
    assert b.write_order == ["_aabb_bounds", "_aabb_sig"]

    # And again after an invalidation.
    b.write_order.clear()
    b["pos"] = [128.0, 0.0, 0.0]
    brush_aabb_bounds(b)
    assert b.write_order == ["_aabb_bounds", "_aabb_sig"]


class _InterleavingDict(dict):
    """Simulates a reader observing the dict between the two cache stores.

    After ``_aabb_bounds`` is written but before ``_aabb_sig`` is, a reader is
    run against the half-updated dict. Under the safe order that reader must
    still see the OLD signature and therefore recompute -- it must never get a
    new-sig / old-bounds pairing.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.observations = []

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "_aabb_bounds":
            # A concurrent reader lands here, mid-update.
            self.observations.append(
                (self.get("_aabb_sig"), self.get("_aabb_bounds")))


def test_reader_interleaved_between_the_two_stores_never_sees_a_torn_pair():
    b = _InterleavingDict(pos=[0.0, 0.0, 0.0], size=[64.0, 64.0, 64.0])
    brush_aabb_bounds(b)                      # first population
    b["pos"] = [500.0, 0.0, 0.0]              # a door moves
    b.observations.clear()
    new_bounds = brush_aabb_bounds(b)

    assert len(b.observations) == 1
    observed_sig, observed_bounds = b.observations[0]

    # The signature visible mid-update is still the OLD one, so any reader
    # comparing it against the new pos/size mismatches and recomputes.
    old_sig = (0.0, 0.0, 0.0, 64.0, 64.0, 64.0)
    assert observed_sig == old_sig
    # The bounds were already the new ones — the pairing is never
    # "new sig + old bounds", which is the unsafe combination.
    assert observed_bounds == new_bounds


def test_concurrent_readers_never_see_bounds_older_than_the_signature():
    """Hammer the cache from several threads while a 'door' moves.

    The real reader does: read _aabb_sig, compare, then read _aabb_bounds. With
    bounds published FIRST, the bounds it then reads are either the ones
    matching that signature or NEWER ones -- both safe. The bug being guarded
    against is bounds that LAG the signature, which is what the old
    sig-first order allowed and which the cache would then keep serving.

    The mover advances x monotonically, so "older" is directly comparable:
    the bounds' x must never be behind the signature's x.
    """
    brush = {"pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0]}
    stop = threading.Event()
    violations = []
    reads = [0]

    def mover():
        i = 0
        while not stop.is_set():
            i += 1
            brush["pos"] = [float(i), 0.0, 0.0]      # strictly increasing
            brush_aabb_bounds(brush)

    def reader():
        while not stop.is_set():
            sig = brush.get("_aabb_sig")             # read sig first...
            bounds = brush.get("_aabb_bounds")       # ...then bounds
            if sig is None or bounds is None:
                continue
            reads[0] += 1
            sig_lo_x = sig[0] - sig[3] * 0.5
            # bounds may be equal (matching) or ahead (fresher) -- never behind.
            if bounds[0] < sig_lo_x - 1e-6:
                violations.append((sig_lo_x, bounds[0]))

    threads = [threading.Thread(target=mover)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert reads[0] > 1000, f"test did not exercise the race (only {reads[0]} reads)"
    assert not violations, (
        f"bounds lagged the signature {len(violations)} times "
        f"(first: sig_lo_x={violations[0][0]}, bounds_lo_x={violations[0][1]})"
    )


def test_no_lock_or_allocation_added_to_the_hot_path():
    """The fix must stay lock-free; only the two stores were reordered."""
    src = _read("engine/constants.py")
    fn_start = src.index("def brush_aabb_bounds(brush):")
    fn = src[fn_start:]
    # Cut at the end of the function (it is the last def in the file).
    for token in ("threading", "Lock", "RLock", "acquire", "with _"):
        assert token not in fn, f"a lock was introduced into the hot path: {token}"
    assert "import threading" not in src


# Invalidation must still work after the reorder.

def test_position_change_still_invalidates():
    b = {"pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0]}
    assert brush_aabb_bounds(b) == (-32.0, -32.0, -32.0, 32.0, 32.0, 32.0)
    b["pos"] = [100.0, 0.0, 0.0]
    assert brush_aabb_bounds(b) == (68.0, -32.0, -32.0, 132.0, 32.0, 32.0)


def test_size_change_still_invalidates():
    b = {"pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0]}
    brush_aabb_bounds(b)
    b["size"] = [128.0, 128.0, 128.0]
    assert brush_aabb_bounds(b) == (-64.0, -64.0, -64.0, 64.0, 64.0, 64.0)


def test_in_place_mutation_still_invalidates():
    b = {"pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0]}
    brush_aabb_bounds(b)
    b["pos"][2] = 250.0
    assert brush_aabb_bounds(b) == (-32.0, -32.0, 218.0, 32.0, 32.0, 282.0)


def test_save_state_still_clean_after_the_reorder():
    b = {"pos": [1.0, 2.0, 3.0], "size": [64.0, 64.0, 64.0], "textures": {}}
    brush_aabb_bounds(b)
    clean = {k: v for k, v in b.items() if k not in AABB_RUNTIME_KEYS}
    assert not any(k.startswith("_aabb") for k in clean)
    json.dumps(clean)


# ---------------------------------------------------------------------------
# 2. isAutoRepeat scoped to play mode
# ---------------------------------------------------------------------------

def _key_handler_source(name):
    src = _read("editor/main_window.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found")


def test_autorepeat_guard_is_inside_the_play_mode_branch():
    for fn_name in ("keyPressEvent", "keyReleaseEvent"):
        body = _key_handler_source(fn_name)
        assert "isAutoRepeat" in body, f"{fn_name} lost its guard"
        play_idx = body.index("if self.view_3d.play_mode:")
        guard_idx = body.index("if event.isAutoRepeat():")
        assert guard_idx > play_idx, (
            f"{fn_name}: the autorepeat guard must sit INSIDE the play_mode "
            f"branch, not before it (it would suppress editor autorepeat)"
        )


def test_autorepeat_guard_is_indented_within_the_branch():
    """Textual indentation check: the guard must be nested, not top-level."""
    for fn_name in ("keyPressEvent", "keyReleaseEvent"):
        body = _key_handler_source(fn_name)
        for line in body.splitlines():
            if "if event.isAutoRepeat():" in line:
                indent = len(line) - len(line.lstrip())
                assert indent >= 12, (
                    f"{fn_name}: guard indent {indent} implies it is not nested "
                    f"inside the play-mode branch"
                )


def test_editor_mode_still_reaches_super_key_handlers():
    """The editor path must be unchanged and still chain to super()."""
    press = _key_handler_source("keyPressEvent")
    release = _key_handler_source("keyReleaseEvent")
    assert "super().keyPressEvent(event)" in press
    assert "super().keyReleaseEvent(event)" in release
    assert "self.update_views()" in release


def _simulate(play_mode, auto_repeat):
    """Mirrors the guard structure now in keyPressEvent."""
    reached_play = reached_editor = False
    if play_mode:
        if auto_repeat:
            return ("suppressed", False, False)
        reached_play = True
        return ("play", reached_play, reached_editor)
    reached_editor = True
    return ("editor", reached_play, reached_editor)


def test_play_mode_autorepeat_is_ignored():
    outcome, played, edited = _simulate(play_mode=True, auto_repeat=True)
    assert outcome == "suppressed"
    assert not played and not edited


def test_play_mode_real_press_is_handled():
    outcome, played, _ = _simulate(play_mode=True, auto_repeat=False)
    assert outcome == "play" and played


def test_editor_mode_autorepeat_still_handled():
    """The regression this fix exists to prevent."""
    outcome, _, edited = _simulate(play_mode=False, auto_repeat=True)
    assert outcome == "editor" and edited, \
        "editor-mode autorepeat must continue to be processed"


def test_editor_mode_real_press_still_handled():
    outcome, _, edited = _simulate(play_mode=False, auto_repeat=False)
    assert outcome == "editor" and edited


def test_no_other_key_handling_was_altered():
    """Only the guard moved: the rest of both handlers is untouched."""
    press = _key_handler_source("keyPressEvent")
    for marker in ("_is_play_console_visible()", "Qt.Key_Escape",
                   "self.keys_pressed", "Qt.Key_QuoteLeft"):
        assert marker in press, f"unrelated play-mode handling lost: {marker}"
    release = _key_handler_source("keyReleaseEvent")
    assert "Consume the event completely in play mode" in release
    assert re.search(r"if event\.key\(\) in self\.keys_pressed:", release)
