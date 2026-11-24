"""Malformed renderers, each wrong in one way, and what the registry says.

``engine.renderer.api.contract_violations`` is the check the registry runs on
what a factory returns. Each implementation here is a conforming renderer
with one defect; the registry must refuse it, say which member is wrong and
why, and clean it up exactly once. The last section is the other side: what
the contract leaves a renderer free to do is still accepted.

No GL: these run in the headless tier. The same refusals made of the two
built-in renderers, with their GL objects counted, are in
``tests/visual/test_renderer_conformance.py``.
"""

import functools

import pytest

from engine import renderer as boundary
from engine.renderer import registry
from engine.view_distance import ViewDistance
from tests.helpers.renderers import conforming_renderer


@pytest.fixture
def refused():
    """Register an object as a renderer; returns the registry's refusal."""
    saved = dict(registry._factories)

    def create(made):
        registry.register_renderer("Malformed", lambda config: made)
        with pytest.raises(boundary.RendererContractError) as caught:
            registry.create_renderer("Malformed")
        return str(caught.value)
    yield create
    registry._factories.clear()
    registry._factories.update(saved)


def _raises(error):
    def read(self):
        raise error
    return property(read)


# -- incorrect signatures -------------------------------------------------------

def _renamed(self, projection, view, camera_pos, selection, config,
             clear=True, brush_slots=None):
    pass


def _reordered(self, view, projection, camera_pos, primary_selection, config,
               clear=True, brush_slots=None):
    pass


def _short(self, projection, view, camera_pos, primary_selection, config, clear=True):
    pass


def _no_default(self, projection, view, camera_pos, primary_selection, config,
                clear, brush_slots=None):
    pass


def _other_default(self, projection, view, camera_pos, primary_selection, config,
                   clear=False, brush_slots=None):
    pass


def _default_of_another_type(self, projection, view, positions,
                             width=40, height=18.0, lift=0.0, sprites=()):
    pass


def _extra_required(self, projection, view, camera_pos, primary_selection, config,
                    clear=True, brush_slots=None, *, target):
    pass


def _extra_positional(self, projection, view, positions, size, tex_id, colour):
    pass


def _catch_all(self, *args, **kwargs):
    pass


def _positional_only(self, texture_name, subfolder, /):
    return 0


def _no_arguments(self):
    pass


SIGNATURES = [
    ("render_scene", _renamed, "argument 4 is 'selection', not 'primary_selection'"),
    ("render_scene", _reordered, "argument 1 is 'view', not 'projection'"),
    ("render_scene", _short, "takes no 'brush_slots' argument"),
    ("render_scene", _no_default, "'clear' has no default"),
    ("render_scene", _other_default, "'clear' defaults to False, not True"),
    ("draw_player_glasses", _default_of_another_type, "'width' defaults to 40, not 40.0"),
    ("render_scene", _extra_required, "requires 'target'"),
    ("draw_billboards", _extra_positional, "requires 'colour'"),
    ("render_scene", _catch_all, "takes no 'projection' argument"),
    ("load_texture", _positional_only, "argument 1 is 'texture_name'"),
    ("set_grid", _no_arguments, "takes no 'world_size' argument"),
    ("cleanup", lambda self, force: None, "requires 'force'"),
    ("invalidate_reflection_probes", staticmethod(lambda force: None), "requires 'force'"),
]


@pytest.mark.parametrize("name, method, why", SIGNATURES,
                         ids=["%s-%d" % (s[0], i) for i, s in enumerate(SIGNATURES)])
def test_a_method_with_the_wrong_signature_is_refused(refused, name, method, why):
    made = conforming_renderer(**{name: method})
    message = refused(made)
    assert name in message and why in message, message
    assert "the contract declares %s(" % name in message
    if name != "cleanup":
        assert made.cleaned == 1


def test_every_method_of_the_contract_is_checked(refused):
    """Not a chosen few: each method, given no arguments, is refused."""
    methods, _attributes = boundary.api._declared_members()
    assert len(methods) == 15
    for name, declared in methods.items():
        takes_arguments = declared.__code__.co_argcount > 1
        made = conforming_renderer(**{name: _catch_all if takes_arguments else "text"})
        problems = boundary.contract_violations(made)
        assert len(problems) == 1 and problems[0].startswith(name), (name, problems)


# -- missing members --------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "view_distance", "shadows_enabled", "water_quality", "render_stats", "ready",
    "cleanup", "render_scene", "load_texture", "draw_collision_visualization"])
def test_a_missing_member_is_refused(refused, name):
    made = conforming_renderer(**{name: None})
    assert "%s is missing" % name in refused(made)
    assert name in boundary.missing_members(made)
    if name != "cleanup":
        assert made.cleaned == 1


def test_something_that_is_no_renderer_at_all_is_refused(refused):
    for made in (None, object(), 5):
        message = refused(made)
        assert "render_scene is missing" in message
        assert "contract_version is not declared" in message


# -- invalid types and values -------------------------------------------------------

TYPES = [
    ("shadows_enabled", 1, "shadows_enabled must be bool, not 1 (int)"),
    ("shadows_enabled", "yes", "shadows_enabled must be bool, not 'yes' (str)"),
    ("shadows_enabled", (lambda self: True), "shadows_enabled must be bool"),
    ("water_quality", "ultra", "water_quality must be one of 'cheap', 'expensive', not 'ultra'"),
    ("water_quality", 1, "water_quality must be str, not 1 (int)"),
    ("render_stats", {}, "render_stats must be RenderStats, not {} (dict)"),
    ("render_stats", boundary.RenderStats, "render_stats must be RenderStats"),
    ("view_distance", 8000.0, "view_distance must be ViewDistance or None, not 8000.0 (float)"),
    ("ready", property(lambda self: 1), "ready must be bool, not 1 (int)"),
    ("ready", property(lambda self: "yes"), "ready must be bool, not 'yes' (str)"),
    ("ready", (lambda self: True), "ready must be bool"),          # a method, not a property
]


@pytest.mark.parametrize("name, value, why", TYPES,
                         ids=["%s-%d" % (t[0], i) for i, t in enumerate(TYPES)])
def test_an_attribute_of_the_wrong_type_is_refused(refused, name, value, why):
    made = conforming_renderer(**{name: value})
    assert why in refused(made)
    assert made.cleaned == 1
    assert boundary.missing_members(made) == [], "it is there; it is wrong"


# -- broken properties ----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "ready", "view_distance", "shadows_enabled", "water_quality", "render_stats",
    "contract_version", "render_scene", "draw_billboards"])
@pytest.mark.parametrize("error", [
    RuntimeError("no context"), AttributeError("_shader_init_failed"), KeyError("lit")],
    ids=lambda error: type(error).__name__)
def test_a_property_that_raises_is_refused(refused, name, error):
    made = conforming_renderer(**{name: _raises(error)})
    message = refused(made)
    assert "%s raised %s" % (name, type(error).__name__) in message, message
    assert "%s is missing" % name not in message
    assert made.cleaned == 1


def test_a_broken_cleanup_property_is_reported_with_the_refusal(refused):
    made = conforming_renderer(cleanup=_raises(RuntimeError("gone")))
    assert "cleanup raised RuntimeError: gone" in refused(made)


# -- incompatible and invalid versions --------------------------------------------------

@pytest.mark.parametrize("declared", [
    "2.0.0", "1.1.0", "1.0.1", "0.9.0", "0.1", "10.0.0"])
def test_a_version_this_fio_cannot_run_is_refused(refused, declared):
    made = conforming_renderer(contract_version=declared)
    message = refused(made)
    assert "contract_version: written for renderer contract %s" % declared in message
    assert made.cleaned == 1


@pytest.mark.parametrize("declared", [
    "", "1", "abc", "v1.0.0", "1.0.0-beta", "1.0.0.0", " 1.0.0", "1.0.0\n", "1.x.0",
    1.0, 1, (1, 0, 0), b"1.0.0", True, (lambda self: "1.0.0")],
    ids=lambda declared: "a method" if callable(declared) else repr(declared))
def test_a_declaration_that_is_not_a_version_is_refused(refused, declared):
    made = conforming_renderer(contract_version=declared)
    assert "is not a contract version" in refused(made)
    assert made.cleaned == 1


# -- every violation, and the clean-up ---------------------------------------------------

def test_every_violation_is_named_at_once(refused):
    made = conforming_renderer(
        contract_version=None, shadows_enabled=0, water_quality="ultra",
        render_scene=_short, ready=_raises(RuntimeError("no context")), set_grid=None)
    message = refused(made)
    for part in ("contract_version is not declared", "shadows_enabled must be bool",
                 "water_quality must be one of", "takes no 'brush_slots' argument",
                 "ready raised RuntimeError: no context", "set_grid is missing"):
        assert part in message, (part, message)
    assert len(boundary.contract_violations(made)) == 6
    assert made.cleaned == 1


def test_inspection_calls_nothing_and_changes_nothing():
    calls = []

    def recorded(name):
        def method(self, *args, **kwargs):
            calls.append(name)
        return method
    made = conforming_renderer(cleanup=recorded("cleanup"),
                               invalidate_reflection_probes=recorded("probes"))
    before = dict(vars(made))
    assert boundary.contract_violations(made) == []
    assert boundary.missing_members(made) == []
    assert boundary.contract_compatible(made) is None
    assert calls == [] and vars(made) == before


def test_a_cleanup_that_raises_does_not_hide_the_refusal():
    def cleanup(self):
        raise RuntimeError("context lost")
    made = conforming_renderer(contract_version="2.0.0", cleanup=cleanup)
    saved = dict(registry._factories)
    try:
        registry.register_renderer("Malformed", lambda config: made)
        with pytest.raises(boundary.RendererContractError) as caught:
            registry.create_renderer("Malformed")
    finally:
        registry._factories.clear()
        registry._factories.update(saved)
    assert "written for renderer contract 2.0.0" in str(caught.value)
    assert any("cleanup() raised RuntimeError: context lost" in note
               for note in caught.value.__notes__)


# -- what the contract leaves free ----------------------------------------------------------

def _with_extras(self, projection, view, camera_pos, primary_selection, config,
                 clear=True, brush_slots=None, target=None, *, debug=False, **more):
    pass


def _required_given_a_default(self, world_size=2048, grid_size=16):
    pass


def test_a_renderer_may_add_optional_arguments_and_members():
    made = conforming_renderer(render_scene=_with_extras,
                               set_grid=_required_given_a_default,
                               view_distance=ViewDistance(),
                               water_quality="cheap", shadows_enabled=False,
                               contract_version="1.0",
                               its_own_setting=3)
    assert boundary.contract_violations(made) == []


def test_a_method_need_not_be_a_plain_function():
    """A bound partial, a callable object or a function set on the instance
    takes the contract's call just as well."""
    class Loader:
        def __call__(self, texture_name, subfolder):
            return 0

    def set_grid(renderer, world_size, grid_size):
        pass
    made = conforming_renderer(load_texture=Loader())
    made.set_grid = functools.partial(set_grid, made)
    made.cleanup = lambda: None
    assert boundary.contract_violations(made) == []


def test_the_contract_lists_its_own_members():
    """The validator reads the protocol; nothing is listed twice to drift."""
    methods, attributes = boundary.api._declared_members()
    assert set(attributes) == {"contract_version", "view_distance", "shadows_enabled",
                               "water_quality", "render_stats", "ready"}
    assert attributes["ready"] is bool and attributes["shadows_enabled"] is bool
    assert attributes["render_stats"] is boundary.RenderStats
    assert set(boundary.api.ATTRIBUTE_VALUES) <= set(attributes)
    assert boundary.api.ATTRIBUTE_VALUES["water_quality"] == boundary.WATER_QUALITIES
    assert not set(methods) & set(attributes)


# -- the optional members (RendererOptions) ---------------------------------------------------

def test_the_optional_members_are_the_ones_declared():
    methods, attributes = boundary.api._declared_members(boundary.RendererOptions)
    assert set(methods) == {"animate_textures", "draw_face_hover", "draw_lens_water"}
    assert attributes == {"hd": bool, "lowpower_mode": bool,
                          "reflections_enabled": bool, "decal_reflection_fade": float}


def test_a_renderer_without_the_optional_members_conforms():
    assert boundary.contract_violations(conforming_renderer()) == []
    made = conforming_renderer(animate_textures=None)
    made.draw_lens_water = None                 # the host reads None as absent too
    assert boundary.contract_violations(made) == []


def test_a_renderer_with_the_optional_members_as_declared_conforms():
    made = conforming_renderer(
        animate_textures=lambda self, clock: None,
        draw_face_hover=lambda self, projection, view, triangles: None,
        draw_lens_water=lambda self, drops, viewport, strength=1.0: 0)
    assert boundary.contract_violations(made) == []


@pytest.mark.parametrize("name, value, why", [
    ("animate_textures", (lambda self: None), "takes no 'clock' argument"),
    ("animate_textures", (lambda self, clock, paused: None), "requires 'paused'"),
    ("draw_face_hover", (lambda self, projection, view, face: None),
     "argument 3 is 'face', not 'triangles'"),
    ("draw_lens_water", (lambda self, viewport, drops: 0),
     "argument 1 is 'viewport', not 'drops'"),
    ("draw_lens_water", True, "draw_lens_water is not callable"),
    ("draw_face_hover", _raises(RuntimeError("no overlay")),
     "draw_face_hover raised RuntimeError: no overlay"),
])
def test_an_optional_member_that_is_there_is_held_to_its_declaration(
        refused, name, value, why):
    made = conforming_renderer(**{name: value})
    assert why in refused(made)
    assert made.cleaned == 1


def test_a_renderer_with_the_optional_settings_as_declared_conforms():
    made = conforming_renderer(hd=True, lowpower_mode=False, reflections_enabled=True,
                               decal_reflection_fade=1536.0)
    assert boundary.contract_violations(made) == []
    made.reflections_enabled = False
    made.decal_reflection_fade = 0.0
    assert boundary.contract_violations(made) == []


@pytest.mark.parametrize("name, value, why", [
    ("hd", 1, "hd must be bool, not 1 (int)"),
    ("hd", "high", "hd must be bool, not 'high' (str)"),
    ("lowpower_mode", "False", "lowpower_mode must be bool"),
    ("reflections_enabled", 0, "reflections_enabled must be bool, not 0 (int)"),
    ("decal_reflection_fade", "far", "decal_reflection_fade must be float, not 'far' (str)"),
    ("decal_reflection_fade", 1536, "decal_reflection_fade must be float, not 1536 (int)"),
    ("decal_reflection_fade", _raises(RuntimeError("no decals")),
     "decal_reflection_fade raised RuntimeError: no decals"),
])
def test_an_optional_setting_that_is_there_is_held_to_its_type(refused, name, value, why):
    made = conforming_renderer(**{name: value})
    assert why in refused(made)
    assert made.cleaned == 1
