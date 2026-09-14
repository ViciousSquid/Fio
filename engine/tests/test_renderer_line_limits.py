"""Tests for the renderer's line-width / point-size clamping.

A core GL profile only has to support a line width of 1.0, and real hardware
does report ``GL_ALIASED_LINE_WIDTH_RANGE == [1, 1]``.  Asking such a driver
for 2.0 raises ``GL_INVALID_VALUE`` rather than clamping quietly, which takes
down the whole frame — so every width and size the renderer asks for goes
through a clamp first.

The GL calls are exercised against a stand-in module, so this runs with no
context and no window.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine import renderer_core  # noqa: E402


class _FakeGLFloatArray(list):
    """Mimics ``(gl.GLfloat * 2)()`` closely enough for the range query."""

    def __init__(self):
        super().__init__([0.0, 0.0])


class FakeGL:
    """A GL stand-in that reports a driver limit and refuses anything above it."""

    GL_ALIASED_LINE_WIDTH_RANGE = 'line-range'
    GL_ALIASED_POINT_SIZE_RANGE = 'point-range'

    class GLError(Exception):
        pass

    def __init__(self, line_range=(1.0, 1.0), point_range=(1.0, 1.0),
                 query_raises=False):
        self.line_range = line_range
        self.point_range = point_range
        self.query_raises = query_raises
        self.line_widths = []
        self.point_sizes = []
        self.queries = 0

    # The renderer builds the output buffer with ``(gl.GLfloat * 2)()``.
    class _GLFloatFactory:
        def __mul__(self, count):
            return _FakeGLFloatArray

    GLfloat = _GLFloatFactory()

    def glGetFloatv(self, name, out):
        self.queries += 1
        if self.query_raises:
            raise self.GLError('no such query')
        values = (self.line_range if name == self.GL_ALIASED_LINE_WIDTH_RANGE
                  else self.point_range)
        out[0], out[1] = values

    def glLineWidth(self, width):
        low, high = self.line_range
        if not (low <= width <= high):
            raise self.GLError('invalid value: %r' % (width,))
        self.line_widths.append(width)

    def glPointSize(self, size):
        low, high = self.point_range
        if not (low <= size <= high):
            raise self.GLError('invalid value: %r' % (size,))
        self.point_sizes.append(size)


@pytest.fixture
def renderer(monkeypatch):
    """A bare renderer instance with the GL module swapped for the fake.

    ``BaseRenderer.__init__`` builds shaders and buffers, so the object is
    created without it — the clamping helpers only touch the two cache
    attributes, which are set here explicitly.
    """
    def _make(fake):
        monkeypatch.setattr(renderer_core, 'gl', fake)
        obj = renderer_core.BaseRenderer.__new__(renderer_core.BaseRenderer)
        obj._line_width_range = None
        obj._point_size_range = None
        return obj
    return _make


def test_width_above_the_driver_limit_is_clamped_not_raised(renderer):
    fake = FakeGL(line_range=(1.0, 1.0))
    obj = renderer(fake)
    assert obj._set_line_width(2.0) == 1.0
    assert fake.line_widths == [1.0]


def test_width_within_the_limit_is_used_as_asked(renderer):
    fake = FakeGL(line_range=(1.0, 8.0))
    obj = renderer(fake)
    assert obj._set_line_width(2.0) == 2.0
    assert fake.line_widths == [2.0]


def test_width_below_the_minimum_is_raised_to_it(renderer):
    fake = FakeGL(line_range=(2.0, 8.0))
    obj = renderer(fake)
    assert obj._set_line_width(1.0) == 2.0


def test_the_driver_limit_is_queried_only_once(renderer):
    fake = FakeGL(line_range=(1.0, 1.0))
    obj = renderer(fake)
    for _ in range(5):
        obj._set_line_width(2.0)
    assert fake.queries == 1


def test_a_failed_range_query_falls_back_to_one(renderer):
    fake = FakeGL(line_range=(1.0, 1.0), query_raises=True)
    obj = renderer(fake)
    assert obj._set_line_width(4.0) == 1.0
    assert fake.line_widths == [1.0]


def test_a_nonsense_range_falls_back_to_one(renderer):
    fake = FakeGL(line_range=(0.0, 0.0))
    obj = renderer(fake)
    assert obj._set_line_width(3.0) == 1.0


def test_a_driver_that_refuses_even_the_clamped_width_does_not_raise(renderer):
    fake = FakeGL(line_range=(1.0, 4.0))
    obj = renderer(fake)
    obj._set_line_width(2.0)

    def _refuse(width):
        raise fake.GLError('nope')
    fake.glLineWidth = _refuse
    assert obj._set_line_width(2.0) == 1.0      # reported, not raised


def test_point_size_is_clamped_the_same_way(renderer):
    fake = FakeGL(point_range=(1.0, 1.0))
    obj = renderer(fake)
    assert obj._set_point_size(9.0) == 1.0
    assert fake.point_sizes == [1.0]


def test_point_size_within_the_limit_is_used_as_asked(renderer):
    fake = FakeGL(point_range=(1.0, 16.0))
    obj = renderer(fake)
    assert obj._set_point_size(9.0) == 9.0


def test_point_and_line_limits_are_cached_separately(renderer):
    fake = FakeGL(line_range=(1.0, 1.0), point_range=(1.0, 16.0))
    obj = renderer(fake)
    assert obj._set_line_width(2.0) == 1.0
    assert obj._set_point_size(6.0) == 6.0
    assert fake.queries == 2


def test_a_failed_point_range_query_falls_back_to_one(renderer):
    fake = FakeGL(point_range=(1.0, 1.0), query_raises=True)
    obj = renderer(fake)
    assert obj._set_point_size(6.0) == 1.0
