"""Renderer line-width and point-size limits on a real OpenGL context.

These tests deliberately use the production Renderer_F and the driver's
actual reported limits. Fault-path cases patch one OpenGL call after a real
context exists, but the renderer, context, and GL state machinery remain real.
"""

import pytest

from tests.helpers import gl as glh

pytestmark = pytest.mark.gl


@pytest.fixture
def renderer():
    with glh.GLTestContext(64, 64) as context:
        renderer = glh.make_renderer()
        try:
            yield renderer, context
        finally:
            renderer.cleanup()


def _driver_range(enum):
    import OpenGL.GL as gl

    values = (gl.GLfloat * 2)()
    gl.glGetFloatv(enum, values)
    return float(values[0]), float(values[1])


def test_width_above_the_real_driver_limit_is_clamped(renderer, monkeypatch):
    import OpenGL.GL as gl
    from engine import renderer_core

    obj, _ = renderer
    low, high = _driver_range(gl.GL_ALIASED_LINE_WIDTH_RANGE)
    request = high + max(1.0, abs(high) * 0.5)
    captured = []

    real_line_width = renderer_core.gl.glLineWidth

    def observe(value):
        captured.append(float(value))
        try:
            return real_line_width(value)
        except Exception:
            # The real driver may reject a width even though it advertises
            # the aliased range. The production renderer must still contain
            # that failure.
            return None

    monkeypatch.setattr(renderer_core.gl, "glLineWidth", observe)

    result = obj._set_line_width(request)

    assert captured == [pytest.approx(high)]
    assert result == pytest.approx(high) or result == pytest.approx(1.0)
    assert obj._line_width_range == pytest.approx((low, high))


def test_width_at_the_real_driver_minimum_is_used(renderer):
    import OpenGL.GL as gl

    obj, _ = renderer
    low, high = _driver_range(gl.GL_ALIASED_LINE_WIDTH_RANGE)

    result = obj._set_line_width(low)

    assert result == pytest.approx(low)
    assert low <= result <= high


def test_width_below_the_real_driver_minimum_is_clamped(renderer):
    import OpenGL.GL as gl

    obj, _ = renderer
    low, _ = _driver_range(gl.GL_ALIASED_LINE_WIDTH_RANGE)

    assert obj._set_line_width(low - 1.0) == pytest.approx(low)


def test_the_real_driver_limit_is_queried_only_once(renderer, monkeypatch):
    import OpenGL.GL as gl
    from engine import renderer_core

    obj, _ = renderer
    obj._line_width_range = None
    calls = []
    real = renderer_core.gl.glGetFloatv

    def counted(enum, out):
        if enum == gl.GL_ALIASED_LINE_WIDTH_RANGE:
            calls.append(enum)
        return real(enum, out)

    monkeypatch.setattr(renderer_core.gl, "glGetFloatv", counted)

    obj._set_line_width(2.0)
    obj._set_line_width(3.0)
    obj._set_line_width(4.0)

    assert len(calls) == 1


def test_a_failed_real_driver_range_query_falls_back_to_one(renderer, monkeypatch):
    import OpenGL.GL as gl
    from engine import renderer_core

    obj, _ = renderer
    obj._line_width_range = None
    real = renderer_core.gl.glGetFloatv

    def failing(enum, out):
        if enum == gl.GL_ALIASED_LINE_WIDTH_RANGE:
            raise RuntimeError("injected driver query failure")
        return real(enum, out)

    monkeypatch.setattr(renderer_core.gl, "glGetFloatv", failing)

    assert obj._set_line_width(4.0) == 1.0
    assert obj._line_width_range == (1.0, 1.0)


def test_a_real_driver_refusal_does_not_take_down_the_renderer(renderer, monkeypatch):
    import OpenGL.GL as gl
    from engine import renderer_core

    obj, _ = renderer
    obj._line_width_range = None
    real_range = renderer_core.gl.glGetFloatv
    real_width = renderer_core.gl.glLineWidth

    monkeypatch.setattr(renderer_core.gl, "glGetFloatv", real_range)
    obj._set_line_width(1.0)

    def refusing(_width):
        raise RuntimeError("injected driver refusal")

    monkeypatch.setattr(renderer_core.gl, "glLineWidth", refusing)

    assert obj._set_line_width(2.0) == 1.0
    assert gl.glGetError() == gl.GL_NO_ERROR
    monkeypatch.setattr(renderer_core.gl, "glLineWidth", real_width)


def test_point_size_is_clamped_to_the_real_driver_limit(renderer):
    import OpenGL.GL as gl

    obj, _ = renderer
    low, high = _driver_range(gl.GL_ALIASED_POINT_SIZE_RANGE)
    request = high + max(1.0, abs(high) * 0.5)

    assert obj._set_point_size(request) == pytest.approx(high)
    assert obj._point_size_range == pytest.approx((low, high))


def test_point_size_within_the_real_driver_limit_is_used(renderer):
    import OpenGL.GL as gl

    obj, _ = renderer
    low, high = _driver_range(gl.GL_ALIASED_POINT_SIZE_RANGE)
    request = low if high == low else (low + high) * 0.5

    assert obj._set_point_size(request) == pytest.approx(request)


def test_a_failed_real_point_range_query_falls_back_to_one(renderer, monkeypatch):
    import OpenGL.GL as gl
    from engine import renderer_core

    obj, _ = renderer
    obj._point_size_range = None
    real = renderer_core.gl.glGetFloatv

    def failing(enum, out):
        if enum == gl.GL_ALIASED_POINT_SIZE_RANGE:
            raise RuntimeError("injected driver query failure")
        return real(enum, out)

    monkeypatch.setattr(renderer_core.gl, "glGetFloatv", failing)

    assert obj._set_point_size(6.0) == 1.0
    assert obj._point_size_range == (1.0, 1.0)
