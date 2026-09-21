"""
The Tidy plugin.

Tidy extends the core Prop primitive with one piece of metadata,
tidy_category, and adds receptacle/goal gameplay. Core Prop remains the sole
owner of pickup, carry, drop and physics behaviour.
"""

from __future__ import annotations

from plugins.api import FioPlugin, TickContext, io_def, prop

from .entities import BOOK_MODEL, TidyGoal, TidyReceptacle
from .runtime import TidySession


class TidyPlugin(FioPlugin):
    name = "tidy"
    version = "2.0.0"
    description = "Put core Props away into categorized receptacles and goals."
    category = "Tidy"
    enabled = False

    def register(self, api):
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

        # Extend the core Prop I/O instead of replacing it. The core system
        # already owns Enable/Disable/Drop/Wake and OnPickedUp/OnDropped/OnRest.
        try:
            from editor.io_system import get_inputs, get_outputs, register_io

            inputs = list(get_inputs("prop"))
            outputs = list(get_outputs("prop"))
            input_names = {d.name.lower() for d in inputs}
            output_names = {d.name.lower() for d in outputs}

            if "reset" not in input_names:
                inputs.append(io_def("Reset", "Return this tidy Prop to its authored position"))
            if "ontidied" not in output_names:
                outputs.append(io_def("OnTidied", "Fired when this Prop is put away"))

            register_io("prop", inputs, outputs)
        except Exception:
            # Headless/player processes do not expose the editor I/O registry.
            pass

    def migrate_map_data(self, map_data: dict) -> None:
        """Convert legacy tidyobject records into core prop records."""
        things = map_data.get("things", []) if isinstance(map_data, dict) else []
        for thing in things:
            if not isinstance(thing, dict):
                continue

            raw_type = thing.get("type") or thing.get("properties", {}).get("type")
            norm = str(raw_type or "").replace("_", "").lower()
            if norm != "tidyobject":
                continue

            props = thing.get("properties")
            if not isinstance(props, dict):
                props = {}
                thing["properties"] = props

            category = props.get("tidy_category")
            if category is None or not str(category).strip():
                category = props.get("category", "object")
            props["tidy_category"] = str(category).strip() or "object"

            # TidyObject already used core Prop-compatible model/physics keys.
            props.setdefault("model_path", BOOK_MODEL)
            props["type"] = "prop"
            props.pop("tidied", None)
            thing["type"] = "prop"

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
            if norm == "tidyobject":
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
