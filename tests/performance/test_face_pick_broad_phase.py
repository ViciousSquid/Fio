"""Face Mode's pick (``ViewPicking.get_brush_face_at_coords``) runs every
frame the camera moves, so it tests a brush's real faces only when the ray
enters its box nearer than the best hit so far -- and answers exactly what
testing every brush would: the nearest entered face, a tie going to the
earlier brush."""

import math
import random

import glm
import pytest

from engine import brush_geometry, brush_placement
from engine.view_picking import ViewPicking
from tests.helpers.worlds import angled_brush, box_brush

# engine.view_picking is the Qt/GL viewport mixin: it imports PyQt5 and
# PyOpenGL at module level, so this belongs to the Qt tier.
pytestmark = pytest.mark.qt


class _Filters:
    @staticmethod
    def hides_brush(_brush):
        return False


class _View:
    def __init__(self, brushes):
        state = type('State', (), {})()
        state.brushes = brushes
        state.view_filters = _Filters()
        self.editor = type('Editor', (), {'state': state})()


def _picker(brushes, ray):
    picking = ViewPicking(_View(brushes))
    picking.get_ray_from_mouse = lambda _x, _y: ray
    return picking


def _exhaustive(brushes, ray_o, ray_d):
    """Every brush tested: the pick as it was before the broad phase."""
    o_t = (float(ray_o.x), float(ray_o.y), float(ray_o.z))
    d_t = (float(ray_d.x), float(ray_d.y), float(ray_d.z))
    best_t, best = math.inf, None
    for brush in brushes:
        if brush.get('hidden', False):
            continue
        o, d = brush_placement.ray_to_shape(brush, o_t, d_t)
        convex = brush_placement.shape_convex(brush)
        if convex is not None:
            hit = brush_geometry.ray_convex_face(convex, o, d, entering=True)
            if hit is not None and hit[0] < best_t:
                best_t, best = hit[0], (brush, brush_geometry.face_key(hit[1]))
            continue
        box = brush_placement.authored_box(brush)
        t = brush_placement.ray_box_entry(box, o, d)
        if t is None or t >= best_t:
            continue
        local = [o[i] + d[i] * t - box[i] for i in range(3)]
        rel = [abs(local[i]) / (2.0 * box[i + 3]) for i in range(3)]
        face = 'north' if local[2] > 0 else 'south'
        if rel[0] > rel[1] and rel[0] > rel[2]:
            face = 'east' if local[0] > 0 else 'west'
        elif rel[1] > rel[0] and rel[1] > rel[2]:
            face = 'top' if local[1] > 0 else 'down'
        best_t, best = t, (brush, face)
    return best


def _scene(rng, count=120):
    brushes = []
    for i in range(count):
        pos = (rng.uniform(-800, 800), rng.uniform(-200, 200), rng.uniform(-1600, -100))
        size = (rng.uniform(16, 200), rng.uniform(16, 200), rng.uniform(16, 200))
        if i % 3 == 0:
            normal = (rng.uniform(-1, 1), rng.uniform(0.2, 1), rng.uniform(-1, 1))
            brushes.append(angled_brush('a%d' % i, pos, size, clip_normal=normal))
        else:
            brushes.append(box_brush('b%d' % i, pos, size))
        if i % 17 == 0:
            brushes[-1]['hidden'] = True
    # A volume around the camera and a duplicate (an exact tie): looked
    # through, and the earlier of two coincident brushes wins.
    brushes.append(box_brush('room', (0, 0, 0), (4000, 4000, 4000)))
    brushes.append(box_brush('twin_a', (0, 0, -60), (64, 64, 8)))
    brushes.append(box_brush('twin_b', (0, 0, -60), (64, 64, 8)))
    return brushes


def test_same_answer_as_testing_every_brush():
    rng = random.Random(7)
    brushes = _scene(rng)
    origin = glm.vec3(0.0, 0.0, 0.0)
    checked = hits = 0
    for _ in range(400):
        direction = glm.normalize(glm.vec3(
            rng.uniform(-0.8, 0.8), rng.uniform(-0.4, 0.4), -1.0))
        got = _picker(brushes, (origin, direction)).get_brush_face_at_coords(0, 0)
        want = _exhaustive(brushes, origin, direction)
        assert (got is None) == (want is None)
        if got is not None:
            assert got[0] is want[0] and got[1] == want[1]
            hits += 1
        checked += 1
    assert hits > checked // 2
    # The tie: straight ahead hits the twins' shared face; the first wins.
    got = _picker(brushes, (origin, glm.vec3(0, 0, -1))).get_brush_face_at_coords(0, 0)
    assert got[0]['name'] == 'twin_a'


def test_faces_tested_only_for_boxes_the_ray_can_reach(monkeypatch):
    """A row of angled brushes along the ray: only the nearest one's faces
    are tested -- the rest are entered beyond its hit -- and none of the
    brushes off to the side."""
    row = [angled_brush('row%d' % i, (0, 0, -200 - 150 * i), (96, 96, 96),
                        clip_normal=(0.3, 1.0, 0.2)) for i in range(40)]
    aside = [angled_brush('aside%d' % i, (400 + 150 * i, 0, -300), (96, 96, 96),
                          clip_normal=(0.3, 1.0, 0.2)) for i in range(40)]
    brushes = aside + row
    tested = []
    real = brush_geometry.ray_convex_face

    def counting(convex, *args, **kwargs):
        tested.append(convex)
        return real(convex, *args, **kwargs)

    monkeypatch.setattr(brush_geometry, 'ray_convex_face', counting)
    ray = (glm.vec3(0, 0, 0), glm.vec3(0, 0, -1))
    hit = _picker(brushes, ray).get_brush_face_at_coords(0, 0)
    assert hit is not None and hit[0] is row[0]
    assert len(tested) == 1


@pytest.mark.parametrize('spin', [None, (0.0, 1.0, 0.0, 30.0)])
def test_moved_and_turned_brushes_are_picked_where_drawn(monkeypatch, spin):
    """A mover's runtime transform: the broad phase works in the frame its
    shape is authored in, as the exact test does."""
    brush = angled_brush('mover', (0, 0, -300), (128, 128, 128),
                         clip_normal=(0.2, 1.0, 0.1), is_mover=True)
    runtime = (0.0, 0.0, -600.0) + ((spin[:3] + (spin[3],)) if spin else (0.0, 1.0, 0.0, 0.0))
    monkeypatch.setattr(brush_placement, 'runtime_of',
                        lambda b: runtime if b is brush else None)
    ray = (glm.vec3(0, 0, 0), glm.vec3(0, 0, -1))
    got = _picker([brush], ray).get_brush_face_at_coords(0, 0)
    want = _exhaustive([brush], *ray)
    assert want is not None and got[0] is brush and got[1] == want[1]
