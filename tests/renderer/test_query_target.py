"""Which occlusion query target the renderer may use -- asked, not assumed.

GL_ANY_SAMPLES_PASSED_CONSERVATIVE is desktop OpenGL 4.3
(ARB_ES3_compatibility), not 3.3 core: on a 3.3 core context glBeginQuery
rejects it with INVALID_ENUM, and a query that was refused never completes,
so the feature would sit "ready" while culling nothing, every issued query
pending forever. These tests pin OcclusionMixin._pick_query_target against
a fake GL namespace -- no context, no PyOpenGL, only the contract.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest

# Headless core CI has no PyOpenGL; the renderer tier verifies this fake
# context negotiation test without requiring a live GPU.
pytest.importorskip("OpenGL.GL")

from engine.renderer.forward.occlusion import OcclusionMixin  # noqa: E402

#: The three desktop targets as raw values, so the fake namespace never
#: needs the real binding constants.
CONSERVATIVE = 0x8DDF
ANY_PASSED = 0x8DDB
SAMPLES_PASSED = 0x8914
INVALID_ENUM = 0x0500
NO_ERROR = 0


class FakeContext:
    """A GL namespace that accepts some query targets and rejects the rest.

    A rejected target surfaces its error once, on the next glGetError, the
    way a real driver reports INVALID_ENUM from a refused glBeginQuery.
    """

    def __init__(self, accepted, constants=(CONSERVATIVE, ANY_PASSED, SAMPLES_PASSED)):
        self.accepted = set(accepted)
        self.error = NO_ERROR
        self.begun = []
        self.deleted = []
        if CONSERVATIVE in constants:
            self.GL_ANY_SAMPLES_PASSED_CONSERVATIVE = CONSERVATIVE
        if ANY_PASSED in constants:
            self.GL_ANY_SAMPLES_PASSED = ANY_PASSED
        self.GL_SAMPLES_PASSED = SAMPLES_PASSED
        self.GL_NO_ERROR = NO_ERROR

    def glGetError(self):
        error, self.error = self.error, NO_ERROR
        return error

    def glGenQueries(self, count):
        assert count == 1
        return 5

    def glBeginQuery(self, target, query):
        if target in self.accepted:
            self.begun.append((target, query))
        else:
            self.error = INVALID_ENUM

    def glEndQuery(self, target):
        pass

    def glDeleteQueries(self, count, queries):
        assert count == 1
        self.deleted.extend(queries)


def _pick(context):
    """_pick_query_target against a fake context; no mixin state involved."""
    return OcclusionMixin()._pick_query_target(context)


def test_conservative_capable_context_gets_the_conservative_target():
    # A 4.3+ context still gets the fail-open over-reporting variant.
    context = FakeContext((CONSERVATIVE, ANY_PASSED, SAMPLES_PASSED))
    assert _pick(context) == CONSERVATIVE
    assert context.begun == [(CONSERVATIVE, 5)]


def test_core_33_context_falls_back_to_any_samples_passed():
    # The reported bug: 3.3 core rejects the conservative target with
    # INVALID_ENUM but takes 3.3's own GL_ANY_SAMPLES_PASSED.
    context = FakeContext((ANY_PASSED, SAMPLES_PASSED))
    assert _pick(context) == ANY_PASSED
    assert context.begun == [(ANY_PASSED, 5)]
    # The rejected candidate's query object is freed too, not leaked.
    assert context.deleted.count(5) == 2


def test_context_with_only_samples_passed_still_culls():
    context = FakeContext((SAMPLES_PASSED,))
    assert _pick(context) == SAMPLES_PASSED


def test_binding_without_the_conservative_constant_skips_it():
    # An old PyOpenGL that does not even name the 4.3 target.
    context = FakeContext((ANY_PASSED,), constants=(ANY_PASSED, SAMPLES_PASSED))
    assert _pick(context) == ANY_PASSED


def test_context_without_any_query_target_disables_the_feature():
    context = FakeContext(())
    assert _pick(context) is None
    assert context.begun == []


def test_a_leftover_gl_error_is_drained_not_misread():
    # Errors left by earlier passes must not sink a target the context
    # actually accepts.
    context = FakeContext((ANY_PASSED, SAMPLES_PASSED))
    context.error = INVALID_ENUM
    assert _pick(context) == ANY_PASSED
