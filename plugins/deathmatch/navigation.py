"""Small authored waypoint graph; no grid bake or external dependencies."""
import heapq
import math


DEFAULT_RADIUS = 36.0


class Waypoint(tuple):
    """A walkable position with the authored node's arrival radius."""
    def __new__(cls, position, radius=DEFAULT_RADIUS):
        point = super().__new__(cls, position)
        try:
            radius = float(radius)
        except (ValueError, TypeError):
            radius = DEFAULT_RADIUS
        point.radius = max(1.0, radius) if math.isfinite(radius) else DEFAULT_RADIUS
        return point


def ground_positions(position, brushes, terrain, clear, margin=(0, 0)):
    """Resolve a marker's X/Z to a body-clear surface near the bot's level.

    The footprint margin matches standing support at step edges. Marker Y
    never chooses a floor; the route's connectivity chooses the storey.
    """
    x, _, z = position
    heights = set()
    for brush in brushes:
        center, size = brush['pos'], brush['size']
        if not (abs(x - center[0]) < size[0] / 2 + margin[0] + 1e-6
                and abs(z - center[2]) < size[2] / 2 + margin[1] + 1e-6):
            continue
        if brush.get('_collision_mode') == 'mesh':
            for vertices, normal in brush.get('_mesh_triangles', ()):
                if normal[1] < 0.7:
                    continue
                a, b, c = vertices
                den = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
                if abs(den) < 1e-9:
                    continue
                u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / den
                v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / den
                if u >= -1e-6 and v >= -1e-6 and u + v <= 1 + 1e-6:
                    heights.add(u * a[1] + v * b[1] + (1 - u - v) * c[1])
        else:
            heights.add(center[1] + size[1] / 2)
    if terrain is not None:
        height = terrain.get_height_at_safe(x, z)
        if height is not None:
            heights.add(height)
    return [(x, y, z) for y in sorted(heights) if clear((x, y, z), (x, y, z))]


def ground_position(position, reference, brushes, terrain, clear):
    points = ground_positions(position, brushes, terrain, clear)
    return min(points, key=lambda p: abs(p[1] - reference[1])) if points else None


class MoverTraversal(Waypoint):
    """A conditional route action: approach, board, ride, then disembark."""
    def __new__(cls, mover, landing, target, board, exit, offset, centres):
        point = super().__new__(cls, (board[0] + offset[0], board[1] + mover['size'][1]/2,
                                     board[2] + offset[2]), 8)
        point.mover, point.landing, point.target = mover, tuple(landing), target
        point.board, point.exit, point.offset = tuple(board), tuple(exit), tuple(offset)
        point.centres = tuple(tuple(p) for p in centres)
        point.stage = 'approach'
        point.cost = distance(landing, point) + sum(distance(a, b) for a, b in zip(centres, centres[1:]))
        point.cost += distance((exit[0]+offset[0], exit[1]+mover['size'][1]/2, exit[2]+offset[2]), target) + 250
        return point


def distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


class Navigation:
    def __init__(self, nodes, project=None, surfaces=None, transition=None):
        nodes = list(nodes)
        self.points = {n.name: tuple(n.pos) for n in nodes}
        self.radii = {n.name: n.properties.get('radius', DEFAULT_RADIUS) for n in nodes}
        self.project = project
        self.surfaces, self.transition = surfaces, transition
        self.edges = {name: set() for name in self.points}
        for node in nodes:
            links = str(node.properties.get("links", "")).replace(",", " ").split()
            for name in links:
                if name in self.points and name != node.name:
                    self.edges[node.name].add(name)
                    self.edges[name].add(node.name)

    def point(self, name, reference):
        marker = self.points[name]
        position = (self.project(marker, reference) if self.project is not None
                    else (marker[0], reference[1], marker[2]))
        return None if position is None else Waypoint(position, self.radii[name])

    def options(self, name, reference):
        if self.surfaces is None:
            point = self.point(name, reference)
            return [point] if point is not None else []
        return [Waypoint(p, self.radii[name]) for p in sorted(
            self.surfaces(self.points[name]), key=lambda p: abs(p[1] - reference[1]))]

    def segment(self, start, end, clear):
        if clear(start, end):
            return [end], distance(start, end)
        action = self.transition(start, end) if self.transition is not None else None
        return ([action, end], action.cost) if action is not None else (None, math.inf)

    @staticmethod
    def advance(route, position, clear, default_radius=DEFAULT_RADIUS, keep_final=False):
        """Consume reached nodes only when the next segment is also clear."""
        while len(route) > (1 if keep_final else 0):
            point = route[0]
            if isinstance(point, MoverTraversal):
                break
            radius = getattr(point, 'radius', default_radius)
            if (math.hypot(position[0] - point[0], position[2] - point[2]) > radius
                    or abs(position[1] - point[1]) > 32):
                break
            # Large radii must not cause a bot to cut through a corner.
            if len(route) > 1:
                # Reaching a lift's fixed landing starts its wait/board
                # action; the future platform need not be present yet.
                next_point = route[1].landing if isinstance(route[1], MoverTraversal) else route[1]
                if not clear(position, next_point):
                    break
            route.pop(0)

    def route(self, start, goal, clear):
        """A* with virtual start/goal edges, checking live door clearance.

        Nearest *reachable* nodes matter: the geometrically nearest waypoint
        may be across a wall. Never fall back to an unchecked edge.
        """
        destination = goal if isinstance(goal, Waypoint) else tuple(goal)
        if clear(start, goal):
            return [destination]
        best_path, best_cost = self.segment(start, destination, clear)
        points = {(name, layer): point for name in self.points
                  for layer, point in enumerate(self.options(name, start))}
        starts = sorted(points, key=lambda key: distance(start, points[key]))
        costs, parent, queue = {}, {}, []
        for key in starts:
            segment, cost = self.segment(start, points[key], clear)
            if segment is None:
                continue
            costs[key], parent[key] = cost, (None, segment)
            heapq.heappush(queue, (cost + distance(points[key], goal), key))
        closed = set()
        while queue:
            estimate, key = heapq.heappop(queue)
            if estimate >= best_cost:
                break
            if key in closed:
                continue
            end, end_cost = self.segment(points[key], destination, clear)
            if end is not None and costs[key] + end_cost < best_cost:
                segments, cursor = [end], key
                while cursor is not None:
                    cursor, segment = parent[cursor]
                    segments.append(segment)
                best_path = [point for segment in reversed(segments) for point in segment]
                best_cost = costs[key] + end_cost
            closed.add(key)
            neighbours = self.edges[key[0]] | {key[0]}
            for nxt in sorted(points):
                if nxt in closed or nxt[0] not in neighbours:
                    continue
                segment, cost = self.segment(points[key], points[nxt], clear)
                if segment is None:
                    continue
                candidate = costs[key] + cost
                if candidate < costs.get(nxt, math.inf):
                    costs[nxt], parent[nxt] = candidate, (key, segment)
                    heapq.heappush(queue, (candidate + distance(points[nxt], goal), nxt))
        return best_path or []
