"""
Monster AI – all enemy behaviour, patrol logic, sight, shooting, and physics.

PERF: All brush collision/raycast methods delegate to SpatialGrid,
reducing per-monster cost from O(all_brushes) to O(nearby_brushes).
"""

import threading
import time
import glm
import math

import numpy as np
from typing import Dict, List, Any, Optional
# The debug console is a Qt widget and lives in the editor package; the AI only
# wants somewhere to write a line.  Guarded exactly like the rest of the engine
# (see logic_thread) so the AI still runs - and is still testable - in the
# head-less player, where neither the editor package nor PyQt5 exists.
try:
    from editor.debug_console import debug_log
except ImportError:  # pragma: no cover - exercised by the head-less player
    def debug_log(category, message):
        print(f"[{category}] {message}")
from .change_journal import JOURNAL, STATE, set_positions, touch
from . import monster_table
from .spatial import TIER_DORMANT, tier_of
from .constants import is_solid_world_brush
from .monster_constants import (
    MONSTER_SIGHT_RANGE,
    MONSTER_SHOOT_INTERVAL,
    MONSTER_SHOOT_ANIM_TIME,
    MONSTER_MOVE_SPEED,
    MONSTER_STOP_DISTANCE,
    MONSTER_GRAVITY,
    MONSTER_TERMINAL_VEL,
    MONSTER_WALL_MARGIN,
    MONSTER_STUCK_THRESHOLD,
    MONSTER_DETOUR_RANGE,
    MONSTER_SHOOT_SOUNDS,
    MONSTER_SHOOT_SOUND_DEFAULT,
    MONSTER_BITE_DISTANCE,
    MONSTER_BITE_DAMAGE_MULT,
)

try:
    from editor.things import PathNode, Monster as MonsterThing
except ImportError:
    PathNode = None
    MonsterThing = None


_NEAREST_ENEMY_MONOLITHIC_SMALL_COUNT = 64
_NEAREST_ENEMY_MONOLITHIC_MIN_TEAMS = 8
_NEAREST_ENEMY_MONOLITHIC_TEAM_RATIO = 0.25
_NEAREST_ENEMY_MONOLITHIC_MAX_COUNT = 1024


def _flatten_to_ground(direction):
    """Return a unit horizontal direction in the XZ plane, or ``None``.

Ground monsters ignore Y when moving.  A target directly above or below the
monster produces a zero horizontal vector; normalising that vector would
produce NaNs that can poison the spatial collision queries.  ``None`` makes
the caller leave the monster in place for that tick.
"""
    flat = glm.vec3(direction.x, 0.0, direction.z)
    length = glm.length(flat)
    if length < 1e-6:
        return None
    return flat / length


def _set_render_flag(thing, key, value):
    """Write a monster property its sprite is resolved from.

    The AI writes these every tick; the render projection is told only when
    the value actually changes, so a monster that keeps shooting costs no
    per-frame re-resolve.
    """
    props = thing.properties
    if props.get(key) != value:
        props[key] = value
        touch(thing)


class MonsterAI:
    """Handles all monster AI updates, patrol, sight, combat, and debug visualisation."""

    def __init__(self, logic_thread):
        self.lt = logic_thread                     # parent LogicThread
        self.monster_states: Dict[int, Dict[str, Any]] = {}
        self._debug_rays: List[Dict[str, Any]] = []   # for F7 debug lines
        self.monster_debug_active = False
        self._grid = None                          # SpatialGrid, set by LogicThread
        #: The dense projection the tick computes over; see _update_dense.
        self.table = monster_table.MonsterTable()
        self._sight_changed = np.zeros(0, dtype=bool)

        # Nearest-enemy batch. Every awake teamed monster without an aggro
        # target used to walk every monster, so the search was O(N^2) Python --
        # 22 ms per tick at 240 monsters. It is one dense pass now; see
        # _enemy_batch(). Invalidated at the top of every update and rebuilt on
        # the first query of the tick, so a map with no teams never builds it.
        self._enemy_rows = {}          # id(monster) -> row
        self._enemy_answered = np.zeros(0, dtype=bool)  # rows the batch answered
        self._enemy_monsters = ()      # row -> monster
        self._enemy_team_codes = {}    # team string -> MonsterTable team code
        self._enemy_nearest = None     # row -> nearest enemy row, or -1
        self._enemy_range = None       # the range the batch was built for
        self._enemy_ready = False      # has this tick's batch been attempted
        self._enemy_signature = None   # direct-helper validity signature
        self._enemy_pos = np.empty((0, 3), dtype=np.float64)
        # Camera-fitted Big World only (see _view_rect): this tick's resident
        # monsters, the off-screen cadence, and the time each monster sitting
        # a tick out is owed (id(monster) -> (monster, seconds)).
        self._tick_monsters = None
        self._offscreen_accum = 0.0
        self._owed = {}

    def set_spatial_grid(self, grid):
        """Called by LogicThread after populating the grid."""
        self._grid = grid

    def forget_monsters(self):
        """Drop every reference a finished session left to its monsters.

        The dense table and the nearest-enemy batch hold the monster objects
        (and their property dicts) until the next tick rebuilds them -- which,
        after Stop, is the next Play, possibly on another map. Debug Tables
        also showed that stale table as if it were live.
        """
        self.monster_states = {}
        self.table.gather(())
        self.table.path = 'not run'
        self.table.phase_ms = {}
        self.table.rays_cast = self.table.python_rows = 0
        self._enemy_rows = {}
        self._enemy_monsters = ()
        self._enemy_team_codes = {}
        self._enemy_nearest = None
        self._enemy_ready = False
        self._enemy_signature = None
        self._tick_monsters = None
        self._offscreen_accum = 0.0
        self._owed = {}
        self._debug_rays.clear()

    # -------------------------------------------------------------------------
    # Main update entry point
    # -------------------------------------------------------------------------

    def update(self, delta: float):
        """Called every tick from LogicThread._tick_play_mode."""
        if not self.lt.player or not MonsterThing:
            return

        if self.lt.player_dead:
            return

        player_pos = self.lt.player.pos
        self._debug_rays.clear()
        # The batch describes one tick. Dropping it here rather than building
        # it means a map with no teams never pays for one.
        self._enemy_ready = False
        self._enemy_nearest = None
        self._enemy_signature = None

        # PERF: iterate the precomputed monster list instead of isinstance-
        # scanning every brush/thing in the level every tick.
        monster_things = self.lt._monster_things
        # Fitted to an overhead camera, the pass and the enemy searches use
        # the resident monsters only: a parked (DORMANT) monster is hidden and
        # disabled, neither acts nor can be a target, so it is left out rather
        # than gathered and skipped every tick. Otherwise nothing new is kept
        # and they read the logic thread's list exactly as before.
        rect = self._view_rect()
        if rect is not None:
            monster_things = self._tick_monsters = [
                m for m in monster_things if tier_of(m) != TIER_DORMANT]
        else:
            self._tick_monsters = None

        path = self._fallback_reason()
        self.table.path = path
        row_delta, sit_out = self._offscreen_rows(monster_things, delta, player_pos, rect)
        if path == 'dense':
            self._update_dense(monster_things, delta if row_delta is None else row_delta,
                               player_pos, sit_out)
        else:
            # The table describes the dense pass; say it did not run.
            self.table.rows_read = 0
            for i, thing in enumerate(monster_things):
                if row_delta is None:
                    self._update_monster(thing, delta, player_pos)
                elif not sit_out[i]:
                    self._update_monster(thing, float(row_delta[i]), player_pos)

        # ---- Player death check (after all monsters processed) ----
        if self.lt.player_health <= 0 and not self.lt.player_dead:
            self.lt.player_dead = True
            if self.lt.io_manager:
                try:
                    from editor.things import PlayerStart
                    for thing in self.lt.editor_state.things:
                        if isinstance(thing, PlayerStart):
                            self.lt.io_manager.fire_output(thing, 'OnPlayerDeath')
                            break
                except ImportError:
                    pass
            debug_log("MonsterAI", "Player has died.")

    #: Set False to run every monster through the per-monster path (the
    #: reference the dense pass is tested against).
    DENSE_UPDATE = True

    def _view_rect(self):
        """Return the overhead simulation rectangle ``(hx, hz)`` or ``None``.

Big World publishes ``sim_view_rect`` only for the fitted overhead camera.
First-person sessions return ``None``, preserving the normal all-monster
tick path.
"""
        if getattr(self.lt, '_bigworld', None) is None:
            return None
        rect = getattr(self.lt, 'sim_view_rect', None)
        return rect if rect else None

    #: Seconds between AI passes for monsters off screen (see
    #: :meth:`_offscreen_rows`).
    OFFSCREEN_INTERVAL = 0.2

    def _offscreen_rows(self, monsters, delta, player_pos, rect):
        """Return per-row elapsed time and the rows to skip this tick.

This throttling is active only for a fitted overhead view.  Off-screen
monsters accumulate elapsed time and run every ``OFFSCREEN_INTERVAL``;
visible monsters continue at the normal tick rate.  A delayed row receives
all accumulated time when it runs, so simulation time is not lost.
"""
        if rect is None or not monsters:
            self._offscreen_accum = 0.0
            self._owed = {}
            return None, None
        n = len(monsters)
        xz = np.array([(m.pos[0], m.pos[2]) for m in monsters], dtype=np.float64)
        off = ((np.abs(xz[:, 0] - float(player_pos[0])) > float(rect[0]))
               | (np.abs(xz[:, 1] - float(player_pos[2])) > float(rect[1])))
        prev = self._owed               # id(monster) -> (monster, seconds)

        def _owed(m):
            entry = prev.get(id(m))
            return entry[1] if entry is not None and entry[0] is m else 0.0
        owed = (np.fromiter(map(_owed, monsters), dtype=np.float64, count=n)
                if prev else np.zeros(n))
        # The epsilon keeps a whole number of fixed ticks on the interval: six
        # 1/30 s ticks sum to just under 0.2 in floating point.
        self._offscreen_accum += delta
        due = self._offscreen_accum >= self.OFFSCREEN_INTERVAL - 1e-9
        if due:
            self._offscreen_accum = 0.0
        sit_out = off & (not due)
        if not sit_out.any() and not owed.any():
            self._owed = {}
            return None, None
        row_delta = owed + delta
        # The entry holds the monster itself, so its id cannot be reused by
        # another object while the time is owed.
        self._owed = {id(monsters[i]): (monsters[i], float(row_delta[i]))
                      for i in np.flatnonzero(sit_out).tolist()}
        return row_delta, sit_out

    def _fallback_reason(self):
        """``'dense'``, or why this tick runs the per-monster path (Debug Tables).

        The dense pass needs the spatial grid (its batched ground and wall
        queries), and it leaves two whole-tick modes to the per-monster path:
        ``notarget`` (a cheat, where every monster only patrols) and the F7
        debug view (which draws every monster's sight ray).
        """
        if not self.DENSE_UPDATE:
            return 'per-monster (DENSE_UPDATE off)'
        if self._grid is None:
            return 'per-monster (no spatial grid)'
        if self.lt.notarget:
            return 'per-monster (notarget)'
        if self.monster_debug_active:
            return 'per-monster (F7 debug view)'
        return 'dense'

    def _update_dense(self, monsters, delta: float, player_pos, sit_out=None):
        """Every monster's tick as columns: gather, batch, scatter, then events.

        The same decisions :meth:`_update_monster` makes, made for all rows at
        once over :class:`engine.monster_table.MonsterTable`:

        * ground height for every falling or walking monster is one batched
          grid query, wall tests for every step one more;
        * target, distance and sight are array expressions, in the float32
          arithmetic the ``glm`` path used, so thresholds agree with it;
        * a line-of-sight ray is cast only for a monster whose shot is due --
          the one place its answer is used;
        * positions and state go back to the objects in one scatter.

        What stays Python is what is irregular or rare: a row whose tick is
        unusual (it wakes, it has a kill input or a named target, its aggro
        target has gone) runs :meth:`_update_monster`; shots, sight changes,
        patrol and sound investigation run per row, in row order, after the
        batch. One difference in ordering is deliberate and bounded: every
        monster's step is applied before any shot, so a monster killed by an
        earlier monster's shot this tick has already taken that tick's step.
        """
        t = self.table
        clock = time.perf_counter
        phase = {}
        started = clock()
        t.gather(monsters)
        n = t.count
        phase['gather'] = clock()
        if not n:
            t.phase_ms = {}
            return
        props = t.props
        states = self.monster_states
        self._sight_changed = np.zeros(n, dtype=bool)
        # *delta* may be one number or one per row, and *sit_out* marks rows
        # that skip this tick altogether (see _offscreen_rows). A plain number
        # with no mask is the ordinary tick for every row, exactly as before.
        dt = np.asarray(delta, dtype=np.float64)
        dt = dt[:n] if dt.ndim > 0 else np.full(n, float(dt))

        hidden = t.hidden[:n]
        skip = hidden | t.disabled[:n]
        if sit_out is not None:
            # A row sitting the tick out is not touched at all, exactly as the
            # per-monster path does not call _update_monster for it.
            sitting = np.asarray(sit_out, dtype=bool)[:n]
            skip = skip | sitting
            hidden = hidden & ~sitting
        dead = ~skip & t.dead[:n]
        mode = t.mode[:n]
        mode[dead] = monster_table.MODE_DEAD

        # Hidden and dead monsters are never mid-shot.
        for row in np.flatnonzero(hidden | dead):
            p = props[row]
            if 'is_shooting' in p:
                del p['is_shooting']

        # ---- Dead monsters fall, as one batch -----------------------------
        dead_rows = np.flatnonzero(dead)
        if len(dead_rows):
            self._fall_dead(t, dead_rows, dt[dead_rows])
        phase['fall'] = clock()

        alive = ~skip & ~dead
        awake = alive & t.awake[:n]
        asleep = alive & ~t.awake[:n]
        pos32 = t.pos[:n].astype(np.float32)
        px, py, pz = float(player_pos[0]), float(player_pos[1]), float(player_pos[2])
        player32 = np.array((px, py, pz), dtype=np.float32)
        sight_sq = MONSTER_SIGHT_RANGE * MONSTER_SIGHT_RANGE
        nearest = None
        scalar = np.zeros(n, dtype=bool)

        # ---- Sleeping monsters: does anything wake them? -------------------
        waiting = asleep & t.triggered[:n]
        mode[waiting] = monster_table.MODE_WAITING
        scalar |= asleep & ~t.triggered[:n] & ~t.wake_on_sight[:n]
        watching = np.flatnonzero(asleep & ~t.triggered[:n] & t.wake_on_sight[:n])
        if len(watching):
            d = player32 - pos32[watching]
            seen = (d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1] + d[:, 2] * d[:, 2]) <= sight_sq
            woke = seen.copy()
            teamed = ~seen & (t.team[:n][watching] >= 0)
            if teamed.any():
                nearest = self._enemy_batch(MONSTER_SIGHT_RANGE, t)
                for i in np.flatnonzero(teamed):
                    row = int(watching[i])
                    if nearest is not None:
                        woke[i] = nearest[row] >= 0
                    else:
                        woke[i] = self._find_closest_enemy_team_monster(
                            monsters[row], props[row].get('team', ''),
                            player_pos, MONSTER_SIGHT_RANGE) is not None
            listening = ~woke & t.can_hear[:n][watching]
            if listening.any() and self.lt.combat_runtime.get_recent_noise_events(max_age=2.0):
                for i in np.flatnonzero(listening):
                    woke[i] = self._hears_noise(monsters[int(watching[i])]) is not None
            scalar[watching[woke]] = True
            mode[watching[~woke]] = monster_table.MODE_ASLEEP

        phase['wake'] = clock()

        # ---- Awake monsters: which ticks are irregular? --------------------
        awake_rows = np.flatnonzero(awake)
        aggro_row = np.full(n, -1, dtype=np.int64)
        row_of = t.row_of
        for row in awake_rows:
            p = props[row]
            if p.get('_kill', False) or p.get('target_name', None) is not None:
                scalar[row] = True
                continue
            aggro_id = p.get('_aggro_target', None)
            if aggro_id is not None:
                target_row = row_of.get(aggro_id)
                if target_row is None or props[target_row].get('dead', False):
                    scalar[row] = True       # the per-monster path drops it
                else:
                    aggro_row[row] = target_row
        chase = awake & ~scalar
        rows = np.flatnonzero(chase)
        mode[scalar] = monster_table.MODE_SCALAR

        phase['classify'] = clock()
        if len(rows):
            self._chase(t, rows, aggro_row, pos32, player32, player_pos,
                        nearest, dt[rows])
        phase['chase'] = clock()

        # ---- Irregular rows, events, search: Python, in row order ----------
        fired = t.fired[:n]
        event_rows = np.flatnonzero(scalar | fired
                                    | (t.mode[:n] == monster_table.MODE_SEARCH)
                                    | self._sight_changed[:n])
        t.python_rows = len(event_rows)
        crossfire = self._table_crossfire
        for row in event_rows:
            row = int(row)
            thing = monsters[row]
            if scalar[row]:
                self._update_monster(thing, float(dt[row]), player_pos)
                continue
            if thing.properties.get('dead', False):
                continue                     # killed earlier in this tick
            state = states[id(thing)]
            # The target monster, aggro or nearest enemy: what the
            # per-monster path calls aggro_monster.
            aggro_monster = monsters[int(t.target[row])] if t.target[row] >= 0 else None
            if self._sight_changed[row]:
                if t.in_sight[row]:
                    self._sight_gained(thing, aggro_monster)
                else:
                    self._sight_lost(thing, aggro_monster, float(t.dist_sq[row]))
            if fired[row]:
                target_pos = glm.vec3(*map(float, t.target_pos[row]))
                eye = glm.vec3(float(pos32[row, 0]), float(pos32[row, 1]) + 64.0,
                               float(pos32[row, 2]))
                if aggro_monster is not None:
                    target_eye = glm.vec3(target_pos.x, target_pos.y + 64.0, target_pos.z)
                else:
                    target_eye = glm.vec3(px, py + self.lt.player.camera_height, pz)
                self._monster_attack(thing, props[row].get('monster_type', 'human'),
                                     target_pos, aggro_monster,
                                     float(t.dist_sq[row]), eye, target_eye,
                                     crossfire=crossfire)
            if t.mode[row] == monster_table.MODE_SEARCH:
                if aggro_monster is not None:
                    thing.properties.pop('_aggro_target', None)
                self._search(thing, state, props[row].get('monster_type', 'human'),
                             float(dt[row]), player_pos)
        phase['events'] = clock()
        # Published whole, so a reader between ticks never sees half of one.
        phase_ms = {}
        last = started
        for name in ('gather', 'fall', 'wake', 'classify', 'chase', 'events'):
            phase_ms[name] = (phase[name] - last) * 1000.0
            last = phase[name]
        t.phase_ms = phase_ms

    def _fall_dead(self, t, rows, delta):
        """Dead monsters drop to the ground: the per-monster rule, batched."""
        props = t.props
        pos = t.pos[rows]
        ground = self._grid.raycast_down_batch(pos[:, 0], pos[:, 2], pos[:, 1])
        t.ground_y[rows] = ground
        vel = np.array([props[r].get('_vel_y', 0.0) for r in rows], dtype=np.float64)
        target_y = ground + t.sprite_height[rows] / 2.0
        has_ground = ~np.isnan(ground)
        falling = has_ground & (pos[:, 1] > target_y + 1.0)
        settle = has_ground & ~falling & (np.abs(pos[:, 1] - target_y) > 1.0)
        new_vel = np.where(has_ground & falling, vel, 0.0)
        dl = np.broadcast_to(np.asarray(delta, dtype=np.float64), (len(rows),))[falling]
        fv = vel[falling] + MONSTER_GRAVITY * dl
        fv = np.maximum(fv, MONSTER_TERMINAL_VEL)
        new_y = pos[falling, 1] + fv * dl
        landed = new_y <= target_y[falling]
        new_y[landed] = target_y[falling][landed]
        fv[landed] = 0.0
        new_vel[falling] = fv
        out_y = pos[:, 1].copy()
        out_y[falling] = new_y
        out_y[settle] = target_y[settle]
        moved = falling | settle
        monsters = t.monsters
        for i, row in enumerate(rows):
            if props[row].get('_vel_y') != new_vel[i] or '_vel_y' not in props[row]:
                props[row]['_vel_y'] = float(new_vel[i])
        if moved.any():
            which = rows[moved]
            out = pos[moved].copy()
            out[:, 1] = out_y[moved]
            self.table.pos[which] = out           # the column, not an entity
            set_positions([monsters[r] for r in which], out)

    def _chase(self, t, rows, aggro_row, pos32, player32, player_pos, nearest, delta):
        """Awake monsters with a regular tick: gravity, target, step, shot timers."""
        monsters = t.monsters
        props = t.props
        states = self.monster_states
        n = t.count
        # Per-monster state is created on the first awake tick, as before.
        for row in rows:
            mid = id(monsters[row])
            if mid not in states:
                states[mid] = {
                    'shoot_timer': MONSTER_SHOOT_INTERVAL,
                    'anim_timer': 0.0,
                    'in_sight': False,
                    'vel_y': 0.0,
                    'investigating_sound': None,
                }
        row_states = [states[id(monsters[row])] for row in rows]
        t.gather_states(rows, row_states)
        p32 = pos32[rows]
        # One delta per chased row (rows sitting this tick out were never
        # chased); a plain number still works.
        delta = np.broadcast_to(np.asarray(delta, dtype=np.float64), (len(rows),))

        # ---- Gravity for ground monsters (one batched ground query) -------
        ground_rows = ~t.flying[rows]
        gravity_y = np.full(len(rows), np.nan)
        vel = t.vel_y[rows].copy()
        if ground_rows.any():
            g = np.flatnonzero(ground_rows)
            x = p32[g, 0].astype(np.float64)
            y = p32[g, 1].astype(np.float64)
            z = p32[g, 2].astype(np.float64)
            ground = self._grid.raycast_down_batch(x, z, y + 10.0)
            t.ground_y[rows[g]] = ground
            half = t.sprite_height[rows[g]] / 2.0
            v = vel[g]
            has_ground = ~np.isnan(ground)
            foot = y - half
            falling = has_ground & (foot > ground + 1.0)
            dg = delta[g][falling]
            fv = np.maximum(v[falling] + MONSTER_GRAVITY * dg, MONSTER_TERMINAL_VEL)
            new_foot = foot[falling] + fv * dg
            landed = new_foot <= ground[falling]
            new_foot[landed] = ground[falling][landed]
            fv[landed] = 0.0
            gy = np.full(len(g), np.nan)
            gy[falling] = new_foot + half[falling]
            desired = ground + half
            settle = has_ground & ~falling & (np.abs(y - desired) > 1.0)
            gy[settle] = desired[settle]
            v = np.where(has_ground & ~falling, 0.0, v)
            v[falling] = fv
            vel[g] = v
            gravity_y[g] = gy
        t.vel_y[rows] = vel
        # The rest of the tick starts from where gravity left each monster, as
        # the per-monster path does (glm reads the new position as float32).
        fell = ~np.isnan(gravity_y)
        p32 = p32.copy()
        p32[fell, 1] = gravity_y[fell].astype(np.float32)

        # ---- Target: aggro monster, nearest enemy, else the player --------
        target = np.full(len(rows), monster_table.TARGET_PLAYER, dtype=np.int32)
        target_pos = np.broadcast_to(player32, (len(rows), 3)).copy()
        has_aggro = aggro_row[rows] >= 0
        target[has_aggro] = aggro_row[rows][has_aggro]
        teamed = ~has_aggro & (t.team[rows] >= 0)
        if teamed.any():
            if nearest is None:
                nearest = self._enemy_batch(MONSTER_SIGHT_RANGE, t)
            teamed_idx = np.flatnonzero(teamed)
            if nearest is not None:
                query_rows = rows[teamed_idx]
                enemy_rows = nearest[query_rows]
                valid = enemy_rows >= 0
                target[teamed_idx[valid]] = enemy_rows[valid]
            else:
                # Only the exceptional no-table path retains the scalar
                # reference implementation. Normal dense ticks already
                # have their MonsterTable gathered.
                for i in teamed_idx:
                    row = rows[i]
                    found = self._find_closest_enemy_team_monster(
                        monsters[row], props[row].get('team', ''), player_pos,
                        MONSTER_SIGHT_RANGE)
                    enemy = t.row_of[id(found)] if found is not None else -1
                    if enemy >= 0:
                        target[i] = enemy
        monster_target = target >= 0
        target_pos[monster_target] = t.pos[target[monster_target]].astype(np.float32)
        t.target[rows] = target
        t.target_pos[rows] = target_pos

        # ---- Distance and sight, in the float32 the glm path used ---------
        d = p32 - target_pos
        dist_sq = d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1] + d[:, 2] * d[:, 2]
        t.dist_sq[rows] = dist_sq
        in_sight = dist_sq <= MONSTER_SIGHT_RANGE * MONSTER_SIGHT_RANGE
        was = t.in_sight[rows].copy()
        changed = np.zeros(n, dtype=bool)
        changed[rows] = in_sight != was
        self._sight_changed = changed
        t.in_sight[rows] = in_sight
        t.mode[rows] = np.where(in_sight, monster_table.MODE_CHASE,
                                monster_table.MODE_SEARCH)

        # ---- Step toward the target, sliding along walls ------------------
        final = p32.copy()
        stepped = np.zeros(len(rows), dtype=bool)
        mover = in_sight & (dist_sq > MONSTER_STOP_DISTANCE * MONSTER_STOP_DISTANCE)
        if mover.any():
            m = np.flatnonzero(mover)
            direction = target_pos[m] - p32[m]
            dir_len = np.sqrt(direction[:, 0] * direction[:, 0]
                              + direction[:, 1] * direction[:, 1]
                              + direction[:, 2] * direction[:, 2])
            ok = dir_len > 0.001
            with np.errstate(divide='ignore', invalid='ignore'):
                direction = direction / dir_len[:, None]
                flat = ~t.flying[rows[m]]
                fl = np.sqrt(direction[:, 0] * direction[:, 0]
                             + np.float32(0.0) * np.float32(0.0)
                             + direction[:, 2] * direction[:, 2])
                ok &= ~(flat & (fl < 1e-6))
                flattened = np.stack((direction[:, 0] / fl,
                                      np.zeros_like(fl),
                                      direction[:, 2] / fl), axis=1)
            direction = np.where(flat[:, None], flattened, direction)
            m = m[ok]
            direction = direction[ok]
            if len(m):
                step = (direction * np.float32(MONSTER_MOVE_SPEED)
                        * delta[m].astype(np.float32)[:, None])
                start = p32[m]
                full = start + step
                slide_x = start.copy()
                slide_x[:, 0] = start[:, 0] + step[:, 0]
                slide_z = start.copy()
                slide_z[:, 2] = start[:, 2] + step[:, 2]
                boxes = np.concatenate((full, slide_x, slide_z)).astype(np.float64)
                blocked = self._grid.overlaps_wall_batch(
                    boxes[:, 0], boxes[:, 1], boxes[:, 2], MONSTER_WALL_MARGIN)
                k = len(m)
                free_full = ~blocked[:k]
                free_x = ~blocked[k:2 * k]
                free_z = ~blocked[2 * k:]
                choice = np.where(free_full[:, None], full,
                                  np.where(free_x[:, None], slide_x,
                                           np.where(free_z[:, None], slide_z, start)))
                moved = free_full | free_x | free_z
                final[m[moved]] = choice[moved]
                stepped[m[moved]] = True

        # ---- Shot timers; a ray only for a shot that is due ---------------
        shoot = t.shoot_timer[rows].copy()
        anim = t.anim_timer[rows].copy()
        shoot[in_sight] -= delta[in_sight]
        due = np.flatnonzero(in_sight & (shoot <= 0.0))
        fired = np.zeros(len(rows), dtype=bool)
        if len(due):
            camera_height = self.lt.player.camera_height
            for i in due:
                row = rows[i]
                eye = glm.vec3(float(p32[i, 0]), float(p32[i, 1]) + 64.0, float(p32[i, 2]))
                if target[i] >= 0:
                    tp = target_pos[i]
                    target_eye = glm.vec3(float(tp[0]), float(tp[1]) + 64.0, float(tp[2]))
                else:
                    target_eye = glm.vec3(float(player32[0]),
                                          float(player32[1]) + camera_height,
                                          float(player32[2]))
                fired[i] = self._has_line_of_sight(eye, target_eye)
                t.ray_cast[row] = True
            t.rays_cast = len(due)
            shoot[due] = np.where(fired[due], MONSTER_SHOOT_INTERVAL, 0.1)
            anim[fired] = MONSTER_SHOOT_ANIM_TIME
        t.fired[rows] = fired
        shooting = in_sight & (anim > 0.0)
        anim[shooting] -= delta[shooting]
        anim[~in_sight] = 0.0
        t.shoot_timer[rows] = shoot
        t.anim_timer[rows] = anim

        # ---- Scatter: positions, state, sprite flags ----------------------
        # A step starts from the post-gravity position, so it carries the
        # fall with it; a monster that did not step keeps gravity's exact y.
        write = stepped | ~np.isnan(gravity_y)
        if write.any():
            which = np.flatnonzero(write)
            out = final[which].astype(np.float64)
            grav_only = ~stepped[which]
            out[grav_only, 1] = gravity_y[which][grav_only]
            self.table.pos[rows[which]] = out     # the column, not an entity
            set_positions([monsters[rows[i]] for i in which], out)
        for i, state in enumerate(row_states):
            state['shoot_timer'] = float(shoot[i])
            state['anim_timer'] = float(anim[i])
            state['in_sight'] = bool(in_sight[i])
            if ground_rows[i]:
                state['vel_y'] = float(vel[i])
        flag_changed = []
        for i, row in enumerate(rows):
            want = bool(shooting[i])
            p = props[row]
            if p.get('is_shooting') != want:
                p['is_shooting'] = want
                flag_changed.append(monsters[row])
        if flag_changed:
            JOURNAL.record_many(flag_changed, STATE)

    def _table_crossfire(self, shooter, ray_start, ray_end):
        """Crossfire from the tick's table instead of a walk over every monster."""
        row = self.table.row_of.get(id(shooter))
        if row is None:
            return self._find_monster_in_crossfire(shooter, ray_start, ray_end)
        return self.table.crossfire(row, (ray_start.x, ray_start.y, ray_start.z),
                                    (ray_end.x, ray_end.y, ray_end.z))

    def _update_monster(self, thing, delta: float, player_pos):
        """One monster's tick, object by object: the reference path.

        What every monster ran before the dense pass existed, unchanged in
        behaviour. :meth:`_update_dense` sends a monster here when its tick is
        irregular -- a kill input, a named target, a stale aggro target, the
        tick it wakes -- and every monster comes here when there is no spatial
        grid or when ``notarget`` is on.
        """
        if thing.properties.get('hidden', False):
            thing.properties.pop('is_shooting', None)
            return
        if thing.properties.get('disabled', False):
            return

        mid = id(thing)

        # ---- Dead monsters: sprite falls ----
        if thing.properties.get('dead', False):
            thing.properties.pop('is_shooting', None)
            vel_y = thing.properties.get('_vel_y', 0.0)
            pos = thing.pos
            ground_y = self._monster_raycast_down(pos[0], pos[2], pos[1])
            if ground_y is not None:
                sprite_h = thing.properties.get('sprite_height', 128)
                target_y = ground_y + sprite_h / 2.0
                if pos[1] > target_y + 1.0:
                    vel_y += MONSTER_GRAVITY * delta
                    if vel_y < MONSTER_TERMINAL_VEL:
                        vel_y = MONSTER_TERMINAL_VEL
                    new_y = pos[1] + vel_y * delta
                    if new_y <= target_y:
                        new_y = target_y
                        vel_y = 0.0
                    thing.pos = [pos[0], new_y, pos[2]]
                    thing.properties['_vel_y'] = vel_y
                else:
                    if abs(pos[1] - target_y) > 1.0:
                        thing.pos = [pos[0], target_y, pos[2]]
                    thing.properties['_vel_y'] = 0.0
            else:
                thing.properties['_vel_y'] = 0.0
            return

        # ---- Awake / triggered logic ----
        triggered = thing.properties.get('triggered', False)
        wake_sight = thing.properties.get('wake_on_sight', True)
        awake = thing.properties.get('awake', False)

        if not awake:
            if triggered:
                # Scripted ambush: waits for its I/O trigger, ignores sight & sound
                return
            elif not wake_sight:
                thing.properties['awake'] = True
                awake = True
            else:
                # Try, in order: see the player, see an enemy-team monster,
                # or HEAR a recent player noise (gunshot / water splash).
                woke_reason = None

                # Sight of the player (squared distance — threshold-only compare)
                diff_to_player = player_pos - glm.vec3(thing.pos)
                dist_to_player_sq = glm.dot(diff_to_player, diff_to_player)
                if dist_to_player_sq <= MONSTER_SIGHT_RANGE * MONSTER_SIGHT_RANGE:
                    woke_reason = 'sight'
                else:
                    # Sight of an enemy-team monster
                    my_team = thing.properties.get('team', '')
                    if my_team:
                        enemy = self._find_closest_enemy_team_monster(
                            thing, my_team, player_pos, MONSTER_SIGHT_RANGE)
                        if enemy is not None:
                            woke_reason = 'enemy'
                            if self.monster_debug_active:
                                name = thing.properties.get('name', '?')
                                ename = enemy.properties.get('name', '?')
                                eteam = enemy.properties.get('team', '?')
                                debug_log("MonsterAI",
                                          f"{name} woke to enemy {ename} (team={eteam})")

                # Hearing: a recent nearby noise wakes a can_hear monster.
                # Investigation (moving to the source) is handled once awake
                # by _investigate_sounds on the out-of-sight path.
                if woke_reason is None and self._hears_noise(thing) is not None:
                    woke_reason = 'sound'
                    if self.monster_debug_active:
                        name = thing.properties.get('name', '?')
                        debug_log("MonsterAI", f"{name} woke to a noise")

                if woke_reason is None:
                    return

                thing.properties['awake'] = True
                awake = True

        # ---- Kill input handling ----
        if thing.properties.pop('_kill', False):
            _set_render_flag(thing, 'dead', True)
            thing.properties.pop('is_shooting', None)
            if self.monster_debug_active:
                name = thing.properties.get('name', '?')
                debug_log("MonsterAI",
                    f'<a href="filter:{name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{name}</a> '
                    f'<span style="color: #B71C1C; font-weight: bold;">DIED</span> (killed by input)')
            return

        mtype = thing.properties.get('monster_type', 'human')
        thing_pos = glm.vec3(thing.pos)

        # ---- Per‑monster state initialisation ----
        if mid not in self.monster_states:
            self.monster_states[mid] = {
                'shoot_timer': MONSTER_SHOOT_INTERVAL,
                'anim_timer': 0.0,
                'in_sight': False,
                'vel_y': 0.0,
                'investigating_sound': None,  # (pos, expiry_time) or None
            }

        state = self.monster_states[mid]

        # ---- Gravity for ground monsters ----
        if mtype != 'flying':
            vel_y = state.get('vel_y', 0.0)
            ground_y = self._monster_raycast_down(thing_pos.x, thing_pos.z, thing_pos.y + 10.0)
            sprite_height = thing.properties.get('sprite_height', 128)
            half_height = sprite_height / 2.0
            if ground_y is not None:
                foot_y = thing_pos.y - half_height
                if foot_y > ground_y + 1.0:
                    vel_y += MONSTER_GRAVITY * delta
                    if vel_y < MONSTER_TERMINAL_VEL:
                        vel_y = MONSTER_TERMINAL_VEL
                    new_foot_y = foot_y + vel_y * delta
                    if new_foot_y <= ground_y:
                        new_foot_y = ground_y
                        vel_y = 0.0
                    new_center_y = new_foot_y + half_height
                    thing.pos = [thing_pos.x, new_center_y, thing_pos.z]
                else:
                    desired_center_y = ground_y + half_height
                    if abs(thing_pos.y - desired_center_y) > 1.0:
                        thing.pos = [thing_pos.x, desired_center_y, thing_pos.z]
                    vel_y = 0.0
            state['vel_y'] = vel_y
            # Everything below starts from where gravity left the monster. It
            # used to start from the position read before gravity ran, so a
            # monster that stepped this tick overwrote its own fall and a
            # chasing monster walked off a ledge into mid-air and stayed there.
            thing_pos = glm.vec3(thing.pos)

        # ---- Notarget: skip all player-targeting when cheat is active ----
        #      Monsters still gravity-fall and patrol, just don't chase/attack.
        if self.lt.notarget:
            _set_render_flag(thing, 'is_shooting', False)
            if mid in self.monster_states:
                self.monster_states[mid]['anim_timer'] = 0.0
            # Even in notarget mode, monsters with can_hear investigate sounds
            if thing.properties.get('can_hear', False):
                self._investigate_sounds(thing, state, mtype, delta, player_pos)
            else:
                self._update_monster_patrol(thing, state, mtype, delta)
            return

        # ---- Target name override (set via I/O settarget input) ----
        target_name = thing.properties.get('target_name', None)
        override_target_pos = None
        if target_name is not None:
            if target_name == '':
                # Empty string: clear override and fall back to normal logic
                thing.properties.pop('target_name', None)
            else:
                target_entity = self.lt.world_runtime.find_entity_by_name(target_name)
                if target_entity is not None and hasattr(target_entity, 'pos'):
                    # Valid named target found — use its position
                    override_target_pos = glm.vec3(target_entity.pos)
                else:
                    # Missing or invalid name: clear override and fall back
                    thing.properties.pop('target_name', None)

        # ---- Resolve target (player or aggro monster for infighting) ----
        if override_target_pos is not None:
            # Bypass normal target selection when a valid override is active
            target_pos = override_target_pos
            aggro_monster = None
        else:
            aggro_id = thing.properties.get('_aggro_target', None)
            aggro_monster = None
            if aggro_id is not None:
                aggro_monster = self._find_monster_by_id(aggro_id)
                if aggro_monster is None or aggro_monster.properties.get('dead', False):
                    # Aggro target gone — revert to player
                    thing.properties.pop('_aggro_target', None)
                    aggro_monster = None

            if aggro_monster is not None:
                target_pos = glm.vec3(aggro_monster.pos)
            else:
                # ---- Team-based enemy targeting (priority over player) ----
                my_team = thing.properties.get('team', '')
                if my_team:
                    enemy_monster = self._find_closest_enemy_team_monster(
                        thing, my_team, player_pos, MONSTER_SIGHT_RANGE)
                    if enemy_monster is not None:
                        aggro_monster = enemy_monster
                        target_pos = glm.vec3(enemy_monster.pos)
                        if self.monster_debug_active:
                            name = thing.properties.get('name', '?')
                            ename = enemy_monster.properties.get('name', '?')
                            eteam = enemy_monster.properties.get('team', '?')
                            debug_log("MonsterAI",
                                      f"{name} (team={my_team}) targeting enemy {ename} (team={eteam})")
                    else:
                        target_pos = player_pos
                else:
                    target_pos = player_pos

        # ---- Line of sight check ----
        monster_eye = glm.vec3(thing_pos.x, thing_pos.y + 64.0, thing_pos.z)
        if aggro_monster is not None:
            target_eye = glm.vec3(target_pos.x, target_pos.y + 64.0, target_pos.z)
        else:
            target_eye = glm.vec3(player_pos.x, player_pos.y + self.lt.player.camera_height, player_pos.z)
        has_los = self._has_line_of_sight(monster_eye, target_eye)

        if self.monster_debug_active:
            self._debug_rays.append({
                'start': [monster_eye.x, monster_eye.y, monster_eye.z],
                'end':   [target_eye.x, target_eye.y, target_eye.z],
                'color': 'green' if has_los else 'red',
            })

        # PERF: squared distance — every use below is a threshold compare.
        _dist_diff = thing_pos - target_pos
        distance_sq = glm.dot(_dist_diff, _dist_diff)

        if distance_sq <= MONSTER_SIGHT_RANGE * MONSTER_SIGHT_RANGE:
            # ---- Entered sight range ----
            if not state['in_sight']:
                state['in_sight'] = True
                self._sight_gained(thing, aggro_monster)

            # ---- Move toward target ----
            if distance_sq > MONSTER_STOP_DISTANCE * MONSTER_STOP_DISTANCE:
                direction = target_pos - thing_pos
                dir_len = glm.length(direction)
                if dir_len > 0.001:
                    direction = direction / dir_len
                    if mtype != 'flying':
                        direction = _flatten_to_ground(direction)
                if dir_len > 0.001 and direction is not None:
                    step = direction * MONSTER_MOVE_SPEED * delta
                    new_pos = thing_pos + step

                    if not self._monster_overlaps_wall(new_pos.x, new_pos.y, new_pos.z, MONSTER_WALL_MARGIN):
                        thing.pos = [new_pos.x, new_pos.y, new_pos.z]
                    else:
                        # slide along walls
                        slide_x = glm.vec3(thing_pos.x + step.x, thing_pos.y, thing_pos.z)
                        slide_z = glm.vec3(thing_pos.x, thing_pos.y, thing_pos.z + step.z)
                        if not self._monster_overlaps_wall(slide_x.x, slide_x.y, slide_x.z, MONSTER_WALL_MARGIN):
                            thing.pos = [slide_x.x, slide_x.y, slide_z.z]
                        elif not self._monster_overlaps_wall(slide_z.x, slide_z.y, slide_z.z, MONSTER_WALL_MARGIN):
                            thing.pos = [slide_z.x, slide_z.y, slide_z.z]
                        # else: blocked on both axes – no movement

            # ---- Shooting ----
            state['shoot_timer'] -= delta
            if state['shoot_timer'] <= 0.0 and has_los:
                state['shoot_timer'] = MONSTER_SHOOT_INTERVAL
                state['anim_timer'] = MONSTER_SHOOT_ANIM_TIME

                self._monster_attack(thing, mtype, target_pos, aggro_monster,
                                     distance_sq, monster_eye, target_eye)

            elif state['shoot_timer'] <= 0.0 and not has_los:
                state['shoot_timer'] = 0.1   # re-check soon

            if state['anim_timer'] > 0.0:
                state['anim_timer'] -= delta
                _set_render_flag(thing, 'is_shooting', True)
            else:
                _set_render_flag(thing, 'is_shooting', False)

        else:
            # ---- Out of sight ----
            if state['in_sight']:
                state['in_sight'] = False
                self._sight_lost(thing, aggro_monster, distance_sq)

            _set_render_flag(thing, 'is_shooting', False)
            state['anim_timer'] = 0.0

            # If we had an aggro target but it's out of range, drop it
            if aggro_monster is not None:
                thing.properties.pop('_aggro_target', None)

            self._search(thing, state, mtype, delta, player_pos)

    # -------------------------------------------------------------------------
    # One monster's events -- shared by the per-monster and the dense paths
    # -------------------------------------------------------------------------

    def _sight_gained(self, thing, aggro_monster):
        """The target came into sight range: outputs and debug text."""
        if aggro_monster is None and self.lt.io_manager:
            self.lt.io_manager.fire_output(thing, 'OnSeePlayer')
        if self.monster_debug_active:
            name = thing.properties.get('name', '?')
            if aggro_monster is not None:
                tgt_name = aggro_monster.properties.get('name', '?')
                debug_log("MonsterAI",
                    f'<a href="filter:{name}" style="color: #42A5F5; font-weight: bold; text-decoration: none;">{name}</a> '
                    f'engaging enemy: '
                    f'<a href="filter:{tgt_name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{tgt_name}</a>')
            else:
                debug_log("MonsterAI",
                    f'<a href="filter:{name}" style="color: #42A5F5; font-weight: bold; text-decoration: none;">{name}</a> '
                    f'engaging enemy: '
                    f'<span style="color: #AB47BC; font-weight: bold;">player</span>')

    def _sight_lost(self, thing, aggro_monster, distance_sq):
        """The target left sight range: outputs and debug text."""
        if aggro_monster is None and self.lt.io_manager:
            self.lt.io_manager.fire_output(thing, 'OnLostPlayer')
        if self.monster_debug_active:
            name = thing.properties.get('name', '?')
            debug_log("MonsterAI", f"{name} lost target (dist={math.sqrt(distance_sq):.0f})")

    def _monster_attack(self, thing, mtype, target_pos, aggro_monster,
                        distance_sq, monster_eye, target_eye, crossfire=None):
        """One shot's effects: damage, projectile, sound, outputs.

        Timers are the caller's: this is what a shot *does*. *crossfire*, when
        given, finds the monster standing in the line of fire (the dense pass
        answers it from its table); otherwise the per-monster walk does.
        """
        mid = id(thing)
        damage = int(thing.properties.get('damage', 20))

        if mtype == 'flying':
            # ---- Flying monsters: bite if very close, else projectile ----
            if distance_sq <= MONSTER_BITE_DISTANCE * MONSTER_BITE_DISTANCE and aggro_monster is None:
                # Bite attack: instant hitscan, double damage
                bite_damage = int(damage * MONSTER_BITE_DAMAGE_MULT)
                self.lt.trigger_runtime._apply_player_damage(bite_damage)
                name = thing.properties.get('name', '?')
                debug_log("MonsterAI", f"{name} used bite attack for 2x damage!")
            else:
                # Too far — spawn projectile sprite
                self._spawn_monster_projectile(thing, target_pos, damage, mid)
                name = thing.properties.get('name', '?')
                debug_log("MonsterAI", f"{name} fired projectile")
        else:
            # ---- Human monsters: instant hitscan damage ----
            if aggro_monster is not None:
                # ---- Infighting: damage the aggro target monster ----
                self._apply_monster_damage(aggro_monster, damage, attacker=thing)
            else:
                # ---- Check for crossfire (Doom-style infighting) ----
                crossfire_victim = (
                    crossfire(thing, monster_eye, target_eye)
                    if crossfire is not None else
                    self._find_monster_in_crossfire(
                        thing, monster_eye, target_eye))
                if crossfire_victim is not None:
                    self._apply_monster_damage(
                        crossfire_victim, damage, attacker=thing)
                    if self.monster_debug_active:
                        v_name = crossfire_victim.properties.get('name', '?')
                        a_name = thing.properties.get('name', '?')
                        debug_log("MonsterAI",
                                  f"CROSSFIRE: {a_name} hit {v_name} — infighting!")
                else:
                    self.lt.trigger_runtime._apply_player_damage(damage)

        # ---- Use per-type shoot sound ----
        sound_file = MONSTER_SHOOT_SOUNDS.get(mtype, MONSTER_SHOOT_SOUND_DEFAULT)
        self.lt.game_state.queue_sound({
            'file': sound_file,
            'volume': 0.6,
            'entity_id': mid,
        })

        if self.lt.io_manager:
            self.lt.io_manager.fire_output(thing, 'OnAttack')

        if self.monster_debug_active:
            name = thing.properties.get('name', '?')
            tgt = aggro_monster.properties.get('name', '?') if aggro_monster else 'player'
            debug_log("MonsterAI", f"{name} attacks {tgt} for {damage} damage (LOS clear)")

    def _search(self, thing, state, mtype, delta, player_pos):
        """Target out of range: investigate a noise, else patrol."""
        # ---- Sound investigation (can_hear monsters) ----
        if thing.properties.get('can_hear', False):
            investigating = self._investigate_sounds(thing, state, mtype, delta, player_pos)
            if not investigating:
                # ---- Patrol behaviour (only when target not in sight and not investigating) ----
                self._update_monster_patrol(thing, state, mtype, delta)
        else:
            # ---- Patrol behaviour (only when target not in sight) ----
            self._update_monster_patrol(thing, state, mtype, delta)

    # -------------------------------------------------------------------------
    # Monster infighting helpers
    # -------------------------------------------------------------------------

    def _find_monster_by_id(self, monster_id: int):
        """Return a living Monster thing by Python id, or None."""
        monster_by_id = getattr(self.lt, '_monster_by_id', None)
        if monster_by_id is not None:
            return monster_by_id.get(monster_id)
        for t in self.lt.things:
            if isinstance(t, MonsterThing) and id(t) == monster_id:
                return t
        return None

    #: Below this many monsters the batch costs more to assemble than the walk
    #: it replaces, because only a fraction of monsters query in a given tick
    #: -- the rest are holding an aggro target -- so an N-by-N matrix is built
    #: to answer a handful of questions. Measured on a driven tick:
    #:
    #:     monsters   queries/tick   search: walk -> batch
    #:        30           4          0.056 -> 0.138 ms   0.41x
    #:        60           8          0.230 -> 0.230 ms   1.00x
    #:       120          15          0.752 -> 0.460 ms   1.63x
    #:       240          29          2.677 -> 1.428 ms   1.87x
    #:       480          68         12.770 -> 5.096 ms   2.51x
    #:
    def _find_closest_enemy_team_monster(self, thing, my_team: str, player_pos: glm.vec3, max_range: float):
        """Find the closest living monster on a different team within ``max_range``.

The dense tick path answers this for all monsters at once.  This helper is
the scalar fallback for callers that do not have the dense table.
"""
        if not my_team or MonsterThing is None:
            return None

        if self._enemy_batch(max_range) is not None:
            row = self._enemy_rows.get(id(thing))
            # The team is re-checked because it is the caller's argument, not
            team_code = self._enemy_team_codes.get(my_team, -1)
            if (row is not None and self.table.team[row] == team_code
                    and self._enemy_answered[row]):
                nearest = int(self._enemy_nearest[row])
                return self._enemy_monsters[nearest] if nearest >= 0 else None

        return self._find_closest_enemy_scalar(thing, my_team, max_range)

    def _enemy_batch(self, max_range: float, table=None):
        """This tick's nearest enemy for every monster, as one dense pass.

        MonsterTable already contains the gathered positions, team codes,
        and alive/hidden flags for this tick. The kernel consumes those
        columns directly in vectorized team blocks: squared distances, a
        team mask, and an argmin per block. There is no per-monster
        candidate walk.

        Direct helper calls may occur outside the dense tick in compatibility
        tests and tools. Those calls keep the batch while the queried monster
        set is unchanged, and invalidate it if a position/team/dead/hidden
        value changed.
        """
        refresh_table = False
        if self._enemy_ready:
            if self._enemy_range != max_range:
                return None
            if table is not None:
                return self._enemy_nearest
            if self._enemy_runtime_signature() == self._enemy_signature:
                return self._enemy_nearest
            self._enemy_ready = False
            refresh_table = True

        self._enemy_ready = True
        self._enemy_nearest = None
        self._enemy_range = max_range

        if table is None:
            monsters = getattr(self, '_tick_monsters', None)
            if monsters is None:
                monsters = getattr(self.lt, '_monster_things', None)
            if monsters is None:
                monsters = getattr(self.lt, 'things', None)
            if not monsters:
                return None

            table = self.table
            same_rows = (
                table.count == len(monsters)
                and all(a is b for a, b in zip(table.monsters, monsters))
            )
            if self._enemy_signature is not None:
                live_signature = self._enemy_runtime_signature()
                if live_signature != self._enemy_signature:
                    refresh_table = True
            if refresh_table or not same_rows:
                table.gather(monsters)
        count = table.count
        if not count:
            return None

        team_id = table.team[:count]
        alive = ~(table.dead[:count] | table.hidden[:count])

        self._enemy_rows = table.row_of
        self._enemy_monsters = table.monsters
        self._enemy_team_codes = {
            name: code for code, name in enumerate(table.team_names)
        }
        self._enemy_answered = alive & (team_id >= 0)
        self._enemy_nearest = self._nearest_enemy_rows(
            table.pos[:count], team_id, alive, max_range)
        if table is not None:
            self._enemy_signature = self._enemy_runtime_signature()
        return self._enemy_nearest

    def _enemy_runtime_signature(self):
        """Return the small compatibility signature used by direct queries."""
        monsters = self._tick_monsters
        if monsters is None:
            monsters = getattr(self.lt, "_monster_things", None)
        if monsters is None:
            monsters = getattr(self.lt, "things", ())
        return tuple(
            (
                id(monster),
                bool(monster.properties.get("dead", False)),
                bool(monster.properties.get("hidden", False)),
                monster.properties.get("team", ""),
                tuple(float(value) for value in monster.pos),
            )
            for monster in monsters
        )

    @staticmethod
    def _nearest_enemy_rows(pos, team_id, alive, max_range):
        """Return each row's nearest living enemy row, or -1, in dense batches.

        The usual path works team-by-team so a very large monster population
        never requires one monolithic N x N allocation.  For small populations,
        or for a population split into many tiny teams, one monolithic active
        matrix is cheaper than launching many tiny block kernels.  The
        crossover is deliberately bounded so a large many-team battle still
        avoids an oversized dense matrix.
        """
        count = len(pos)
        nearest = np.full(count, -1, dtype=np.int32)
        if not count:
            return nearest

        p = np.asarray(pos, dtype=np.float32)
        team = np.asarray(team_id[:count], dtype=np.int32)
        active = np.asarray(alive[:count], dtype=bool) & (team >= 0)
        if not active.any():
            return nearest

        active_rows = np.flatnonzero(active)
        team_count = len(np.unique(team[active]))
        if (
            count <= _NEAREST_ENEMY_MONOLITHIC_MAX_COUNT
            and (
                count <= _NEAREST_ENEMY_MONOLITHIC_SMALL_COUNT
                or (
                    team_count >= _NEAREST_ENEMY_MONOLITHIC_MIN_TEAMS
                    and team_count / float(len(active_rows))
                    >= _NEAREST_ENEMY_MONOLITHIC_TEAM_RATIO
                )
            )
        ):
            return MonsterAI._nearest_enemy_rows_monolithic(
                p, team, active_rows, np.float32(max_range) * np.float32(max_range)
            )

        return MonsterAI._nearest_enemy_rows_by_team(
            p,
            team,
            active,
            np.float32(max_range) * np.float32(max_range),
        )

    @staticmethod
    def _nearest_enemy_rows_monolithic(p, team, active_rows, limit):
        """Dense N x N nearest-enemy kernel for the small/tiny-team crossover."""
        a = p[active_rows]
        distance = np.subtract.outer(a[:, 0], a[:, 0])
        np.multiply(distance, distance, out=distance)
        term = np.subtract.outer(a[:, 1], a[:, 1])
        np.multiply(term, term, out=term)
        distance += term
        np.subtract.outer(a[:, 2], a[:, 2], out=term)
        np.multiply(term, term, out=term)
        distance += term

        same_team = np.equal.outer(team[active_rows], team[active_rows])
        distance[same_team] = np.float32(np.inf)
        best = np.argmin(distance, axis=1)
        found = distance[np.arange(len(active_rows)), best] <= limit

        nearest = np.full(len(p), -1, dtype=np.int32)
        nearest[active_rows] = np.where(
            found, active_rows[best], -1
        )
        return nearest

    @staticmethod
    def _nearest_enemy_rows_by_team(p, team, active, limit):
        """Team-block kernel for larger populations."""
        count = len(p)
        nearest = np.full(count, -1, dtype=np.int32)
        for code in np.unique(team[active]):
            rows = np.flatnonzero(active & (team == code))
            cols = np.flatnonzero(active & (team != code))
            if not len(cols):
                continue

            a = p[rows]
            b = p[cols]
            distance = np.subtract.outer(a[:, 0], b[:, 0])
            np.multiply(distance, distance, out=distance)
            term = np.subtract.outer(a[:, 1], b[:, 1])
            np.multiply(term, term, out=term)
            distance += term
            np.subtract.outer(a[:, 2], b[:, 2], out=term)
            np.multiply(term, term, out=term)
            distance += term

            best = np.argmin(distance, axis=1)
            found = distance[np.arange(len(rows)), best] <= limit
            nearest[rows] = np.where(found, cols[best], -1)

        return nearest

    def _find_closest_enemy_scalar(self, thing, my_team: str, max_range: float):
        """The per-monster walk: the batch's reference, and its fallback."""
        my_pos = glm.vec3(thing.pos)
        best_dist_sq = float('inf')
        best_monster = None
        max_range_sq = max_range * max_range
        # This tick's resident monsters when the AI is running (parked ones
        # are hidden and never a candidate), else the logic thread's list.
        monster_things = getattr(self, '_tick_monsters', None)
        if monster_things is None:
            monster_things = getattr(self.lt, '_monster_things', None) or self.lt.things
        # Hoisted: this used to be re-evaluated per candidate, and `things` is
        # a property, so a 240-monster tick called it 57,600 times.
        needs_type_check = monster_things is self.lt.things

        for t in monster_things:
            if needs_type_check and not isinstance(t, MonsterThing):
                continue
            if t is thing:
                continue
            if t.properties.get('dead', False) or t.properties.get('hidden', False):
                continue
            other_team = t.properties.get('team', '')
            if not other_team:
                continue
            if other_team == my_team:
                continue  # Same team = ally, not enemy

            # PERF: compare squared distances — only used for a threshold
            # and closest-of check, so the sqrt in glm.distance is wasted.
            diff = my_pos - glm.vec3(t.pos)
            dist_sq = glm.dot(diff, diff)
            if dist_sq > max_range_sq:
                continue
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_monster = t

        return best_monster


    def _find_monster_in_crossfire(self, shooter, ray_start: glm.vec3,
                                    ray_end: glm.vec3):
        """Check if a living monster (other than the shooter) intersects
        the ray from ray_start to ray_end.  Returns the closest hit monster
        or None.  Used for Doom-style infighting — when monster A fires at
        the player and monster B is in the way, B takes the hit instead.

        Team-aware: same-team monsters are never hit by crossfire."""
        ray_dir = ray_end - ray_start
        ray_len = glm.length(ray_dir)
        if ray_len < 1.0:
            return None
        ray_dir = ray_dir / ray_len

        best_t = ray_len
        best_victim = None
        shooter_team = shooter.properties.get('team', '')
        # This tick's resident monsters when the AI is running (parked ones
        # are hidden and never a candidate), else the logic thread's list.
        monster_things = getattr(self, '_tick_monsters', None)
        if monster_things is None:
            monster_things = getattr(self.lt, '_monster_things', None) or self.lt.things

        for t in monster_things:
            if monster_things is self.lt.things and not isinstance(t, MonsterThing):
                continue
            if t is shooter:
                continue
            if t.properties.get('dead', False) or t.properties.get('hidden', False):
                continue

            # Team-aware crossfire: never hit same-team allies
            target_team = t.properties.get('team', '')
            if shooter_team and target_team and shooter_team == target_team:
                continue

            # Sphere intersection (same radius used by player shooting)
            radius = 80.0
            center = glm.vec3(t.pos[0], t.pos[1] + 64.0, t.pos[2])
            oc = ray_start - center
            a = glm.dot(ray_dir, ray_dir)
            b = 2.0 * glm.dot(oc, ray_dir)
            c = glm.dot(oc, oc) - radius * radius
            disc = b * b - 4.0 * a * c
            if disc < 0.0:
                continue
            hit_t = (-b - math.sqrt(disc)) / (2.0 * a)
            if 0.0 < hit_t < best_t:
                best_t = hit_t
                best_victim = t

        return best_victim

    def _apply_monster_damage(self, victim, damage: int, attacker=None):
        """Deal damage to a monster from another monster (infighting).
        Sets the victim's aggro target to the attacker so it retaliates."""
        health_raw = victim.properties.get('health', 100)
        try:
            health = int(health_raw)
        except (ValueError, TypeError):
            health = 100

        new_health = health - damage
        victim.properties['health'] = new_health

        if self.lt.io_manager:
            self.lt.io_manager.fire_output(victim, 'OnDamaged')

        if self.monster_debug_active:
            v_name = victim.properties.get('name', '?')
            a_name = attacker.properties.get('name', '?') if attacker else '?'
            debug_log("MonsterAI",
                       f"Infighting: {v_name} took {damage} dmg from {a_name} "
                       f"(health {health} -> {new_health})")

        if new_health <= 0:
            _set_render_flag(victim, 'dead', True)
            victim.properties.pop('is_shooting', None)
            victim.properties.pop('_aggro_target', None)
            if self.lt.io_manager:
                self.lt.io_manager.fire_output(victim, 'OnDeath')
            if self.monster_debug_active:
                v_name = victim.properties.get('name', '?')
                debug_log("MonsterAI",
                    f'<a href="filter:{v_name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{v_name}</a> '
                    f'<span style="color: #B71C1C; font-weight: bold;">DIED</span>')
        elif attacker is not None:
            # Retaliate — set aggro toward the attacker
            victim.properties['_aggro_target'] = id(attacker)
            # Wake the victim if it was asleep
            victim.properties['awake'] = True
            if self.monster_debug_active:
                v_name = victim.properties.get('name', '?')
                a_name = attacker.properties.get('name', '?')
                debug_log("MonsterAI",
                    f'<a href="filter:{v_name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{v_name}</a> '
                    f'was shot by '
                    f'<a href="filter:{a_name}" style="color: #42A5F5; font-weight: bold; text-decoration: none;">{a_name}</a> '
                    f'and has gone '
                    f'<span style="color: #FFEE58; font-weight: bold;">AGGRO</span>')

    # -------------------------------------------------------------------------
    # Projectile system (flying monsters)
    # -------------------------------------------------------------------------

    def _spawn_monster_projectile(self, thing, target_pos: glm.vec3, damage: int, owner_id: int):
        """Spawn a projectile sprite for a flying monster.
        The projectile travels toward the target position and can be dodged."""
        from .monster_constants import (
            MONSTER_PROJECTILE_SPEED,
            MONSTER_PROJECTILE_MAX_DIST,
        )

        start_pos = glm.vec3(thing.pos[0], thing.pos[1] + 64.0, thing.pos[2])
        direction = target_pos - start_pos
        dir_len = glm.length(direction)
        if dir_len < 0.001:
            direction = glm.vec3(0, 0, 1)
            dir_len = 1.0
        direction = direction / dir_len

        lifetime = MONSTER_PROJECTILE_MAX_DIST / MONSTER_PROJECTILE_SPEED
        self.lt.combat_runtime._add_monster_projectile(
            (start_pos.x, start_pos.y, start_pos.z),
            (direction.x * MONSTER_PROJECTILE_SPEED,
             direction.y * MONSTER_PROJECTILE_SPEED,
             direction.z * MONSTER_PROJECTILE_SPEED),
            owner_id,
            damage,
            lifetime,
        )

        if self.monster_debug_active:
            name = thing.properties.get('name', '?')
            debug_log("MonsterAI", f"{name} spawned projectile → ({target_pos.x:.0f}, {target_pos.y:.0f}, {target_pos.z:.0f})")

    # -------------------------------------------------------------------------
    # Patrol system (PathNode navigation)
    # -------------------------------------------------------------------------

    @staticmethod
    def _nearest_audible_noise(thing_pos: glm.vec3, hearing_range: float, events: list):
        """Return the closest noise event audible from thing_pos, or None.

        Each event's reach is the monster's hearing range scaled by the
        event's 'loudness' (gunfire carries further than a water splash), so
        a quiet event has to be closer to register.
        PERF: squared distances — only used for threshold + closest compares.
        """
        best_event = None
        best_dist_sq = float('inf')
        for event in events:
            ex, ey, ez = event['pos']
            dx = thing_pos.x - ex
            dy = thing_pos.y - ey
            dz = thing_pos.z - ez
            dist_sq = dx * dx + dy * dy + dz * dz
            reach = hearing_range * event.get('loudness', 1.0)
            if dist_sq <= reach * reach and dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_event = event
        return best_event

    def _hears_noise(self, monster):
        """Return the closest recent player-noise event this monster can hear,
        or None. Deaf monsters (can_hear False) never hear anything. Used to
        wake sleeping monsters — investigation of the source is handled by
        _investigate_sounds once the monster is awake."""
        if not monster.properties.get('can_hear', False):
            return None
        events = self.lt.combat_runtime.get_recent_noise_events(max_age=2.0)
        if not events:
            return None
        hearing_range = float(monster.properties.get('sight', MONSTER_SIGHT_RANGE))
        return self._nearest_audible_noise(glm.vec3(monster.pos), hearing_range, events)

    def _investigate_sounds(self, monster, state: Dict, mtype: str, delta: float, player_pos: glm.vec3) -> bool:
        """Check for recent player noises and move toward them if within range. Returns True if investigating."""
        if not monster.properties.get('can_hear', False):
            return False

        # Check for recent player-noise events (gunfire, water splashes, …)
        noise_events = self.lt.combat_runtime.get_recent_noise_events(max_age=3.0)
        if not noise_events:
            # Clear any expired investigation
            if state.get('investigating_sound') is not None:
                state['investigating_sound'] = None
            return False

        thing_pos = glm.vec3(monster.pos)
        hearing_range = float(monster.properties.get('sight', MONSTER_SIGHT_RANGE))
        current_time = time.perf_counter()

        # Find the closest noise event within (loudness-scaled) hearing range
        best_event = self._nearest_audible_noise(thing_pos, hearing_range, noise_events)

        if best_event is None:
            # No sounds in range
            if state.get('investigating_sound') is not None:
                state['investigating_sound'] = None
            return False

        # Check if investigation has expired (sound is too old)
        sound_pos = glm.vec3(best_event['pos'][0], best_event['pos'][1], best_event['pos'][2])
        sound_age = current_time - best_event['time']

        # If the sound is older than 2 seconds, stop investigating
        if sound_age > 2.0:
            state['investigating_sound'] = None
            return False

        # Check if we've arrived at the sound source (within 64 units)
        _arrive_diff = thing_pos - sound_pos
        if glm.dot(_arrive_diff, _arrive_diff) <= 64.0 * 64.0:
            # Reached the sound location - look around briefly then resume patrol
            if self.monster_debug_active:
                name = monster.properties.get('name', '?')
                debug_log("MonsterAI", f"{name} reached sound location, looking around...")
            state['investigating_sound'] = None
            return False

        # Move toward the sound source
        direction = sound_pos - thing_pos
        dir_len = glm.length(direction)
        if dir_len > 0.001:
            direction = direction / dir_len
            if mtype != 'flying':
                direction = _flatten_to_ground(direction)
        if dir_len > 0.001 and direction is not None:
            step = direction * MONSTER_MOVE_SPEED * delta
            new_pos = thing_pos + step

            if not self._monster_overlaps_wall(new_pos.x, new_pos.y, new_pos.z, MONSTER_WALL_MARGIN):
                monster.pos = [new_pos.x, new_pos.y, new_pos.z]
            else:
                # Try sliding along walls
                slide_x = glm.vec3(thing_pos.x + step.x, thing_pos.y, thing_pos.z)
                slide_z = glm.vec3(thing_pos.x, thing_pos.y, thing_pos.z + step.z)
                if not self._monster_overlaps_wall(slide_x.x, slide_x.y, slide_x.z, MONSTER_WALL_MARGIN):
                    monster.pos = [slide_x.x, slide_x.y, slide_z.z]
                elif not self._monster_overlaps_wall(slide_z.x, slide_z.y, slide_z.z, MONSTER_WALL_MARGIN):
                    monster.pos = [slide_z.x, slide_z.y, slide_z.z]

        state['investigating_sound'] = (sound_pos, current_time + 3.0)

        if self.monster_debug_active:
            name = monster.properties.get('name', '?')
            src = best_event.get('source', 'noise')
            noise_label = {
                'gunfire': 'gunfire', 'player': 'gunfire',
                'water_enter': 'a splash', 'water_exit': 'a splash',
            }.get(src, 'a noise')
            debug_log("MonsterAI",
                f'<a href="filter:{name}" style="color: #FFA726; font-weight: bold; text-decoration: none;">{name}</a> '
                f'<span style="color: #FFA726;">investigating {noise_label} within Range at ({sound_pos.x:.0f}, {sound_pos.y:.0f}, {sound_pos.z:.0f})</span>')
            # Draw debug ray to sound source
            self._debug_rays.append({
                'start': [thing_pos.x, thing_pos.y + 64.0, thing_pos.z],
                'end': [sound_pos.x, sound_pos.y, sound_pos.z],
                'color': 'orange',
            })

        return True

    def _update_monster_patrol(self, monster, state: Dict, mtype: str, delta: float):
        """Move monster along a chain of PathNodes when player is out of sight."""
        if not monster.properties.get('patrol', False):
            if state.get('patrol_at_target'):
                state['patrol_at_target'] = False
                state['patrol_chain'] = []
            state.pop('detour_node', None)
            return

        target_name = monster.properties.get('patrol_target', '') or ''
        if not target_name:
            return

        mname = monster.properties.get('name', '?')
        patrol_mode = str(monster.properties.get('patrol_mode', 'loop')).lower()
        if patrol_mode not in ('loop', 'ping_pong', 'once'):
            patrol_mode = 'loop'

        # ---- Build / rebuild chain when target changes ----
        chain_built_from = state.get('patrol_chain_built_from', '')
        if chain_built_from != target_name or not state.get('patrol_chain'):
            chain = self._build_patrol_chain(target_name, mtype)
            if not chain:
                if state.get('patrol_warn_missing') != target_name:
                    state['patrol_warn_missing'] = target_name
                    node = self._find_path_node_by_name(target_name)
                    if node is None:
                        debug_log("Pathfinding", f"Monster '{mname}' patrol_target '{target_name}' not found.")
                    else:
                        debug_log("Pathfinding", f"Monster '{mname}' (type={mtype}) rejected by PathNode '{target_name}' (affects_type={node.get_affects_type()}).")
                return
            state['patrol_chain'] = chain
            state['patrol_chain_built_from'] = target_name
            state['patrol_chain_idx'] = 0
            state['patrol_chain_dir'] = 1
            state['patrol_at_target'] = False
            state['patrol_waiting'] = False
            state['patrol_wait_remaining'] = 0.0
            state['patrol_finished'] = False
            state['patrol_warn_missing'] = ''
            state['patrol_walking_to'] = ''
            state['detour_node'] = ''
            debug_log("Pathfinding", f"'{mname}' patrol chain built: {' -> '.join(chain)}  (mode={patrol_mode})")

        chain = state.get('patrol_chain', [])
        if not chain:
            return

        if state.get('patrol_finished'):
            return

        idx = state.get('patrol_chain_idx', 0)
        if idx < 0 or idx >= len(chain):
            idx = 0
            state['patrol_chain_idx'] = 0

        current_node_name = chain[idx]
        node = self._find_path_node_by_name(current_node_name)
        if node is None:
            state['patrol_chain'] = []
            return

        # ---- Waiting at node? ----
        if state.get('patrol_waiting'):
            remaining = state.get('patrol_wait_remaining', 0.0) - delta
            if remaining > 0.0:
                state['patrol_wait_remaining'] = remaining
                return
            state['patrol_waiting'] = False
            state['patrol_wait_remaining'] = 0.0
            if self.lt.io_manager:
                self.lt.io_manager.fire_output(node, 'OnWaitEnd')
            debug_log("Pathfinding", f"'{mname}' finished waiting at '{current_node_name}'")
            self._advance_patrol_index(monster, state, chain, patrol_mode, mname)
            if state.get('patrol_finished'):
                return
            idx = state.get('patrol_chain_idx', 0)
            if idx < 0 or idx >= len(chain):
                return
            current_node_name = chain[idx]
            node = self._find_path_node_by_name(current_node_name)
            if node is None:
                state['patrol_chain'] = []
                return

        # ---- Distance to target node ----
        m_pos = glm.vec3(monster.pos)
        n_pos = glm.vec3(node.pos)
        if mtype == 'flying':
            to_node = n_pos - m_pos
            dist_to_node = glm.length(to_node)
        else:
            flat = glm.vec3(n_pos.x - m_pos.x, 0.0, n_pos.z - m_pos.z)
            dist_to_node = glm.length(flat)
            to_node = flat

        radius = node.get_radius()

        # ---- Arrived at current node? ----
        if dist_to_node <= radius:
            if not state.get('patrol_at_target'):
                state['patrol_at_target'] = True
                if self.lt.io_manager:
                    self.lt.io_manager.fire_output(node, 'OnMonsterArrived')
                debug_log("Pathfinding", f"'{mname}' arrived at '{current_node_name}' (dist={dist_to_node:.0f}, radius={radius:.0f})")

            wait = node.get_wait_time()
            if wait > 0.0 and not state.get('patrol_waiting'):
                state['patrol_waiting'] = True
                state['patrol_wait_remaining'] = wait
                if self.lt.io_manager:
                    self.lt.io_manager.fire_output(node, 'OnWaitStart')
                debug_log("Pathfinding", f"'{mname}' waiting {wait:.1f}s at '{current_node_name}'")
                return

            self._advance_patrol_index(monster, state, chain, patrol_mode, mname)
            if state.get('patrol_finished'):
                return
            idx = state.get('patrol_chain_idx', 0)
            if idx < 0 or idx >= len(chain):
                return
            current_node_name = chain[idx]
            node = self._find_path_node_by_name(current_node_name)
            if node is None:
                state['patrol_chain'] = []
                return
            n_pos = glm.vec3(node.pos)
            if mtype == 'flying':
                to_node = n_pos - m_pos
                dist_to_node = glm.length(to_node)
            else:
                flat = glm.vec3(n_pos.x - m_pos.x, 0.0, n_pos.z - m_pos.z)
                dist_to_node = glm.length(flat)
                to_node = flat
            radius = node.get_radius()
            if dist_to_node <= radius:
                return

        # ---- Left a node? ----
        if state.get('patrol_at_target'):
            prev_name = chain[state.get('patrol_chain_idx', 0)]
            prev_node = self._find_path_node_by_name(prev_name)
            if prev_node is not None and self.lt.io_manager:
                self.lt.io_manager.fire_output(prev_node, 'OnMonsterLeft')
            state['patrol_at_target'] = False
            debug_log("Pathfinding", f"'{mname}' left radius of '{prev_name}'")

        if state.get('patrol_walking_to') != current_node_name:
            state['patrol_walking_to'] = current_node_name
            debug_log("Pathfinding", f"'{mname}' patrolling -> '{current_node_name}' (dist={dist_to_node:.0f})")

        # ---- Detour handling ----
        detour_name = state.get('detour_node', '')
        if detour_name:
            detour_node = self._find_path_node_by_name(detour_name)
            if detour_node is None or detour_node.properties.get('disabled', False):
                state['detour_node'] = ''
                debug_log("Pathfinding", f"'{mname}' detour node '{detour_name}' gone – resuming normal patrol")
            else:
                d_pos = glm.vec3(detour_node.pos)
                if mtype == 'flying':
                    d_vec = d_pos - m_pos
                else:
                    d_vec = glm.vec3(d_pos.x - m_pos.x, 0.0, d_pos.z - m_pos.z)
                d_dist = glm.length(d_vec)
                d_radius = detour_node.get_radius()
                if d_dist <= d_radius:
                    state['detour_node'] = ''
                    state['patrol_blocked_count'] = 0
                    debug_log("Pathfinding", f"'{mname}' reached detour node '{detour_name}' – resuming patrol toward '{current_node_name}'")
                    return
                to_node = d_vec
                dist_to_node = d_dist
                speed_mult = detour_node.get_patrol_speed()
        else:
            speed_mult = node.get_patrol_speed()

        # ---- Movement toward node ----
        dir_len = glm.length(to_node)
        if dir_len <= 0.001:
            return
        direction = to_node / dir_len
        if mtype != 'flying':
            direction = _flatten_to_ground(direction)
            if direction is None:
                return      # the node is directly overhead; no way to walk to it

        step = direction * MONSTER_MOVE_SPEED * speed_mult * delta
        new_pos = m_pos + step

        if not self._monster_overlaps_wall(new_pos.x, new_pos.y, new_pos.z, MONSTER_WALL_MARGIN):
            monster.pos = [new_pos.x, new_pos.y, new_pos.z]
            state['patrol_blocked_count'] = 0
        else:
            slide_x = glm.vec3(m_pos.x + step.x, m_pos.y, m_pos.z)
            slide_z = glm.vec3(m_pos.x, m_pos.y, m_pos.z + step.z)
            if not self._monster_overlaps_wall(slide_x.x, slide_x.y, slide_x.z, MONSTER_WALL_MARGIN):
                monster.pos = [slide_x.x, slide_x.y, slide_z.z]
                state['patrol_blocked_count'] = 0
            elif not self._monster_overlaps_wall(slide_z.x, slide_z.y, slide_z.z, MONSTER_WALL_MARGIN):
                monster.pos = [slide_z.x, slide_z.y, slide_z.z]
                state['patrol_blocked_count'] = 0
            else:
                blocked_count = state.get('patrol_blocked_count', 0) + 1
                state['patrol_blocked_count'] = blocked_count
                if blocked_count == 1 or blocked_count % 120 == 0:
                    debug_log("Pathfinding", f"'{mname}' blocked by wall en route to '{current_node_name}' (stuck for {blocked_count} ticks)")

                if blocked_count >= MONSTER_STUCK_THRESHOLD and not state.get('detour_node'):
                    detour = self._find_nearby_detour_node(m_pos, current_node_name, mtype)
                    if detour:
                        state['detour_node'] = detour
                        state['patrol_blocked_count'] = 0
                        debug_log("Pathfinding", f"'{mname}' DETOUR: blocked at '{current_node_name}', switching to '{detour}'")

    def _advance_patrol_index(self, monster, state: Dict, chain: List[str], patrol_mode: str, mname: str):
        """Advance the patrol index, firing OnMonsterLeft on the node left behind.

        The next index is worked out *before* anything is announced, because
        two cases advance to nowhere and must not announce a departure:

        * a ``once`` patrol that has reached the end holds at its final node;
        * a one-node chain advances back onto the node it is already standing
          on.  Announcing that would emit an OnMonsterLeft/OnMonsterArrived
          pair on every tick for the rest of the level - a logic counter wired
          to the node would count 30 arrivals a second.
        """
        if not chain:
            return

        old_idx = state.get('patrol_chain_idx', 0)
        old_name = chain[old_idx] if old_idx < len(chain) else ''
        direction = state.get('patrol_chain_dir', 1)

        new_idx = old_idx + direction

        if patrol_mode == 'loop':
            if new_idx >= len(chain):
                new_idx = 0
            elif new_idx < 0:
                new_idx = len(chain) - 1

        elif patrol_mode == 'ping_pong':
            if new_idx >= len(chain):
                direction = -1
                new_idx = max(0, old_idx - 1)
                if len(chain) == 1:
                    new_idx = 0
                debug_log("Pathfinding", f"'{mname}' ping_pong reverse at end of chain")
            elif new_idx < 0:
                direction = 1
                new_idx = min(len(chain) - 1, old_idx + 1)
                if len(chain) == 1:
                    new_idx = 0
                debug_log("Pathfinding", f"'{mname}' ping_pong reverse at start of chain")
            state['patrol_chain_dir'] = direction

        elif patrol_mode == 'once':
            if new_idx >= len(chain) or new_idx < 0:
                state['patrol_finished'] = True
                debug_log("Pathfinding", f"'{mname}' completed 'once' patrol – holding at '{old_name}'")
                return

        if new_idx == old_idx:
            return      # nowhere to advance to; the monster has not left

        if old_name:
            old_node = self._find_path_node_by_name(old_name)
            if old_node is not None and self.lt.io_manager:
                self.lt.io_manager.fire_output(old_node, 'OnMonsterLeft')
        state['patrol_at_target'] = False
        state['patrol_walking_to'] = ''

        state['patrol_chain_idx'] = new_idx
        next_name = chain[new_idx] if new_idx < len(chain) else ''
        if next_name:
            debug_log("Pathfinding", f"'{mname}' advancing → '{next_name}' (chain idx {new_idx}/{len(chain)-1})")

    def _build_patrol_chain(self, start_name: str, mtype: str) -> List[str]:
        """Walk next_node links to build an ordered patrol chain."""
        chain = []
        visited = set()
        current = start_name
        while current and current not in visited:
            node = self._find_path_node_by_name(current)
            if node is None:
                break
            if not node.accepts_monster_type(mtype):
                break
            visited.add(current)
            chain.append(current)
            current = node.get_next_node_name()
        return chain

    def _find_path_node_by_name(self, name: str):
        """Return PathNode thing with given name, or None.
        Uses LogicThread's name cache for O(1) lookup."""
        if not name or PathNode is None:
            return None
        # Use the O(1) name cache on the parent LogicThread
        entity = self.lt.world_runtime.find_entity_by_name(name)
        if entity is not None and isinstance(entity, PathNode):
            return entity
        return None

    def _find_nearby_detour_node(self, m_pos: glm.vec3, blocked_node_name: str, mtype: str) -> str:
        """Find a nearby PathNode that accepts this monster type to route around an obstacle."""
        if PathNode is None:
            return ''

        best_name = ''
        best_dist = MONSTER_DETOUR_RANGE + 1.0

        for t in self.lt.things:
            if not isinstance(t, PathNode):
                continue
            node_name = t.properties.get('name', '')
            if not node_name or node_name == blocked_node_name:
                continue
            if t.properties.get('disabled', False):
                continue
            if not t.accepts_monster_type(mtype):
                continue

            n_pos = glm.vec3(t.pos)
            if mtype == 'flying':
                diff = n_pos - m_pos
            else:
                diff = glm.vec3(n_pos.x - m_pos.x, 0.0, n_pos.z - m_pos.z)
            dist = glm.length(diff)
            if dist > MONSTER_DETOUR_RANGE or dist < 1.0:
                continue
            if dist >= best_dist:
                continue

            direction = diff / dist
            test_pos = m_pos + direction * MONSTER_MOVE_SPEED * 0.016
            if self._monster_overlaps_wall(test_pos.x, test_pos.y, test_pos.z, MONSTER_WALL_MARGIN):
                continue

            best_dist = dist
            best_name = node_name

        if best_name:
            debug_log("Pathfinding", f"Detour found: {best_name} at distance {best_dist:.0f}")
        return best_name

    # -------------------------------------------------------------------------
    # Helper methods — NOW DELEGATE TO SPATIAL GRID
    # -------------------------------------------------------------------------

    def _has_line_of_sight(self, start: glm.vec3, end: glm.vec3) -> bool:
        """Return True if ray from start to end hits no solid wall brush."""
        if self._grid:
            return self._grid.has_line_of_sight(start, end)

        # Fallback: full brush scan (should not happen in play mode)
        ray_dir = end - start
        ray_len = glm.length(ray_dir)
        if ray_len < 0.001:
            return True
        ray_dir = ray_dir / ray_len

        for brush in self.lt.editor_state.brushes:
            if not is_solid_world_brush(brush):
                continue
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5
            hit, dist = self.lt.combat_runtime.intersect_ray_aabb(start, ray_dir, b_min, b_max)
            if hit and dist < ray_len - 0.1:
                return False
        return True

    def _monster_raycast_down(self, x: float, z: float, start_y: float = 10000.0) -> Optional[float]:
        """Return Y of the highest solid brush surface below (x, z), or None."""
        if self._grid:
            return self._grid.raycast_down(x, z, start_y)

        # Fallback
        best_y = None
        for brush in self.lt.brushes:
            if not is_solid_world_brush(brush):
                continue
            pos = brush['pos']
            size = brush['size']
            bx_min = pos[0] - size[0] * 0.5
            bx_max = pos[0] + size[0] * 0.5
            bz_min = pos[2] - size[2] * 0.5
            bz_max = pos[2] + size[2] * 0.5
            by_max = pos[1] + size[1] * 0.5

            if bx_min <= x <= bx_max and bz_min <= z <= bz_max:
                if by_max <= start_y:
                    if best_y is None or by_max > best_y:
                        best_y = by_max
        return best_y

    def _monster_overlaps_wall(self, mx: float, my: float, mz: float, margin: float) -> bool:
        """Check if a monster-sized box at (mx, my, mz) overlaps any solid wall brush."""
        if self._grid:
            return self._grid.overlaps_wall(mx, my, mz, margin)

        # Fallback
        for brush in self.lt.brushes:
            if not is_solid_world_brush(brush):
                continue
            pos = brush['pos']
            size = brush['size']
            bx_min = pos[0] - size[0] * 0.5
            bx_max = pos[0] + size[0] * 0.5
            by_min = pos[1] - size[1] * 0.5
            by_max = pos[1] + size[1] * 0.5
            bz_min = pos[2] - size[2] * 0.5
            bz_max = pos[2] + size[2] * 0.5

            m_xmin = mx - margin
            m_xmax = mx + margin
            m_ymin = my
            m_ymax = my + 128.0
            m_zmin = mz - margin
            m_zmax = mz + margin

            if (m_xmax > bx_min and m_xmin < bx_max and
                m_ymax > by_min and m_ymin < by_max and
                m_zmax > bz_min and m_zmin < bz_max):
                return True
        return False


class MonsterAIThread(threading.Thread):
    """
    Dedicated thread for running MonsterAI updates.
    Runs at a lower tick rate (default 30 Hz) to reduce contention
    with the main logic thread.
    """
    def __init__(self, logic_thread, monster_ai, lock, tick_rate: int = 30):
        super().__init__(daemon=True, name="MonsterAIThread")
        self.lt = logic_thread
        self.monster_ai = monster_ai
        self.lock = lock
        self.tick_rate = tick_rate
        self.tick_duration = 1.0 / tick_rate
        self.running = False
        self._stop_event = threading.Event()

    def start(self):
        # Set before the thread exists, not in run(): a stop() arriving before
        # run() got going would otherwise be overwritten, leaving a thread
        # nobody holds running for good.
        self.running = True
        super().start()

    def run(self):
        last_time = time.perf_counter()
        accumulator = 0.0

        while self.running:
            current_time = time.perf_counter()
            frame_time = current_time - last_time
            last_time = current_time

            if frame_time > 0.25:
                frame_time = 0.25

            accumulator += frame_time

            # The world is paused (LogicThread.set_world_paused): monsters hold
            # still, and the paused time is dropped rather than caught up on
            # resume, which would fast-forward every monster at once.
            if getattr(self.lt, 'world_paused', False):
                accumulator = 0.0

            while accumulator >= self.tick_duration and self.running:
                started = time.perf_counter()
                with self.lock:
                    #: How much of ``update_ms`` was spent waiting for the
                    #: logic thread to release the lock (Debug Tables).
                    self.lock_wait_ms = (time.perf_counter() - started) * 1000.0
                    try:
                        self.monster_ai.update(self.tick_duration)
                    except Exception:
                        # Same policy as LogicThread.run: one bad update is
                        # logged in full and the AI carries on, rather than
                        # every monster silently freezing for the rest of the
                        # session.
                        import traceback
                        debug_log("MonsterAI", "Unhandled exception in update:\n"
                                  + traceback.format_exc())
                #: Milliseconds the last AI update took, lock wait included
                #: (Debug Tables).
                self.update_ms = (time.perf_counter() - started) * 1000.0
                accumulator -= self.tick_duration

            sleep_time = self.tick_duration - (time.perf_counter() - current_time)
            if sleep_time > 0:
                self._stop_event.wait(sleep_time * 0.9)

    def stop(self):
        self.running = False
        self._stop_event.set()
