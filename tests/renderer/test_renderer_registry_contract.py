"""The renderer registry checks what a factory returns before the host
paints with it, as the physics registry does: every member of the contract,
and a contract version it can run on. A renderer refused after it was made
is cleaned up first, so its GL objects do not outlive it."""

import pytest

from engine import renderer as boundary
from engine.renderer import registry
from engine.renderer.api import Renderer


def _complete(**attrs):
    """An object with every member of the contract, doing nothing."""
    class Complete:
        cleaned = 0

        def cleanup(self):
            type(self).cleaned += 1

    obj = Complete()
    for name in boundary.missing_members(object()):
        if name == 'cleanup':
            continue
        if callable(getattr(Renderer, name, None)):
            setattr(obj, name, lambda *a, **k: None)
        else:
            setattr(obj, name, None)
    obj.ready = True
    for name, value in attrs.items():
        setattr(obj, name, value)
    return obj


@pytest.fixture
def registered():
    saved = dict(registry._factories)
    yield lambda name, factory: registry.register_renderer(name, factory) and name
    registry._factories.clear()
    registry._factories.update(saved)


def test_the_renderer_contract_has_its_own_version():
    assert boundary.RENDERER_CONTRACT_VERSION == "1.0.0"
    assert boundary.RENDERER_CONTRACT_VERSION_INFO == (1, 0, 0)


def test_a_complete_renderer_is_created(registered):
    made = _complete()
    name = registered("Complete", lambda config: made)
    assert registry.create_renderer(name) is made
    assert boundary.missing_members(made) == []


def test_an_incomplete_renderer_is_refused_and_cleaned_up(registered):
    made = _complete()
    del made.render_scene
    made.draw_billboards = "not a method"
    name = registered("Half", lambda config: made)
    with pytest.raises(boundary.RendererContractError) as caught:
        registry.create_renderer(name)
    message = str(caught.value)
    assert "'Half'" in message and "render_scene" in message and "draw_billboards" in message
    assert type(made).cleaned == 1, "its GL objects must not outlive the refusal"
    assert issubclass(boundary.RendererContractError, TypeError)


def test_a_factory_that_raises_is_an_init_error_not_a_contract_error(registered):
    def missing_library(config):
        raise ImportError("No module named 'moderngl'")

    def constructor_bug(config):
        return int("x", base=None)                     # a TypeError inside

    for factory, cause in ((missing_library, ImportError), (constructor_bug, TypeError)):
        name = registered(factory.__name__, factory)
        with pytest.raises(boundary.RendererInitError) as caught:
            registry.create_renderer(name)
        assert not isinstance(caught.value, boundary.RendererContractError)
        assert isinstance(caught.value.__cause__, cause)


def test_an_unknown_name_is_a_key_error():
    with pytest.raises(KeyError):
        registry.create_renderer("NoSuchRenderer")


@pytest.mark.parametrize("declared, runs", [
    (None, True), ("1.0.0", True), ("1.0", True),
    ("1.1.0", False),                 # newer than this Fio's
    ("2.0.0", False), ("0.9.0", False),
])
def test_a_renderer_written_for_another_contract_version(registered, declared, runs):
    attrs = {} if declared is None else {"contract_version": declared}
    made = _complete(**attrs)
    name = registered("Versioned", lambda config: made)
    if runs:
        assert registry.create_renderer(name) is made
    else:
        with pytest.raises(boundary.RendererContractError, match="renderer contract"):
            registry.create_renderer(name)
        assert type(made).cleaned == 1
