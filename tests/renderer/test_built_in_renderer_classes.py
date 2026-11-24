"""Classic's and HD_High's classes against the contract, without a GL context.

The same checks for both renderers. Importing either needs PyOpenGL (the
``qt`` tier) but no context: a class says what its methods take and where
each was defined, which is all these read. What a made renderer holds -- its
attributes' values, its GL objects -- is in
``tests/visual/test_renderer_conformance.py``.
"""

import functools
import inspect

import pytest

from engine.renderer import (
    RENDERER_CONTRACT_VERSION, available_renderers, renderer_factory)
from engine.renderer import api

pytestmark = [pytest.mark.qt]

BUILT_IN = {"Forward": "engine.renderer.forward", "HD_High": "engine.renderer.hd_high"}
SHARED = "engine.renderer.core"

def test_both_built_in_renderers_are_registered():
    assert set(BUILT_IN) <= set(available_renderers())


@pytest.mark.parametrize("name", BUILT_IN)
def test_every_method_takes_what_the_contract_declares(name):
    """Each method as the class defines it, with ``self`` taken: what the
    registry checks on an instance, asked of the class."""
    cls = renderer_factory(name)
    for options in (api.Renderer, api.RendererOptions):
        methods, _attributes = api._declared_members(options)
        assert methods
        for member, declared in methods.items():
            bound = functools.partial(getattr(cls, member), None)
            assert api._method_violation(member, declared, bound) is None
    assert isinstance(inspect.getattr_static(cls, "ready"), property)


@pytest.mark.parametrize("name", BUILT_IN)
def test_the_contract_version_is_declared_by_the_renderer_itself(name):
    cls = renderer_factory(name)
    assert vars(cls).get("contract_version") == RENDERER_CONTRACT_VERSION
    assert api.contract_compatible(cls) is None


@pytest.mark.parametrize("name", BUILT_IN)
def test_a_renderer_is_made_of_its_own_package_and_the_shared_core(name):
    """No class of the other renderer is among its bases, so no method of
    the other's -- public or internal -- is one it could run."""
    cls = renderer_factory(name)
    assert cls.__module__.startswith(BUILT_IN[name])
    for base in cls.__mro__[:-1]:
        assert base.__module__.startswith((BUILT_IN[name], SHARED)), (
            "%s is built on %s.%s" % (name, base.__module__, base.__qualname__))
    methods, attributes = api._declared_members()
    for member in list(methods) + ["ready"]:
        owner = next(base for base in cls.__mro__ if member in vars(base))
        assert owner.__module__.startswith((BUILT_IN[name], SHARED)), (member, owner)


def test_the_two_renderers_share_no_class_outside_the_core():
    forward, hd_high = (set(renderer_factory(name).__mro__) for name in BUILT_IN)
    shared = (forward & hd_high) - {object}
    assert shared, "both are built on RendererCore"
    assert all(cls.__module__.startswith(SHARED) for cls in shared), shared
