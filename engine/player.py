import math
import glm
from PyQt5.QtCore import Qt
from .constants import (
    TILE_SIZE, GRAVITY, JUMP_STRENGTH, TERMINAL_VELOCITY,
    WATER_SWIM_SPEED_MULT, WATER_VERTICAL_SPEED_MULT, WATER_DRAG,
    WATER_WADE_SPEED_MULT, WATER_MAX_SINK_SPEED,
    WATERJUMP_MAX_CLIMB, WATERJUMP_EDGE_ABOVE_SURFACE, WATERJUMP_MAX_BOOST,
    is_water_brush, brush_aabb_bounds,
)


# =============================================================================
# Mesh Collision Helpers
# =============================================================================

def _point_in_triangle(p, a, b, c):
    """Barycentric test: is point p inside triangle abc? All are (x,y,z) tuples."""
    def sub(v1, v2):
        return (v1[0]-v2[0], v1[1]-v2[1], v1[2]-v2[2])
    def cross(v1, v2):
        return (v1[1]*v2[2]-v1[2]*v2[1], v1[2]*v2[0]-v1[0]*v2[2], v1[0]*v2[1]-v1[1]*v2[0])
    def dot(v1, v2):
        return v1[0]*v2[0]+v1[1]*v2[1]+v1[2]*v2[2]
    
    ab = sub(b, a)
    ac = sub(c, a)
    ap = sub(p, a)
    n = cross(ab, ac)
    d = dot(n, n)
    
    if d < 0.0001:
        return False
        
    w = dot(cross(ab, ap), n) / d
    v = dot(cross(ap, ac), n) / d
    u = 1.0 - w - v
    
    return u >= -0.001 and v >= -0.001 and w >= -0.001


def _intersect_swept_sphere_triangle(sphere_pos, sphere_vel, radius, triangle, normal):
    """Swept sphere vs triangle. Returns (hit, hit_time, hit_point, slide_normal).
    
    sphere_pos: glm.vec3 - current position
    sphere_vel: glm.vec3 - velocity this frame (will move by this amount)
    radius: float - player radius (half of width/depth)
    triangle: ((v0,v1,v2), normal) from mesh collision data
    """
    v0, v1, v2 = triangle
    
    # Convert to tuples for math
    sp = (sphere_pos.x, sphere_pos.y, sphere_pos.z)
    sv = (sphere_vel.x, sphere_vel.y, sphere_vel.z)
    
    # Distance from sphere center to triangle plane
    to_v0 = (sp[0] - v0[0], sp[1] - v0[1], sp[2] - v0[2])
    dist = to_v0[0]*normal[0] + to_v0[1]*normal[1] + to_v0[2]*normal[2]
    
    # Relative velocity along normal
    vel_dot_n = sv[0]*normal[0] + sv[1]*normal[1] + sv[2]*normal[2]
    
    # If moving away from triangle and clearly not touching, no collision
    if vel_dot_n > 0 and dist > radius:
        return False, 1.0, None, None

    # Sphere fully behind the plane (inside-out geometry) — skip
    if dist < -radius:
        return False, 1.0, None, None

    # Time when sphere surface touches plane.
    # We solve: dist + vel_dot_n * t = radius  →  t = (radius - dist) / vel_dot_n
    # BUG FIX: the original code had (dist - radius) which is the wrong sign, producing
    # a negative t for any approaching sphere and causing all mesh collision to be skipped.
    if dist > radius:
        # Sphere not yet touching the plane — find exact contact time
        if abs(vel_dot_n) < 0.0001:
            # Moving parallel to a plane we haven't touched yet — no contact
            return False, 1.0, None, None
        t0 = (radius - dist) / vel_dot_n   # positive when vel_dot_n < 0 (approaching)
    else:
        # Sphere is already overlapping the plane (dist <= radius).
        # If moving further in, treat as immediate (t=0) collision so the player is
        # deflected this frame.  If moving out, let it escape without interference.
        if vel_dot_n >= 0:
            return False, 1.0, None, None
        t0 = 0.0

    if t0 < 0 or t0 > 1.0:
        return False, 1.0, None, None
    
    # Point on plane at collision time (sphere center projected to plane)
    hit_point = (
        sp[0] + sv[0]*t0 - normal[0]*radius,
        sp[1] + sv[1]*t0 - normal[1]*radius,
        sp[2] + sv[2]*t0 - normal[2]*radius,
    )
    
    # Check if point is inside triangle
    if _point_in_triangle(hit_point, v0, v1, v2):
        return True, t0, glm.vec3(*hit_point), glm.vec3(*normal)
    
    # Edge/vertex collision - test sphere vs each edge
    def closest_point_on_segment(p, a, b):
        ab = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
        t = max(0.0, min(1.0, ((p[0]-a[0])*ab[0] + (p[1]-a[1])*ab[1] + (p[2]-a[2])*ab[2]) / 
               (ab[0]*ab[0] + ab[1]*ab[1] + ab[2]*ab[2] + 0.0001)))
        return (a[0] + t*ab[0], a[1] + t*ab[1], a[2] + t*ab[2])
    
    # Test against 3 edges
    edges = [(v0, v1), (v1, v2), (v2, v0)]
    for a, b in edges:
        closest = closest_point_on_segment(hit_point, a, b)
        dx = hit_point[0] - closest[0]
        dy = hit_point[1] - closest[1]
        dz = hit_point[2] - closest[2]
        edge_dist_sq = dx*dx + dy*dy + dz*dz
        if edge_dist_sq < radius*radius:
            # Hit edge - push out along vector from edge to sphere center
            edge_normal = glm.normalize(glm.vec3(dx, dy, dz))
            return True, t0, glm.vec3(*closest), edge_normal
    
    return False, 1.0, None, None

def _collide_and_slide(sphere_pos, velocity, radius, mesh_tris, mesh_bounds, max_iterations=3):
    """
    Fauerby-style collide-and-slide against a triangle mesh.

    Returns: (new_position, new_velocity, ground_normal, hit_ground)

    The key insight: when we hit a triangle, we don't stop. Instead:
    1. Move to just before the collision point
    2. Define a "sliding plane" perpendicular to the collision normal
    3. Project the remaining velocity onto this plane
    4. Recurse with the new velocity
    """
    VERY_CLOSE_DIST = 0.005  # Don't move all the way to the surface

    # Broad-phase rejection
    if mesh_bounds:
        min_b, max_b = mesh_bounds
        vel_mag = glm.length(velocity)
        expanded_min = (min_b[0] - radius - vel_mag, min_b[1] - radius - vel_mag, min_b[2] - radius - vel_mag)
        expanded_max = (max_b[0] + radius + vel_mag, max_b[1] + radius + vel_mag, max_b[2] + radius + vel_mag)

        sp = (sphere_pos.x, sphere_pos.y, sphere_pos.z)
        if (sp[0] < expanded_min[0] or sp[0] > expanded_max[0] or
            sp[1] < expanded_min[1] or sp[1] > expanded_max[1] or
            sp[2] < expanded_min[2] or sp[2] > expanded_max[2]):
            return sphere_pos + velocity, velocity, None, False

    # Working copies
    pos = glm.vec3(sphere_pos)
    vel = glm.vec3(velocity)

    ground_normal = None
    hit_ground = False

    # Keep track of sliding planes to prevent corner jitter
    sliding_planes = []

    for iteration in range(max_iterations):
        if glm.length(vel) < VERY_CLOSE_DIST:
            break

        # Find nearest collision
        nearest_t = 1.0
        nearest_hit_point = None
        nearest_normal = None

        for tri, normal in mesh_tris:
            hit, t, hit_point, hit_normal = _intersect_swept_sphere_triangle(
                pos, vel, radius, tri, normal
            )
            if hit and t < nearest_t:
                nearest_t = t
                nearest_hit_point = hit_point
                nearest_normal = hit_normal

        # No collision - move freely
        if nearest_normal is None:
            pos = pos + vel
            break

        # Collision found - move close to intersection but not exactly to it
        if nearest_t >= VERY_CLOSE_DIST:
            # Move to just before the collision
            move_dist = nearest_t - VERY_CLOSE_DIST / glm.length(vel)
            pos = pos + vel * move_dist

        # Define sliding plane
        # The sliding plane origin is the collision point, normal is the collision normal
        # We want to project the remaining velocity onto this plane

        # Distance from destination to sliding plane
        dest = pos + vel * (1.0 - nearest_t)
        slide_plane_d = -glm.dot(nearest_normal, nearest_hit_point)
        dist_to_plane = glm.dot(nearest_normal, dest) + slide_plane_d
        
        # New destination = project onto sliding plane
        new_dest = dest - nearest_normal * dist_to_plane
        
        # New velocity for next iteration
        vel = new_dest - pos
        
        # Track if we hit ground (normal pointing up)
        if nearest_normal.y > 0.5:
            ground_normal = nearest_normal
            hit_ground = True
        
        # Track sliding planes for corner cases
        sliding_planes.append((nearest_hit_point, nearest_normal))
        
        # If we have 2+ sliding planes, check if we're in a crease
        if len(sliding_planes) >= 2:
            # Project onto crease (intersection of two planes)
            n1 = sliding_planes[-2][1]
            n2 = sliding_planes[-1][1]
            crease = glm.cross(n1, n2)
            if glm.length(crease) > 0.001:
                crease = glm.normalize(crease)
                # Project velocity onto crease
                signed_dist = glm.dot(dest - pos, crease)
                vel = crease * signed_dist
            else:
                # Parallel planes - stop
                vel = glm.vec3(0)
                break
        
        # If 3 planes, we're cornered - stop
        if len(sliding_planes) >= 3:
            vel = glm.vec3(0)
            break
    
    return pos, vel, ground_normal, hit_ground


# =============================================================================
# Player Class
# =============================================================================

class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        # Initialize position (Y is set to 2 tiles high by default)
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle = angle
        self.pitch = 0.0

        # Physics constants
        self.speed = 200.0
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = TILE_SIZE, TILE_SIZE * 2, TILE_SIZE
        self.camera_height = 40.0  # Offset from pos.y

        # Physics state
        self.on_ground = False
        self.ground_object = None  # Reference to the brush we are standing on
        self.physics_enabled = physics_enabled
        self.step_height = 18.0  # Max height the player can step up automatically

        # Water / swimming state (refreshed every update)
        self.in_water = False          # any meaningful immersion (wading or deeper)
        self.swimming = False          # deep enough that swim physics take over
        self.eye_underwater = False    # camera below a water surface (drives the underwater overlay)
        self.water_surface_y = None    # world Y of the surface we're swimming under
        self.water_depth_frac = 0.0    # 0..1 fraction of feet->eye span that is submerged
        self.water_tint = [0.0, 0.4, 0.6]
        self._waterjump_timer = 0.0    # while > 0, swim drag leaves the launch arc alone
        self._waterjump_cooldown = 0.0 # prevents re-triggering every frame

        # Pre-computed half-extents (constant for the lifetime of this player instance)
        self._half = glm.vec3(self.width / 2.0, self.height / 2.0, self.depth / 2.0)

    def get_view_matrix(self):
        """Calculate the view matrix for rendering."""
        cam_pos = self.pos + glm.vec3(0, self.camera_height, 0)
        direction = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch),
        )
        return glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))

    def update(self, delta, move_input, jump, crouch, brushes, movers=None, doors=None, terrain=None, spatial_grid=None):
        """
        Update player physics.

        PERF: If spatial_grid is provided, static brush colliders are fetched
        from the grid (only nearby cells) instead of iterating every brush.
        Movers and doors are always included since they're dynamic.
        """
        # --- Build collider list ---
        if spatial_grid:
            # Use the grid to get only nearby static brushes
            half = self._half
            player_min = self.pos - half - glm.vec3(self.speed * delta + 32)  # pad for movement
            player_max = self.pos + half + glm.vec3(self.speed * delta + 32)
            colliders = spatial_grid.get_potential_colliders(player_min, player_max)
        else:
            # Fallback: all brushes (old behaviour)
            colliders = list(brushes)

        if movers:
            colliders.extend(movers)
        if doors:
            colliders.extend(doors)

        # --- Water immersion (before movement so swim physics can use it) ---
        if spatial_grid is not None:
            water_brushes = getattr(spatial_grid, 'water_brushes', None)
            if water_brushes is None:
                water_brushes = [b for b in brushes if is_water_brush(b)]
        else:
            water_brushes = [b for b in brushes if is_water_brush(b)]
        self._update_water_state(water_brushes)
        if self._waterjump_timer > 0.0:
            self._waterjump_timer = max(0.0, self._waterjump_timer - delta)
        if self._waterjump_cooldown > 0.0:
            self._waterjump_cooldown = max(0.0, self._waterjump_cooldown - delta)

        # --- 1. Movement Physics ---

        forward_vec = glm.vec3(math.sin(self.angle), 0, math.cos(self.angle))
        right_vec   = glm.vec3(math.cos(self.angle), 0, -math.sin(self.angle))

        wish_dir = forward_vec * move_input.z + right_vec * move_input.x

        if glm.length(wish_dir) > 0.1:
            wish_dir = glm.normalize(wish_dir)

        swimming = self.swimming and self.physics_enabled

        if swimming:
            self._apply_swim_physics(delta, move_input, right_vec, jump, crouch)
        else:
            target_speed = self.speed * (0.5 if crouch else 1.0)
            if self.in_water:
                target_speed *= WATER_WADE_SPEED_MULT

            self.velocity.x = wish_dir.x * target_speed
            self.velocity.z = wish_dir.z * target_speed

        if not self.physics_enabled:
            self.pos += self.velocity * delta
            return

        # --- 2. Gravity & Jumping (suspended while swimming) ---

        if not swimming:
            self.velocity.y += GRAVITY * delta

            if self.velocity.y < TERMINAL_VELOCITY:
                self.velocity.y = TERMINAL_VELOCITY

            # Wading: water resistance breaks a fall almost immediately
            if self.in_water and self.velocity.y < WATER_MAX_SINK_SPEED:
                self.velocity.y = WATER_MAX_SINK_SPEED

            if jump and self.on_ground:
                self.velocity.y = JUMP_STRENGTH
                self.on_ground  = False
                self.ground_object = None

        # --- 2b. Waterjump: climbing out of pools ---
        # Holding jump while pushing against a wall whose top edge is near the
        # waterline launches the player hard enough to actually clear it.
        # Works while wading AND swimming; without it, any pool whose rim is
        # taller than a normal jump becomes an inescapable trap.
        if self.in_water and jump:
            self._try_water_jump(colliders, wish_dir)

        # --- 3. Collision Resolution ---

        # Separate mesh brushes from AABB brushes (single pass over colliders)
        mesh_brushes = []
        aabb_brushes = []
        for b in colliders:
            if b.get('_collision_mode') == 'mesh':
                mesh_brushes.append(b)
            else:
                aabb_brushes.append(b)

        # A. Horizontal movement with AABB collision.  This integrates X/Z
        # once (it advances pos even with an empty brush list), so mesh brushes
        # must NOT integrate again — they are resolved by depenetration below.
        self._move_with_collision(delta, aabb_brushes, axis='x')
        self._move_with_collision(delta, aabb_brushes, axis='z')

        # B. Vertical Y Movement
        self.pos.y += self.velocity.y * delta

        # Reset ground state before Y collision check
        self.on_ground     = False
        self.ground_object = None

        # 1. AABB / mover collision for Y
        self._resolve_collision(aabb_brushes, axis='y', delta=delta)

        # 2. Angled (mesh) brushes: capsule depenetration for X/Y/Z together —
        #    pushes the player out of ramps/wedges and lands them on slopes.
        self._resolve_mesh_capsule(mesh_brushes)

        # 2. Terrain collision
        if terrain and terrain.is_solid():
            terrain_height = terrain.get_height_at_safe(self.pos.x, self.pos.z)
            if terrain_height is not None:
                feet_y = self.pos.y - self.height / 2.0
                if feet_y < terrain_height:
                    self.pos.y         = terrain_height + self.height / 2.0
                    self.velocity.y    = 0
                    self.on_ground     = True
                    self.ground_object = terrain

        # Floor safety clamp
        if self.pos.y < -2000:
            self.pos     = glm.vec3(0, 100, 0)
            self.velocity = glm.vec3(0, 0, 0)

    # ------------------------------------------------------------------
    # Water / swimming
    # ------------------------------------------------------------------

    def _update_water_state(self, water_brushes):
        """Measure how deep the player is in any water volume this frame."""
        feet_y = self.pos.y - self._half.y
        eye_y = self.pos.y + self.camera_height
        px, pz = self.pos.x, self.pos.z

        depth = 0.0
        surface_y = None
        tint = None
        eye_under = False

        for brush in water_brushes:
            if brush.get('hidden'):
                continue
            bpos = brush['pos']
            bsize = brush['size']
            hx, hz = bsize[0] * 0.5, bsize[2] * 0.5
            if not (bpos[0] - hx <= px <= bpos[0] + hx and
                    bpos[2] - hz <= pz <= bpos[2] + hz):
                continue
            b_top = bpos[1] + bsize[1] * 0.5
            b_bot = bpos[1] - bsize[1] * 0.5

            overlap_top = min(eye_y, b_top)
            overlap_bot = max(feet_y, b_bot)
            if overlap_top <= overlap_bot:
                continue

            depth = max(depth, overlap_top - overlap_bot)
            if surface_y is None or b_top > surface_y:
                surface_y = b_top
                tint = brush.get('water_tint')
            if b_bot <= eye_y <= b_top:
                eye_under = True

        span = max(eye_y - feet_y, 0.001)
        frac = depth / span
        self.water_depth_frac = frac
        self.in_water = frac > 0.12
        self.swimming = frac > 0.55
        self.eye_underwater = eye_under
        self.water_surface_y = surface_y
        if tint is not None:
            self.water_tint = list(tint)

    def _apply_swim_physics(self, delta, move_input, right_vec, jump, crouch):
        """Buoyant, drag-damped movement while submerged.

        Forward motion follows the view pitch (look down + forward = dive),
        jump swims up, crouch sinks, and idling drifts gently toward a
        floating position at the surface. Velocity converges on the swim
        target through drag instead of snapping, which gives water its
        characteristic weight.
        """
        cos_p = math.cos(self.pitch)
        swim_fwd = glm.vec3(math.sin(self.angle) * cos_p,
                            math.sin(self.pitch),
                            math.cos(self.angle) * cos_p)

        wish = swim_fwd * move_input.z + right_vec * move_input.x
        if glm.length(wish) > 0.1:
            wish = glm.normalize(wish)

        swim_speed = self.speed * WATER_SWIM_SPEED_MULT
        target = wish * swim_speed

        vertical_speed = swim_speed * WATER_VERTICAL_SPEED_MULT
        if jump:
            target.y += vertical_speed
        if crouch:
            target.y -= vertical_speed

        # Idle buoyancy: bob up until the eyes sit just above the surface
        if not jump and not crouch and abs(move_input.z) < 0.1 and self.water_surface_y is not None:
            float_target_y = self.water_surface_y - self.camera_height * 0.35
            err = float_target_y - self.pos.y
            target.y += max(min(err * 1.8, 40.0), -25.0)

        k = min(1.0, WATER_DRAG * delta)
        self.velocity.x += (target.x - self.velocity.x) * k
        self.velocity.z += (target.z - self.velocity.z) * k
        # A waterjump launch is ballistic: leave its vertical arc alone or
        # drag would smother the boost before the player clears the edge
        if self._waterjump_timer <= 0.0:
            self.velocity.y += (target.y - self.velocity.y) * k

        self.on_ground = False
        self.ground_object = None

    @staticmethod
    def _waterjump_solid(brush):
        """Solid AABB obstacles a waterjump can vault onto (or be blocked by)."""
        if brush.get('hidden') or brush.get('disabled') or brush.get('is_fog') or is_water_brush(brush):
            return False
        if brush.get('is_trigger') and not (brush.get('is_mover') or brush.get('is_door')):
            return False
        if brush.get('_collision_mode') == 'mesh':
            return False
        return True

    def _try_water_jump(self, colliders, wish_dir):
        """Vault out of water onto a nearby ledge.

        Probes one step ahead in the movement direction for a solid wall whose
        top edge is (a) above the feet, (b) within climbing reach, and (c) not
        far above the waterline, then launches with exactly the vertical speed
        needed for the feet to clear that edge. This is what lets the player
        get OUT of a pool: the rim is usually taller than a normal jump from
        the pool floor, and swimming alone can never lift the body over it.
        """
        if self._waterjump_cooldown > 0.0:
            return
        if glm.length(wish_dir) < 0.1:
            return  # must be pushing toward the edge, not just treading water

        feet_y = self.pos.y - self._half.y
        surface_y = self.water_surface_y if self.water_surface_y is not None else feet_y
        body_r = max(self._half.x, self._half.z)

        # Two probe depths so thin rims aren't stepped over by a single point
        for probe_dist in (body_r + 10.0, body_r + 26.0):
            px = self.pos.x + wish_dir.x * probe_dist
            pz = self.pos.z + wish_dir.z * probe_dist

            # Highest climbable ledge at this probe point
            ledge_top = None
            for brush in colliders:
                if not self._waterjump_solid(brush):
                    continue
                bpos, bsize = brush['pos'], brush['size']
                if not (abs(px - bpos[0]) <= bsize[0] * 0.5 and
                        abs(pz - bpos[2]) <= bsize[2] * 0.5):
                    continue
                top = bpos[1] + bsize[1] * 0.5
                bottom = bpos[1] - bsize[1] * 0.5
                if bottom > feet_y + 8.0:
                    continue  # floating overhang, not a wall rising from below
                if top <= feet_y + 4.0:
                    continue  # already below our feet
                if top > feet_y + WATERJUMP_MAX_CLIMB:
                    continue  # too tall to vault — needs stairs
                if top > surface_y + WATERJUMP_EDGE_ABOVE_SURFACE:
                    continue  # rim too far above the waterline
                if ledge_top is None or top > ledge_top:
                    ledge_top = top

            if ledge_top is None:
                continue

            # Headroom: the player must fit standing on the ledge
            blocked = False
            for brush in colliders:
                if not self._waterjump_solid(brush):
                    continue
                bpos, bsize = brush['pos'], brush['size']
                if not (abs(px - bpos[0]) <= bsize[0] * 0.5 and
                        abs(pz - bpos[2]) <= bsize[2] * 0.5):
                    continue
                top = bpos[1] + bsize[1] * 0.5
                bottom = bpos[1] - bsize[1] * 0.5
                if top > ledge_top + 4.0 and bottom < ledge_top + self.height:
                    blocked = True
                    break
            if blocked:
                continue

            rise = (ledge_top - feet_y) + 12.0
            self.velocity.y = min(math.sqrt(2.0 * abs(GRAVITY) * rise), WATERJUMP_MAX_BOOST)
            self._waterjump_timer = 0.6
            self._waterjump_cooldown = 0.7
            return

    def _move_with_collision(self, delta, aabb_brushes, axis):
        """
        Handles movement along a specific axis with collision detection.
        """
        # 1. Determine velocity component based on axis
        if axis == 'z':
            move_val = self.velocity.z * delta
        elif axis == 'x':
            move_val = self.velocity.x * delta
        else:
            move_val = self.velocity.y * delta

        if abs(move_val) < 0.0001:
            return

        # Store current position to revert if collision occurs
        orig_pos = glm.vec3(self.pos)
        
        # 2. Apply movement
        if axis == 'z':
            self.pos.z += move_val
        elif axis == 'x':
            self.pos.x += move_val
        else:
            self.pos.y += move_val

        # 3. Calculate current player bounds using individual dimensions
        half_dims = glm.vec3(self.width * 0.5, self.height * 0.5, self.depth * 0.5)
        p_min = self.pos - half_dims
        p_max = self.pos + half_dims

        # 4. Check for collisions with potential colliders
        for brush in aabb_brushes:
            # Check headroom using the fixed signature (brush, p_min, p_max)
            if not self._has_headroom(brush, p_min, p_max):
                # Blocked — before treating this as a wall, try the automatic
                # stair "lip": a low obstacle is climbed instead of walked into.
                if axis in ('x', 'z') and self._try_step_up(aabb_brushes, orig_pos):
                    return
                # Collision detected: revert position and stop movement
                self.pos = orig_pos
                if axis == 'z':
                    self.velocity.z = 0
                elif axis == 'x':
                    self.velocity.x = 0
                else:
                    self.velocity.y = 0
                return

    def _try_step_up(self, aabb_brushes, orig_pos):
        """
        Automatic stair "lip": when horizontal movement is blocked, check
        whether every blocking brush is a low step (top edge no more than
        step_height above the feet). If so — and there is headroom to stand
        on it — lift the player onto the step and let the move stand.

        Called with self.pos already at the blocked destination. Returns
        True if the player was stepped up (caller keeps the move), False
        if this is a real wall (caller reverts as before).
        """
        # Only step while walking on the ground; never mid-jump or swimming
        if self.swimming or not self.on_ground:
            return False
        if self.velocity.y > 0.01:
            return False

        half = self._half
        feet_y = orig_pos.y - half.y
        p_min = self.pos - half
        p_max = self.pos + half

        # Every brush blocking the destination must qualify as a step;
        # a single taller wall means this is not a staircase lip.
        step_top = None
        for brush in aabb_brushes:
            if self._has_headroom(brush, p_min, p_max):
                continue
            top = brush['pos'][1] + brush['size'][1] * 0.5
            rise = top - feet_y
            if rise <= 0.0 or rise > self.step_height:
                return False
            if step_top is None or top > step_top:
                step_top = top
        if step_top is None:
            return False

        # Headroom: the player must fit standing with feet on the step
        lifted_y = step_top + half.y + 0.001
        l_min = glm.vec3(p_min.x, lifted_y - half.y, p_min.z)
        l_max = glm.vec3(p_max.x, lifted_y + half.y, p_max.z)
        for brush in aabb_brushes:
            if not self._has_headroom(brush, l_min, l_max):
                return False

        self.pos.y = lifted_y
        return True

    def _capsule_offsets(self):
        """Vertical sample points + radius approximating the player as a capsule.

        The player is a 50x100x50 box; a single centre sphere (radius ~half
        width) leaves the feet 50 units below it, so on a ramp the body sinks
        in until that centre sphere finally touches the slope.  Sampling the
        vertical axis at feet / mid / head — each a sphere of the footprint's
        inscribed radius — lets the lowest sphere rest the feet on the surface
        and the upper spheres block the torso against angled walls.
        """
        r = min(self._half.x, self._half.z)
        span = max(self._half.y - r, 0.0)
        offsets = (glm.vec3(0.0, -span, 0.0),
                   glm.vec3(0.0, 0.0, 0.0),
                   glm.vec3(0.0, span, 0.0))
        return offsets, r

    def _resolve_mesh_capsule(self, mesh_brushes):
        """Resolve the player capsule against angled (mesh) brushes by pushing
        it out of any penetration, and set ground/ceiling state from the
        contact normals.

        This runs *after* the shared position integration (the AABB pass moves
        X/Z, ``pos.y += vel.y*dt`` moves Y), so it only corrects for angled
        geometry.  A discrete depenetration model is what makes ramps behave:
        walking into an incline embeds the capsule, then the push-out along the
        face normal lifts the feet onto the slope (you climb) and leaves the
        body resting exactly on the surface instead of sinking in — while a
        vertical angled face just shoves you back like a wall.

        Each brush is a convex solid, so a sphere is depenetrated with the
        separating-plane rule: the plane of greatest signed distance gives the
        minimum push out.  That works whether the sphere merely clips a face or
        has its centre fully inside the solid (which the earlier triangle test
        missed, letting the player tunnel through angled walls).
        """
        if not mesh_brushes:
            return

        offsets, radius = self._capsule_offsets()
        ground_normal = None      # steepest-up contact this resolve
        wall_normal = None        # last horizontal contact, for velocity slide
        hit_ceiling = False

        # A few relaxation passes: each pushes out of the deepest overlap, then
        # re-tests, so a capsule touching several faces settles cleanly.
        for _ in range(4):
            deepest = 1e-4
            push = None
            push_n = None
            for brush in mesh_brushes:
                planes = brush.get('_mesh_planes')
                if not planes:
                    continue
                bounds = brush.get('_mesh_bounds')
                if bounds:
                    mn, mx = bounds
                    pmn = self.pos - self._half
                    pmx = self.pos + self._half
                    if (pmx.x < mn[0] - radius or pmn.x > mx[0] + radius or
                        pmx.y < mn[1] - radius or pmn.y > mx[1] + radius or
                        pmx.z < mn[2] - radius or pmn.z > mx[2] + radius):
                        continue
                for off in offsets:
                    cx = self.pos.x + off.x
                    cy = self.pos.y + off.y
                    cz = self.pos.z + off.z
                    # Sphere vs convex: largest signed distance across faces.
                    best_sd = -1e30
                    best_n = None
                    separated = False
                    for (nx, ny, nz, d) in planes:
                        sd = nx * cx + ny * cy + nz * cz - d
                        if sd > radius:
                            separated = True   # a separating plane exists
                            break
                        if sd > best_sd:
                            best_sd = sd
                            best_n = (nx, ny, nz)
                    if separated or best_n is None:
                        continue
                    pen = radius - best_sd
                    if pen > deepest:
                        deepest = pen
                        push = (best_n[0] * pen, best_n[1] * pen, best_n[2] * pen)
                        push_n = best_n

            if push is None:
                break

            self.pos.x += push[0]
            self.pos.y += push[1]
            self.pos.z += push[2]

            if push_n[1] > 0.3:
                if ground_normal is None or push_n[1] > ground_normal[1]:
                    ground_normal = push_n
            elif push_n[1] < -0.3:
                hit_ceiling = True
            else:
                wall_normal = push_n

        # Apply the resulting contact state to velocity + ground flags.
        if ground_normal is not None:
            if self.velocity.y < 0.0:
                self.velocity.y = 0.0
            self.on_ground = True
            for brush in mesh_brushes:
                if brush.get('_mesh_triangles'):
                    self.ground_object = brush
                    break
        if hit_ceiling and self.velocity.y > 0.0:
            self.velocity.y = 0.0
        if wall_normal is not None:
            # Cancel the horizontal velocity heading into the wall so the
            # player slides along it instead of jamming.
            vn = self.velocity.x * wall_normal[0] + self.velocity.z * wall_normal[2]
            if vn < 0.0:
                self.velocity.x -= wall_normal[0] * vn
                self.velocity.z -= wall_normal[2] * vn

    def _has_headroom(self, brush, player_min, player_max):
        # Mesh collision: use mesh bounds for broad-phase AABB check
        if brush.get('_collision_mode') == 'mesh':
            bounds = brush.get('_mesh_bounds')
            if bounds:
                min_b, max_b = bounds
                # Standard AABB overlap test using mesh bounds
                if (player_max.x > min_b[0] and player_min.x < max_b[0] and
                    player_max.y > min_b[1] and player_min.y < max_b[1] and
                    player_max.z > min_b[2] and player_min.z < max_b[2]):
                    return False  # Overlapping mesh bounds → collision
            return True  # No bounds or no overlap → pass through

        # Standard AABB check for solid world brushes.
        # PERF: cached float32 bounds (bit-identical to glm.vec3(pos) +/- size*0.5)
        # instead of constructing four throwaway glm.vec3 per brush per axis pass.
        b = brush_aabb_bounds(brush)

        if (player_max.x > b[0] and player_min.x < b[3] and
            player_max.y > b[1] and player_min.y < b[4] and
            player_max.z > b[2] and player_min.z < b[5]):
            return False
            
        return True

    def _resolve_collision(self, brushes, axis, delta, ignore_brush=None):
        """
        Resolve AABB or mesh collision by pushing the player out of overlapping brushes.
        """
        half = self._half

        for brush in brushes:
            if ignore_brush and brush is ignore_brush:
                continue

            if brush.get('hidden') or brush.get('disabled') or is_water_brush(brush) or brush.get('is_fog'):
                continue

            is_dynamic_solid = brush.get('is_mover') or brush.get('is_door')
            if brush.get('is_trigger') and not is_dynamic_solid:
                continue

            # === MESH COLLISION ===
            if brush.get('_collision_mode') == 'mesh':
                mesh_tris = brush.get('_mesh_triangles', [])
                if not mesh_tris:
                    continue
                
                # Broad-phase: check AABB first
                bounds = brush.get('_mesh_bounds')
                if bounds:
                    min_b, max_b = bounds
                    player_min = self.pos - half
                    player_max = self.pos + half
                    if (player_max.x < min_b[0] or player_min.x > max_b[0] or
                        player_max.y < min_b[1] or player_min.y > max_b[1] or
                        player_max.z < min_b[2] or player_min.z > max_b[2]):
                        continue

                # Build velocity for this frame on the current axis
                if axis == 'x':
                    vel = glm.vec3(self.velocity.x * delta, 0, 0)
                elif axis == 'z':
                    vel = glm.vec3(0, 0, self.velocity.z * delta)
                else:
                    vel = glm.vec3(0, self.velocity.y * delta, 0)
                
                # Use a small radius for the player capsule (slightly smaller than half-extents)
                radius = min(half.x, half.z) * 0.9
                
                nearest_hit = 1.0
                nearest_normal = None
                
                for tri, normal in mesh_tris:
                    # Only test triangles facing the movement direction (optimization)
                    if axis == 'y' and normal[1] <= 0.1:
                        # For ground collision, only care about upward-facing triangles
                        continue
                    
                    hit, t, hit_point, hit_normal = _intersect_swept_sphere_triangle(
                        self.pos, vel, radius, tri, normal
                    )
                    if hit and t < nearest_hit:
                        nearest_hit = t
                        nearest_normal = hit_normal
                
                if nearest_normal is not None:
                    # Push player out along the collision normal
                    penetration = radius * 0.5  # push to clear the surface
                    
                    if axis == 'x':
                        self.pos.x += nearest_normal.x * penetration
                        self.velocity.x = 0
                    elif axis == 'z':
                        self.pos.z += nearest_normal.z * penetration
                        self.velocity.z = 0
                    elif axis == 'y':
                        # For Y, we need to know if we hit from above (floor) or below (ceiling)
                        if nearest_normal.y > 0.3:  # Floor or slope
                            self.pos.y += nearest_normal.y * penetration
                            self.velocity.y = 0
                            self.on_ground = True
                            self.ground_object = brush
                        else:  # Ceiling or steep wall
                            self.pos.y += nearest_normal.y * penetration
                            if self.velocity.y > 0:
                                self.velocity.y = 0
                
                continue  # Done with this mesh brush

            # === AABB COLLISION (existing code) ===
            player_min = self.pos - half
            player_max = self.pos + half

            pos   = glm.vec3(brush['pos'])
            size  = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5

            if (player_max.x < b_min.x or player_min.x > b_max.x or
                    player_max.y < b_min.y or player_min.y > b_max.y or
                    player_max.z < b_min.z or player_min.z > b_max.z):
                continue

            # Resolve on the relevant axis
            if axis == 'x':
                dx1 = player_max.x - b_min.x
                dx2 = b_max.x - player_min.x
                if dx1 < dx2:
                    self.pos.x -= dx1 + 0.001
                else:
                    self.pos.x += dx2 + 0.001
                self.velocity.x = 0

            elif axis == 'z':
                dz1 = player_max.z - b_min.z
                dz2 = b_max.z - player_min.z
                if dz1 < dz2:
                    self.pos.z -= dz1 + 0.001
                else:
                    self.pos.z += dz2 + 0.001
                self.velocity.z = 0

            elif axis == 'y':
                dy1 = player_max.y - b_min.y
                dy2 = b_max.y - player_min.y
                if dy1 < dy2:
                    self.pos.y -= dy1 + 0.001
                    if self.velocity.y > 0:
                        self.velocity.y = 0
                else:
                    self.pos.y        += dy2
                    self.velocity.y    = 0
                    self.on_ground     = True
                    self.ground_object = brush

    def _check_overlap(self, brushes, ignore_brush=None):
        """
        Returns True if the player currently overlaps any solid brush.
        Handles both AABB and mesh collision brushes.
        """
        half       = self._half
        player_min = self.pos - half
        player_max = self.pos + half

        for brush in brushes:
            if brush.get('hidden') or brush.get('disabled') or is_water_brush(brush) or brush.get('is_fog'):
                continue

            is_dynamic_solid = brush.get('is_mover') or brush.get('is_door')
            if brush.get('is_trigger') and not is_dynamic_solid:
                continue

            if ignore_brush is not None and brush is ignore_brush:
                continue

            # Mesh collision: use bounds for broad-phase
            if brush.get('_collision_mode') == 'mesh':
                bounds = brush.get('_mesh_bounds')
                if bounds:
                    min_b, max_b = bounds
                    if (player_max.x > min_b[0] and player_min.x < max_b[0] and
                        player_max.y > min_b[1] and player_min.y < max_b[1] and
                        player_max.z > min_b[2] and player_min.z < max_b[2]):
                        # Broad-phase overlap - do narrow-phase test
                        mesh_tris = brush.get('_mesh_triangles', [])
                        for tri, normal in mesh_tris:
                            # Simple sphere vs triangle test
                            sp = (self.pos.x, self.pos.y, self.pos.z)
                            # Distance to plane
                            to_v0 = (sp[0] - tri[0][0], sp[1] - tri[0][1], sp[2] - tri[0][2])
                            dist = abs(to_v0[0]*normal[0] + to_v0[1]*normal[1] + to_v0[2]*normal[2])
                            if dist < min(half.x, half.z):
                                # Close to plane - check if point projects into triangle
                                plane_point = (
                                    sp[0] - normal[0]*dist,
                                    sp[1] - normal[1]*dist,
                                    sp[2] - normal[2]*dist,
                                )
                                if _point_in_triangle(plane_point, tri[0], tri[1], tri[2]):
                                    return True
                continue

            pos   = glm.vec3(brush['pos'])
            size  = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5

            if (player_max.x > b_min.x and player_min.x < b_max.x and
                    player_max.y > b_min.y and player_min.y < b_max.y and
                    player_max.z > b_min.z and player_min.z < b_max.z):
                return True

        return False