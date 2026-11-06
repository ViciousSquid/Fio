"""Walkable surfaces and conditional platform transfers over live world geometry."""
import math

from .navigation import MoverTraversal, distance, ground_positions


class NavigationWorld:
    def __init__(self, logic, hull=(25, 50, 25), step_height=18):
        self.logic, self.hull, self.step_height = logic, hull, step_height

    def brushes(self, overrides=None, omit=(), static=False):
        # Imported here: engine.player needs PyGLM, and plugin discovery must
        # stay importable in the player process without it.
        from engine.player import _blocks_player
        logic = self.logic
        collision = getattr(logic, 'collision_runtime', None)
        moving = getattr(logic, 'mover_runtime', None)
        sources = [getattr(collision, '_collision_brushes_cache', logic.editor_state.brushes),
                   getattr(moving, '_mover_brush_list', ()), getattr(moving, '_door_brush_list', ())]
        found, result = set(), []
        for source in sources:
            for brush in source:
                key = id(brush)
                if key in found or key in omit or not _blocks_player(brush):
                    continue
                found.add(key)
                if static and (brush.get('is_mover') or brush.get('is_door')):
                    continue
                if overrides and key in overrides:
                    brush = dict(brush, pos=overrides[key])
                result.append(brush)
        return result

    def clear(self, start, end, brushes=None):
        brushes = self.brushes() if brushes is None else brushes
        for brush in brushes:
            lo, hi = 0.0, 1.0
            for axis in range(3):
                half = brush['size'][axis]/2 + self.hull[axis] - (1 if axis == 1 else 0)
                center = brush['pos'][axis]
                origin = start[axis] + (self.hull[1] if axis == 1 else 0)
                delta = end[axis] - start[axis]
                if abs(delta) < 1e-9:
                    if origin < center-half or origin > center+half:
                        break
                else:
                    a, b = (center-half-origin)/delta, (center+half-origin)/delta
                    lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
                    if lo > hi:
                        break
            else:
                return False
        return True

    def surfaces(self, marker, brushes=None, footprint=False):
        brushes = self.brushes() if brushes is None else brushes
        terrain = getattr(getattr(self.logic, 'world_runtime', None), 'terrain', None)
        # Match collision support overlap exactly. A narrower footprint leaves
        # an artificial unsupported strip when stepping off a raised platform.
        margin = (self.hull[0], self.hull[2]) if footprint else (0, 0)
        return ground_positions(marker, brushes, terrain,
                                lambda a, b: self.clear(a, b, brushes), margin)

    def project(self, marker, reference):
        points = self.surfaces(marker)
        return min(points, key=lambda p: abs(p[1]-reference[1])) if points else None

    def walkable(self, start, end, brushes=None):
        """Body clearance plus supported steps; a clear line over a pit is not a walk."""
        brushes = self.brushes() if brushes is None else brushes
        steps = max(1, math.ceil(math.hypot(end[0]-start[0], end[2]-start[2])/8))
        # Bound accidental searches across enormous worlds.
        if steps > 4096:
            return False
        previous = tuple(start)
        if not any(abs(p[1]-start[1]) <= 2 for p in self.surfaces(start, brushes, True)):
            return False
        for index in range(1, steps+1):
            t = index/steps
            sample = (start[0]+(end[0]-start[0])*t, previous[1], start[2]+(end[2]-start[2])*t)
            candidates = [p for p in self.surfaces(sample, brushes, True)
                          if abs(p[1]-previous[1]) <= self.step_height + 0.01]
            if not candidates:
                return False
            point = min(candidates, key=lambda p: abs(p[1]-previous[1]))
            height = max(previous[1], point[1])
            if not self.clear((previous[0], height, previous[2]), (point[0], height, point[2]), brushes):
                return False
            previous = point
        return abs(previous[1]-end[1]) <= self.step_height + 0.01

    def movers(self):
        runtime = getattr(self.logic, 'mover_runtime', None)
        return (list(runtime.movers) if runtime is not None else
                [(i, b) for i, b in enumerate(self.logic.editor_state.brushes) if b.get('is_mover')])

    def _path_node(self, name):
        return next((t for t in self.logic.editor_state.things
                     if t.properties.get('type') == 'path_node' and t.name == name), None)

    def stops(self, index, brush, authored=False):
        """Ordered future centres, including reversals; never simulate the live brush."""
        if brush.get('hidden') or brush.get('disabled') or float(brush.get('speed', 64)) <= 0:
            return []
        if not authored and not brush.get('start_on', False):
            return []
        current = tuple(brush['pos'])
        path = brush.get('path_target')
        runtime = getattr(self.logic, 'mover_runtime', None)
        if path:
            state = getattr(runtime, 'mover_path_states', {}).get(index)
            if state is None and not authored:
                return []
            name = path if authored else state['current_node']
            points, seen = [current], set()
            while name and len(points) < 33:
                node = self._path_node(name)
                if node is None:
                    break
                points.append(tuple(node.pos))
                if name in seen:
                    break
                seen.add(name)
                name = node.get_next_node_name()
            return points
        if brush.get('move_once'):
            return []  # Do not invent a return trip for one-shot scripted motion.
        origin = tuple(brush.get('original_pos', current))
        raw = brush.get('direction', (0, 1, 0))
        length = math.sqrt(sum(float(v)**2 for v in raw))
        reach = max(0, float(brush.get('distance', 128)))
        if length < 1e-9 or reach < 1:
            return []
        end = tuple(origin[i]+float(raw[i])*reach/length for i in range(3))
        if authored:
            return [origin, end, origin]
        state = getattr(runtime, 'mover_states', {}).get(index, {})
        first, second = (end, origin) if state.get('forward', True) else (origin, end)
        return [current, first, second, first, second]

    def transfer(self, start, end, authored=False):
        best = None
        for index, mover in self.movers():
            if mover['size'][0] < self.hull[0]*2+4 or mover['size'][2] < self.hull[2]*2+4:
                continue
            centres = self.stops(index, mover, authored)
            for board_index, board in enumerate(centres[:-1]):
                top = board[1]+mover['size'][1]/2
                if abs(start[1]-top) > self.step_height+2:
                    continue
                if math.hypot(start[0]-board[0], start[2]-board[2]) > 1024:
                    continue
                offset = (max(-mover['size'][0]/2+self.hull[0]+3,
                              min(mover['size'][0]/2-self.hull[0]-3, start[0]-board[0])), 0,
                          max(-mover['size'][2]/2+self.hull[2]+3,
                              min(mover['size'][2]/2-self.hull[2]-3, start[2]-board[2])))
                boarding = (board[0]+offset[0], top, board[2]+offset[2])
                for exit_index in range(board_index+1, len(centres)):
                    exit = centres[exit_index]
                    if distance(board, exit) < 1 or abs(end[1]-exit[1]-mover['size'][1]/2) > self.step_height+2:
                        continue
                    if math.hypot(end[0]-exit[0], end[2]-exit[2]) > 1024:
                        continue
                    departing = (exit[0]+offset[0], exit[1]+mover['size'][1]/2, exit[2]+offset[2])
                    at_board = self.brushes({id(mover): board}, static=False)
                    at_exit = self.brushes({id(mover): exit}, static=False)
                    corridor = self.brushes(omit=(id(mover),))
                    if authored:
                        at_board = [b for b in at_board if not b.get('is_door')]
                        at_exit = [b for b in at_exit if not b.get('is_door')]
                        corridor = [b for b in corridor if not b.get('is_door')]
                    if not self.walkable(start, boarding, at_board) or not self.walkable(departing, end, at_exit):
                        continue
                    legs = centres[board_index:exit_index+1]
                    rider = [(p[0]+offset[0], p[1]+mover['size'][1]/2, p[2]+offset[2]) for p in legs]
                    if not all(self.clear(a, b, corridor) for a, b in zip(rider, rider[1:])):
                        continue
                    action = MoverTraversal(mover, start, end, board, exit, offset, legs)
                    if best is None or action.cost < best.cost:
                        best = action
        return best

    def blocker(self, start, end):
        for brush in self.brushes():
            if not self.clear(start, end, [brush]):
                kind = 'door' if brush.get('is_door') else 'mover' if brush.get('is_mover') else 'geometry'
                return f'Blocked by {kind}: {brush.get("name") or brush.get("id") or "unnamed"}'
        return 'No supported walking route'
