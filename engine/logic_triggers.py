"""Trigger and activation runtime delegated from LogicThread.

This module owns trigger detection, polling, use-trigger prompting, trigger
enter/exit dispatch, and persistent hurt-trigger cadence. The LogicThread
remains the simulation owner and is supplied as the runtime host so trigger
actions can access player, I/O, physics, persistence, and plugin state without
duplicating those systems here.
"""

import math
import random

import glm
import numpy as np

from .constants import brush_aabb_bounds
try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


def _trigger_is_once(brush) -> bool:
    """Whether a trigger brush fires only once ('Once', any case)."""
    return str(brush.get('trigger_type', 'multiple')).strip().lower() == 'once'


def _trigger_activation(brush) -> str:
    """'touch' or 'use'. Older editor builds wrote the setting under
    ``trigger_collect_activation``; it is honoured when the real key is absent."""
    value = brush.get('trigger_activation')
    if value is None:
        value = brush.get('trigger_collect_activation', 'touch')
    return str(value or 'touch').strip().lower()


def _trigger_damage(brush):
    """A hurt trigger's damage: the editor's ``hurt_amount``, else ``damage``."""
    value = brush.get('hurt_amount', brush.get('damage', 10))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 10


#: ``trigger_save`` values a trigger may request, and the console command each
#: runs. Anything else (including the default, 'none') does nothing.
_TRIGGER_SAVE_COMMANDS = {'quicksave': 'quicksave', 'quickload': 'quickload'}


def _trigger_save(brush):
    """The console command a trigger's optional save action asks for, or None."""
    return _TRIGGER_SAVE_COMMANDS.get(
        str(brush.get('trigger_save', 'none') or 'none').strip().lower())



class LogicTriggers:
    """Trigger subsystem for a single LogicThread host."""

    def __init__(self, logic):
        self.logic = logic
        self.fired_once_triggers = set()
        self.hurt_trigger_timers = {}
        self._trigger_contacts = {}
        self.player_in_triggers = set()
        self._nonplayer_trigger_contacts = {}
        self._trigger_poll_elapsed = 0.0
        self._trigger_poll_elapsed_by_bid = {}
        self._trigger_use_generation = 0
        self._trigger_use_seen = {}
        self._trigger_use_prompt = ""
        self._trigger_brushes = []
        self._trigger_brush_by_bid = {}
        self._use_trigger_entries = []

    def rebuild_trigger_index(self, brushes):
        """Build the live trigger index from the authoritative brush list."""
        self._trigger_brushes = [
            (brush.get("id") or index, brush)
            for index, brush in enumerate(brushes)
            if brush.get("is_trigger")
        ]
        self._trigger_brush_by_bid = dict(self._trigger_brushes)
        self._refresh_use_triggers()

    def clear_trigger_index(self):
        """Release the trigger index for the finished play session."""
        self._trigger_brushes = []
        self._trigger_brush_by_bid = {}
        self._use_trigger_entries = []

    @staticmethod
    def _trigger_filters(brush):
        """Return configured trigger detection categories.

        Missing filters are the compatibility default: player only.
        """
        filters = brush.get('trigger_filters', ['player'])
        if isinstance(filters, str):
            # Editor-authored filters historically accepted both comma- and
            # whitespace-separated spellings (e.g. "player, props monsters").
            filters = [
                token
                for part in filters.replace(",", " ").split()
                for token in (part,)
            ]
        if not isinstance(filters, (list, tuple, set)):
            filters = ['player']
        return {
            str(name).strip().lower()
            for name in filters
            if str(name).strip().lower() in ('player', 'props', 'monsters')
        }

    def _reset_trigger_state(self):
        """Create or clear all trigger occupancy and scheduler state.

        Containers are cleared in place when they already exist, so any
        holder of a reference (e.g. ``player_in_triggers``) sees the reset.
        """
        logic = self.logic

        def fresh(name):
            getattr(self, name).clear()

        # Active occupants keyed by trigger id, then (entity type, entity id).
        # Each trigger is sampled at its own configured interval; unchanged
        # contacts are retained between that trigger's polls.
        fresh('_trigger_contacts')
        # Mirrors for code that inspects player-only or non-player state.
        fresh('player_in_triggers')
        fresh('_nonplayer_trigger_contacts')
        # Scheduler: wakes every TRIGGER_POLL_TICK and polls only triggers
        # whose own interval has elapsed; never scans at the 60 Hz tick rate.
        self._trigger_poll_elapsed = 0.0
        fresh('_trigger_poll_elapsed_by_bid')
        # Each use-key press gets a generation number consumed independently
        # per trigger, so a fast trigger cannot steal a slower one's press.
        self._trigger_use_generation = 0
        fresh('_trigger_use_seen')
        # Evaluated per tick by _sample_use_prompt; kept as an attribute only
        # so the render state and tests can read the frame's current prompt.
        self._trigger_use_prompt = ""
        self._refresh_use_triggers()

    @staticmethod
    def use_trigger_contains(distance_sq, use_radius):
        """Whether something at *distance_sq* is inside a use trigger's volume.

        **Fio's authored use volume is a sphere.** ``use_radius`` is the exact
        activation radius in every direction, which is what a mapper writing
        ``use_radius = 128`` means and what 2.4.2 implemented
        (``glm.distance(player, trigger) < use_radius``).

        The spatial broad phase bounds a use trigger with an axis-aligned box
        of the same radius because that is what a batched pass can do cheaply.
        That box is an **acceleration structure, not a second trigger shape**:
        it fully contains the sphere, so it can only ever admit candidates, and
        this predicate is the only thing that decides. Letting the box decide
        made a button usable from up to sqrt(3) times its authored radius on
        the diagonal.

        Squared throughout -- no square roots, and it vectorises, so the
        prompt pass and the firing pass share one definition rather than
        keeping two that can drift apart.
        """
        radius = np.asarray(use_radius, dtype=np.float64)
        result = np.asarray(distance_sq) < radius * radius
        # Preserve the historical scalar-bool API while retaining NumPy
        # vectorisation for batched prompt/culling callers.
        if np.ndim(result) == 0:
            return bool(result)
        return result

    def _refresh_use_triggers(self):
        """The use-activated subset of the trigger list, in trigger order.

        Kept apart because the prompt for a use trigger is evaluated every
        tick while occupancy for everything else stays on the poll scheduler.
        Refreshed wherever the trigger list is rebuilt and again on each poll,
        so an activation mode changed at runtime is picked up.
        """
        # Tolerates being called before the trigger list exists: state reset
        # runs during construction, ahead of the first cache build.
        self._use_trigger_entries = [
            (bid, brush)
            for bid, brush in self._trigger_brushes
            if _trigger_activation(brush) == 'use'
        ]

    def _use_prompt_candidates(self):
        """(bid, brush, centre, radius) for every use trigger a prompt may name."""
        for bid, brush in self._use_trigger_entries:
            if brush.get('disabled', False):
                continue
            if 'player' not in self._trigger_filters(brush):
                continue
            # A spent 'once' trigger does nothing, so it must not keep
            # advertising itself -- 2.4.2 suppressed the prompt for exactly
            # this case and the rewrite dropped the check.
            if _trigger_is_once(brush) and bid in self.fired_once_triggers:
                continue
            centre = brush.get('pos', (0.0, 0.0, 0.0))
            yield (bid, brush,
                   (float(centre[0]), float(centre[1]), float(centre[2])),
                   float(brush.get('use_radius', 96.0)))

    def _sample_use_prompt(self):
        """The '[E] ...' line for the use trigger the player is facing, now.

        Occupancy for touch triggers is the expensive pass -- every entity
        against every trigger -- and stays on the poll scheduler. This is only
        the use-activated subset, which is buttons, and it is evaluated against
        the live player position and angle so the prompt appears and clears the
        moment the player moves or turns instead of up to a poll interval
        later. The arithmetic is one batched pass over that subset.
        """
        player = self.logic.player
        if player is None or not self._use_trigger_entries:
            return ""

        candidates = list(self._use_prompt_candidates())
        if not candidates:
            return ""

        centres = np.asarray([c[2] for c in candidates], dtype=np.float64)
        radii = np.asarray([c[3] for c in candidates], dtype=np.float64)
        pos = player.pos
        origin = np.asarray(
            (float(pos[0]), float(pos[1]), float(pos[2])), dtype=np.float64)

        offset = centres - origin
        distance_sq = np.einsum('ij,ij->i', offset, offset)
        in_range = self.use_trigger_contains(distance_sq, radii)
        if not in_range.any():
            return ""

        forward = np.asarray(
            (math.sin(player.angle), 0.0, math.cos(player.angle)),
            dtype=np.float64)
        # Facing is undefined when the player stands on the trigger centre;
        # 2.4.2 and the poll path both treat that as facing it.
        coincident = distance_sq <= 1.0e-8
        with np.errstate(invalid='ignore', divide='ignore'):
            facing = (offset @ forward) / np.sqrt(distance_sq)
        usable = in_range & (coincident | (facing > 0.5))
        if not usable.any():
            return ""

        index = int(np.flatnonzero(usable)[0])
        label = candidates[index][1].get('use_label', '') or 'Activate'
        return f"[E] {label}"

    def _trigger_poll_interval(self, brush):
        """Return a valid per-trigger polling interval in seconds."""
        try:
            value = float(brush.get('trigger_poll_interval', 1.0))
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(value) or value <= 0.0:
            return 1.0

        allowed = (1.0, 0.5, 0.25)
        return min(allowed, key=lambda interval: abs(interval - value))

    def _poll_triggers(self, use_key_pressed=False, trigger_ids=None):
        """Run one batched trigger poll for the triggers that are due.

        The scheduler wakes every 0.25 s, but only triggers whose configured
        polling interval has elapsed are included in the NumPy broad-phase.
        With the default 1.0 s setting this preserves the old 1 Hz workload.
        """
        if not self.logic.player:
            return

        if use_key_pressed:
            self._trigger_use_generation += 1

        if trigger_ids is None:
            trigger_ids = {
                bid for bid, _ in self._trigger_brushes
            }
        else:
            trigger_ids = set(trigger_ids)

        if not trigger_ids:
            return

        # The use-activated subset can change if a brush's activation mode is
        # edited mid-session; refreshing it here keeps the per-tick prompt pass
        # correct without walking the whole trigger list every frame.
        self._refresh_use_triggers()

        # Snapshot the trigger AABBs due for this poll.
        trigger_entries = []
        polled_ids = set()
        for bid, brush in self._trigger_brushes:
            if bid not in trigger_ids:
                continue

            polled_ids.add(bid)
            if brush.get('disabled', False):
                continue

            activation = _trigger_activation(brush)
            if activation == 'use':
                center = brush.get('pos', (0.0, 0.0, 0.0))
                radius = float(brush.get('use_radius', 96.0))
                bounds = (
                    float(center[0]) - radius,
                    float(center[1]) - radius,
                    float(center[2]) - radius,
                    float(center[0]) + radius,
                    float(center[1]) + radius,
                    float(center[2]) + radius,
                )
            else:
                bounds = brush_aabb_bounds(brush)

            filters = self._trigger_filters(brush)
            filter_mask = (
                (1 if 'player' in filters else 0) |
                (2 if 'props' in filters else 0) |
                (4 if 'monsters' in filters else 0)
            )
            if filter_mask:
                trigger_entries.append(
                    (bid, brush, bounds, filter_mask, activation)
                )

        # Preserve contacts for triggers that were not due. Replace only the
        # state belonging to triggers sampled on this pass.
        new_contacts = dict(self._trigger_contacts)
        for bid in polled_ids:
            new_contacts.pop(bid, None)

        if not trigger_entries:
            self._trigger_contacts = new_contacts
            self.player_in_triggers = {
                bid for bid, contacts in new_contacts.items()
                if any(entity_type == 'player' for entity_type, _ in contacts)
            }
            self._nonplayer_trigger_contacts = {
                bid: {
                    contact for contact in contacts
                    if contact[0] != 'player'
                }
                for bid, contacts in new_contacts.items()
                if any(contact[0] != 'player' for contact in contacts)
            }
            return

        # ------------------------------------------------------------------
        # Snapshot ALL eligible entities into one compact array.
        # The first row is always the player; props and monsters follow.
        # ------------------------------------------------------------------
        entities = [self.logic.player]
        entity_types = [1]  # player
        entity_ids = [id(self.logic.player)]

        for entity in (self.logic.prop_runtime.props if True else ()):
            if not getattr(entity, 'properties', {}).get('disabled', False):
                entities.append(entity)
                entity_types.append(2)
                entity_ids.append(id(entity))

        for entity in self.logic.world_runtime.monster_things:
            if not getattr(entity, 'properties', {}).get('disabled', False):
                entities.append(entity)
                entity_types.append(4)
                entity_ids.append(id(entity))

        positions = np.asarray(
            [entity.pos for entity in entities],
            dtype=np.float32,
        )
        entity_type_mask = np.asarray(entity_types, dtype=np.uint8)

        # ------------------------------------------------------------------
        # ONE vectorised broad-phase over only the trigger subset that is due.
        # No Python entity × trigger nested loop.
        # ------------------------------------------------------------------
        trigger_bounds = np.asarray(
            [entry[2] for entry in trigger_entries],
            dtype=np.float32,
        )
        inside = (
            (positions[:, None, 0] >= trigger_bounds[None, :, 0]) &
            (positions[:, None, 0] <= trigger_bounds[None, :, 3]) &
            (positions[:, None, 1] >= trigger_bounds[None, :, 1]) &
            (positions[:, None, 1] <= trigger_bounds[None, :, 4]) &
            (positions[:, None, 2] >= trigger_bounds[None, :, 2]) &
            (positions[:, None, 2] <= trigger_bounds[None, :, 5])
        )

        trigger_filter_masks = np.asarray(
            [entry[3] for entry in trigger_entries],
            dtype=np.uint8,
        )
        inside &= (
            (entity_type_mask[:, None] & trigger_filter_masks[None, :]) != 0
        )

        # Sparse result: only actual overlaps are materialised from NumPy.
        entity_indices, trigger_indices = np.nonzero(inside)

        for entity_index, trigger_index in zip(entity_indices, trigger_indices):
            entry = trigger_entries[int(trigger_index)]
            bid = entry[0]
            entity_type = entity_type_mask[int(entity_index)]
            category = (
                'player' if entity_type == 1
                else 'props' if entity_type == 2
                else 'monsters'
            )
            new_contacts.setdefault(bid, set()).add(
                (category, entity_ids[int(entity_index)])
            )

        old_contacts = self._trigger_contacts

        # ------------------------------------------------------------------
        # Only changed contacts generate trigger enter/exit I/O.
        # Unchanged occupancy produces no I/O work.
        # ------------------------------------------------------------------
        for bid in polled_ids:
            old = old_contacts.get(bid, set())
            new = new_contacts.get(bid, set())
            entered = new - old
            exited = old - new

            if entered:
                brush = self._trigger_brush_by_bid.get(bid)
                if brush:
                    for activator_type, entity_id in entered:
                        if activator_type == 'player':
                            activator = self.logic.player
                        elif activator_type == 'props':
                            activator = (self.logic.prop_runtime.by_id(entity_id)
                                         if True else None)
                        else:
                            activator = self.logic.world_runtime.monster_by_id.get(entity_id)

                        if activator is not None:
                            # Use triggers are activation-driven rather than
                            # touch-state-driven; their broad-phase contact is
                            # handled below, but it must not fire OnStartTouch
                            # merely because the player entered its AABB.
                            if _trigger_activation(brush) != 'use':
                                self._on_trigger_enter(
                                    brush,
                                    bid,
                                    activator_type=activator_type,
                                    activator_entity=activator,
                                )

            if exited:
                brush = self._trigger_brush_by_bid.get(bid)
                if brush:
                    for activator_type, entity_id in exited:
                        if activator_type == 'player':
                            activator = self.logic.player
                        elif activator_type == 'props':
                            activator = (self.logic.prop_runtime.by_id(entity_id)
                                         if True else None)
                        else:
                            activator = self.logic.world_runtime.monster_by_id.get(entity_id)

                        if activator is not None:
                            if _trigger_activation(brush) != 'use':
                                self._on_trigger_exit(
                                    brush,                                    bid,
                                    activator_type=activator_type,
                                    activator_entity=activator,
                                )

                    # A player leaving a hurt trigger clears its cadence.
                    if any(entity_type == 'player' for entity_type, _ in exited):
                        self.hurt_trigger_timers.pop(bid, None)

        self._trigger_contacts = new_contacts

        # Maintain the legacy mirrors from the same sampled contact state.
        self.player_in_triggers = {
            bid for bid, contacts in new_contacts.items()
            if any(entity_type == 'player' for entity_type, _ in contacts)
        }
        self._nonplayer_trigger_contacts = {
            bid: {
                contact for contact in contacts
                if contact[0] != 'player'
            }
            for bid, contacts in new_contacts.items()
            if any(contact[0] != 'player' for contact in contacts)
        }

        # ------------------------------------------------------------------
        # Use triggers: broad-phase already identified candidate player
        # contacts. Facing and the queued use-key generation are the
        # narrow-phase.
        # ------------------------------------------------------------------
        for trigger_index, entry in enumerate(trigger_entries):
            bid, brush, bounds, _, activation = entry
            if activation != 'use':
                continue

            generation = self._trigger_use_generation
            last_seen = self._trigger_use_seen.get(bid, generation)
            use_edge = last_seen < generation
            self._trigger_use_seen[bid] = generation

            if not inside[0, trigger_index]:
                continue
            if not use_edge:
                continue

            center = np.asarray(
                brush.get('pos', (0.0, 0.0, 0.0)),
                dtype=np.float32,
            )
            offset = center - positions[0]
            distance_sq = float(np.dot(offset, offset))
            # The sphere is what decides; the broad-phase box only nominated
            # this trigger as a candidate. Same predicate the prompt uses, so
            # what the player is shown and what pressing E does cannot drift.
            if not self.use_trigger_contains(
                    distance_sq, float(brush.get('use_radius', 96.0))):
                continue
            if distance_sq > 1.0e-8:
                to_trigger = offset / math.sqrt(distance_sq)
                p_forward = np.asarray(
                    [math.sin(self.logic.player.angle), 0.0, math.cos(self.logic.player.angle)],
                    dtype=np.float32,
                )
                if float(np.dot(p_forward, to_trigger)) <= 0.5:
                    continue

            if _trigger_is_once(brush) and bid in self.fired_once_triggers:
                continue

            self._on_trigger_enter(
                brush,
                bid,
                activator_type='player',
                activator_entity=self.logic.player,
            )

        # ------------------------------------------------------------------
        # Persistent hurt triggers are evaluated only for triggers sampled on
        # this pass, never on the 60 Hz logic path.
        # ------------------------------------------------------------------
        for bid in polled_ids:
            if bid not in self.player_in_triggers:
                continue
            brush = self._trigger_brush_by_bid.get(bid)
            if (
                brush
                and brush.get('trigger_action') == 'hurt'
                and _trigger_activation(brush) != 'use'
            ):
                self._process_hurt_trigger(
                    brush,
                    bid,
                    self._trigger_poll_interval(brush),
                )

        # Use prompts are no longer sampled here: _sample_use_prompt evaluates
        # them every tick against the live player position and angle, which is
        # both more responsive and cheaper than carrying per-trigger prompt
        # state between polls.

    def _handle_triggers(self, use_key_pressed: bool, delta=None):
        """Schedule trigger polls without scanning occupancy at 60 Hz."""
        if use_key_pressed:
            self._trigger_use_generation += 1

        # The use prompt is evaluated here, every tick, against the live player
        # position and angle -- not republished from the last poll. Sampling it
        # at the poll cadence made it appear up to a poll interval late and
        # linger that long after the player turned away.
        #
        # Set only when there is one. _handle_triggers runs after
        # _handle_interactions and PropSession.tick in _tick_play_mode, so this
        # is the last word on the HUD line before the render state is
        # published; assigning unconditionally wiped the line those earlier
        # stages had just set, which is what silently removed "NEED: <key>",
        # "[E] Open", "[E] Unlock (...)", "[E] Pick up ...",
        # "[E] Complete Level" and "[E] Drop" from the HUD.
        self._trigger_use_prompt = self._sample_use_prompt()
        if self._trigger_use_prompt:
            self.logic.interaction_runtime.current_hud_message = self._trigger_use_prompt

        step = float(delta) if delta is not None else float(self.logic.TICK_DURATION)
        self._trigger_poll_elapsed += max(0.0, step)

        scheduler_tick = self.logic.TRIGGER_POLL_TICK
        # Tolerance: 15 x (1/60) sums to 0.2499999..., which would otherwise
        # push every poll one logic tick late (same epsilon as per-trigger).
        while self._trigger_poll_elapsed + self.logic.TRIGGER_POLL_EPSILON >= scheduler_tick:
            self._trigger_poll_elapsed = max(0.0, self._trigger_poll_elapsed - scheduler_tick)

            due_ids = set()
            for bid, brush in self._trigger_brushes:
                elapsed = (
                    self._trigger_poll_elapsed_by_bid.get(bid, 0.0)
                    + scheduler_tick
                )
                interval = self._trigger_poll_interval(brush)
                if elapsed + self.logic.TRIGGER_POLL_EPSILON >= interval:
                    due_ids.add(bid)
                    elapsed %= interval
                self._trigger_poll_elapsed_by_bid[bid] = elapsed

            if due_ids:
                self._poll_triggers(trigger_ids=due_ids)

    def _apply_player_damage(self, damage):
        with self.logic._player_damage_lock:
            if self.logic.god_mode:
                return
            was_alive = self.logic.player_health > 0
            self.logic.player_health = max(0, self.logic.player_health - damage)
            if self.logic.buddha_mode and self.logic.player_health < 2:
                self.logic.player_health = 2
            became_dead = was_alive and self.logic.player_health <= 0
            took_damage = was_alive and damage > 0

            # Queue the pain response at the instant damage is applied. Copy the
            # player position so subsequent movement cannot move the sound
            # source before the render thread consumes the request.
            pain_position = None
            if took_damage and self.logic.player:
                pain_position = (
                    float(self.logic.player.pos.x),
                    float(self.logic.player.pos.y),
                    float(self.logic.player.pos.z),
                )

        if took_damage and pain_position is not None:
            pain_file = random.choice((
                "assets/sounds/pain01.mp3",
                "assets/sounds/pain02.mp3",
                "assets/sounds/pain03.mp3",
            ))
            self.logic.game_state.queue_sound({
                "file": pain_file,
                "volume": 1.0,
                "position": pain_position,
                "radius": 512.0,
            })

        # Emit outside the lock so a handler can't deadlock on the damage path.
        self.logic._plugin_emit("player_damage", damage=damage, health=self.logic.player_health)
        if became_dead:
            self.logic._plugin_emit("player_death")

    def _on_trigger_enter(
        self,
        brush: dict,
        trigger_id: int,
        activator_type='player',
        activator_entity=None,
    ):
        # Authored as 'Once'/'Multiple' by the editor and the shipped maps; the
        # raw compare against 'once' made every Once trigger fire on each entry.
        once = _trigger_is_once(brush)
        if once and trigger_id in self.fired_once_triggers:
            return

        action = brush.get('trigger_action', 'target')

        if action == 'teleport':
            target_node_name = brush.get('target_node', '')
            if target_node_name:
                node = self.logic.world_runtime.find_path_node_by_name(target_node_name)
                if node and (activator_entity or self.logic.player):
                    activator = activator_entity or self.logic.player
                    dest = glm.vec3(node.pos[0], node.pos[1], node.pos[2])
                    if activator is self.logic.player:
                        self.logic.player.pos = dest
                        self.logic.player.velocity = glm.vec3(0, 0, 0)
                        self.logic.portal_runtime.note_player_teleported()
                    else:
                        activator.pos = [dest.x, dest.y, dest.z]
                        physics_world = self.logic.session_runtime.physics_world
                        if physics_world is not None:
                            try:
                                physics_world.sync_entity_position(activator, wake=True)
                            except (AttributeError, TypeError, ValueError):
                                pass
                    if self.logic.io_manager:
                        self.logic.io_manager.fire_output(
                            brush, 'OnTeleport', activator_entity=activator
                        )
                    debug_log("IO", f"Trigger teleported {activator_type} → '{target_node_name}' "
                                     f"({node.pos[0]:.0f}, {node.pos[1]:.0f}, {node.pos[2]:.0f})")
            else:
                debug_log("Warning", "Trigger action 'teleport' used but no target_node set.")

        elif action == 'hurt':
            # Only the player has damage/health semantics at present.
            if activator_type == 'player':
                damage = _trigger_damage(brush)
                self._apply_player_damage(damage)
                self.hurt_trigger_timers[trigger_id] = self.logic.HURT_INTERVAL

        elif action == 'target':
            if self.logic.io_manager:
                self.logic.io_manager.fire_output(
                    brush, 'OnStartTouch', activator_entity=activator_entity
                )
                self.logic.io_manager.fire_output(
                    brush, 'OnTrigger', activator_entity=activator_entity
                )

        # Optional checkpoint: the save/load runs on the UI thread, where the
        # console's quicksave/quickload own the save slot, one frame later.
        save = _trigger_save(brush)
        if save:
            self.logic.game_state.queue_console_command(save)

        self.logic._plugin_emit(
            "trigger_enter",
            trigger=brush,
            action=action,
            trigger_id=trigger_id,
            activator_type=activator_type,
        )
        if once:
            self.fired_once_triggers.add(trigger_id)

    def _on_trigger_exit(
        self,
        brush: dict,
        trigger_id: int,
        activator_type='player',
        activator_entity=None,
    ):
        if self.logic.io_manager:
            self.logic.io_manager.fire_output(
                brush, 'OnEndTouch', activator_entity=activator_entity
            )
        self.logic._plugin_emit(
            "trigger_exit",
            trigger=brush,
            trigger_id=trigger_id,
            activator_type=activator_type,
        )

    def _process_hurt_trigger(self, brush: dict, trigger_id: int, poll_interval=1.0):
        if trigger_id in self.hurt_trigger_timers:
            self.hurt_trigger_timers[trigger_id] -= float(poll_interval)
            if self.hurt_trigger_timers[trigger_id] <= 0:
                damage = _trigger_damage(brush)
                self._apply_player_damage(damage)
                self.hurt_trigger_timers[trigger_id] = self.logic.HURT_INTERVAL


