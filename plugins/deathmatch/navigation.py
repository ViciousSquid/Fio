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


def ground_position(position, reference, brushes, terrain, clear):
    """Resolve a marker's X/Z to a body-clear surface near the bot's level.

    Marker Y is deliberately not a movement target. Stacked surfaces are
    resolved relative to the bot, so an eye-height marker does not attach it
    to a ceiling or another storey.
    """
    x, _, z = position
    heights = set()
    for brush in brushes:
        center, size = brush['pos'], brush['size']
        if not (abs(x - center[0]) <= size[0] / 2
                and abs(z - center[2]) <= size[2] / 2):
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
    for y in sorted(heights, key=lambda h: (abs(h - reference[1]), h)):
        point = (x, y, z)
        if clear(point, point):
            return point
    return None


def distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


class Navigation:
    def __init__(self, nodes, project=None):
        nodes = list(nodes)
        self.points = {n.name: tuple(n.pos) for n in nodes}
        self.radii = {n.name: n.properties.get('radius', DEFAULT_RADIUS) for n in nodes}
        self.project = project
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

    @staticmethod
    def advance(route, position, clear, default_radius=DEFAULT_RADIUS, keep_final=False):
        """Consume reached nodes only when the next segment is also clear."""
        while len(route) > (1 if keep_final else 0):
            point = route[0]
            radius = getattr(point, 'radius', default_radius)
            if (math.hypot(position[0] - point[0], position[2] - point[2]) > radius
                    or abs(position[1] - point[1]) > 32):
                break
            # Large radii must not cause a bot to cut through a corner.
            if len(route) > 1 and not clear(position, route[1]):
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
        if not self.points:
            return []
        points = {name: self.point(name, start) for name in self.points}
        points = {name: point for name, point in points.items() if point is not None}
        starts = sorted(points, key=lambda n: distance(start, points[n]))
        ends = sorted(points, key=lambda n: distance(goal, points[n]))
        starts = [n for n in starts if clear(start, points[n])][:3]
        ends = {n for n in ends if clear(points[n], goal)}
        costs, parent, queue = {}, {}, []
        for name in starts:
            costs[name] = distance(start, points[name])
            parent[name] = None
            heapq.heappush(queue, (costs[name] + distance(points[name], goal), name))
        closed = set()
        while queue:
            _, name = heapq.heappop(queue)
            if name in closed:
                continue
            if name in ends:
                path = [destination]
                while name is not None:
                    path.append(points[name])
                    name = parent[name]
                return path[::-1]
            closed.add(name)
            for nxt in sorted(self.edges[name]):
                if nxt not in points or nxt in closed or not clear(points[name], points[nxt]):
                    continue
                candidate = costs[name] + distance(points[name], points[nxt])
                if candidate < costs.get(nxt, math.inf):
                    costs[nxt], parent[nxt] = candidate, name
                    heapq.heappush(queue, (candidate + distance(points[nxt], goal), nxt))
        return []
