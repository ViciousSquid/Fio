"""The renderer registry checks what a factory returns before the host
paints with it, as the physics registry does: the whole contract
(``contract_violations``) -- every member, each method's arguments, each
attribute's type, a declared contract version it can run on. A renderer
refused after it was made is cleaned up first, so its GL objects do not
outlive it. The malformed implementations are in
``test_renderer_contract_validation.py``."""

import pytest

from engine import renderer as boundary
from engine.renderer import registry
from tests.helpers.renderers import conforming_renderer


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
    made = conforming_renderer()
    name = registered("Complete", lambda config: made)
    assert registry.create_renderer(name) is made
    assert boundary.contract_violations(made) == []
    assert boundary.missing_members(made) == []
    assert made.cleaned == 0, "a renderer that is accepted is not cleaned up"


def test_an_incomplete_renderer_is_refused_and_cleaned_up(registered):
    made = conforming_renderer(render_scene=None, draw_billboards="not a method")
    name = registered("Half", lambda config: made)
    with pytest.raises(boundary.RendererContractError) as caught:
        registry.create_renderer(name)
    message = str(caught.value)
    assert "'Half'" in message
    assert "render_scene is missing" in message
    assert "draw_billboards is not callable" in message
    assert boundary.missing_members(made) == ["draw_billboards", "render_scene"]
    assert made.cleaned == 1, "its GL objects must not outlive the refusal"
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
    ("1.0.0", True), ("1.0", True),
    ("1.1.0", False),                 # newer than this Fio's
    ("2.0.0", False), ("0.9.0", False),
])
def test_a_renderer_written_for_another_contract_version(registered, declared, runs):
    made = conforming_renderer(contract_version=declared)
    name = registered("Versioned", lambda config: made)
    if runs:
        assert registry.create_renderer(name) is made
        assert boundary.contract_compatible(made) is None
    else:
        with pytest.raises(boundary.RendererContractError, match="written for renderer contract"):
            registry.create_renderer(name)
        assert "written for renderer contract" in boundary.contract_compatible(made)
        assert made.cleaned == 1


def test_a_renderer_that_does_not_say_which_contract_is_refused(registered):
    """An undeclared version is not taken as this one."""
    made = conforming_renderer(contract_version=None)
    name = registered("Unversioned", lambda config: made)
    with pytest.raises(boundary.RendererContractError, match="contract_version is not declared"):
        registry.create_renderer(name)
    assert boundary.contract_compatible(made) == "contract_version is not declared"
    assert made.cleaned == 1
