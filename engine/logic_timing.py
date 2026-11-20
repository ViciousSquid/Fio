"""Time-driven entity runtime delegated from LogicThread.

Owns logic_timer countdowns and light FadeIn/FadeOut transitions.
"""

from __future__ import annotations

from .change_journal import touch


class LogicTiming:
    """Runtime for timers and time-based light transitions."""

    def __init__(self, logic):
        self.logic = logic
        self.timer_states = {}
        self.light_fade_states = {}

    @staticmethod
    def timer_key(thing):
        """Return the stable identity used by a logic_timer countdown."""
        return thing.properties.get("id") or thing.properties.get("name", "")

    def arm_timer(self, thing):
        """Start or restart one authored timer from its full interval."""
        try:
            interval = max(0.01, float(thing.properties.get('interval', 1.0)))
        except (TypeError, ValueError):
            interval = 1.0
        self.timer_states[self.timer_key(thing)] = {
            'remaining': interval,
            'interval': interval,
        }

    def start_light_fade(self, entity, target, duration, end_off):
        """Start one light fade, or apply it immediately when duration is zero."""
        try:
            duration = max(0.0, float(duration))
        except (ValueError, TypeError):
            duration = 1.0
        start = float(entity.properties.get('intensity', 0.0))
        key = id(entity)
        if duration <= 0.0:
            entity.properties['intensity'] = target
            entity.properties['state'] = 'off' if end_off else 'on'
            self.light_fade_states.pop(key, None)
            return
        self.light_fade_states[key] = {
            'entity': entity,
            'from': start,
            'to': target,
            'elapsed': 0.0,
            'duration': duration,
            'end_off': end_off,
        }

    def init_logic_timers(self):
        """Arm every authored timer whose start_on property is enabled."""
        logic = self.logic
        if not logic.world_runtime.timer_things:
            return

        for thing in logic.world_runtime.timer_things:
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
            self.timer_states[self.timer_key(thing)] = {
                "remaining": interval,
                "interval": interval,
            }

    def update_logic_timers(self, delta: float):
        """Advance enabled timers and fire their I/O outputs."""
        logic = self.logic
        if not logic.world_runtime.timer_things:
            return

        for thing in logic.world_runtime.timer_things:
            if not thing.properties.get("timer_enabled", False):
                continue

            key = self.timer_key(thing)
            state = self.timer_states.get(key)
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
                self.timer_states[key] = state

            state["remaining"] -= delta
            if state["remaining"] > 0:
                continue

            if logic.io_manager:
                logic.io_manager.fire_output(thing, "OnTimer")

            if thing.properties.get("one_shot", False):
                thing.properties["timer_enabled"] = False
                self.timer_states.pop(key, None)
                if logic.io_manager:
                    logic.io_manager.fire_output(thing, "OnFinished")
            else:
                state["remaining"] = state["interval"]

    def update_light_fades(self, delta: float):
        """Advance active light fade transitions."""
        logic = self.logic
        if not self.light_fade_states:
            return

        # A finished fade's OnTurnedOff can start another fade at once (a
        # zero-delay connection runs in place): another light's, which would
        # grow the dict under the loop, or this light's own (A.OnTurnedOff ->
        # A.FadeIn, a pulsing light), which replaces its entry. So the loop
        # runs over a copy, the outputs fire after it, and a finished fade is
        # removed only while its entry is still the state that finished.
        finished = []
        for key, state in list(self.light_fade_states.items()):
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
                finished.append((key, state))

        for key, state in finished:
            if self.light_fade_states.get(key) is state:
                del self.light_fade_states[key]
        if logic.io_manager:
            for _key, state in finished:
                if state["end_off"]:
                    logic.io_manager.fire_output(state["entity"], "OnTurnedOff")
