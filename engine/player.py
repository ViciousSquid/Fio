"""The player controller: Fio's collision, Quake 2's movement rules.

Collision is Fio's own -- the spatial grid's colliders, per-axis box
resolution with the stair lip, capsule depenetration against angled brushes,
terrain, and Fio's water. Gravity comes from the live PhysicsWorld, the one
authority (``engine.physics.world_gravity``; Quake 2's 800 by default).

On top of that, movement follows Quake 2 (qcommon/pmove.c, cl_input.c):
200 walk / 300 run (sprint is +speed) / 100 ducked, with diagonal input
adding up as Quake 2's does; ground acceleration 10 and friction 6 with stop
speed 100, air acceleration 1, the wish shortened by a third of the view
pitch; a 270 jump that must be released between jumps and is locked out
briefly after a hard landing; stepping up 18 units in the air as well as on
the ground; ducking that halves the hull; and ground no steeper than ~45
degrees.
"""

import math
import glm
from .constants import (
    TILE_SIZE, JUMP_STRENGTH, TERMINAL_VELOCITY,
    CL_FORWARDSPEED, CL_SIDESPEED, CL_RUN_SCALE,
    PM_STOPSPEED, PM_MAXSPEED, PM_DUCKSPEED, PM_ACCELERATE, PM_AIRACCELERATE,
    PM_FRICTION, PM_GROUND_NORMAL,
    PM_LAND_SPEED, PM_HARD_LAND_SPEED, PM_LAND_TIME, PM_HARD_LAND_TIME,
    PM_DUCK_HEIGHT_FRACTION, PM_DUCK_EYE_FRACTION,
    PM_SPRINT_SCALE,
    WATER_SWIM_SPEED_MULT, WATER_VERTICAL_SPEED_MULT, WATER_DRAG,
    WATER_WADE_SPEED_MULT, WATER_MAX_SINK_SPEED,
    WATERJUMP_MAX_CLIMB, WATERJUMP_EDGE_ABOVE_SURFACE, WATERJUMP_MAX_BOOST,
    is_solid_world_brush, is_water_brush, brush_aabb_bounds,
)
from .physics import world_gravity


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

# =============================================================================
# Player Class
# =============================================================================

def _blocks_player(brush):
    """Single source of truth for "does the player collide with this brush?".

    Every collision pass the player runs — horizontal sweep, step-up, vertical
    resolve, capsule depenetration, waterjump probe — used to re-derive this per
    brush, so a brush near the player was classified five or six times a frame,
    ``is_water_brush`` (which lower-cases every face texture) included.  It is
    derived once per frame now, in ``Player.update``, and the passes just walk
    the list they are handed.

    The expensive water test goes last deliberately: ``SpatialGrid.populate``
    already keeps water, fog, non-dynamic triggers and physics bodies out of the
    grid, so on the normal path the only brushes that reach it are the movers
    and doors appended afterwards.  The flags *are* still tested here, because
    ``hidden``/``disabled`` are runtime state the grid cannot filter on (it
    indexes by ``authored_hidden`` precisely so a streamed-out brush can come
    back), and because the no-grid fallback path hands over the raw brush list.
    """
    if brush.get('_physics_body'):
        # Simulated by PhysicsWorld as a dynamic body, not a static wall.
        return False
    if brush.get('disabled'):
        return False
    return is_solid_world_brush(brush)


class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        # Initialize position (Y is set to 2 tiles high by default)
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle = angle
        self.pitch = 0.0

        # Walking speed (Quake 2's cl_forwardspeed); sprint runs at PM_MAXSPEED.
        self.speed = CL_FORWARDSPEED
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = TILE_SIZE, TILE_SIZE * 2, TILE_SIZE
        self.camera_height = 40.0  # Offset from pos.y
        # The standing hull and eye; ducking derives its own from these.
        self.stand_height = self.height
        self.stand_camera_height = self.camera_height
        self.ducked = False
        # Quake 2's landing lockout: no jumping for a moment after a hard landing.
        self.landing = False
        self.pm_time = 0.0

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

        # Pre-computed half-extents (they change only when ducking)
        self._half = glm.vec3(self.width / 2.0, self.height / 2.0, self.depth / 2.0)

        # Quake 2's jump latch: set only by a jump, so holding jump does not
        # repeat on landing, but a press made in the air jumps on landing.
        self._jump_held = False
        self._gravity = world_gravity()

    def _duck_drop(self):
        """How far the centre moves when the hull shrinks from the top."""
        return self.stand_height * (1.0 - PM_DUCK_HEIGHT_FRACTION) * 0.5

    def set_ducked(self, ducked):
        """Set the hull and eye for ducking, leaving the centre where it is."""
        ducked = bool(ducked)
        stand_eye = self.stand_height * 0.5 + self.stand_camera_height
        if ducked:
            height = self.stand_height * PM_DUCK_HEIGHT_FRACTION
            self.camera_height = stand_eye * PM_DUCK_EYE_FRACTION - height * 0.5
        else:
            height = self.stand_height
            self.camera_height = self.stand_camera_height
        self.ducked = ducked
        self.height = height
        self._half = glm.vec3(self.width / 2.0, height / 2.0, self.depth / 2.0)

    def _check_duck(self, crouch, aabb_brushes):
        """Duck on the ground with crouch held; stand up only where there is room."""
        if crouch and self.on_ground:
            if not self.ducked:
                self.pos.y -= self._duck_drop()
                self.set_ducked(True)
            return
        if not self.ducked:
            return
        lift = self._duck_drop()
        half = glm.vec3(self.width / 2.0, self.stand_height / 2.0, self.depth / 2.0)
        centre = glm.vec3(self.pos.x, self.pos.y + lift, self.pos.z)
        lo, hi = centre - half, centre + half
        if all(self._has_headroom(b, lo, hi) for b in aabb_brushes):
            self.pos.y += lift
            self.set_ducked(False)

    def get_view_matrix(self):
        """Calculate the view matrix for rendering."""
        cam_pos = self.pos + glm.vec3(0, self.camera_height, 0)
        direction = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch),
        )
        return glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))

    def update(self, delta, move_input, jump, crouch, brushes, movers=None, doors=None, terrain=None,
           spatial_grid=None, sprint=False, gravity=None):
        """
        Update player physics.

        *gravity* is the live PhysicsWorld's (``engine.physics.world_gravity``);
        None means that world's default.

        PERF: If spatial_grid is provided, static brush colliders are fetched
        from the grid (only nearby cells) instead of iterating every brush.
        Movers and doors are always included since they're dynamic.
        """
        if gravity is None:
            gravity = world_gravity()
        self._gravity = gravity
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

        # One pass over everything that could collide this frame: drop what the
        # player does not collide with at all (see _blocks_player) and split the
        # rest by collision mode.  Movers and doors are always included since
        # they're dynamic and the static grid never sees them.
        mesh_brushes = []
        aabb_brushes = []
        for source in (colliders, movers, doors):
            if not source:
                continue
            for b in source:
                if not _blocks_player(b):
                    continue
                if b.get('_collision_mode') == 'mesh':
                    mesh_brushes.append(b)
                else:
                    aabb_brushes.append(b)

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
        if self.pm_time > 0.0:
            self.pm_time -= delta
            if self.pm_time <= 0.0:
                self.pm_time = 0.0
                self.landing = False

        # --- 1. Movement Physics ---

        forward_vec = glm.vec3(math.sin(self.angle), 0, math.cos(self.angle))
        right_vec   = glm.vec3(math.cos(self.angle), 0, -math.sin(self.angle))

        # The wish as Quake 2 builds it: each key asks for 200 (400 with
        # +speed), so diagonals add up; the forward part shortens by a third
        # of the view pitch; then it is clamped to 300 (100 ducked).
        run = CL_RUN_SCALE if sprint else 1.0
        fmove = max(-1.0, min(1.0, float(move_input.z))) * CL_FORWARDSPEED * run
        smove = max(-1.0, min(1.0, float(move_input.x))) * CL_SIDESPEED * run
        wish = (forward_vec * (fmove * math.cos(self.pitch / 3.0))
                + right_vec * smove)
        wish_speed = glm.length(wish)
        wish_dir = wish / wish_speed if wish_speed > 0.0 else glm.vec3(0.0)
        wish_speed = min(wish_speed, PM_DUCKSPEED if self.ducked else PM_MAXSPEED)

        swimming = self.swimming and self.physics_enabled

        # Preserve the existing kinematic/no-physics path.
        if not self.physics_enabled:
            self._jump_held = bool(jump)
            target_speed = min(wish_speed, PM_DUCKSPEED) if crouch else wish_speed
            if self.in_water:
                target_speed *= WATER_WADE_SPEED_MULT
            self.velocity.x = wish_dir.x * target_speed
            self.velocity.z = wish_dir.z * target_speed
            self.pos += self.velocity * delta
            return

        self._check_duck(crouch, aabb_brushes + mesh_brushes)

        if swimming:
            self._apply_swim_physics(delta, move_input, right_vec, jump, crouch)
        else:
            if self.in_water:
                wish_speed *= WATER_WADE_SPEED_MULT

            # Ground friction and projected acceleration; weak air
            # acceleration keeps momentum and allows strafing.
            if self.on_ground:
                self._apply_move_friction(delta)
                self._accelerate(wish_dir, wish_speed, PM_ACCELERATE, delta)
            else:
                self._accelerate(wish_dir, wish_speed, PM_AIRACCELERATE, delta)

        # --- 2. Jumping & gravity (gravity suspended while swimming) ---

        if not swimming:
            self._check_jump(jump)

            self.velocity.y += gravity * delta

            if self.velocity.y < TERMINAL_VELOCITY:
                self.velocity.y = TERMINAL_VELOCITY

            # Wading: water resistance breaks a fall almost immediately
            if self.in_water and self.velocity.y < WATER_MAX_SINK_SPEED:
                self.velocity.y = WATER_MAX_SINK_SPEED
        elif not jump:
            self._jump_held = False

        # --- 2b. Waterjump: climbing out of pools ---
        # Holding jump while pushing against a wall whose top edge is near the
        # waterline launches the player hard enough to actually clear it.
        # Works while wading AND swimming; without it, any pool whose rim is
        # taller than a normal jump becomes an inescapable trap.
        if self.in_water and jump:
            self._try_water_jump(aabb_brushes, wish_dir)

        # --- 3. Collision Resolution ---

        was_on_ground = self.on_ground
        fall_speed = float(self.velocity.y)

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

        # A landing faster than 200 u/s holds off the next jump briefly.
        if self.on_ground and not was_on_ground and fall_speed < PM_LAND_SPEED:
            self.landing = True
            self.pm_time = (PM_HARD_LAND_TIME if fall_speed < PM_HARD_LAND_SPEED
                            else PM_LAND_TIME)

        # Floor safety clamp
        if self.pos.y < -2000:
            self.pos     = glm.vec3(0, 100, 0)
            self.velocity = glm.vec3(0, 0, 0)

    # ------------------------------------------------------------------
    # Quake-style ground / air movement
    # ------------------------------------------------------------------

    def _check_jump(self, jump):
        """Quake 2's jump: released between jumps, never just after a hard landing."""
        if self.landing:
            return
        if not jump:
            self._jump_held = False
            return
        if self._jump_held or not self.on_ground:
            return
        self._jump_held = True
        self.velocity.y = max(self.velocity.y + JUMP_STRENGTH, JUMP_STRENGTH)
        self.on_ground = False
        self.ground_object = None

    def _apply_move_friction(self, delta):
        """Quake 2 ground friction on horizontal velocity (6, stop speed 100)."""
        speed = math.hypot(float(self.velocity.x), float(self.velocity.z))
        if speed < 1.0:
            self.velocity.x = 0.0
            self.velocity.z = 0.0
            return

        control = max(speed, PM_STOPSPEED)
        drop = control * PM_FRICTION * delta
        newspeed = max(speed - drop, 0.0)

        if newspeed == 0.0:
            self.velocity.x = 0.0
            self.velocity.z = 0.0
            return

        scale = newspeed / speed
        self.velocity.x *= scale
        self.velocity.z *= scale

    def _accelerate(self, wish_dir, wish_speed, accelerate, delta):
        """Quake 2's projected acceleration step."""
        if wish_speed <= 0.0 or glm.length(wish_dir) < 0.0001:
            return

        current_speed = (
            float(self.velocity.x) * float(wish_dir.x)
            + float(self.velocity.z) * float(wish_dir.z)
        )
        add_speed = wish_speed - current_speed
        if add_speed <= 0.0:
            return

        accel_speed = min(accelerate * delta * wish_speed, add_speed)
        self.velocity.x += accel_speed * wish_dir.x
        self.velocity.z += accel_speed * wish_dir.z

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

    def _try_water_jump(self, aabb_brushes, wish_dir):
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
            for brush in aabb_brushes:
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
            for brush in aabb_brushes:
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
            self.velocity.y = min(math.sqrt(2.0 * abs(self._gravity) * rise), WATERJUMP_MAX_BOOST)
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
        # Quake 2 steps in the air too (how a jump lands on a ledge a step
        # above its peak); never while swimming.
        if self.swimming:
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

            if push_n[1] >= PM_GROUND_NORMAL:
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
        """Push the player out of any overlapping brush on one axis.

        Takes brushes the caller has already filtered with ``_blocks_player``.
        """
        half = self._half

        for brush in brushes:
            if ignore_brush and brush is ignore_brush:
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

            # === AABB COLLISION ===
            player_min = self.pos - half
            player_max = self.pos + half

            # PERF: cached float32 bounds (bit-identical to glm.vec3(pos) +/-
            # size*0.5) instead of four throwaway glm.vec3 per brush per axis
            # pass — the same cache _has_headroom already reads.
            b = brush_aabb_bounds(brush)

            if (player_max.x < b[0] or player_min.x > b[3] or
                    player_max.y < b[1] or player_min.y > b[4] or
                    player_max.z < b[2] or player_min.z > b[5]):
                continue

            # Resolve on the relevant axis
            if axis == 'x':
                dx1 = player_max.x - b[0]
                dx2 = b[3] - player_min.x
                if dx1 < dx2:
                    self.pos.x -= dx1 + 0.001
                else:
                    self.pos.x += dx2 + 0.001
                self.velocity.x = 0

            elif axis == 'z':
                dz1 = player_max.z - b[2]
                dz2 = b[5] - player_min.z
                if dz1 < dz2:
                    self.pos.z -= dz1 + 0.001
                else:
                    self.pos.z += dz2 + 0.001
                self.velocity.z = 0

            elif axis == 'y':
                dy1 = player_max.y - b[1]
                dy2 = b[4] - player_min.y
                if dy1 < dy2:
                    self.pos.y -= dy1 + 0.001
                    if self.velocity.y > 0:
                        self.velocity.y = 0
                else:
                    self.pos.y        += dy2
                    self.velocity.y    = 0
                    self.on_ground     = True
                    self.ground_object = brush
