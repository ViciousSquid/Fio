"""
The Tidy plugin.

Tidy extends the core Prop primitive with one piece of metadata,
tidy_category, and adds receptacle/goal gameplay. Core Prop remains the sole
owner of pickup, carry, drop and physics behaviour.
"""

from __future__ import annotations

import os

from plugins.api import FioPlugin, TickContext, io_def, prop

from .entities import TidyGoal, TidyReceptacle
from .runtime import TidySession


class TidyPlugin(FioPlugin):
    name = "tidy"
    version = "2.0.0"
    description = "Put core Props away into categorized receptacles and goals."
    category = "Tidy"
    enabled = False

    def _load_demo_map(self, main_window):
        if not main_window.check_unsaved_changes():
            return
        path = os.path.join(os.path.dirname(__file__), "Tidy_Test.json")
        if not os.path.isfile(path):
            main_window.show_toast(
                "Tidy demo map is missing from the plugin.",
                is_error=True,
            )
            return
        main_window.load_level_file(path)

    def register(self, api):
        api.register_menu_action(
            "Load Demo map",
            self._load_demo_map,
            "Load the bundled Tidy demonstration map",
        )

        # Tidyable objects are ordinary core Props. The plugin only adds its
        # metadata to the Prop property panel.
        api.register_extra_fields(
            "prop",
            [
                prop(
                    "tidy_category",
                    type="string",
                    label="Tidy category",
                    default="",
                    help="Non-empty categories make this Prop a Tidy object.",
                    group="Tidy",
                ),
            ],
        )

        api.register_entity(
            TidyReceptacle,
            menu_label="Tidy Receptacle (shelf/bin)",
        )
        api.register_entity(
            TidyGoal,
            menu_label="Tidy Goal",
        )

        api.register_properties(
            "tidyreceptacle",
            [
                prop(
                    "accepts",
                    type="string",
                    label="Accepts category",
                    default="any",
                    help="Category accepted by this receptacle, or 'any'.",
                    group="Tidy",
                ),
                prop(
                    "capacity",
                    type="int",
                    label="Capacity",
                    default=24,
                    min=1,
                    max=100000,
                    help="Maximum number of Props this receptacle can hold.",
                    group="Tidy",
                ),
                prop(
                    "slot_cols",
                    type="int",
                    label="Slots per row",
                    default=6,
                    min=1,
                    max=1000,
                    help="Number of slots across before a new row begins.",
                    group="Placement",
                ),
                prop(
                    "slot_spacing",
                    type="vec3",
                    label="Slot spacing",
                    default=[28.0, 40.0, 0.0],
                    help="Spacing between successive slots as [x, y, z].",
                    group="Placement",
                ),
                prop(
                    "slot_offset",
                    type="vec3",
                    label="Slot offset",
                    default=[0.0, 0.0, 0.0],
                    help="Offset of the first slot from the receptacle origin.",
                    group="Placement",
                ),
                prop(
                    "reach",
                    type="float",
                    label="Placement reach",
                    default=140.0,
                    min=1.0,
                    max=10000.0,
                    help="Maximum distance at which a held Prop can be placed.",
                    group="Placement",
                ),
                prop(
                    "disabled",
                    type="bool",
                    label="Disabled",
                    default=False,
                    help="When enabled, the receptacle refuses new objects.",
                    group="Tidy",
                ),
            ],
        )
        api.register_properties(
            "tidygoal",
            [
                prop(
                    "target",
                    type="string",
                    label="Target",
                    default="all",
                    help="Use 'all' or enter a numeric target count.",
                    group="Goal",
                ),
                prop(
                    "category",
                    type="string",
                    label="Category",
                    default="any",
                    help="Restrict progress to one Tidy category, or use 'any'.",
                    group="Goal",
                ),
                prop(
                    "show_hud",
                    type="bool",
                    label="Show HUD",
                    default=True,
                    help="Show the live Tidied: N / M counter.",
                    group="Goal",
                ),
                prop(
                    "disabled",
                    type="bool",
                    label="Disabled",
                    default=False,
                    help="When enabled, this goal stops counting progress.",
                    group="Goal",
                ),
            ],
        )

        # Extend the core Prop I/O instead of replacing it. The core system
        # already owns Enable/Disable/Drop/Wake and OnPickedUp/OnDropped/OnRest.
        api.register_io(
            "tidyreceptacle",
            inputs=[
                io_def("Reset", "Empty the receptacle; send its objects home"),
                io_def("Enable", "Allow objects to be placed here"),
                io_def("Disable", "Refuse new objects"),
            ],
            outputs=[
                io_def("OnObjectPlaced", "Fired each time an object is stowed here", "int"),
                io_def("OnFull", "Fired when the receptacle reaches capacity"),
            ],
        )
        api.register_io(
            "tidygoal",
            inputs=[
                io_def("Enable", "Count toward completion"),
                io_def("Disable", "Stop counting"),
            ],
            outputs=[
                io_def("OnProgress", "Fired on every stow: 'done/need'", "string"),
                io_def("OnComplete", "Fired once when the tidy target is reached"),
            ],
        )

        # Tidy extends the *core* prop type rather than owning one, so this
        # adds to prop's declarations instead of replacing them. Going through
        # the API (rather than reaching into editor.io_system and doing the
        # read-merge-write by hand) keeps the registration visible to the
        # manager, which is what lets it be replayed if the process-wide
        # registry is ever reset. It is a no-op where there is no editor tier.
        api.extend_io(
            "prop",
            inputs=[io_def("Reset", "Return this tidy Prop to its authored position")],
            outputs=[io_def("OnTidied", "Fired when this Prop is put away")],
        )

    def map_uses_plugin(self, map_data: dict) -> bool:
        """Auto-enable Tidy for receptacles, goals, or marked core Props."""
        things = map_data.get("things", []) if isinstance(map_data, dict) else []
        for thing in things:
            if not isinstance(thing, dict):
                continue
            raw_type = thing.get("type") or thing.get("properties", {}).get("type")
            norm = str(raw_type or "").replace("_", "").lower()
            if norm in ("tidyreceptacle", "tidygoal"):
                return True
            if norm == "prop":
                props = thing.get("properties", {})
                if isinstance(props, dict) and str(props.get("tidy_category", "")).strip():
                    return True
        return False

    def register_runtime(self, api):
        def _session(logic):
            return getattr(logic, "_tidy", None)

        def prop_reset(entity, param, logic):
            session = _session(logic)
            if session is not None:
                session.reset_object(entity)

        def recept_reset(entity, param, logic):
            session = _session(logic)
            if session is not None:
                session.reset_receptacle(entity)

        def recept_enable(entity, param, logic):
            entity.properties["disabled"] = False

        def recept_disable(entity, param, logic):
            entity.properties["disabled"] = True

        def goal_enable(entity, param, logic):
            entity.properties["disabled"] = False

        def goal_disable(entity, param, logic):
            entity.properties["disabled"] = True

        api.register_input_handler("prop", "reset", prop_reset)
        api.register_input_handler("tidyreceptacle", "reset", recept_reset)
        api.register_input_handler("tidyreceptacle", "enable", recept_enable)
        api.register_input_handler("tidyreceptacle", "disable", recept_disable)
        api.register_input_handler("tidygoal", "enable", goal_enable)
        api.register_input_handler("tidygoal", "disable", goal_disable)

    def on_play_start(self, logic):
        session = TidySession(logic)
        session.start()
        logic._tidy = session

        previous = getattr(logic, "_prop_drop_interceptor", None)

        def intercept(prop):
            if self.enabled and session.consume_drop(prop):
                return True
            if previous is not None:
                try:
                    return bool(previous(prop))
                except Exception:
                    return False
            return False

        session._drop_interceptor = intercept
        session._previous_drop_interceptor = previous
        logic._prop_drop_interceptor = intercept

    def on_play_stop(self, logic):
        session = getattr(logic, "_tidy", None)
        if session is not None:
            session.stop()

            interceptor = getattr(session, "_drop_interceptor", None)
            if getattr(logic, "_prop_drop_interceptor", None) is interceptor:
                logic._prop_drop_interceptor = getattr(
                    session, "_previous_drop_interceptor", None
                )

        logic._tidy = None

    def on_tick(self, logic, ctx: TickContext):
        session = getattr(logic, "_tidy", None)
        if session is None:
            return
        session.tick(ctx)


PLUGIN = TidyPlugin()
