"""Small authored waypoint graph; no grid bake or external dependencies."""
import heapq
import math


def distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


class Navigation:
    def __init__(self, nodes):
        self.points = {n.name: tuple(n.pos) for n in nodes}
        self.edges = {name: set() for name in self.points}
        for node in nodes:
            links = str(node.properties.get("links", "")).replace(",", " ").split()
            for name in links:
                if name in self.points and name != node.name:
                    self.edges[node.name].add(name)
                    self.edges[name].add(node.name)

    def route(self, start, goal, clear):
        """A* with virtual start/goal edges, checking live door clearance.

        Nearest *reachable* nodes matter: the geometrically nearest waypoint
        may be across a wall. Never fall back to an unchecked edge.
        """
        if clear(start, goal):
            return [tuple(goal)]
        if not self.points:
            return []
        starts = sorted(self.points, key=lambda n: distance(start, self.points[n]))
        ends = sorted(self.points, key=lambda n: distance(goal, self.points[n]))
        starts = [n for n in starts if clear(start, self.points[n])][:3]
        ends = {n for n in ends if clear(self.points[n], goal)}
        costs, parent, queue = {}, {}, []
        for name in starts:
            costs[name] = distance(start, self.points[name])
            parent[name] = None
            heapq.heappush(queue, (costs[name] + distance(self.points[name], goal), name))
        closed = set()
        while queue:
            _, name = heapq.heappop(queue)
            if name in closed:
                continue
            if name in ends:
                path = [tuple(goal)]
                while name is not None:
                    path.append(self.points[name])
                    name = parent[name]
                return path[::-1]
            closed.add(name)
            for nxt in sorted(self.edges[name]):
                if nxt in closed or not clear(self.points[name], self.points[nxt]):
                    continue
                candidate = costs[name] + distance(self.points[name], self.points[nxt])
                if candidate < costs.get(nxt, math.inf):
                    costs[nxt], parent[nxt] = candidate, name
                    heapq.heappush(queue, (candidate + distance(self.points[nxt], goal), nxt))
        return []
