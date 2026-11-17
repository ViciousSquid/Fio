"""A physics for tests of the physics contract and its host.

It shares nothing with Soma, the built-in physics, -- no NumPy rows, no
solver -- and implements the ``engine.physics.Physics`` protocol directly,
which is the point: a replacement physics needs nothing from Fio's own.
"""

from engine.physics import SV_GRAVITY


class StubBody:
    def __init__(self, entity, brush):
        self.entity = entity
        self.velocity = [0.0, 0.0, 0.0]
        self.mass = float(entity.properties.get('mass', 1.0))
        size = brush['size']
        self.half_extents = (size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
        self.awake = False
        self.kinematic = False
        self.rest_callback = None
        self.rest_listeners = []
        self.friction = float(entity.properties.get('friction', 0.55))
        self.grounded = False
        self.resting_on = None

    @property
    def position(self):
        return list(self.entity.pos)


class StubPhysics:
    """The smallest conforming physics: every member, no simulation beyond
    moving an awake body by its velocity."""

    def __init__(self, spatial_grid):
        self.spatial_grid = spatial_grid
        self.bodies = {}
        self.steps = []
        self.player_crushed = False
        self.events_wanted = False
        self.reset_controls()

    # -- lifecycle
    def rebuild(self, brushes):
        self.bodies = {id(b['_physics_entity']): StubBody(b['_physics_entity'], b)
                       for b in brushes if b.get('_physics_body')}

    def clear(self):
        self.bodies = {}

    def reset_controls(self):
        self.gravity = -SV_GRAVITY
        self.time_scale = 1.0
        self.friction_scale = 1.0
        self.damping_scale = 1.0
        self.sleep_enabled = True
        self.wake_all()

    # -- the tick
    def step(self, delta, player=None):
        self.steps.append(delta)
        for body in self.bodies.values():
            if body.awake and not body.kinematic:
                # Assigned, never written in place: that is what journals it.
                body.entity.pos = [p + v * delta * self.time_scale
                                   for p, v in zip(body.entity.pos, body.velocity)]

    # -- bodies
    def get_body(self, entity):
        return self.bodies.get(id(entity))

    def wake(self, entity, velocity=None):
        body = self.get_body(entity)
        if body is not None:
            body.awake = True
            if velocity is not None:
                body.velocity = [float(v) for v in velocity]

    def wake_all(self):
        for body in getattr(self, 'bodies', {}).values():
            body.awake = not body.kinematic

    def set_kinematic(self, entity, value):
        body = self.get_body(entity)
        if body is not None:
            body.kinematic = bool(value)

    def sync_entity_position(self, entity, wake=False):
        body = self.get_body(entity)
        if body is not None:
            body.velocity = [0.0, 0.0, 0.0]
            body.awake = bool(wake)

    def set_rest_callback(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.rest_callback = callback

    def entities_that_changed_cell(self):
        return ()

    # -- bodies that come and go
    def add_body(self, brush):
        body = StubBody(brush['_physics_entity'], brush)
        self.bodies[id(body.entity)] = body
        return body

    def remove_body(self, entity):
        return self.bodies.pop(id(entity), None) is not None

    # -- pushing
    def impulse(self, entity, impulse):
        body = self.get_body(entity)
        if body is None:
            return False
        body.velocity = [v + j / body.mass for v, j in zip(body.velocity, impulse)]
        body.awake = True
        return True

    def impulse_radial(self, centre, radius, strength):
        pushed = self.bodies_near(centre, radius)
        for entity in pushed:
            away = [p - c for p, c in zip(entity.pos, centre)]
            self.impulse(entity, [strength * a / radius for a in away])
        return pushed

    # -- being told
    def on_rest(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.rest_listeners.append(callback)

    def off_rest(self, entity, callback):
        body = self.get_body(entity)
        if body is not None and callback in body.rest_listeners:
            body.rest_listeners.remove(callback)

    def drain_events(self):
        return []

    # -- asking
    def bodies_in_box(self, lo, hi):
        return [body.entity for body in self.bodies.values()
                if all(l <= p <= h for l, p, h in zip(lo, body.entity.pos, hi))]

    def bodies_near(self, point, radius):
        return [body.entity for body in self.bodies.values()
                if sum((p - c) ** 2 for p, c in zip(body.entity.pos, point)) <= radius ** 2]

    def raycast(self, origin, direction, reach):
        return None
