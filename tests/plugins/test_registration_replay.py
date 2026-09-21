"""Plugin registrations must be restorable, not fire-once.

``PluginManager.discover_and_load`` early-outs on ``self._loaded``, so a
plugin's ``register()`` runs once per process and never again. The registries
it writes into -- ``editor.io_system.IO_REGISTRY`` and
``editor.things.ENTITY_TYPES`` -- are module-level singletons. Anything that
resets one of them therefore used to strip the plugins' declarations with no
way to get them back: the plugin stayed loaded and enabled while its I/O and
entity types silently vanished, and which tests had run first decided whether
the next one saw a working plugin.
"""
import pytest

pytest.importorskip("PyQt5", reason="the plugin registries are editor-tier")

from plugins.manager import get_manager, load_plugins  # noqa: E402


@pytest.fixture
def manager():
    load_plugins()
    return get_manager()


def _prop_inputs():
    from editor.io_system import get_input_names
    return [name.lower() for name in get_input_names("prop")]


def _prop_outputs():
    from editor.io_system import get_output_names
    return [name.lower() for name in get_output_names("prop")]


def test_registrations_are_recorded_for_replay(manager):
    kinds = {(kind, entity_type)
             for _plugin, kind, entity_type, _in, _out in manager._io_registrations}
    # Tidy owns its own two types and extends the core prop type.
    assert ("set", "tidyreceptacle") in kinds
    assert ("set", "tidygoal") in kinds
    assert ("extend", "prop") in kinds


def test_a_stripped_registry_is_restored_by_replay(manager):
    import editor.io_system as io_system
    from editor.things import ENTITY_TYPES

    before_inputs = set(_prop_inputs())
    before_outputs = set(_prop_outputs())
    assert "reset" in before_inputs, "Tidy's prop extension is not present to begin with"

    entry = io_system.IO_REGISTRY["prop"]
    io_system.IO_REGISTRY["prop"] = {
        "inputs": [d for d in entry["inputs"] if d.name.lower() != "reset"],
        "outputs": [d for d in entry["outputs"] if d.name.lower() != "ontidied"],
    }
    removed_class = ENTITY_TYPES.pop("TidyReceptacle", None)
    assert "reset" not in _prop_inputs()

    manager.reapply_registrations()

    assert set(_prop_inputs()) == before_inputs
    assert set(_prop_outputs()) == before_outputs
    assert "TidyReceptacle" in ENTITY_TYPES
    assert removed_class is not None


def test_replaying_an_extension_keeps_the_core_declarations(manager):
    """An 'extend' must re-merge, never replace.

    Recording the merged result and replaying it as a plain registration would
    pin prop's core declarations as they were at load, so a later change to
    them would be quietly reverted by the replay.
    """
    core = [name for name in _prop_inputs() if name != "reset"]
    assert core, "prop should declare inputs of its own"

    manager.reapply_registrations()

    after = _prop_inputs()
    assert all(name in after for name in core)
    assert "reset" in after


def test_replay_is_idempotent(manager):
    manager.reapply_registrations()
    once = _prop_inputs()
    manager.reapply_registrations()
    twice = _prop_inputs()

    assert once == twice
    assert len(twice) == len(set(twice)), "replay duplicated a declaration"


def test_replay_does_not_grow_the_recorded_list(manager):
    """Replaying goes through the same API, so it must not re-record."""
    before = len(manager._io_registrations)
    manager.reapply_registrations()
    assert len(manager._io_registrations) == before


def test_extend_io_is_idempotent_and_additive(manager):
    """The API a plugin uses to extend a type it does not own."""
    from plugins.api import EditorAPI, io_def

    plugin = next(p for p in manager.plugins if p.name.lower() == "tidy")
    api = EditorAPI(manager, plugin)
    before = _prop_inputs()

    api.extend_io("prop", inputs=[io_def("Reset", "already there")])
    assert _prop_inputs() == before, "extend_io re-added an existing declaration"

    api.extend_io("prop", inputs=[io_def("AuditProbe", "added by this test")])
    assert "auditprobe" in _prop_inputs()
    assert all(name in _prop_inputs() for name in before)
