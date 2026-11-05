"""One-time, undoable neighbour linking when a Bot Navigation Node is placed."""
import math
from types import SimpleNamespace

from engine.change_journal import touch
from .moving_navigation import NavigationWorld


def connect_neighbours(node, state):
    if not node.properties.get('auto_link', True):
        return []
    try:
        reach = float(node.properties.get('link_range', 512))
        if not math.isfinite(reach):
            reach = 512
    except (ValueError, TypeError):
        reach = 512
    world = NavigationWorld(SimpleNamespace(editor_state=state))
    static = world.brushes(static=True)
    source = world.surfaces(node.pos, static)
    # Designers can sketch nodes before adding the floor. These remain
    # potential connections; runtime never accepts unsupported walks.
    if not source:
        source = [(node.pos[0], 0, node.pos[2])]
    candidates = sorted((other for other in state.things if other is not node
        and other.properties.get('type') == 'deathmatchnode'
        and math.hypot(other.pos[0]-node.pos[0], other.pos[2]-node.pos[2]) <= reach),
        key=lambda other: (math.hypot(other.pos[0]-node.pos[0], other.pos[2]-node.pos[2]), other.name))
    linked = str(node.properties.get('links', '')).replace(',', ' ').split()
    added = []
    for other in candidates:
        target = world.surfaces(other.pos, static) or [(other.pos[0], 0, other.pos[2])]
        possible = any(world.walkable(a, b, static) for a in source for b in target)
        if not possible and not static:
            possible = world.clear(source[0], target[0], static)
        if not possible:
            possible = any(world.transfer(a, b, authored=True) is not None for a in source for b in target)
        if possible and other.name not in linked:
            linked.append(other.name)
            added.append(other.name)
            if len(added) >= 6:
                break
    node.properties['links'] = ','.join(linked)
    touch(node)
    return added
