"""A plugin that raises on every lifecycle callback it is given.

Its per-frame ``on_tick`` is the important one: a plugin that throws 60 times a
second must not take the play session with it, and must not stop the plugins
dispatched after it.
"""

from plugins.api import FioPlugin


class TickBombPlugin(FioPlugin):
    name = "raises_on_tick"
    version = "1.0.0"
    description = "Raises in on_play_start / on_tick / on_play_stop on purpose."

    def __init__(self):
        self.attempts = {"start": 0, "tick": 0, "stop": 0}

    def register(self, api):
        pass

    def on_play_start(self, logic):
        self.attempts["start"] += 1
        raise RuntimeError("on_play_start failed on purpose")

    def on_tick(self, logic, ctx):
        self.attempts["tick"] += 1
        raise RuntimeError("on_tick failed on purpose")

    def on_play_stop(self, logic):
        self.attempts["stop"] += 1
        raise RuntimeError("on_play_stop failed on purpose")


PLUGIN = TickBombPlugin()
