"""Generic Prop gameplay must not depend on the Tidy plugin."""
from types import SimpleNamespace

from engine.prop_runtime import PropSession
from plugins.entitybase import Prop


class Grid:
    def raycast_down(self, x, z, from_y):
        return 0.0


class IO:
    def __init__(self):
        self.events = []

    def fire_output(self, entity, name):
        self.events.append((entity, name))


def test_core_prop_pickup_drop_and_rest_without_plugins():
    prop = Prop(pos=[0, 40, 30], properties={'physics_enabled': True,
                                             'no_collision': False,
                                             'drop_angular_velocity': [10, 0, 0]})
    io = IO()
    logic = SimpleNamespace(
        things=[prop], io_manager=io, _spatial_grid=Grid(),
        player=SimpleNamespace(pos=[0, 0, 0], angle=0.0, pitch=0.0,
                               camera_height=40.0), current_hud_message='',
    )
    session = PropSession(logic)
    session.start()

    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert io.events[-1][1] == 'OnPickedUp'

    session.tick(1 / 60, use_pressed=True)
    assert session.held is None
    assert 'OnDropped' in [event for _, event in io.events]
    for _ in range(30):
        session.tick(1 / 60, use_pressed=False)
    assert prop.pos[1] == 0.0
    assert ('OnRest' in [event for _, event in io.events])
    assert prop.properties['rotation'][0] > 0
