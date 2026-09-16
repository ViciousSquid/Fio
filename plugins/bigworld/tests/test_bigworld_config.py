"""One authoritative description of a Big World map's configuration.

A map's Big World settings are read by three things that share no code path:
the placeable :class:`~plugins.bigworld.entities.BigWorldSettings` entity (which
seeds its own defaults), the plugin's editor property schema (labels, widget
types, ranges) and
:func:`~plugins.bigworld.persistence.config_from_settings` (which coerces a
saved map into the config a session is built from).

They used to hold three hand-written copies of one field list, which agreed
only for as long as nobody edited one of them.  The failure that costs is
silent: a field added to the editor schema but not to the coercer is editable
in the property panel and read back as its *fallback*, so the map plays at a
radius the mapper never chose and nothing reports an error.

These tests hold the three to :mod:`plugins.bigworld.config` — and to each
other — so the copies cannot come back.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from plugins.bigworld import config, persistence          # noqa: E402
from plugins.bigworld.entities import BigWorldSettings    # noqa: E402
from plugins.bigworld.plugin import BigWorldPlugin        # noqa: E402


class _RecordingAPI:
    """The slice of the plugin API ``register()`` uses."""

    def __init__(self):
        self.entities = []
        self.properties = {}

    def register_entity(self, cls, menu_label=""):
        self.entities.append((cls, menu_label))

    def register_properties(self, entity_type, specs):
        self.properties[entity_type] = list(specs)


def _registered_schema():
    api = _RecordingAPI()
    BigWorldPlugin().register(api)
    return api.properties[BigWorldPlugin.SETTINGS_TYPE]


# ---------------------------------------------------------------------------
# One definition, one set of keys
# ---------------------------------------------------------------------------

def test_there_is_exactly_one_bigworld_settings_class():
    """The entity is the plugin's, and the engine has no rival definition.

    Big World is a plugin: the engine and the editor may know the *type string*
    a map opts in with, but neither may carry its own settings class, or a map
    would mean one thing to the editor and another to the streamer.
    """
    import editor.things as things
    assert not hasattr(things, "BigWorldSettings"), (
        "editor.things has grown a second BigWorldSettings; the plugin's entity "
        "is the only definition, or the editor and the runtime read different "
        "defaults from the same map")
    assert BigWorldSettings.TYPE == BigWorldPlugin.SETTINGS_TYPE, (
        "the entity's type string and the plugin's opt-in test disagree, so a "
        "map carrying the entity would not switch streaming on")


def test_the_entity_the_schema_and_the_coercer_describe_the_same_keys():
    entity_keys = set(BigWorldSettings().properties) - {"type", "id", "name"}
    schema_keys = {spec.name for spec in _registered_schema()}
    config_keys = set(persistence.config_from_settings(None))
    table_keys = set(config.BY_KEY)

    assert table_keys == schema_keys, (
        "the editor property schema and the field table disagree: %s"
        % (table_keys ^ schema_keys,))
    assert table_keys == config_keys, (
        "a key is editable but is not in the config a session is built from, "
        "or vice versa: %s" % (table_keys ^ config_keys,))
    assert table_keys <= entity_keys, (
        "a placed settings entity does not carry every configurable key: %s"
        % (table_keys - entity_keys,))


def test_every_default_is_the_same_number_everywhere():
    """Three readers, one default per key.

    A default that drifts is the quietest bug of the three: the map opens, the
    panel shows one number and the session runs on another.
    """
    schema = {spec.name: spec.default for spec in _registered_schema()}
    entity = BigWorldSettings().properties
    runtime = persistence.config_from_settings(None)

    for key, expected in config.defaults().items():
        assert schema[key] == expected, (
            "editor schema default for %r is %r, the table says %r"
            % (key, schema[key], expected))
        assert entity[key] == expected, (
            "a placed entity's %r is %r, the table says %r"
            % (key, entity[key], expected))
        assert runtime[key] == expected, (
            "the runtime config's %r is %r, the table says %r"
            % (key, runtime[key], expected))


def test_the_schema_carries_a_label_and_help_for_every_key():
    for spec in _registered_schema():
        assert spec.label, "%s has no label in the property panel" % spec.name
        assert spec.help, "%s has no help text" % spec.name
        assert spec.type in ("bool", "float"), (
            "%s declares widget type %r, which config.coerce cannot read back"
            % (spec.name, spec.type))


# ---------------------------------------------------------------------------
# Reading a map back
# ---------------------------------------------------------------------------

def test_values_that_survived_a_json_round_trip_still_read_correctly():
    """A saved map carries strings and ints where the panel wrote bools/floats."""
    cfg = persistence.config_from_settings({"properties": {
        "enabled": "false",
        "activation_radius": "4096",
        "terrain_fill": 1,
        "terrain_stream_radius": "",
    }})
    assert cfg["enabled"] is False
    assert cfg["activation_radius"] == 4096.0
    assert cfg["terrain_fill"] is True
    assert cfg["terrain_stream_radius"] == 0.0, (
        "an unparseable value should fall back to the field's default, not raise")


def test_the_deactivation_radius_can_never_sit_inside_the_activation_radius():
    """The hysteresis band has to be a band.

    Inverted, a cell would be dropped the same frame it was added — thrash, not
    hysteresis — so the clamp belongs with the field table and has to hold
    wherever the config is read from.
    """
    props = {"activation_radius": 4096.0, "deactivation_radius": 100.0}
    assert persistence.config_from_settings({"properties": props})["deactivation_radius"] == 4096.0
    assert BigWorldSettings(properties=dict(props)).deactivation_radius() == 4096.0


def test_the_entitys_accessors_and_the_runtime_config_give_the_same_answers():
    """The typed accessors are a convenience, not a second opinion."""
    props = {"enabled": "yes", "activation_radius": "3000",
             "deactivation_radius": "3100", "sim_near_radius": "900",
             "terrain_fill": "on", "terrain_infinite": 0,
             "terrain_stream_radius": "512", "disk_streaming": "true",
             "show_cell_debug": "off"}
    entity = BigWorldSettings(properties=dict(props))
    cfg = persistence.config_from_settings({"properties": dict(props)})

    assert entity.is_enabled() == cfg["enabled"]
    assert entity.activation_radius() == cfg["activation_radius"]
    assert entity.deactivation_radius() == cfg["deactivation_radius"]
    assert entity.sim_near_radius() == cfg["sim_near_radius"]
    assert entity.show_cell_debug() == cfg["show_cell_debug"]
    assert entity.terrain_fill() == cfg["terrain_fill"]
    assert entity.terrain_infinite() == cfg["terrain_infinite"]
    assert entity.terrain_stream_radius() == cfg["terrain_stream_radius"]
    assert entity.disk_streaming() == cfg["disk_streaming"]


def test_an_entity_placed_today_reads_back_as_its_own_defaults():
    """Placing the entity and changing nothing must not change how a map plays."""
    entity = BigWorldSettings()
    cfg = persistence.config_from_settings(entity)
    assert cfg == config.config_from_properties(config.defaults())
