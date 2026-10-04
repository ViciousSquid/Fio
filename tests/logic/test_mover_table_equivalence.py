"""The dense mover table moves every mover and door exactly as the loop did.

:mod:`engine.mover_table` replaced a per-row Python loop with one vectorised
pass. The pass has to reproduce that loop exactly, down to the last bit:
positions, spins, state, which I/O fires and in what order, and how far the
player is carried. The loop itself is kept below, verbatim, as
:class:`ReferenceLogic`, with the state dicts it always had. The real I/O
dispatcher and output graph are shared with the live implementation; only the
input handlers that mutate the retired reference state are registered
separately so the production API is not kept alive just for this oracle. Both
are driven through the same worlds and scripted inputs, and compared after
every tick.

The worlds are built to reach the awkward cases:

* an I/O chain in which a mover finishing starts, stops, reverses or
  repositions movers and doors *later* in the list, which must react on this
  same tick, and *earlier* ones, which must not react until the next;
* a mover whose finishing repositions *itself*;
* path-following movers, with waits, speed changes and the end of the chain;
* doors that cycle, doors stopped and reversed mid-travel, doors opened by I/O
  outside the initial state set;
* spinning movers, zero-distance movers, move-once movers, disabled movers,
  un-normalised directions, and a brush that is both a mover and a door;
* a player riding one of them;
* saved-game state restored in the middle of the run.
"""

import copy
import math
import random
from types import SimpleNamespace

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.io_handlers import register_all_input_handlers   # noqa: E402
from editor.io_system import IOManager, OutputConnection      # noqa: E402
from editor.things import PathNode                             # noqa: E402
from engine.logic_thread import DOOR_DIRECTION_MAP, LogicThread  # noqa: E402
from engine.logic_world import LogicWorld                              # noqa: E402
from engine.logic_movers import LogicMovers                    # noqa: E402
from engine.savegame import _public_state                      # noqa: E402

pytestmark = pytest.mark.qt


class ReferenceLogic:
    """The mover and door loop as it was before the dense table (verbatim)."""

    def __init__(self, brushes, things, io_manager, player):
        self.brushes = brushes
        self.things = things
        self.io_manager = io_manager
        self.player = player
        self.movers = []
        self.doors = []
        self.mover_states = {}
        self.door_states = {}
        self.mover_path_states = {}

    def _find_path_node_by_name(self, name):
        for t in self.things:
            if isinstance(t, PathNode) and t.properties.get('name', '') == name:
                return t
        return None

    def _init_movers(self):
        self.mover_states = {}
        self.mover_path_states = {}
        self.movers = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover'):
                self.movers.append((i, brush))
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])

                # FIX: initialise rotation_yaw if mover rotates
                if brush.get('rotate', False) and 'rotation_yaw' not in brush:
                    brush['rotation_yaw'] = 0.0

                path_target = brush.get('path_target', '')
                if path_target and brush.get('start_on', False):
                    self.mover_path_states[i] = {
                        'current_node': path_target,
                        'lerp_t':       0.0,
                        'origin':       list(brush['pos']),
                        'waiting':      False,
                        'wait_remaining': 0.0,
                    }
                elif not brush.get('move_once', False):
                    self.mover_states[i] = {'progress': 0.0, 'forward': True}
        # PERF: cache the brush-only view of self.movers — was rebuilt via a
        # list comprehension every tick in _tick_play_mode.
        self._mover_brush_list = [b for _, b in self.movers]

    def _reset_movers(self):
        self.movers = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.mover_states = {}
        self._mover_brush_list = []

    def _init_doors(self):
        self.door_states = {}
        self.doors = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_door'):
                # Resolve runtime parameters from editor properties without mutating the source brush
                speed = float(brush.get('door_speed', brush.get('speed', 128.0)))
                distance = float(brush.get('door_distance', brush.get('distance', 128.0)))
                dir_str = brush.get('door_direction', '')
                direction = DOOR_DIRECTION_MAP.get(dir_str, [0, 1, 0])

                if 'door_lip' in brush:
                    lip = float(brush.get('door_lip', 0.0))
                    distance = max(1.0, distance - lip)

                self.doors.append((i, brush))
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                # PERF: DOOR_DIRECTION_MAP entries are already unit vectors,
                # and door direction never changes at runtime, so normalize
                # once here instead of every tick in _update_doors.
                self.door_states[i] = {
                    'progress': 0.0,
                    'state': 'closed',
                    'open_timer': 0.0,
                    'speed': speed,
                    'distance': distance,
                    'direction': direction,
                    # Scalar unit-direction tuple (DOOR_DIRECTION_MAP entries are
                    # already unit vectors). Kept as plain Python floats -- not a
                    # NumPy array -- so the per-tick offset maths below produces
                    # ordinary floats and brush['pos'] stays JSON-serialisable.
                    '_direction_np': (float(direction[0]), float(direction[1]),
                                      float(direction[2])),
                }
        # PERF: cache the brush-only view of self.doors — was rebuilt via a
        # list comprehension every tick in _tick_play_mode.
        self._door_brush_list = [b for _, b in self.doors]

    def _reset_doors(self):
        self.doors = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_door') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.door_states = {}
        self._door_brush_list = []

    def _update_movers(self, delta: float):
        for i, brush in self.movers:
            if brush.get('move_once', False):
                continue
            if not brush.get('start_on', False):
                continue

            if i in self.mover_path_states:
                self._update_mover_path(i, brush, delta)
                continue

            if brush.get('rotate', False):
                speed = brush.get('speed', 45.0)
                current = brush.get('_rot_angle', 0.0)
                new_angle = (current + speed * delta) % 360.0
                brush['_rot_angle'] = new_angle
                brush['rotation_yaw'] = new_angle

            if i not in self.mover_states:
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                self.mover_states[i] = {'progress': 0.0, 'forward': True}
            state = self.mover_states[i]
            speed = brush.get('speed', 64.0)
            distance = brush.get('distance', 128.0)
            # PERF: mover direction is static during play — normalize once
            # (via NumPy, for identical rounding) and cache as a plain scalar
            # tuple so the per-tick offset maths below is pure Python and never
            # rebuilds a small NumPy array each frame.
            direction = state.get('_direction_np')
            if direction is None:
                d = np.array(brush.get('direction', [0, 1, 0]), dtype=float)
                dir_length = np.linalg.norm(d)
                if dir_length > 0:
                    d = d / dir_length
                direction = (float(d[0]), float(d[1]), float(d[2]))
                state['_direction_np'] = direction
            progress_delta = (speed * delta) / distance if distance > 0 else 0
            was_at_end = state['progress'] >= 1.0
            was_at_start = state['progress'] <= 0.0
            if state['forward']:
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['forward'] = False
                    if not was_at_end and self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyOpen')
            else:
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['forward'] = True
                    if not was_at_start and self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
            t = state['progress']
            eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
            # PERF: scalar offset — bit-identical to the old NumPy expression
            # (original + direction*distance*eased, which associates as
            # (direction*distance)*eased), with no per-tick array allocation.
            original = brush['original_pos']
            cur = brush['pos']
            nx = original[0] + (direction[0] * distance) * eased
            ny = original[1] + (direction[1] * distance) * eased
            nz = original[2] + (direction[2] * distance) * eased
            brush['pos'] = [nx, ny, nz]
            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(nx - cur[0], ny - cur[1], nz - cur[2])

    def _update_mover_path(self, idx: int, brush: dict, delta: float):
        state = self.mover_path_states[idx]
        node_name = state['current_node']
        if not node_name:
            return

        node = self._find_path_node_by_name(node_name)
        if node is None:
            debug_log("IO", f"Mover path: node '{node_name}' not found — stopping")
            self.mover_path_states.pop(idx, None)
            return

        if state['waiting']:
            state['wait_remaining'] -= delta
            if state['wait_remaining'] <= 0.0:
                state['waiting'] = False
                next_name = node.get_next_node_name()
                if next_name:
                    state['origin'] = list(brush['pos'])
                    state['current_node'] = next_name
                    state['lerp_t'] = 0.0
                else:
                    brush['start_on'] = False
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
                    self.mover_path_states.pop(idx, None)
            return

        origin = np.array(state['origin'], dtype=float)
        target = np.array(node.pos, dtype=float)
        segment_vec = target - origin
        segment_len = np.linalg.norm(segment_vec)

        if segment_len < 1.0:
            state['lerp_t'] = 1.0
        else:
            speed = brush.get('speed', 64.0) * node.get_speed()
            state['lerp_t'] += (speed * delta) / segment_len

        if state['lerp_t'] >= 1.0:
            state['lerp_t'] = 1.0
            new_pos = target
            move_delta = new_pos - np.array(brush['pos'])
            brush['pos'] = new_pos.tolist()

            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(float(move_delta[0]), float(move_delta[1]), float(move_delta[2]))

            if self.io_manager:
                # Per-node arrival event (fires at every PathNode in the chain),
                # plus OnFullyOpen for backward compatibility with existing maps.
                self.io_manager.fire_output(brush, 'OnPathNodeReached', value=node_name)
                self.io_manager.fire_output(brush, 'OnFullyOpen')

            wait_time = node.get_wait_time()
            if wait_time > 0.0:
                state['waiting'] = True
                state['wait_remaining'] = wait_time
            else:
                next_name = node.get_next_node_name()
                if next_name:
                    state['origin'] = list(brush['pos'])
                    state['current_node'] = next_name
                    state['lerp_t'] = 0.0
                else:
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
                    self.mover_path_states.pop(idx, None)
        else:
            t = state['lerp_t']
            eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
            new_pos = origin + segment_vec * eased
            move_delta = new_pos - np.array(brush['pos'])
            brush['pos'] = new_pos.tolist()

            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(float(move_delta[0]), float(move_delta[1]), float(move_delta[2]))

    def _update_doors(self, delta: float):
        for i, brush in self.doors:
            if i not in self.door_states:
                continue
            state = self.door_states[i]
            # PERF: fully-closed, idle doors cost nothing until triggered.
            if state['state'] == 'closed' and state['progress'] == 0.0:
                continue
            speed = state.get('speed', 128.0)
            distance = state.get('distance', 128.0)
            open_time = brush.get('open_time', 3.0)
            # PERF: direction is precomputed (already unit-length) in
            # _init_doors — no need to renormalize every tick. Cached as a
            # scalar tuple so the offset maths below allocates no NumPy arrays.
            direction = state.get('_direction_np')
            if direction is None:
                d = np.array(state.get('direction', [0, 1, 0]), dtype=float)
                dir_length = np.linalg.norm(d)
                if dir_length > 0:
                    d = d / dir_length
                direction = (float(d[0]), float(d[1]), float(d[2]))
                state['_direction_np'] = direction
            progress_delta = (speed * delta) / distance if distance > 0 else 0
            if state['state'] == 'opening':
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['state'] = 'open'
                    state['open_timer'] = open_time
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyOpen')
            elif state['state'] == 'open':
                state['open_timer'] -= delta
                if state['open_timer'] <= 0:
                    state['state'] = 'closing'
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnClose')
            elif state['state'] == 'closing':
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['state'] = 'closed'
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
            # PERF: scalar offset — bit-identical to the old NumPy expression
            # (original + direction*distance*progress), no per-tick array alloc.
            original = brush['original_pos']
            cur = brush['pos']
            progress = state['progress']
            nx = original[0] + (direction[0] * distance) * progress
            ny = original[1] + (direction[1] * distance) * progress
            nz = original[2] + (direction[2] * distance) * progress
            brush['pos'] = [nx, ny, nz]
            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(nx - cur[0], ny - cur[1], nz - cur[2])


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------

MOVER_INPUTS = [("Open", ""), ("Close", ""), ("Toggle", ""), ("SetPosition", "0.25"),
                ("SetPosition", "1"), ("Enable", ""), ("Disable", ""), ("Stop", ""),
                ("Reverse", ""), ("SetSpeed", "150"), ("SetSpeed", "0"),
                ("FollowPath", "P0"), ("FollowPath", "P3"), ("StopPath", "")]
DOOR_INPUTS = [("Open", ""), ("Close", ""), ("Toggle", ""), ("Stop", ""),
               ("Reverse", "")]
#: Only completion outputs are wired: they are fired by the tick, never by an
#: input handler, so no wiring can recurse.
WIRED_OUTPUTS = ("OnFullyOpen", "OnFullyClosed", "OnPathNodeReached")


def _node(name, pos, next_node="", wait=0.0, speed=1.0):
    node = PathNode(pos=list(pos), properties={"name": name})
    node.properties.update({"next_node": next_node, "wait_time": wait,
                            "speed": speed, "id": "node-" + name})
    return node


def _world(seed, count=70):
    rng = random.Random(seed)
    things = [_node("P0", (0, 300, 0), "P1", speed=2.0),
              _node("P1", (400, 300, 0), "P2", wait=0.3),
              _node("P2", (400, 300, 700), ""),
              _node("P3", (-500, 0, 0), "P4", wait=0.1, speed=0.5),
              _node("P4", (-500, 0, -400), "P3")]
    brushes = []
    for k in range(count):
        roll = rng.random()
        brush = {"id": "b%d" % k, "name": "b%d" % k,
                 "pos": [rng.uniform(-900, 900), rng.uniform(0, 300),
                         rng.uniform(-900, 900)],
                 "size": [64.0, 64.0, 64.0]}
        if roll < 0.15:
            pass                                          # static, in between
        elif roll < 0.6:
            brush.update(is_mover=True,
                         start_on=rng.random() < 0.8,
                         speed=rng.choice([64, 100.0, 333.3, 0, 1e-3]),
                         distance=rng.choice([128, 37.5, 900, 0, -5]),
                         direction=rng.choice([[0, 1, 0], [3, 0, 4], [0, 0, 0],
                                               [rng.uniform(-1, 1) for _ in range(3)]]))
            if rng.random() < 0.25:
                brush["rotate"] = True
            if rng.random() < 0.05:
                brush["move_once"] = True
            if rng.random() < 0.08:
                brush["path_target"] = rng.choice(["P0", "P3"])
                brush["start_on"] = True
        elif roll < 0.95:
            brush.update(is_door=True,
                         door_speed=rng.choice([128, 64.0, 500.0]),
                         door_distance=rng.choice([128, 96.0, 20.0]),
                         door_direction=rng.choice(list(DOOR_DIRECTION_MAP) + [""]),
                         open_time=rng.choice([0.2, 1, 3.0]))
            if rng.random() < 0.3:
                brush["door_lip"] = rng.choice([4.0, 200.0])
        else:
            brush.update(is_mover=True, is_door=True, start_on=True,
                         speed=80.0, distance=64.0, direction=[1, 0, 0])
        brushes.append(brush)

    movable = [b for b in brushes if b.get("is_mover") or b.get("is_door")]
    for brush in movable:
        for _ in range(rng.choice([0, 0, 1, 2])):
            target = rng.choice(movable)
            inputs = DOOR_INPUTS if target.get("is_door") else MOVER_INPUTS
            name, param = rng.choice(inputs)
            brush.setdefault("_io_connections", []).append(OutputConnection(
                output_name=rng.choice(WIRED_OUTPUTS), target_name=target["name"],
                input_name=name, parameter=param,
                delay=rng.choice([0.0, 0.0, 0.0, 0.05]),
                target_id=target["id"]))
    return brushes, things



# ---------------------------------------------------------------------------
# Reference-side I/O
# ---------------------------------------------------------------------------

def register_reference_input_handlers(io_manager):
    """Register the pre-table mover/door input semantics for ReferenceLogic.

    The live handlers now correctly target LogicThread.mover_runtime. The
    reference implementation deliberately does not expose that retired host
    field, so this test keeps the old loop's input semantics here instead of
    adding a production compatibility shim.
    """
    def brush_index(entity, logic):
        for index, brush in enumerate(logic.brushes):
            if brush is entity:
                return index
        return -1

    def finite_value(param, default):
        if not param:
            return default
        try:
            value = float(param)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def door_open(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0:
            return
        if idx not in logic.door_states:
            if "original_pos" not in entity:
                entity["original_pos"] = list(entity["pos"])
            logic.door_states[idx] = {
                "progress": 0.0,
                "state": "closed",
                "open_timer": 0.0,
            }
        state = logic.door_states[idx]
        if state["state"] in ("closed", "closing", "stopped"):
            state["state"] = "opening"
            logic.io_manager.fire_output(entity, "OnOpen")

    def door_close(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        state = logic.door_states[idx]
        if state["state"] in ("open", "opening", "stopped"):
            state["state"] = "closing"
            logic.io_manager.fire_output(entity, "OnClose")

    def door_stop(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        state = logic.door_states[idx]
        if state["state"] in ("opening", "closing"):
            state["state"] = "stopped"

    def door_reverse(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        state = logic.door_states[idx]
        current = state["state"]
        if current == "opening":
            door_close(entity, param, logic)
        elif current == "closing":
            door_open(entity, param, logic)
        elif current == "stopped":
            if state.get("progress", 0.0) >= 0.5:
                door_close(entity, param, logic)
            else:
                door_open(entity, param, logic)
        else:
            door_toggle(entity, param, logic)

    def door_toggle(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0:
            return
        if idx in logic.door_states:
            state = logic.door_states[idx]
            if state["state"] == "closed":
                door_open(entity, param, logic)
            elif state["state"] == "open":
                door_close(entity, param, logic)
        else:
            door_open(entity, param, logic)

    def door_set_speed(entity, param, logic):
        value = finite_value(param, 128.0)
        if value is not None:
            entity["speed"] = value

    def mover_open(entity, param, logic):
        entity["start_on"] = True
        idx = brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]["forward"] = True

    def mover_close(entity, param, logic):
        entity["start_on"] = True
        idx = brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]["forward"] = False

    def mover_toggle(entity, param, logic):
        entity["start_on"] = not entity.get("start_on", False)

    def mover_set_position(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0:
            return
        value = finite_value(param, None)
        if value is None:
            return
        if idx in logic.mover_states:
            logic.mover_states[idx]["progress"] = max(0.0, min(1.0, value))

    def mover_enable(entity, param, logic):
        entity["start_on"] = True

    def mover_disable(entity, param, logic):
        entity["start_on"] = False

    def mover_set_speed(entity, param, logic):
        value = finite_value(param, 64.0)
        if value is not None:
            entity["speed"] = max(0.0, value)

    def mover_stop(entity, param, logic):
        entity["start_on"] = False

    def mover_reverse(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            state = logic.mover_states[idx]
            state["forward"] = not state.get("forward", True)

    def mover_follow_path(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx < 0:
            return
        target = param or entity.get("path_target", "")
        if not target:
            return
        entity["path_target"] = target
        entity["start_on"] = True
        if idx not in logic.mover_path_states:
            logic.mover_path_states[idx] = {
                "current_node": target,
                "lerp_t": 0.0,
                "origin": list(entity["pos"]),
                "waiting": False,
                "wait_remaining": 0.0,
            }
        logic.mover_states.pop(idx, None)

    def mover_stop_path(entity, param, logic):
        idx = brush_index(entity, logic)
        if idx >= 0:
            logic.mover_path_states.pop(idx, None)

    io_manager.register_input_handler("door", "open", door_open)
    io_manager.register_input_handler("door", "close", door_close)
    io_manager.register_input_handler("door", "toggle", door_toggle)
    io_manager.register_input_handler("door", "stop", door_stop)
    io_manager.register_input_handler("door", "reverse", door_reverse)
    io_manager.register_input_handler("door", "setspeed", door_set_speed)

    io_manager.register_input_handler("mover", "open", mover_open)
    io_manager.register_input_handler("mover", "close", mover_close)
    io_manager.register_input_handler("mover", "toggle", mover_toggle)
    io_manager.register_input_handler("mover", "setposition", mover_set_position)
    io_manager.register_input_handler("mover", "enable", mover_enable)
    io_manager.register_input_handler("mover", "disable", mover_disable)
    io_manager.register_input_handler("mover", "stop", mover_stop)
    io_manager.register_input_handler("mover", "reverse", mover_reverse)
    io_manager.register_input_handler("mover", "setspeed", mover_set_speed)
    io_manager.register_input_handler("mover", "followpath", mover_follow_path)
    io_manager.register_input_handler("mover", "stoppath", mover_stop_path)

# ---------------------------------------------------------------------------
# Running both
# ---------------------------------------------------------------------------

class _Side:
    """One copy of a world, driven by one implementation."""

    def __init__(self, reference, brushes, things, ride_index):
        self.brushes = copy.deepcopy(brushes)
        self.things = copy.deepcopy(things)
        self.events = []
        io = IOManager()
        if reference:
            register_reference_input_handlers(io)
        else:
            register_all_input_handlers(io)
        entities = self.brushes + self.things

        def by_name(name):
            for entity in entities:
                if io._get_entity_name(entity) == name:
                    return entity
            return None

        def by_id(eid):
            for entity in entities:
                if io._get_entity_id(entity) == eid:
                    return entity
            return None

        io.set_entity_finder(by_name)
        io.set_entity_finder_by_id(by_id)
        fire = io.fire_output

        def recording_fire(source, output, value=None, activator_entity=None):
            self.events.append((io._get_entity_name(source), output, value))
            return fire(source, output, value=value, activator_entity=activator_entity)

        io.fire_output = recording_fire
        ground = self.brushes[ride_index] if ride_index is not None else None
        player = SimpleNamespace(pos=glm.vec3(1.0, 2.0, 3.0), ground_object=ground)
        self.reference = reference
        if reference:
            logic = ReferenceLogic(self.brushes, self.things, io, player)
        else:
            logic = LogicThread.__new__(LogicThread)
            logic.editor_state = SimpleNamespace(brushes=self.brushes,
                                                 things=self.things)
            logic.io_manager = io
            logic.player_runtime = SimpleNamespace(player=player)
            logic.world_runtime = LogicWorld(logic, path_node_type=PathNode)
            logic.mover_runtime = LogicMovers(logic)
        io.set_logic_thread(logic)
        self.io = io
        self.logic = logic
        if reference:
            logic._init_movers()
            logic._init_doors()
        else:
            logic.mover_runtime._init_movers()
            logic.mover_runtime._init_doors()

    def tick(self, delta):
        if self.reference:
            self.logic._update_movers(delta)
            self.logic._update_doors(delta)
        else:
            self.logic.mover_runtime._update_movers(delta)
            self.logic.mover_runtime._update_doors(delta)
        self.io.update(delta)

    def poke(self, index, input_name, param):
        brush = self.brushes[index]
        self.io._execute_input(brush["name"], input_name, param, "test",
                               target_id=brush["id"])

    def snapshot(self, states=True):
        logic = self.logic
        moving = {
            "brushes": [{k: b.get(k) for k in ("pos", "_rot_angle", "rotation_yaw",
                                               "start_on", "original_pos", "speed",
                                               "path_target")}
                        for b in self.brushes],
            "player": tuple(logic.player_runtime.player.pos),
        }
        if not states:
            return moving
        mover_states = logic.mover_states if self.reference else logic.mover_runtime.mover_states
        door_states = logic.door_states if self.reference else logic.mover_runtime.door_states
        return dict(moving, **{
            # Sorted by index: the loop's dicts iterate in insertion order (a
            # popped-and-recreated state moves to the end), the views in row
            # order. Only a saved game's JSON key order could see that.
            "movers": {i: _numbers_as_float(s) for i, s in sorted(mover_states.items())},
            "doors": {i: _numbers_as_float(s) for i, s in sorted(door_states.items())},
            "mover_keys": {i: list(s) for i, s in sorted(mover_states.items())},
            "door_keys": {i: list(s) for i, s in sorted(door_states.items())},
            "paths": copy.deepcopy(
                logic.mover_path_states
                if self.reference else logic.mover_runtime.mover_path_states
            ),
        })


def _numbers_as_float(state):
    """A state dict with its int values as floats.

    The loop copied a brush's ``open_time`` into ``open_timer`` as given, so a
    map that wrote ``1`` held the int 1 until the first subtraction; the table
    holds 1.0. Nothing can tell them apart (the timer is only ever decremented
    and compared, and a saved game writes and reads it back as a number).
    """
    return {key: float(value) if type(value) is int else value
            for key, value in state.items()}


def _first_difference(a, b, path=""):
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return "%s: %r (%s) != %r (%s)" % (path, a, type(a).__name__, b, type(b).__name__)
    if isinstance(a, dict):
        if set(a) != set(b):
            return "%s: keys %r != %r" % (path, sorted(map(str, a)), sorted(map(str, b)))
        for key in a:
            found = _first_difference(a[key], b[key], "%s[%r]" % (path, key))
            if found:
                return found
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return "%s: length %d != %d" % (path, len(a), len(b))
        for k, (x, y) in enumerate(zip(a, b)):
            found = _first_difference(x, y, "%s[%d]" % (path, k))
            if found:
                return found
        return None
    if isinstance(a, float) and isinstance(b, float):
        same = (a == b and math.copysign(1, a) == math.copysign(1, b)) or (a != a and b != b)
        return None if same else "%s: %r != %r" % (path, a, b)
    return None if a == b else "%s: %r != %r" % (path, a, b)


def _run(seed, ticks=700, ride=True, script=None, io=True):
    brushes, things = _world(seed)
    movable = [i for i, b in enumerate(brushes) if b.get("is_mover") or b.get("is_door")]
    rng = random.Random(seed * 7 + 1)
    ride_index = rng.choice(movable) if ride and movable else None
    ref = _Side(True, brushes, things, ride_index)
    new = _Side(False, brushes, things, ride_index)
    if not io:
        ref.logic.io_manager = new.logic.io_manager = None
    for tick in range(ticks):
        delta = rng.choice([1 / 60, 1 / 60, 1 / 30, 0.004, 0.25])
        if io and rng.random() < 0.08:
            index = rng.choice(movable)
            inputs = DOOR_INPUTS if brushes[index].get("is_door") else MOVER_INPUTS
            name, param = rng.choice(inputs)
            ref.poke(index, name, param)
            new.poke(index, name, param)
        if script is not None:
            script(tick, ref, new, rng)
        ref.tick(delta)
        new.tick(delta)
        # Positions, spins, the rider and the I/O every tick; the state dicts
        # (slow to read through the views) every third tick and at the end.
        states = tick % 3 == 0 or tick == ticks - 1
        for label, a, b in (("state", ref.snapshot(states), new.snapshot(states)),
                            ("events", ref.events, new.events)):
            # repr tells every float apart, -0.0 from 0.0 included; the walk
            # is only there to say where.
            if repr(a) != repr(b):
                found = _first_difference(a, b, label) or "%s: %r != %r" % (label, a, b)
                raise AssertionError("seed %d, tick %d: %s" % (seed, tick, found))
    return ref, new


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(10))
def test_every_tick_matches_the_loop(seed):
    ref, _ = _run(seed)
    assert ref.events, "the run fired no I/O at all; the wiring is not exercised"


@pytest.mark.parametrize("seed", range(3))
def test_without_io_every_transition_takes_the_vectorised_path(seed):
    """With nothing to fire, no row is a sequence point for an event: every
    mover reversal and every door open, wait and close is committed by the
    vectorised step itself, and has to agree with the loop too."""
    def open_doors(tick, ref, new, rng):
        if tick % 40 == 0:            # no I/O to open them, so open them here
            for side in (ref, new):
                door_states = (
                    side.logic.door_states
                    if side.reference
                    else side.logic.mover_runtime.door_states
                )
                for state in door_states.values():
                    if state["state"] == "closed":
                        state["state"] = "opening"
    _run(seed, ticks=500, script=open_doors, io=False)


def test_saved_state_restored_mid_run_matches():
    """A restore replaces the state dicts wholesale, from JSON-shaped dicts."""
    def restore(tick, ref, new, rng):
        if tick % 97 != 50:
            return
        for side in (ref, new):
            logic = side.logic
            if side.reference:
                door_states = logic.door_states
                mover_states = logic.mover_states
            else:
                door_states = logic.mover_runtime.door_states
                mover_states = logic.mover_runtime.mover_states
            doors = {i: _public_state(s) for i, s in door_states.items()}
            movers = {i: _public_state(s) for i, s in mover_states.items()}
            if side.reference:
                logic.door_states = copy.deepcopy(doors)
                logic.mover_states = copy.deepcopy(movers)
            else:
                logic.mover_runtime.door_states = copy.deepcopy(doors)
                logic.mover_runtime.mover_states = copy.deepcopy(movers)
    _run(101, ticks=600, script=restore)


def test_a_door_opened_outside_play_keeps_its_plain_state():
    """door_open on an index that is no row stores the dict it was given."""
    brushes, things = _world(5)
    new = _Side(False, brushes, things, None)
    stray = {"progress": 0.0, "state": "closed", "open_timer": 0.0}
    new.logic.mover_runtime.door_states[10_000] = stray
    assert new.logic.mover_runtime.door_states[10_000] is stray
    assert 10_000 in new.logic.mover_runtime.door_states
    del new.logic.mover_runtime.door_states[10_000]
    assert 10_000 not in new.logic.mover_runtime.door_states


def test_state_views_read_and_write_like_dicts():
    brushes, things = _world(6)
    new = _Side(False, brushes, things, None)
    index = next(i for i in new.logic.mover_runtime.door_states)
    state = new.logic.mover_runtime.door_states[index]
    assert list(state) == ["progress", "state", "open_timer", "speed",
                           "distance", "direction", "_direction_np"]
    state["state"] = "opening"
    assert new.logic.mover_runtime.door_states[index]["state"] == "opening"
    state["state"] = "wobbling"                       # kept as given
    assert new.logic.mover_runtime.door_states[index]["state"] == "wobbling"
    state["custom"] = [1, 2]
    assert state["custom"] == [1, 2] and "custom" in dict(state)
    del state["speed"]
    assert "speed" not in state and state.get("speed", 128.0) == 128.0
    import json
    json.dumps({str(i): _public_state(s) for i, s in new.logic.mover_runtime.door_states.items()})
    json.dumps({str(i): _public_state(s) for i, s in new.logic.mover_runtime.mover_states.items()})


def test_positions_are_plain_floats():
    """brush['pos'] is serialised with the map; no NumPy scalars in it."""
    _, new = _run(3, ticks=120)
    for brush in new.brushes:
        assert all(type(v) is float or type(v) is int for v in brush["pos"]), brush["pos"]


def test_the_ease_matches_the_scalar_arithmetic_bit_for_bit():
    from engine.mover_table import _eased
    rng = np.random.default_rng(0)
    t = np.concatenate([rng.random(200000), [0.0, 0.5, 1.0, 0.4999999999999999,
                                             np.nextafter(0.5, 1), 1e-300]])
    got = _eased(t)
    want = np.array([4 * x * x * x if x < 0.5 else 1 - pow(-2 * x + 2, 3) / 2
                     for x in t.tolist()])
    assert np.array_equal(got, want)


def test_the_spin_matches_the_scalar_arithmetic_bit_for_bit():
    rng = random.Random(1)
    angles = [rng.uniform(-720, 720) for _ in range(20000)] + [359.9999999, -0.0, 0.0]
    speeds = [rng.choice([45.0, 90, 1e-3, 1234.5]) for _ in angles]
    delta = 1 / 60
    got = np.mod(np.array(angles) + np.array(speeds, dtype=float) * delta, 360.0)
    want = [(a + s * delta) % 360.0 for a, s in zip(angles, speeds)]
    for g, w in zip(got.tolist(), want):
        assert g == w and math.copysign(1, g) == math.copysign(1, w)
