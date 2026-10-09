"""The player: Quake 2 movement on Fio's hull.

``Player.update`` runs the same sequence Quake 2's ``Pmove`` does each frame,
with the same rules and constants (see ``engine/constants.py``):

1. set the hull for ducking, and stand back up only where there is room;
2. find the ground (a short trace down onto a surface no steeper than ~45
   degrees) and how deep in water the player is (feet, waist, eyes);
3. with the waist under water and a ledge ahead, throw the player out;
4. jump (released-and-pressed, never within a moment of a hard landing),
   then ground and water friction;
5. accelerate toward the wished direction -- fully on the ground, barely in
   the air, at half speed in water -- add gravity, and move: sweep the box,
   slide along whatever it hits, and step up anything up to 18 units high,
   keeping whichever of the plain and the stepped move got further;
6. find the ground again for the position the move ended at.

Collision is the box trace in ``engine/pmove.py``. This is a reimplementation
of the published behaviour, not a copy of the GPL source.

Fio keeps its own hull (50 x 100, eye 90 above the feet; Quake 2's is
32 x 56 with the eye at 46) because its maps are laid out around it; the few
Quake 2 rules that are measured against the hull (the ducked height and eye,
the waterjump probes, the water sample points) are scaled to it.
"""

import math

import glm

from .constants import (
    TILE_SIZE, GRAVITY, SV_MAXVELOCITY,
    CL_FORWARDSPEED, CL_SIDESPEED, CL_UPSPEED, CL_RUN_SCALE,
    PM_STOPSPEED, PM_MAXSPEED, PM_DUCKSPEED,
    PM_ACCELERATE, PM_AIRACCELERATE, PM_WATERACCELERATE,
    PM_FRICTION, PM_WATERFRICTION, PM_WATER_WISH_SCALE, PM_WATER_DRIFT,
    PM_STEPSIZE, PM_MIN_STEP_NORMAL, PM_GROUND_NORMAL, PM_GROUND_PROBE,
    PM_AIRBORNE_SPEED, PM_NUM_BUMPS, PM_MAX_CLIP_PLANES, PM_OVERCLIP,
    PM_STOP_EPSILON,
    PM_JUMP_SPEED, PM_SWIM_JUMP_SPEED, PM_SWIM_JUMP_MAX_FALL,
    PM_LAND_SPEED, PM_HARD_LAND_SPEED, PM_LAND_TIME, PM_HARD_LAND_TIME,
    PM_WATERJUMP_UP, PM_WATERJUMP_FORWARD, PM_WATERJUMP_TIME,
    PM_WATERJUMP_SOLID_FRACTION, PM_WATERJUMP_CLEAR_FRACTION,
    PM_WATERJUMP_REACH,
    PM_DUCK_HEIGHT_FRACTION, PM_DUCK_EYE_FRACTION,
    PM_SPRINT_SCALE,
    is_solid_world_brush, is_water_brush,
)
from .pmove import BoxTracer

__all__ = ['Player', 'PM_SPRINT_SCALE']


def _blocks_player(brush):
    """Single source of truth for "does the player collide with this brush?".

    Derived once per frame in ``Player.update``; every trace the frame makes
    walks the list that produced.

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


def _clip_velocity(v, normal, overbounce):
    """Slide *v* along a plane, in place (Quake 2's PM_ClipVelocity)."""
    backoff = (v[0] * normal[0] + v[1] * normal[1] + v[2] * normal[2]) * overbounce
    for i in range(3):
        out = v[i] - normal[i] * backoff
        v[i] = 0.0 if -PM_STOP_EPSILON < out < PM_STOP_EPSILON else out


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _clamp_input(value):
    return max(-1.0, min(1.0, float(value)))


class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        # Initialize position (Y is set to 2 tiles high by default)
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle = angle
        self.pitch = 0.0

        # Walking speed (cl_forwardspeed); sprint is Quake 2's +speed and runs
        # at pm_maxspeed.
        self.speed = CL_FORWARDSPEED
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = TILE_SIZE, TILE_SIZE * 2, TILE_SIZE
        self.camera_height = 40.0  # Offset from pos.y
        # Standing hull and eye; ducking derives its own from these.
        self.stand_height = self.height
        self.stand_camera_height = self.camera_height
        self.ducked = False

        # Ground state. on_ground is Quake 2's PMF_ON_GROUND (it survives a
        # jump until the next ground check); ground_object is its
        # groundentity, the brush or terrain under the feet (movers carry it).
        self.on_ground = False
        self.ground_object = None
        self.ground_normal = None
        self.physics_enabled = physics_enabled
        self.step_height = PM_STEPSIZE

        # Water, measured at the feet, the waist and the eyes (0..3).
        self.waterlevel = 0
        self.in_water = False          # waterlevel >= 1
        self.swimming = False          # waterlevel >= 2: water movement
        self.eye_underwater = False    # camera below a water surface (drives the underwater overlay)
        self.water_surface_y = None    # world Y of the surface the player is in
        self.water_depth_frac = 0.0    # waterlevel / 3
        self.water_tint = [0.0, 0.4, 0.6]

        # Quake 2's pm_time and its timed flags, in seconds.
        self.pm_time = 0.0
        self.landing = False           # PMF_TIME_LAND: no jumping yet
        self.waterjumping = False      # PMF_TIME_WATERJUMP: no control

        # Pre-computed half-extents (changes only when ducking)
        self._half = glm.vec3(self.width / 2.0, self.height / 2.0, self.depth / 2.0)

        # PMF_JUMP_HELD: holding jump does not auto-repeat on landing.
        self._jump_held = False

        # Per-frame working state (Quake 2's pml).
        self._o = [0.0, 0.0, 0.0]
        self._v = [0.0, 0.0, 0.0]
        self._tracer = None
        self._water = ()

    def get_view_matrix(self):
        """Calculate the view matrix for rendering."""
        cam_pos = self.pos + glm.vec3(0, self.camera_height, 0)
        direction = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch),
        )
        return glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))

    # ------------------------------------------------------------------
    # Hull
    # ------------------------------------------------------------------

    def _duck_drop(self):
        """How far the centre moves when the hull shrinks from the top."""
        return (self.stand_height - self.stand_height * PM_DUCK_HEIGHT_FRACTION) * 0.5

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

    def _half_tuple(self):
        h = self._half
        return (float(h.x), float(h.y), float(h.z))

    # ------------------------------------------------------------------
    # The frame
    # ------------------------------------------------------------------

    def update(self, delta, move_input, jump, crouch, brushes, movers=None, doors=None, terrain=None,
           spatial_grid=None, sprint=False):
        """Run one Quake 2 player move.

        PERF: If spatial_grid is provided, static brush colliders are fetched
        from the grid (only nearby cells) instead of iterating every brush.
        Movers and doors are always included since they're dynamic.
        """
        delta = float(delta)

        # --- Everything that could be touched this frame ---
        if spatial_grid:
            half = self._half
            vel = self.velocity
            reach = ((math.sqrt(vel.x * vel.x + vel.y * vel.y + vel.z * vel.z)
                      + PM_MAXSPEED) * delta + PM_STEPSIZE + 32.0)
            pad = glm.vec3(reach)
            colliders = spatial_grid.get_potential_colliders(
                self.pos - half - pad, self.pos + half + pad)
        else:
            colliders = brushes
        solid = []
        for source in (colliders, movers, doors):
            if not source:
                continue
            for b in source:
                if _blocks_player(b):
                    solid.append(b)

        if spatial_grid is not None:
            water_brushes = getattr(spatial_grid, 'water_brushes', None)
            if water_brushes is None:
                water_brushes = [b for b in brushes if is_water_brush(b)]
        else:
            water_brushes = [b for b in brushes if is_water_brush(b)]
        self._water = water_brushes

        # --- Input as a Quake 2 user command ---
        run = CL_RUN_SCALE if sprint else 1.0
        fmove = _clamp_input(move_input.z) * CL_FORWARDSPEED * run
        smove = _clamp_input(move_input.x) * CL_SIDESPEED * run
        upmove = ((CL_UPSPEED if jump else 0.0) - (CL_UPSPEED if crouch else 0.0)) * run

        if not self.physics_enabled:
            self._kinematic_move(delta, fmove, smove, crouch)
            return

        self._tracer = BoxTracer(solid, terrain)
        self._o = [float(self.pos.x), float(self.pos.y), float(self.pos.z)]
        self._v = [float(self.velocity.x), float(self.velocity.y), float(self.velocity.z)]

        # Fio's movers carry but do not push, so one may have moved into us.
        if self._tracer.position_is_solid(self._o, self._half_tuple()):
            self._o = list(self._tracer.nudge_out(self._o, self._half_tuple()))

        self._check_duck(upmove)
        self._categorize_position()
        self._check_special_movement()

        if self.pm_time > 0.0:
            self.pm_time -= delta
            if self.pm_time <= 0.0:
                self._clear_timers()

        v = self._v
        if self.waterjumping:
            # No control, but falls; ends as soon as it starts coming down.
            v[1] += GRAVITY * delta
            if v[1] < 0.0:
                self._clear_timers()
            self._step_slide_move(delta)
        else:
            self._check_jump(upmove)
            self._friction(delta)
            if self.waterlevel >= 2:
                self._water_move(fmove, smove, upmove, delta)
            else:
                self._air_move(fmove, smove, delta)

        self._categorize_position()

        for i in range(3):
            v[i] = max(-SV_MAXVELOCITY, min(SV_MAXVELOCITY, v[i]))
        o = self._o
        self.pos.x, self.pos.y, self.pos.z = o
        self.velocity.x, self.velocity.y, self.velocity.z = v
        self._tracer = None

        # Floor safety clamp
        if self.pos.y < -2000:
            self.pos     = glm.vec3(0, 100, 0)
            self.velocity = glm.vec3(0, 0, 0)

    def _kinematic_move(self, delta, fmove, smove, crouch):
        """Physics off: fly level at the wished speed, through everything."""
        self._categorize_water()
        forward = (math.sin(self.angle), math.cos(self.angle))
        right = (math.cos(self.angle), -math.sin(self.angle))
        wx = forward[0] * fmove + right[0] * smove
        wz = forward[1] * fmove + right[1] * smove
        speed = math.hypot(wx, wz)
        limit = PM_DUCKSPEED if crouch else PM_MAXSPEED
        if speed > limit:
            wx *= limit / speed
            wz *= limit / speed
        self.velocity.x = wx
        self.velocity.z = wz
        self.pos += self.velocity * delta

    def _clear_timers(self):
        self.pm_time = 0.0
        self.landing = False
        self.waterjumping = False

    # ------------------------------------------------------------------
    # Tracing
    # ------------------------------------------------------------------

    def _trace(self, start, end):
        return self._tracer.trace(start, end, self._half_tuple())

    def _step_slide_move_(self, delta):
        """Move for *delta*, sliding along up to five planes (PM_StepSlideMove_)."""
        o, v = self._o, self._v
        primal = list(v)
        planes = []
        time_left = delta
        for _ in range(PM_NUM_BUMPS):
            end = (o[0] + time_left * v[0], o[1] + time_left * v[1],
                   o[2] + time_left * v[2])
            tr = self._trace(o, end)
            if tr.allsolid:
                v[1] = 0.0      # trapped in a solid
                return
            if tr.fraction > 0.0:
                o[:] = tr.endpos
                planes = []
            if tr.fraction == 1.0:
                break
            time_left -= time_left * tr.fraction

            if len(planes) >= PM_MAX_CLIP_PLANES:
                v[:] = (0.0, 0.0, 0.0)
                break
            planes.append(tr.normal)

            # Make the velocity run parallel to every plane touched.
            for i, plane in enumerate(planes):
                _clip_velocity(v, plane, PM_OVERCLIP)
                if all(_dot(v, other) >= 0.0
                       for j, other in enumerate(planes) if j != i):
                    break
            else:
                # Along the crease of exactly two planes, or not at all.
                if len(planes) != 2:
                    v[:] = (0.0, 0.0, 0.0)
                    break
                a, b = planes
                crease = (a[1] * b[2] - a[2] * b[1],
                          a[2] * b[0] - a[0] * b[2],
                          a[0] * b[1] - a[1] * b[0])
                d = _dot(crease, v)
                v[:] = (crease[0] * d, crease[1] * d, crease[2] * d)

            # Turned back on itself: stop dead rather than jitter in a corner.
            if _dot(v, primal) <= 0.0:
                v[:] = (0.0, 0.0, 0.0)
                break

        if self.pm_time > 0.0:
            v[:] = primal

    def _step_slide_move(self, delta):
        """Slide, and also try the move from one step up (PM_StepSlideMove).

        Whichever of the two got further across the floor wins; the stepped
        try is pressed back down by up to a step at the end. Stepping happens in
        the air too, which is how a jump lands on a ledge a step above its peak.
        """
        o, v = self._o, self._v
        start_o = list(o)
        start_v = list(v)

        self._step_slide_move_(delta)
        down_o = list(o)
        down_v = list(v)

        up = (start_o[0], start_o[1] + PM_STEPSIZE, start_o[2])
        if self._trace(up, up).allsolid:
            return              # no room to step up

        o[:] = up
        v[:] = start_v
        self._step_slide_move_(delta)

        down = (o[0], o[1] - PM_STEPSIZE, o[2])
        tr = self._trace(o, down)
        if not tr.allsolid:
            o[:] = tr.endpos

        down_dist = (down_o[0] - start_o[0]) ** 2 + (down_o[2] - start_o[2]) ** 2
        up_dist = (o[0] - start_o[0]) ** 2 + (o[2] - start_o[2]) ** 2
        if down_dist > up_dist or tr.normal[1] < PM_MIN_STEP_NORMAL:
            o[:] = down_o
            v[:] = down_v
            return
        # Walking along a plane: keep the vertical speed the plain move had.
        v[1] = down_v[1]

    # ------------------------------------------------------------------
    # Position, ground and water
    # ------------------------------------------------------------------

    def _categorize_position(self):
        """Ground and water state for the current position (PM_CatagorizePosition)."""
        o, v = self._o, self._v
        if v[1] > PM_AIRBORNE_SPEED:
            # Rising this fast (a jump, a ramp launch) is not standing.
            self.on_ground = False
            self.ground_object = None
            self.ground_normal = None
        else:
            tr = self._trace(o, (o[0], o[1] - PM_GROUND_PROBE, o[2]))
            grounded = tr.startsolid or (
                tr.ent is not None and tr.normal[1] >= PM_GROUND_NORMAL)
            if not grounded:
                self.on_ground = False
                self.ground_object = None
                self.ground_normal = None
            else:
                self.ground_object = tr.ent
                self.ground_normal = tr.normal
                if self.waterjumping:
                    self._clear_timers()    # solid ground ends a waterjump
                if not self.on_ground:
                    self.on_ground = True
                    # Coming down a slope is not a landing.
                    if v[1] < PM_LAND_SPEED:
                        self.landing = True
                        self.pm_time = (PM_HARD_LAND_TIME if v[1] < PM_HARD_LAND_SPEED
                                        else PM_LAND_TIME)
        self._categorize_water()

    def _water_at(self, x, y, z):
        """The water brush containing the point, or None."""
        for brush in self._water:
            if brush.get('hidden'):
                continue
            bpos = brush['pos']
            bsize = brush['size']
            if (abs(x - bpos[0]) <= bsize[0] * 0.5 and abs(y - bpos[1]) <= bsize[1] * 0.5
                    and abs(z - bpos[2]) <= bsize[2] * 0.5):
                return brush
        return None

    def _categorize_water(self):
        """Water level from three samples: feet, waist and eyes."""
        if self.physics_enabled and self._tracer is not None:
            x, y, z = self._o
        else:
            x, y, z = float(self.pos.x), float(self.pos.y), float(self.pos.z)
        feet = y - float(self._half.y)
        eye = float(self._half.y) + self.camera_height      # eye above the feet
        level = 0
        surface = self._water_at(x, feet + 1.0, z)
        eye_water = self._water_at(x, feet + eye, z)
        if surface is not None:
            level = 1
            if self._water_at(x, feet + eye * 0.5, z) is not None:
                level = 3 if eye_water is not None else 2
        self.waterlevel = level
        self.in_water = level >= 1
        self.swimming = level >= 2
        self.water_depth_frac = level / 3.0
        self.eye_underwater = eye_water is not None
        if surface is not None:
            self.water_surface_y = surface['pos'][1] + surface['size'][1] * 0.5
            tint = surface.get('water_tint')
            if tint is not None:
                self.water_tint = list(tint)
        else:
            self.water_surface_y = None

    # ------------------------------------------------------------------
    # Special movement
    # ------------------------------------------------------------------

    def _check_duck(self, upmove):
        """Duck on the ground with crouch held; stand up only where there is room."""
        if upmove < 0.0 and self.on_ground:
            if not self.ducked:
                self._o[1] -= self._duck_drop()
                self.set_ducked(True)
        elif self.ducked:
            o = self._o
            standing = (o[0], o[1] + self._duck_drop(), o[2])
            stand_half = (self.width / 2.0, self.stand_height / 2.0, self.depth / 2.0)
            if not self._tracer.position_is_solid(standing, stand_half):
                o[1] = standing[1]
                self.set_ducked(False)

    def _check_special_movement(self):
        """Throw the player out of water onto a ledge ahead (the waterjump).

        Quake 2 also finds ladders here; Fio has no ladder contents.
        """
        if self.pm_time > 0.0 or self.waterlevel != 2:
            return
        fx, fz = math.sin(self.angle), math.cos(self.angle)
        o = self._o
        reach = float(self._half.x) + PM_WATERJUMP_REACH
        feet = o[1] - float(self._half.y)
        eye = float(self._half.y) + self.camera_height
        x = o[0] + fx * reach
        z = o[2] + fz * reach
        if not self._tracer.point_is_solid((x, feet + eye * PM_WATERJUMP_SOLID_FRACTION, z)):
            return
        top = feet + eye * PM_WATERJUMP_CLEAR_FRACTION
        if self._tracer.point_is_solid((x, top, z)) or self._water_at(x, top, z) is not None:
            return
        v = self._v
        v[0] = fx * PM_WATERJUMP_FORWARD
        v[1] = PM_WATERJUMP_UP
        v[2] = fz * PM_WATERJUMP_FORWARD
        self.waterjumping = True
        self.pm_time = PM_WATERJUMP_TIME

    def _check_jump(self, upmove):
        """Jump, or swim up (PM_CheckJump)."""
        if self.landing:
            return              # too soon after a hard landing
        if upmove < 10.0:
            self._jump_held = False
            return
        if self._jump_held:
            return              # must release jump first
        v = self._v
        if self.waterlevel >= 2:
            # Swimming, not jumping: held jump keeps the player rising.
            self.ground_object = None
            if v[1] <= PM_SWIM_JUMP_MAX_FALL:
                return
            v[1] = PM_SWIM_JUMP_SPEED
            return
        if self.ground_object is None:
            return              # in the air
        self._jump_held = True
        self.ground_object = None
        v[1] += PM_JUMP_SPEED
        if v[1] < PM_JUMP_SPEED:
            v[1] = PM_JUMP_SPEED

    # ------------------------------------------------------------------
    # Friction, acceleration and the moves
    # ------------------------------------------------------------------

    def _friction(self, delta):
        """Ground friction and water friction (PM_Friction)."""
        v = self._v
        speed = math.sqrt(_dot(v, v))
        if speed < 1.0:
            v[0] = 0.0
            v[2] = 0.0
            return
        drop = 0.0
        if self.ground_object is not None:
            control = PM_STOPSPEED if speed < PM_STOPSPEED else speed
            drop += control * PM_FRICTION * delta
        if self.waterlevel:
            drop += speed * PM_WATERFRICTION * self.waterlevel * delta
        scale = max(speed - drop, 0.0) / speed
        v[0] *= scale
        v[1] *= scale
        v[2] *= scale

    def _accelerate(self, wishdir, wishspeed, accel, delta):
        """Add speed toward *wishdir*, up to *wishspeed* (PM_Accelerate)."""
        v = self._v
        addspeed = wishspeed - _dot(v, wishdir)
        if addspeed <= 0.0:
            return
        accelspeed = min(accel * delta * wishspeed, addspeed)
        v[0] += accelspeed * wishdir[0]
        v[1] += accelspeed * wishdir[1]
        v[2] += accelspeed * wishdir[2]

    def _air_move(self, fmove, smove, delta):
        """Walking and falling (PM_AirMove).

        The wish is level; looking up or down shortens it as Quake 2's
        one-third-pitch forward vector does.
        """
        pitch_scale = math.cos(self.pitch / 3.0)
        sin_a, cos_a = math.sin(self.angle), math.cos(self.angle)
        wx = sin_a * pitch_scale * fmove + cos_a * smove
        wz = cos_a * pitch_scale * fmove - sin_a * smove
        wishspeed = math.hypot(wx, wz)
        wishdir = (wx / wishspeed, 0.0, wz / wishspeed) if wishspeed else (0.0, 0.0, 0.0)
        maxspeed = PM_DUCKSPEED if self.ducked else PM_MAXSPEED
        if wishspeed > maxspeed:
            wishspeed = maxspeed

        v = self._v
        if self.ground_object is not None:
            v[1] = 0.0
            self._accelerate(wishdir, wishspeed, PM_ACCELERATE, delta)
            v[1] = 0.0
            if not v[0] and not v[2]:
                return
            self._step_slide_move(delta)
        else:
            self._accelerate(wishdir, wishspeed, PM_AIRACCELERATE, delta)
            v[1] += GRAVITY * delta
            self._step_slide_move(delta)

    def _water_move(self, fmove, smove, upmove, delta):
        """Swimming: along the view, at half speed, no gravity (PM_WaterMove)."""
        cos_p = math.cos(self.pitch)
        sin_a, cos_a = math.sin(self.angle), math.cos(self.angle)
        wx = sin_a * cos_p * fmove + cos_a * smove
        wy = math.sin(self.pitch) * fmove
        wz = cos_a * cos_p * fmove - sin_a * smove
        if not fmove and not smove and not upmove:
            wy -= PM_WATER_DRIFT        # drift towards the bottom
        else:
            wy += upmove
        wishspeed = math.sqrt(wx * wx + wy * wy + wz * wz)
        if wishspeed:
            wishdir = (wx / wishspeed, wy / wishspeed, wz / wishspeed)
        else:
            wishdir = (0.0, 0.0, 0.0)
        if wishspeed > PM_MAXSPEED:
            wishspeed = PM_MAXSPEED
        wishspeed *= PM_WATER_WISH_SCALE
        self._accelerate(wishdir, wishspeed, PM_WATERACCELERATE, delta)
        self._step_slide_move(delta)
