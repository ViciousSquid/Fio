"""A model pass sends a material uniform when it differs, not once a group.

Both renderers draw a model one material group at a time, and each group
sent its emissive factor, its opacity and its colour whether or not the
group before had the same: on ``Reel_To_Reel_Test`` 320 of a frame's 719 GL
calls. Each clear group also asked GL for the blend state it would put back
(six queries), though every group puts back the same.

``_ModelSends`` is what a pass keeps of what it has sent. The same tests for
Classic's and HD_High's, which share no code. No GL context: the module's
``gl`` is replaced by a recorder.
"""

import importlib

import pytest

pytestmark = [pytest.mark.qt]

PACKAGES = ("engine.renderer.forward.passes", "engine.renderer.hd_high.passes")

BLEND, DEPTH_MASK = "GL_BLEND", "GL_DEPTH_WRITEMASK"


class RecordingGL:
    """The handful of GL entry points a model pass's sends use."""

    GL_BLEND, GL_DEPTH_WRITEMASK = BLEND, DEPTH_MASK
    GL_BLEND_SRC_RGB, GL_BLEND_DST_RGB = "src_rgb", "dst_rgb"
    GL_BLEND_SRC_ALPHA, GL_BLEND_DST_ALPHA = "src_a", "dst_a"
    GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_FALSE = 770, 771, False

    def __init__(self, blend=False, depth_mask=True):
        self.calls = []
        self.blend, self.depth_mask = blend, depth_mask
        self.func = [1, 0, 1, 0]

    def named(self, *names):
        return [call for call in self.calls if call[0] in names]

    def glUniform3f(self, location, x, y, z):
        self.calls.append(("glUniform3f", location, (x, y, z)))

    def glUniform1f(self, location, value):
        self.calls.append(("glUniform1f", location, value))

    def glIsEnabled(self, cap):
        self.calls.append(("glIsEnabled", cap))
        return self.blend

    def glGetBooleanv(self, name):
        self.calls.append(("glGetBooleanv", name))
        return self.depth_mask

    def glGetIntegerv(self, name):
        self.calls.append(("glGetIntegerv", name))
        return self.func[("src_rgb", "dst_rgb", "src_a", "dst_a").index(name)]

    def glEnable(self, cap):
        self.calls.append(("glEnable", cap))
        self.blend = True

    def glDisable(self, cap):
        self.calls.append(("glDisable", cap))
        self.blend = False

    def glBlendFunc(self, src, dst):
        self.calls.append(("glBlendFunc", src, dst))
        self.func = [src, dst, src, dst]

    def glBlendFuncSeparate(self, *func):
        self.calls.append(("glBlendFuncSeparate",) + func)
        self.func = list(func)

    def glDepthMask(self, flag):
        self.calls.append(("glDepthMask", flag))
        self.depth_mask = bool(flag)


class Program(dict):
    """A program's uniform locations, as a UniformCache gives them."""


def _program(base, emissive=True):
    return Program(emissive_factor=base if emissive else -1,
                   object_color=base + 1, alpha=base + 2)


@pytest.fixture(params=PACKAGES, ids=["Forward", "HD_High"])
def sends(request, monkeypatch):
    module = importlib.import_module(request.param)
    recorder = RecordingGL()
    monkeypatch.setattr(module, "gl", recorder)
    # The sends go through core.gl_state, on its own ``gl``; outside a frame
    # it remembers nothing and sends every call.
    from engine.renderer.core import gl_state
    monkeypatch.setattr(gl_state, "gl", recorder)
    return module._ModelSends(), recorder


def test_the_first_send_to_a_program_always_goes_out(sends):
    """Nothing is assumed of what a program held before the pass."""
    made, gl = sends
    lit = _program(10)
    made.emissive(lit, (0.0, 0.0, 0.0))
    made.colour(lit, (0.8, 0.8, 0.8))
    made.alpha(lit, 1.0)
    assert gl.calls == [("glUniform3f", 10, (0.0, 0.0, 0.0)),
                        ("glUniform3f", 11, (0.8, 0.8, 0.8)),
                        ("glUniform1f", 12, 1.0)]


def test_a_value_the_program_already_holds_is_not_sent_again(sends):
    made, gl = sends
    lit = _program(10)
    for _group in range(27):                 # one reel: 27 groups, one material
        made.emissive(lit, (0.0, 0.0, 0.0))
        made.colour(lit, [0.8, 0.8, 0.8])    # a list or a tuple: the same value
        made.alpha(lit, 1.0)
    assert len(gl.calls) == 3

    made.colour(lit, (0.2, 0.4, 0.6))
    made.emissive(lit, (1.0, 0.5, 0.0))
    made.alpha(lit, 0.5)
    made.colour(lit, (0.8, 0.8, 0.8))        # back again: a change, so sent
    assert [call[2] for call in gl.calls[3:]] == [
        (0.2, 0.4, 0.6), (1.0, 0.5, 0.0), 0.5, (0.8, 0.8, 0.8)]


def test_each_program_is_remembered_apart(sends):
    made, gl = sends
    lit, textured = _program(10), _program(20)
    made.alpha(lit, 1.0)
    made.alpha(textured, 1.0)                # its own state: sent
    made.alpha(lit, 1.0)
    made.alpha(textured, 1.0)
    made.alpha(lit, 0.3)
    made.alpha(textured, 1.0)                # still what it holds
    assert gl.calls == [("glUniform1f", 12, 1.0), ("glUniform1f", 22, 1.0),
                        ("glUniform1f", 12, 0.3)]


def test_a_program_without_an_emissive_factor_is_sent_none(sends):
    made, gl = sends
    made.emissive(_program(10, emissive=False), (1.0, 1.0, 1.0))
    made.emissive(Program(emissive_factor=None, object_color=1, alpha=2), (1.0, 1.0, 1.0))
    assert gl.calls == []


def test_a_new_pass_sends_afresh(sends, request):
    made, gl = sends
    lit = _program(10)
    made.alpha(lit, 1.0)
    again = type(made)()                     # the next pass, the next frame
    again.alpha(lit, 1.0)
    assert len(gl.calls) == 2


def test_an_opaque_material_leaves_the_state_alone(sends):
    made, gl = sends
    with made.material(_program(10), 1.0):
        pass
    assert gl.calls == [("glUniform1f", 12, 1.0)]


@pytest.mark.parametrize("blend, depth_mask", [(False, True), (True, True), (True, False)])
def test_a_clear_material_blends_and_puts_the_state_back(sends, blend, depth_mask):
    """A fading model is drawn with blending already on; that is kept."""
    made, gl = sends
    gl.blend, gl.depth_mask, gl.func = blend, depth_mask, [1, 0, 1, 771]
    with made.material(_program(10), 0.4):
        assert gl.blend is True and gl.depth_mask is False
        assert gl.func[:2] == [770, 771]
    assert (gl.blend, gl.depth_mask, gl.func) == (blend, depth_mask, [1, 0, 1, 771])


def test_the_blend_state_is_asked_for_once_a_pass(sends):
    made, gl = sends
    glass = _program(10)
    for _group in range(9):
        with made.material(glass, 0.4):
            pass
    queries = gl.named("glIsEnabled", "glGetBooleanv", "glGetIntegerv")
    assert len(queries) == 6, "one set of queries, not one a group"
    assert len(gl.named("glUniform1f")) == 1
    assert len(gl.named("glBlendFuncSeparate")) == 9, "each group still restores"
    assert (gl.blend, gl.depth_mask, gl.func) == (False, True, [1, 0, 1, 0])


def test_both_renderers_draw_their_models_through_it():
    """No group loop sends these three uniforms directly any more.

    Neither renderer's instanced pass is among them: each consumes a
    prepared plan (``tests/visual/test_model_plan.py``) and keeps what it has
    sent each program itself.
    """
    import ast
    import inspect
    for name in PACKAGES:
        module = importlib.import_module(name)
        for method in ("draw_models_instanced", "_draw_dense_model_single"):
            source = inspect.getsource(getattr(module.PassesMixin, method))
            if "_model_plan(" in source:
                assert method == "draw_models_instanced"
                continue
            tree = ast.parse("class _:\n" + source)
            sent = [ast.unparse(node) for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("glUniform")
                    and any(key in ast.unparse(node)
                            for key in ("'alpha'", "'object_color'", "'emissive_factor'"))]
            assert not sent, (name, method, sent)
            assert "_ModelSends()" in source and "glGet" not in source
