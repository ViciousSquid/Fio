"""Every RenderState field the paint path reads exists before the first publish.

``paintGL`` reads the published buffer directly. A field that only the logic
thread's ``prepare_render_state`` ever assigns is missing from a buffer that
has not been through a full publish yet, and the paint raises on it:
``level_complete_ui`` did exactly that on entering Play once the
``getattr(..., None)`` fallback around it was removed.
"""

import ast
import os

import pytest

from engine.threaded_game_state import RenderState

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
READERS = ("engine/qt_game_view.py",)
READER_PREFIX = "engine/renderer"


def _reader_paths():
    paths = [os.path.join(ROOT, p) for p in READERS]
    engine = os.path.join(ROOT, "engine")
    paths += sorted(
        os.path.join(engine, name) for name in os.listdir(engine)
        if name.startswith(os.path.basename(READER_PREFIX)) and name.endswith(".py")
    )
    return paths


def _fields_read():
    fields = set()
    for path in _reader_paths():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "render_state"
                    and isinstance(node.ctx, ast.Load)):
                fields.add(node.attr)
    return fields


def test_the_paint_path_reads_render_state():
    assert _fields_read(), "no render_state reads found - this test is looking in the wrong place"


@pytest.mark.parametrize("field", sorted(_fields_read()))
def test_a_fresh_render_state_has_the_field(field):
    state = RenderState()
    if not hasattr(state, field) and callable(getattr(RenderState, field, None)):
        return
    assert hasattr(state, field), f"RenderState() has no '{field}' but the paint path reads it"


@pytest.mark.parametrize("field", sorted(_fields_read()))
def test_a_recycled_render_state_keeps_the_field(field):
    state = RenderState()
    reset = getattr(state, "reset", None)
    if reset is None:
        pytest.skip("RenderState has no reset()")
    reset()
    assert hasattr(state, field), f"RenderState.reset() drops '{field}'"
