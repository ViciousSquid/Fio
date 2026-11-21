"""A light fade's OnTurnedOff may start another fade, in place.

A zero-delay connection runs its input inside ``fire_output``, so a fade that
finishes can start a fade on another light while ``update_light_fades`` is
walking its table (which raised "dictionary changed size during iteration"
and ended Play), or restart its own (A.OnTurnedOff -> A.FadeIn, a pulsing
light), whose new entry the old loop then deleted.
"""

from engine.logic_timing import LogicTiming

TICK = 1.0 / 60.0


class _Light:
    def __init__(self, name, intensity=1.0):
        self.properties = {"intensity": intensity, "name": name}


class _Logic:
    io_manager = None


class _IO:
    """fire_output that runs a zero-delay reaction in place, as IOManager does."""

    def __init__(self, reaction):
        self.reaction = reaction
        self.fired = []

    def fire_output(self, entity, output):
        self.fired.append((entity.properties["name"], output))
        self.reaction(entity, output)


def _timing(reaction):
    logic = _Logic()
    timing = LogicTiming(logic)
    logic.io_manager = _IO(reaction)
    return timing, logic.io_manager


def test_a_finished_fade_can_start_another_lights_fade():
    a, b = _Light("a"), _Light("b", intensity=0.0)
    timing, io = _timing(
        lambda entity, output: entity is a and timing.start_light_fade(b, 1.0, 0.5, False))
    timing.start_light_fade(a, 0.0, 0.1, True)

    for _ in range(12):                  # must not raise
        timing.update_light_fades(TICK)

    assert io.fired == [("a", "OnTurnedOff")]
    assert a.properties["state"] == "off" and a.properties["intensity"] == 0.0
    assert id(b) in timing.light_fade_states, "the fade a's output started was lost"
    for _ in range(40):
        timing.update_light_fades(TICK)
    assert b.properties["intensity"] == 1.0
    assert timing.light_fade_states == {}


def test_a_light_that_restarts_its_own_fade_keeps_pulsing():
    a = _Light("a")
    timing, io = _timing(
        lambda entity, output: timing.start_light_fade(entity, 1.0, 0.1, False))
    timing.start_light_fade(a, 0.0, 0.1, True)

    for _ in range(8):
        timing.update_light_fades(TICK)

    assert io.fired == [("a", "OnTurnedOff")]
    state = timing.light_fade_states.get(id(a))
    assert state is not None, "the self-loop's new fade was deleted with the old one"
    assert state["to"] == 1.0 and not state["end_off"]


def test_outputs_fire_after_every_fade_has_advanced():
    """Two lights finishing on the same tick both land before either output
    runs, so a reaction sees the whole tick's state."""
    a, b = _Light("a"), _Light("b")
    seen = []
    timing, io = _timing(
        lambda entity, output: seen.append((a.properties.get("state"),
                                            b.properties.get("state"))))
    timing.start_light_fade(a, 0.0, 0.05, True)
    timing.start_light_fade(b, 0.0, 0.05, True)
    for _ in range(4):
        timing.update_light_fades(TICK)
    assert seen == [("off", "off"), ("off", "off")]
    assert timing.light_fade_states == {}
