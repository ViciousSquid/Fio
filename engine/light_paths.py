"""Lights that travel along path-node chains during Play.

A Light whose ``path_target`` names a PathNode glides from where it was
placed to that node, then on along each node's ``next_node`` -- a chain
that leads back to an earlier node loops for ever -- at ``path_speed``
world units a second times each node's own Speed, pausing for each node's
Wait Time. It starts when Play does (``path_start_on``) or when told to
(I/O: StartPath, StopPath, TogglePath), and fires OnPathNodeReached (the
node's name) at every node and OnPathEnd at the end of an open chain, where
it stays.

The same path nodes and the same rules as a mover following a path
(engine.logic_movers), except that a light glides at constant speed rather
than easing in and out of every node: a moving light that stutters at each
node reads as a fault. Parenting a light to a mover (``parent_mover``) still
works and takes precedence: a light parented to a mover ignores its path.

Only the light's position moves, and it is put back where it was placed
when Play stops.
"""

from __future__ import annotations

import math

#: What a Light's path properties default to (editor.things.Light).
LIGHT_PATH_DEFAULTS = {
    #: The PathNode the light travels to first ('' -- it stays put).
    'path_target': '',
    #: World units a second, times each node's Speed.
    'path_speed': 64.0,
    #: Travelling from the start of Play; otherwise it waits for StartPath.
    'path_start_on': True,
}

MAX_PATH_SPEED = 100000.0


def path_speed(props):
    try:
        speed = float(props.get('path_speed', LIGHT_PATH_DEFAULTS['path_speed']))
    except (TypeError, ValueError):
        speed = LIGHT_PATH_DEFAULTS['path_speed']
    if not math.isfinite(speed):
        speed = LIGHT_PATH_DEFAULTS['path_speed']
    return max(0.0, min(MAX_PATH_SPEED, speed))


def follows_a_path(thing):
    """Whether *thing* (a Light) travels a path in Play: it names a start
    node and is not parented to a mover."""
    props = getattr(thing, 'properties', None) or {}
    return bool(str(props.get('path_target') or '').strip()) and not props.get('parent_mover')


class LightPaths:
    """Moves path-following Lights each Play tick (LogicThread owns one)."""

    def __init__(self, logic, *, light_type=None):
        self.logic = logic
        self.light_type = light_type
        #: id(light) -> its journey (see start).
        self.journeys = {}

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Play started: every path-following Light sets off (or waits)."""
        # A Play restarted without a Stop: back where they were placed first,
        # or the next Stop would leave each where this restart found it.
        self.stop()
        Light = self.light_type
        if Light is None:
            return
        for thing in self.logic.editor_state.things:
            if not isinstance(thing, Light) or not follows_a_path(thing):
                continue
            props = thing.properties
            first = str(props.get('path_target')).strip()
            if self._node(first) is None:
                print(f"[Light] Warning: path node '{first}' not found for light "
                      f"'{props.get('name', '')}'")
                continue
            self.journeys[id(thing)] = {
                'light': thing,
                'home': [float(v) for v in thing.pos[:3]],
                'origin': [float(v) for v in thing.pos[:3]],
                'node': first,
                'travelled': 0.0,
                'waiting': 0.0,
                'running': bool(props.get('path_start_on', True)),
                'ended': False,
            }

    def stop(self):
        """Play stopped: every light back where it was placed."""
        for journey in self.journeys.values():
            journey['light'].pos = list(journey['home'])
        self.journeys = {}

    # -- saving -------------------------------------------------------------

    #: A journey's fields a save carries (its light and home are the live
    #: session's own: a load overlays the same objects).
    SAVED = ('origin', 'node', 'travelled', 'waiting', 'running', 'ended')

    def snapshot(self):
        """Every journey's progress, by its light's id (engine.savegame)."""
        out = {}
        for journey in self.journeys.values():
            lid = str(journey['light'].properties.get('id') or '')
            if lid:
                out[lid] = {key: (list(journey[key]) if key == 'origin' else journey[key])
                            for key in self.SAVED}
        return out

    def restore(self, saved):
        """Carry on each journey from where :meth:`snapshot` found it, so a
        loaded light goes on from its saved place, not from where it was."""
        if not isinstance(saved, dict):
            return
        for journey in self.journeys.values():
            entry = saved.get(str(journey['light'].properties.get('id') or ''))
            if not isinstance(entry, dict):
                continue
            try:
                origin = [float(v) for v in entry['origin']][:3]
                if len(origin) != 3:
                    continue
                journey.update(origin=origin, node=str(entry['node']),
                               travelled=max(0.0, float(entry['travelled'])),
                               waiting=max(0.0, float(entry['waiting'])),
                               running=bool(entry['running']),
                               ended=bool(entry['ended']))
            except (KeyError, TypeError, ValueError):
                continue

    # -- I/O ----------------------------------------------------------------

    def start_path(self, light):
        journey = self.journeys.get(id(light))
        if journey is None or journey['ended']:
            return False
        journey['running'] = True
        return True

    def stop_path(self, light):
        """Pause where it is; StartPath carries on from there."""
        journey = self.journeys.get(id(light))
        if journey is None:
            return False
        journey['running'] = False
        return True

    def toggle_path(self, light):
        journey = self.journeys.get(id(light))
        if journey is None:
            return False
        return self.stop_path(light) if journey['running'] else self.start_path(light)

    # -- the tick -----------------------------------------------------------

    def _node(self, name):
        world = getattr(self.logic, 'world_runtime', None)
        return world.find_path_node_by_name(name) if world is not None else None

    def _fire(self, light, output, **kwargs):
        io = getattr(self.logic, 'io_manager', None)
        if io is not None:
            io.fire_output(light, output, **kwargs)

    def update(self, delta):
        if not self.journeys:
            return
        for journey in list(self.journeys.values()):
            if journey['running'] and not journey['ended']:
                self._advance(journey, max(0.0, float(delta)))

    def _advance(self, journey, delta):
        light = journey['light']
        # A long tick may pass several short segments: spend it all.
        for _ in range(64):
            if delta <= 0.0 or journey['ended']:
                return
            if journey['waiting'] > 0.0:
                spent = min(delta, journey['waiting'])
                journey['waiting'] -= spent
                delta -= spent
                if journey['waiting'] > 0.0:
                    return
                if not self._next(journey):
                    return
                continue
            node = self._node(journey['node'])
            if node is None:
                journey['ended'] = True
                return
            origin = journey['origin']
            target = [float(v) for v in node.pos[:3]]
            length = math.dist(origin, target)
            speed = path_speed(light.properties) * float(node.get_speed())
            standing = length < 1e-6
            if standing:
                reached = True
            elif speed <= 0.0:
                return
            else:
                remaining = length - journey['travelled']
                step = speed * delta
                if step < remaining:
                    journey['travelled'] += step
                    f = journey['travelled'] / length
                    light.pos = [o + (t - o) * f for o, t in zip(origin, target)]
                    return
                delta -= remaining / speed
                reached = True
            if reached:
                light.pos = list(target)
                self._fire(light, 'OnPathNodeReached', value=journey['node'])
                wait = float(node.get_wait_time())
                if wait > 0.0:
                    journey['waiting'] = wait
                elif not self._next(journey):
                    return
                if not journey['running']:
                    return          # stopped by the arrival's output: stay here
                if standing:
                    # A node where the light already was took none of the
                    # tick. One such arrival a tick, as a mover's path takes:
                    # a path that loops through coincident nodes would
                    # otherwise arrive, and fire, on every pass of this loop.
                    return

    def _next(self, journey):
        """On to the current node's next node; False at the end of the path."""
        node = self._node(journey['node'])
        following = node.get_next_node_name() if node is not None else ''
        if not following or self._node(following) is None:
            journey['ended'] = True
            journey['running'] = False
            self._fire(journey['light'], 'OnPathEnd')
            return False
        journey['origin'] = [float(v) for v in journey['light'].pos[:3]]
        journey['node'] = following
        journey['travelled'] = 0.0
        return True
