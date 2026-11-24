"""The model pass consumes a prepared plan.

A frame's model pass used to work everything out again: sort its rows into
runs by recipe, look up each run's model and its material groups, choose a
program for each group, and aim eight instance attributes at each run's
first row -- 10,000 Python calls a frame for five reel models, in a world
where none of that had changed. ``PassesMixin._model_plan`` keeps what it
worked out (the rows in draw order; per run a vertex array and its draws)
and the pass consumes it until the rows, their recipes, the loaded models or
the programs change. What moves every frame, the rows' transforms, is the
instance buffer's business.
"""

import numpy as np
import pytest

import OpenGL.GL as gl

from engine.prop_entity import Prop
from engine.renderer.forward import passes
from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

REEL = 'assets/models/tape_reel_blue.glb'
DRUM = 'assets/models/Oil_Drum.obj'


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(128, 128) as made:
        yield made
    glh.reset_texture_cache()


def _renderer():
    made = glh.make_renderer()
    made.shadows_enabled = False
    return made


def _props(*placed):
    return [Prop.for_model(path, pos=list(pos), properties={'scale': [scale] * 3})
            for path, pos, scale in placed]


SCENE = ((REEL, (-90.0, 30.0, 0.0), 2.5), (DRUM, (0.0, 0.0, -40.0), 1.0),
         (REEL, (90.0, 30.0, 0.0), 2.5))


def _config(placed=SCENE):
    brushes, things = glh.lit_cube_scene(shadows=False)
    return glh.render_config(all_brushes=brushes[:1], all_things=things + _props(*placed))


def _draw(renderer, context, config):
    context.bind()
    with glh.no_gl_errors("model frame"):
        renderer.render_scene(*glh.camera_matrices(eye=(0.0, 160.0, 320.0), target=(0, 30, 0)),
                              None, config, brush_slots=config['all_brush_slots'])
    return context.read_pixels()


def _plans(renderer):
    return [stream.plan for stream in renderer._model_instance_streams.values()]


def _count(monkeypatch, target, name):
    calls = []
    real = getattr(target, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)
    monkeypatch.setattr(target, name, counted)
    return calls


@pytest.mark.perf
def test_a_still_world_draws_its_models_from_the_plan_it_has(context, monkeypatch):
    from engine.renderer.core import raw_calls
    renderer = _renderer()
    try:
        config = _config()
        first = _draw(renderer, context, config)
        assert not glh.is_blank(first)
        (plan,) = _plans(renderer)
        assert plan is not None and len(plan.runs) == 2, "two recipes: two runs"
        assert sum(len(items) for _vao, _n, items in plan.runs) > 2, "several material groups"

        sorts = _count(monkeypatch, passes, 'sort_into_runs')
        loads = _count(monkeypatch, renderer, '_model_run_vao')
        pointers = _count(monkeypatch, raw_calls, 'vertex_attrib_pointer')
        frames = [_draw(renderer, context, config) for _ in range(3)]
        assert _plans(renderer) == [plan] and _plans(renderer)[0] is plan
        assert sorts == [] and loads == [] and pointers == []
        assert all(np.array_equal(frame, first) for frame in frames)
    finally:
        renderer.cleanup()


def test_the_other_render_table_with_the_same_rows_uses_the_same_plan(context):
    """The render tables are double-buffered: every other frame arrives in
    another table object holding the same rows."""
    renderer = _renderer()
    try:
        first = _draw(renderer, context, _config())
        (plan,) = _plans(renderer)
        again = _draw(renderer, context, _config())           # a new table
        assert _plans(renderer)[0] is plan
        assert np.array_equal(again, first)
    finally:
        renderer.cleanup()


def test_a_model_that_moves_keeps_the_plan_and_draws_where_it_is(context):
    renderer = _renderer()
    try:
        first = _draw(renderer, context, _config())
        (plan,) = _plans(renderer)
        moved = list(SCENE)
        moved[1] = (DRUM, (0.0, 0.0, 60.0), 1.0)
        after = _draw(renderer, context, _config(moved))
        assert _plans(renderer)[0] is plan, "a transform is not the plan's business"
        assert not np.array_equal(after, first)
        fresh = _renderer()
        try:
            assert np.array_equal(_draw(fresh, context, _config(moved)), after)
        finally:
            fresh.cleanup()
    finally:
        renderer.cleanup()


@pytest.mark.parametrize("change", ["another model", "one fewer", "another order"])
def test_a_change_of_rows_or_recipes_makes_a_new_plan(context, change):
    renderer = _renderer()
    try:
        _draw(renderer, context, _config())
        (plan,) = _plans(renderer)
        scene = list(SCENE)
        if change == "another model":
            scene[0] = (DRUM, scene[0][1], 1.0)
        elif change == "one fewer":
            scene = scene[:2]
        else:
            scene = [scene[1], scene[0], scene[2]]
        after = _draw(renderer, context, _config(scene))
        (new,) = _plans(renderer)
        assert new is not plan
        fresh = _renderer()
        try:
            assert np.array_equal(_draw(fresh, context, _config(scene)), after)
        finally:
            fresh.cleanup()
    finally:
        renderer.cleanup()


def test_a_model_loaded_again_is_drawn_from_a_new_plan(context):
    """The plan holds the loaded model: one replaced (a file changed and
    reloaded) is a different object with different GL names."""
    renderer = _renderer()
    try:
        config = _config()
        first = _draw(renderer, context, config)
        (plan,) = _plans(renderer)
        path = next(key for key in renderer.loaded_models if 'Oil_Drum' in key)
        old = renderer.loaded_models.pop(path)
        after = _draw(renderer, context, config)              # loads it afresh
        assert renderer.loaded_models[path] is not old
        (new,) = _plans(renderer)
        assert new is not plan
        assert np.array_equal(after, first)
    finally:
        renderer.cleanup()


def test_a_model_not_loaded_yet_is_asked_for_again(context, monkeypatch):
    """A plan made while a model could not be loaded is not kept."""
    renderer = _renderer()
    try:
        config = _config()
        real = renderer.load_model
        monkeypatch.setattr(renderer, 'load_model',
                            lambda name: None if 'Oil_Drum' in name else real(name))
        without = _draw(renderer, context, config)
        assert _plans(renderer) == [None]
        monkeypatch.setattr(renderer, 'load_model', real)
        with_it = _draw(renderer, context, config)
        assert _plans(renderer)[0] is not None
        assert not np.array_equal(with_it, without)
    finally:
        renderer.cleanup()


def test_the_runs_vertex_arrays_leave_the_models_own_alone_and_are_released(context):
    renderer = _renderer()
    try:
        _draw(renderer, context, _config())
        streams = list(renderer._model_instance_streams.values())
        names = [name for stream in streams for name in stream.run_vaos.values()]
        assert len(names) == 2 and all(gl.glIsVertexArray(name) for name in names)
        for model in renderer.loaded_models.values():
            gl.glBindVertexArray(model.vao)
            assert not any(gl.glGetVertexAttribiv(index, gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED)[0]
                           for index in range(3, 11)), "no instance attribute on the model's own"
        gl.glBindVertexArray(0)
    finally:
        renderer.cleanup()
    assert not any(gl.glIsVertexArray(name) for name in names)
