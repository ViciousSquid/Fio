"""Time-driven entity runtime delegated from LogicThread.

Owns logic_timer countdowns and light FadeIn/FadeOut transitions. The
authoritative timer/fade state remains on LogicThread for compatibility with
existing I/O handlers, tests, save/load, and tools.
"""

from __future__ import annotations

from .change_journal import touch


class LogicTiming:
    """Runtime for timers and time-based light transitions."""

    def __init__(self, logic):
        self.logic = logic

    @staticmethod
    def timer_key(thing):
        """Return the stable identity used by a logic_timer countdown."""
        return thing.properties.get("id") or thing.properties.get("name", "")

    def init_logic_timers(self):
        """Arm every authored timer whose start_on property is enabled."""
        logic = self.logic
        if not logic._timer_things:
            return

        for thing in logic._timer_things:
            if not thing.properties.get("start_on", False):
                continue

            try:
                interval = max(
                    0.01,
                    float(thing.properties.get("interval", 1.0)),
                )
            except (TypeError, ValueError):
                interval = 1.0

            thing.properties["timer_enabled"] = True
            logic.timer_states[self.timer_key(thing)] = {
                "remaining": interval,
                "interval": interval,
            }

    def update_logic_timers(self, delta: float):
        """Advance enabled timers and fire their I/O outputs."""
        logic = self.logic
        if not logic._timer_things:
            return

        for thing in logic._timer_things:
            if not thing.properties.get("timer_enabled", False):
                continue

            key = self.timer_key(thing)
            state = logic.timer_states.get(key)
            if state is None:
                try:
                    interval = max(
                        0.01,
                        float(thing.properties.get("interval", 1.0)),
                    )
                except (TypeError, ValueError):
                    interval = 1.0
                state = {
                    "remaining": interval,
                    "interval": interval,
                }
                logic.timer_states[key] = state

            state["remaining"] -= delta
            if state["remaining"] > 0:
                continue

            if logic.io_manager:
                logic.io_manager.fire_output(thing, "OnTimer")

            if thing.properties.get("one_shot", False):
                thing.properties["timer_enabled"] = False
                logic.timer_states.pop(key, None)
                if logic.io_manager:
                    logic.io_manager.fire_output(thing, "OnFinished")
            else:
                state["remaining"] = state["interval"]

    def update_light_fades(self, delta: float):
        """Advance active light fade transitions."""
        logic = self.logic
        if not logic.light_fade_states:
            return

        finished = []
        for key, state in logic.light_fade_states.items():
            entity = state["entity"]
            state["elapsed"] += delta
            duration = state["duration"]
            t = (
                1.0
                if duration <= 0.0
                else min(1.0, state["elapsed"] / duration)
            )

            entity.properties["intensity"] = (
                state["from"] + (state["to"] - state["from"]) * t
            )
            touch(entity)

            if t >= 1.0:
                entity.properties["intensity"] = state["to"]
                if state["end_off"]:
                    entity.properties["state"] = "off"
                    if logic.io_manager:
                        logic.io_manager.fire_output(
                            entity, "OnTurnedOff"
                        )
                finished.append(key)

        for key in finished:
            logic.light_fade_states.pop(key, None)
