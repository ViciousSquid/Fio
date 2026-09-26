"""
Input Handlers for Fio I/O System

This module registers all input handlers that define what happens when
an input is called on an entity.

Handler signature: (entity, parameter: str, logic_thread) -> None
"""

from .io_system import IOManager, authored_flag, set_authored_flag
from . import state_values as _sv
import glm
import os
import time

# Import debug logger - with fallback to print if not available
try:
    from .debug_console import debug_log
except ImportError:
    try:
        from editor.debug_console import debug_log
    except ImportError:
        def debug_log(category, message):
            print(f"[{category}] {message}")

try:
    from editor.things import ENTITY_TYPES
except ImportError:
    ENTITY_TYPES = {}


def register_all_input_handlers(io_manager: IOManager):
    """Register all input handlers with the I/O manager."""
    
    # ==========================================================================
    # LIGHT INPUTS
    # ==========================================================================
    
    def light_turn_on(entity, param, logic):
        entity.properties['state'] = 'on'
        logic.io_manager.fire_output(entity, 'OnTurnedOn')
    
    def light_turn_off(entity, param, logic):
        entity.properties['state'] = 'off'
        logic.io_manager.fire_output(entity, 'OnTurnedOff')
    
    def light_toggle(entity, param, logic):
        current = entity.properties.get('state', 'on')
        if current == 'on':
            light_turn_off(entity, param, logic)
        else:
            light_turn_on(entity, param, logic)
    
    def light_set_brightness(entity, param, logic):
        try:
            value = float(param) if param else 1.0
            entity.properties['intensity'] = max(0.0, min(10.0, value))
        except ValueError:
            pass
    
    def light_set_color(entity, param, logic):
        """Set color from 'R G B' string (0-255)."""
        try:
            parts = param.split()
            if len(parts) >= 3:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                entity.properties['colour'] = [r, g, b]
        except (ValueError, IndexError):
            pass

    def _light_shadows_on(entity):
        """Read casts_shadows robustly (may be a bool or a "true"/"false" str)."""
        val = entity.properties.get('casts_shadows', False)
        if isinstance(val, str):
            return val.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(val)

    def light_enable_shadows(entity, param, logic):
        entity.properties['casts_shadows'] = True
        logic.io_manager.fire_output(entity, 'OnShadowsEnabled')

    def light_disable_shadows(entity, param, logic):
        entity.properties['casts_shadows'] = False
        logic.io_manager.fire_output(entity, 'OnShadowsDisabled')

    def light_toggle_shadows(entity, param, logic):
        if _light_shadows_on(entity):
            light_disable_shadows(entity, param, logic)
        else:
            light_enable_shadows(entity, param, logic)

    def _light_nominal_intensity(entity):
        """The intensity a light fades *up* to.  Captured once so repeated
        FadeOut/FadeIn cycles return to the light's authored brightness rather
        than to whatever (possibly 0) value it currently sits at.

        Stored as a plain instance attribute (not in ``properties``) so it is
        never serialized into saved map files."""
        nominal = getattr(entity, '_fade_nominal', None)
        if nominal is None:
            cur = float(entity.properties.get('intensity', 1.0))
            nominal = cur if cur > 0.0 else 1.0
            try:
                entity._fade_nominal = nominal
            except (AttributeError, TypeError):
                pass
        return float(nominal)

    def _start_light_fade(entity, logic, target, duration, end_off):
        if not hasattr(logic, 'light_fade_states'):
            logic.light_fade_states = {}
        try:
            duration = max(0.0, float(duration))
        except (ValueError, TypeError):
            duration = 1.0
        start = float(entity.properties.get('intensity', 0.0))
        if duration <= 0.0:
            # Instant: apply immediately, no per-frame state needed.
            entity.properties['intensity'] = target
            entity.properties['state'] = 'off' if end_off else 'on'
            logic.light_fade_states.pop(id(entity), None)
            return
        logic.light_fade_states[id(entity)] = {
            'entity':   entity,
            'from':     start,
            'to':       target,
            'elapsed':  0.0,
            'duration': duration,
            'end_off':  end_off,
        }

    def light_fade_in(entity, param, logic):
        """Fade the light up to its nominal intensity over `param` seconds."""
        target = _light_nominal_intensity(entity)
        # A light that was off starts its fade from black.
        if entity.properties.get('state', 'on') != 'on':
            entity.properties['intensity'] = 0.0
        entity.properties['state'] = 'on'
        _start_light_fade(entity, logic, target, param or 1.0, end_off=False)
        logic.io_manager.fire_output(entity, 'OnTurnedOn')

    def light_fade_out(entity, param, logic):
        """Fade the light down to zero over `param` seconds, then turn off."""
        # Remember the current brightness so a later FadeIn returns to it.
        _light_nominal_intensity(entity)
        _start_light_fade(entity, logic, 0.0, param or 1.0, end_off=True)

    io_manager.register_input_handler('light', 'turnon', light_turn_on)
    io_manager.register_input_handler('light', 'turnoff', light_turn_off)
    io_manager.register_input_handler('light', 'toggle', light_toggle)
    io_manager.register_input_handler('light', 'setbrightness', light_set_brightness)
    io_manager.register_input_handler('light', 'setcolor', light_set_color)
    io_manager.register_input_handler('light', 'enableshadows', light_enable_shadows)
    io_manager.register_input_handler('light', 'disableshadows', light_disable_shadows)
    io_manager.register_input_handler('light', 'toggleshadows', light_toggle_shadows)
    io_manager.register_input_handler('light', 'fadein', light_fade_in)
    io_manager.register_input_handler('light', 'fadeout', light_fade_out)
    
    # ==========================================================================
    # EFFECT INPUTS
    # ==========================================================================

    def effect_set_type(entity, param, logic):
        """Set the Effect TYPE by name and notify connected outputs."""
        effect_type = str(param or "").strip().upper()
        if not entity.set_effect_type(effect_type):
            return

        table = getattr(logic, '_entity_table', None)
        if table is not None:
            slot = table.slot_of_id.get(entity.properties.get('id'))
            if slot is not None:
                slot = int(slot)
                table.refresh_rows([entity], [slot])
                # A SetType-to-EXPLOSION switch is not a trigger. It leaves
                # EXPLOSION dormant until Explode is received.
                animated = table.effect_type[slot] != 1
                table.effect_active[slot] = animated
                table.effect_alive[slot] = animated
                table.light_enabled[slot] = (
                    bool(table.effect_light_enabled[slot])
                    if animated
                    else False
                )

        logic.io_manager.fire_output(
            entity, 'OnChanged', value=entity.properties['effect_type']
        )

    def effect_explode(entity, param, logic):
        """Switch an Effect to EXPLOSION permanently and play it once."""
        now = time.perf_counter()
        if not entity.trigger_explosion(now):
            return

        game_state = getattr(logic, 'game_state', None)
        if game_state is None and hasattr(logic, 'io_manager'):
            game_state = logic.io_manager.get_game_state()
        if (
            game_state is not None
            and not bool(entity.properties.get('silent', False))
        ):
            try:
                source_position = [
                    float(entity.pos.x),
                    float(entity.pos.y),
                    float(entity.pos.z),
                ]
            except AttributeError:
                source_position = [
                    float(entity.pos[0]),
                    float(entity.pos[1]),
                    float(entity.pos[2]),
                ]
            game_state.queue_sound({
                'action': 'play',
                'file': 'assets/sounds/explode.mp3',
                'volume': 1.0,
                'position': source_position,
            })

        table = getattr(logic, '_entity_table', None)
        if table is None:
            return
        slot = table.slot_of_id.get(entity.properties.get('id'))
        if slot is None:
            return

        slot = int(slot)
        table.effect_type[slot] = 1  # EXPLOSION
        table.effect_preview[slot] = False
        table.effect_spawn_time[slot] = now
        table.effect_elapsed[slot] = 0.0
        table.effect_active[slot] = True
        table.effect_alive[slot] = True

        value = entity.properties.get('light_colour', [255, 165, 70])
        try:
            rgb = [
                max(0.0, min(1.0, float(value[i]) / 255.0))
                for i in range(3)
            ]
        except (TypeError, ValueError, IndexError):
            rgb = [1.0, 165.0 / 255.0, 70.0 / 255.0]
        table.effect_light_color[slot] = rgb
        table.light_color[slot] = rgb
        table.light_enabled[slot] = bool(
            entity.properties.get('light_enabled', True)
        )

    io_manager.register_input_handler('effect', 'settype', effect_set_type)
    io_manager.register_input_handler('effect', 'explode', effect_explode)

    # ==========================================================================
    # DOOR INPUTS
    # ==========================================================================
    
    def door_open(entity, param, logic):
        """Open a door brush."""
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        
        if idx not in logic.door_states:
            if 'original_pos' not in entity:
                entity['original_pos'] = list(entity['pos'])
            logic.door_states[idx] = {
                'progress': 0.0,
                'state': 'closed',
                'open_timer': 0.0
            }
        
        state = logic.door_states[idx]
        # A door that is closing or has been stopped part-way is not open, and
        # Open should send it back up rather than do nothing — the old check
        # accepted only 'closed', so a door caught mid-close ignored the input
        # until it had finished shutting.
        if state['state'] in ('closed', 'closing', 'stopped'):
            state['state'] = 'opening'
            logic.io_manager.fire_output(entity, 'OnOpen')

    def door_close(entity, param, logic):
        """Close a door brush."""
        idx = _get_brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        
        state = logic.door_states[idx]
        if state['state'] in ('open', 'opening', 'stopped'):
            state['state'] = 'closing'
            logic.io_manager.fire_output(entity, 'OnClose')

    def door_stop(entity, param, logic):
        """Halt a moving door where it is.

        'stopped' is a state the door animation simply has no branch for, so
        the door holds its current offset and costs nothing until something
        sends it on its way again.  A door that is not moving is left alone.
        """
        idx = _get_brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        state = logic.door_states[idx]
        if state['state'] in ('opening', 'closing'):
            state['state'] = 'stopped'

    def door_reverse(entity, param, logic):
        """Send a door back the way it came.

        A stopped door resumes in whichever direction is further from where it
        already is, which is the only reading of "reverse" that does something
        for a door halted in the middle.
        """
        idx = _get_brush_index(entity, logic)
        if idx < 0 or idx not in logic.door_states:
            return
        state = logic.door_states[idx]
        current = state['state']
        if current == 'opening':
            door_close(entity, param, logic)
        elif current == 'closing':
            door_open(entity, param, logic)
        elif current == 'stopped':
            if state.get('progress', 0.0) >= 0.5:
                door_close(entity, param, logic)
            else:
                door_open(entity, param, logic)
        else:
            door_toggle(entity, param, logic)
    
    def door_toggle(entity, param, logic):
        """Toggle door open/closed."""
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        
        if idx in logic.door_states:
            state = logic.door_states[idx]
            if state['state'] == 'closed':
                door_open(entity, param, logic)
            elif state['state'] == 'open':
                door_close(entity, param, logic)
        else:
            door_open(entity, param, logic)
    
    def door_lock(entity, param, logic):
        entity['door_locked'] = True
    
    def door_unlock(entity, param, logic):
        entity['door_locked'] = False
    
    def door_set_speed(entity, param, logic):
        try:
            entity['speed'] = float(param) if param else 128.0
        except ValueError:
            pass
    
    io_manager.register_input_handler('door', 'open', door_open)
    io_manager.register_input_handler('door', 'close', door_close)
    io_manager.register_input_handler('door', 'toggle', door_toggle)
    io_manager.register_input_handler('door', 'stop', door_stop)
    io_manager.register_input_handler('door', 'reverse', door_reverse)
    io_manager.register_input_handler('door', 'lock', door_lock)
    io_manager.register_input_handler('door', 'unlock', door_unlock)
    io_manager.register_input_handler('door', 'setspeed', door_set_speed)
    
    # ==========================================================================
    # MOVER INPUTS
    # ==========================================================================
    
    def mover_open(entity, param, logic):
        entity['start_on'] = True
        idx = _get_brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]['forward'] = True
    
    def mover_close(entity, param, logic):
        entity['start_on'] = True
        idx = _get_brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            logic.mover_states[idx]['forward'] = False
    
    def mover_toggle(entity, param, logic):
        entity['start_on'] = not entity.get('start_on', False)
    
    def mover_set_position(entity, param, logic):
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        try:
            value = max(0.0, min(1.0, float(param)))
            if idx in logic.mover_states:
                logic.mover_states[idx]['progress'] = value
        except ValueError:
            pass
    
    def mover_enable(entity, param, logic):
        entity['start_on'] = True

    def mover_disable(entity, param, logic):
        entity['start_on'] = False

    def mover_set_speed(entity, param, logic):
        try:
            entity['speed'] = max(0.0, float(param)) if param else 64.0
        except (ValueError, TypeError):
            pass

    def mover_stop(entity, param, logic):
        """Halt the mover where it is, keeping its progress and direction."""
        entity['start_on'] = False

    def mover_reverse(entity, param, logic):
        """Reverse the direction of travel without stopping.

        A mover's motion is a progress value and a direction flag, so reversing
        is flipping the flag — the position it has already reached is kept, and
        it retraces from there.
        """
        idx = _get_brush_index(entity, logic)
        if idx >= 0 and idx in logic.mover_states:
            state = logic.mover_states[idx]
            state['forward'] = not state.get('forward', True)

    io_manager.register_input_handler('mover', 'open', mover_open)
    io_manager.register_input_handler('mover', 'close', mover_close)
    io_manager.register_input_handler('mover', 'toggle', mover_toggle)
    io_manager.register_input_handler('mover', 'setposition', mover_set_position)
    io_manager.register_input_handler('mover', 'enable', mover_enable)
    io_manager.register_input_handler('mover', 'disable', mover_disable)
    io_manager.register_input_handler('mover', 'stop', mover_stop)
    io_manager.register_input_handler('mover', 'reverse', mover_reverse)
    io_manager.register_input_handler('mover', 'setspeed', mover_set_speed)

    # ==========================================================================
    # MOVER — PathNode waypoint inputs
    # ==========================================================================

    def mover_follow_path(entity, param, logic):
        """Switch a mover from direction-based to PathNode chain movement."""
        idx = _get_brush_index(entity, logic)
        if idx < 0:
            return
        target = param or entity.get('path_target', '')
        if not target:
            return

        # ✅ Guard: ensure the state dictionary exists
        if not hasattr(logic, 'mover_path_states'):
            logic.mover_path_states = {}

        entity['path_target'] = target
        entity['start_on'] = True
        if idx not in logic.mover_path_states:
            logic.mover_path_states[idx] = {
                'current_node': target,
                'lerp_t':       0.0,
                'origin':       list(entity['pos']),
                'waiting':      False,
                'wait_remaining': 0.0,
            }
        logic.mover_states.pop(idx, None)

    def mover_stop_path(entity, param, logic):
        """Stop PathNode following and hold position."""
        idx = _get_brush_index(entity, logic)
        if idx >= 0:
            # ✅ Guard: use getattr with default empty dict, then pop safely
            states = getattr(logic, 'mover_path_states', None)
            if states is not None:
                states.pop(idx, None)

    def mover_set_path_target(entity, param, logic):
        """Change the target PathNode name for this mover."""
        if param:
            entity['path_target'] = param

    io_manager.register_input_handler('mover', 'followpath',    mover_follow_path)
    io_manager.register_input_handler('mover', 'stoppath',      mover_stop_path)
    io_manager.register_input_handler('mover', 'setpathtarget', mover_set_path_target)

    # ==========================================================================
    # TRIGGER INPUTS
    # ==========================================================================
    
    def trigger_enable(entity, param, logic):
        entity['disabled'] = False
        debug_log('Trigger', f"Enabled trigger '{entity.get('name', 'unnamed')}'")
    
    def trigger_disable(entity, param, logic):
        entity['disabled'] = True
        debug_log('Trigger', f"Disabled trigger '{entity.get('name', 'unnamed')}'")
    
    def trigger_toggle(entity, param, logic):
        entity['disabled'] = not entity.get('disabled', False)
    
    def trigger_touch_test(entity, param, logic):
        if not logic.player:
            return
        
        pos = glm.vec3(entity['pos'])
        size = glm.vec3(entity['size'])
        half_size = size / 2.0
        min_b = pos - half_size
        max_b = pos + half_size
        
        player_pos = logic.player.pos
        if (min_b.x <= player_pos.x <= max_b.x and
            min_b.y <= player_pos.y <= max_b.y and
            min_b.z <= player_pos.z <= max_b.z):
            logic.io_manager.fire_output(entity, 'OnTrigger')
    
    io_manager.register_input_handler('trigger', 'enable', trigger_enable)
    io_manager.register_input_handler('trigger', 'disable', trigger_disable)
    io_manager.register_input_handler('trigger', 'toggle', trigger_toggle)
    io_manager.register_input_handler('trigger', 'touchtest', trigger_touch_test)

    # ==========================================================================
    # TRIGGER — teleport inputs
    # ==========================================================================

    def trigger_teleport(entity, param, logic):
        """Teleport the player to the named PathNode."""
        target_name = param or entity.get('target_node', '')
        node = logic._find_path_node_by_name(target_name)
        if not node or not logic.player:
            return
        dest = glm.vec3(node.pos[0], node.pos[1], node.pos[2])
        logic.player.pos = dest
        # Zero velocity to prevent carry-over momentum
        logic.player.velocity = glm.vec3(0, 0, 0)
        if logic.io_manager:
            logic.io_manager.fire_output(entity, 'OnTeleport')
        debug_log("IO", f"Trigger teleported player → '{target_name}' ({dest.x:.0f}, {dest.y:.0f}, {dest.z:.0f})")

    def trigger_set_target_node(entity, param, logic):
        """Change the target PathNode name for this trigger."""
        if param:
            entity['target_node'] = param

    io_manager.register_input_handler('trigger', 'teleport',      trigger_teleport)
    io_manager.register_input_handler('trigger', 'settargetnode', trigger_set_target_node)

    # ==========================================================================
    # SPEAKER INPUTS
    # ==========================================================================
    
    def _speaker_game_state(logic):
        """Resolve the game_state a speaker request must be queued on.

        Tries the logic thread directly, then the I/O manager, mirroring the
        original two-path lookup. Returns None when neither is available.
        """
        if getattr(logic, 'game_state', None) is not None:
            return logic.game_state
        if hasattr(logic, 'io_manager'):
            gs = logic.io_manager.get_game_state()
            if gs is not None:
                return gs
        return None

    def speaker_play(entity, param, logic):
        """Start playing sound - queues to main thread via game_state."""
        entity_name = entity.properties.get('name', 'unnamed')
        sound_file = entity.properties.get('sound_file', '')
        volume = float(entity.properties.get('volume', 1.0))
        looping = bool(entity.properties.get('looping', False))

        debug_log('Speaker', f"PlaySound called on '{entity_name}'")
        debug_log('Speaker', f"  sound_file='{sound_file}', volume={volume}, looping={looping}")

        entity.properties['state'] = 'on'
        speaker_id = id(entity)
        logic.active_speakers.add(speaker_id)

        if not sound_file:
            debug_log('Error', f"No sound file configured for speaker '{entity_name}'!")
            return

        game_state = _speaker_game_state(logic)

        if game_state is None:
            debug_log('Error', f"Could not find game_state for speaker '{entity_name}'!")
            return

        # Queue the sound for the main thread to play (thread-safe). ``looping``
        # asks the mixer to repeat it until an explicit StopSound; ``entity_id``
        # lets that stop find and silence this speaker's channel.
        game_state.queue_sound({
            'action': 'play',
            'file': sound_file,
            'volume': volume,
            'looping': looping,
            'entity_id': speaker_id,
        })
        debug_log('Speaker', f"  Queued '{sound_file}'" + (" (looping)" if looping else ""))

        # Fire output event
        logic.io_manager.fire_output(entity, 'OnSoundStarted')
    
    def speaker_stop(entity, param, logic):
        entity.properties['state'] = 'off'
        speaker_id = id(entity)
        logic.active_speakers.discard(speaker_id)
        # Actually silence the channel on the audio thread -- a looping sound
        # would otherwise play forever (StopSound could not reach the mixer).
        game_state = _speaker_game_state(logic)
        if game_state is not None:
            game_state.queue_sound({'action': 'stop', 'entity_id': speaker_id})
        debug_log('Speaker', f"Stopped speaker '{entity.properties.get('name', 'unnamed')}'")
        logic.io_manager.fire_output(entity, 'OnSoundFinished')
    
    def speaker_toggle(entity, param, logic):
        current = entity.properties.get('state', 'off')
        if current == 'on':
            speaker_stop(entity, param, logic)
        else:
            speaker_play(entity, param, logic)
    
    def speaker_set_volume(entity, param, logic):
        try:
            entity.properties['volume'] = max(0.0, min(1.0, float(param)))
        except ValueError:
            pass
    
    io_manager.register_input_handler('speaker', 'playsound', speaker_play)
    io_manager.register_input_handler('speaker', 'stopsound', speaker_stop)
    io_manager.register_input_handler('speaker', 'toggle', speaker_toggle)
    io_manager.register_input_handler('speaker', 'setvolume', speaker_set_volume)
    
    # ==========================================================================
    # PICKUP INPUTS
    # ==========================================================================
    
    def pickup_enable(entity, param, logic):
        entity.properties['disabled'] = False
    
    def pickup_disable(entity, param, logic):
        entity.properties['disabled'] = True
    
    def pickup_respawn(entity, param, logic):
        entity.properties['collected'] = False
        # FIX#3: use id(entity) — matches new collected_pickups key scheme
        logic.collected_pickups.discard(id(entity))
        logic.io_manager.fire_output(entity, 'OnRespawn')
    
    def pickup_set_value(entity, param, logic):
        try:
            entity.properties['value'] = int(param)
        except ValueError:
            pass
    
    io_manager.register_input_handler('pickup', 'enable', pickup_enable)
    io_manager.register_input_handler('pickup', 'disable', pickup_disable)
    io_manager.register_input_handler('pickup', 'respawn', pickup_respawn)
    io_manager.register_input_handler('pickup', 'setvalue', pickup_set_value)
    
    # ==========================================================================
    # PROP INPUTS
    # ==========================================================================

    def prop_enable(entity, param, logic):
        entity.properties['disabled'] = False

    def prop_disable(entity, param, logic):
        entity.properties['disabled'] = True

    def prop_wake(entity, param, logic):
        entity.properties['_physics_awake'] = True

    def prop_drop(entity, param, logic):
        # The active prop runtime observes this one-shot request on its next tick.
        entity.properties['_drop_requested'] = True

    io_manager.register_input_handler('prop', 'enable', prop_enable)
    io_manager.register_input_handler('prop', 'disable', prop_disable)
    io_manager.register_input_handler('prop', 'wake', prop_wake)
    io_manager.register_input_handler('prop', 'drop', prop_drop)

    # ==========================================================================
    # LOGIC_RELAY INPUTS
    # ==========================================================================
    
    def relay_trigger(entity, param, logic):
        """Route one event on, honouring ``disabled`` and ``fire_once``.

        ``fire_once`` is a property the relay has always carried and nothing
        ever read, so a relay marked one-shot in the editor fired every time.
        It latches here instead: the first trigger passes, the rest are dropped
        until ``Reset``.  The parameter rides through, so a relay stays
        transparent to whatever value it is routing.
        """
        props = entity.properties
        if authored_flag(entity, 'disabled'):
            return
        if props.get('fire_once', False):
            if props.get('_relay_fired', False):
                return
            props['_relay_fired'] = True
        logic.io_manager.fire_output(entity, 'OnTrigger', value=param or None)

    def relay_reset(entity, param, logic):
        """Re-arm a one-shot relay so it can fire again."""
        entity.properties.pop('_relay_fired', None)

    def relay_enable(entity, param, logic):
        set_authored_flag(entity, 'disabled', False)

    def relay_disable(entity, param, logic):
        set_authored_flag(entity, 'disabled', True)

    def relay_toggle(entity, param, logic):
        set_authored_flag(entity, 'disabled', not authored_flag(entity, 'disabled'))

    def relay_cancel_pending(entity, param, logic):
        """Cancel any delayed events this relay has already queued."""
        mgr = logic.io_manager
        if not mgr:
            return
        relay_name = entity.properties.get('name', getattr(entity, 'name', ''))
        before = len(mgr.pending_events)
        mgr.pending_events = [
            ev for ev in mgr.pending_events if ev.source_name != relay_name
        ]
        cancelled = before - len(mgr.pending_events)
        if cancelled:
            debug_log('IO', f"LogicRelay '{relay_name}': cancelled {cancelled} pending event(s)")

    io_manager.register_input_handler('logic_relay', 'trigger', relay_trigger)
    io_manager.register_input_handler('logic_relay', 'enable', relay_enable)
    io_manager.register_input_handler('logic_relay', 'disable', relay_disable)
    io_manager.register_input_handler('logic_relay', 'toggle', relay_toggle)
    io_manager.register_input_handler('logic_relay', 'reset', relay_reset)
    io_manager.register_input_handler('logic_relay', 'cancelpending', relay_cancel_pending)
    
    # ==========================================================================
    # LOGIC_GATE INPUTS
    # ==========================================================================
    
    def _gate_key(entity):
        """The identity a gate's signal set is filed under.

        Its UUID, not its name.  Names are a human convenience and two entities
        may share one (a brush and a thing, a duplicate before it is renamed),
        which would have two gates quietly sharing one set of input signals.
        """
        return entity.properties.get('id') or entity.properties.get('name', '')

    def _gate_expected_inputs(entity, logic):
        """How many signals this gate is waiting for.

        The number of *connections* calling its Trigger input — two wires from
        one entity are two signals, and only Trigger counts, so a gate that also
        has a Reset or an Enable wired into it still waits for the right number.

        Read out of the I/O reverse-target index, which is built once per
        connection change and cached against a revision counter, rather than
        rescanning every brush and thing on every signal: a gate in a large
        level used to walk the whole scene each time an input arrived.

        It also counts connections addressed by UUID.  The previous count
        matched target *names* only, so a gate wired the way the editor actually
        wires things (by id) saw zero expected inputs, fell back to one, and an
        AND gate fired on its first signal.
        """
        try:
            from .io_system import count_incoming_connections
        except ImportError:                              # pragma: no cover
            from editor.io_system import count_incoming_connections
        return count_incoming_connections(
            logic.brushes, logic.things,
            target_name=entity.properties.get('name', ''),
            target_id=entity.properties.get('id', ''),
            input_name='Trigger')

    def _gate_evaluate(entity, logic):
        """Test the gate against its current signals and fire the result.

        Evaluation happens because a signal arrived, never on a clock: a gate
        holds a set of asserted inputs and answers a question about that set
        when asked to.
        """
        gate_key = _gate_key(entity)
        active = len(logic.gate_inputs.get(gate_key, ()))
        expected = max(1, _gate_expected_inputs(entity, logic))
        logic_type = str(entity.properties.get('logic_type', 'AND')).upper()

        if logic_type == 'AND':
            result = active >= expected
        elif logic_type == 'OR':
            result = active > 0
        elif logic_type == 'XOR':
            result = active == 1
        elif logic_type == 'NAND':
            result = active < expected
        elif logic_type == 'NOR':
            result = active == 0
        else:
            debug_log('Error', f"LogicGate '{entity.name}': unknown type "
                               f"'{logic_type}' — treating as OR")
            result = active > 0

        logic.io_manager.fire_output(entity, 'OnTrigger' if result else 'OnFalse')
        return result

    def _gate_signal(entity, param, logic, mode):
        """Change one input signal, then evaluate.

        *mode* is 'assert', 'clear' or 'flip'.  ``Trigger`` asserts, and does so
        idempotently: two switches wired into an AND gate close it whether or
        not either switch reports twice.  (Before 2.4 ``Trigger`` *flipped* the
        signal, so a source firing twice silently un-asserted itself and the
        gate could never close.  ``ToggleInput`` keeps that behaviour for maps
        that wanted it.)
        """
        if authored_flag(entity, 'disabled'):
            return
        gate_key = _gate_key(entity)
        signals = logic.gate_inputs.setdefault(gate_key, set())
        source = param.strip() if param else ''
        if not source:
            # An unnamed signal is identified by whoever fired it, so two
            # different sources wired without a parameter still count as two
            # inputs instead of overwriting each other.
            source = logic.io_manager.current_source_id() or 'anonymous'

        if mode == 'assert':
            signals.add(source)
        elif mode == 'clear':
            signals.discard(source)
        else:
            signals.symmetric_difference_update({source})

        _gate_evaluate(entity, logic)

    def gate_trigger(entity, param, logic):
        """Assert one input signal."""
        _gate_signal(entity, param, logic, 'assert')

    def gate_clear_input(entity, param, logic):
        """De-assert one input signal."""
        _gate_signal(entity, param, logic, 'clear')

    def gate_toggle_input(entity, param, logic):
        """Flip one input signal — the pre-2.4 Trigger behaviour."""
        _gate_signal(entity, param, logic, 'flip')

    def gate_evaluate(entity, param, logic):
        """Re-test the gate without changing any signal."""
        if authored_flag(entity, 'disabled'):
            return
        _gate_evaluate(entity, logic)

    def gate_reset(entity, param, logic):
        """De-assert every input signal."""
        logic.gate_inputs.pop(_gate_key(entity), None)

    io_manager.register_input_handler('logic_gate', 'trigger', gate_trigger)
    io_manager.register_input_handler('logic_gate', 'clearinput', gate_clear_input)
    io_manager.register_input_handler('logic_gate', 'toggleinput', gate_toggle_input)
    io_manager.register_input_handler('logic_gate', 'evaluate', gate_evaluate)
    io_manager.register_input_handler('logic_gate', 'reset', gate_reset)
    io_manager.register_input_handler('logic_gate', 'enable', relay_enable)
    io_manager.register_input_handler('logic_gate', 'disable', relay_disable)
    io_manager.register_input_handler('logic_gate', 'toggle', relay_toggle)
    
    # ==========================================================================
    # LOGIC_TIMER INPUTS
    # ==========================================================================
    
    def _timer_key(entity):
        """The identity a timer's countdown is filed under.

        Its UUID.  ``id(entity)`` was used before, which is a memory address:
        it changes on every load, so a timer's countdown could not survive a
        save, and CPython reuses addresses, so a freed entity's slot could be
        inherited by an unrelated one.
        """
        return entity.properties.get('id') or entity.properties.get('name', '')

    def _timer_arm(entity, logic):
        """Start (or restart) the countdown from the full interval."""
        try:
            interval = max(0.01, float(entity.properties.get('interval', 1.0)))
        except (TypeError, ValueError):
            interval = 1.0
        logic.timer_states[_timer_key(entity)] = {
            'remaining': interval,
            'interval': interval,
        }

    def timer_enable(entity, param, logic):
        entity.properties['timer_enabled'] = True
        _timer_arm(entity, logic)

    def timer_disable(entity, param, logic):
        entity.properties['timer_enabled'] = False

    def timer_toggle(entity, param, logic):
        if entity.properties.get('timer_enabled', False):
            timer_disable(entity, param, logic)
        else:
            timer_enable(entity, param, logic)

    def timer_fire(entity, param, logic):
        logic.io_manager.fire_output(entity, 'OnTimer')

    def timer_set_time(entity, param, logic):
        try:
            entity.properties['interval'] = max(0.01, float(param))
        except (TypeError, ValueError):
            pass

    def timer_reset(entity, param, logic):
        """Reset the countdown to the full interval without firing."""
        _timer_arm(entity, logic)

    io_manager.register_input_handler('logic_timer', 'enable', timer_enable)
    io_manager.register_input_handler('logic_timer', 'disable', timer_disable)
    io_manager.register_input_handler('logic_timer', 'start', timer_enable)
    io_manager.register_input_handler('logic_timer', 'stop', timer_disable)
    io_manager.register_input_handler('logic_timer', 'toggle', timer_toggle)
    io_manager.register_input_handler('logic_timer', 'firetimer', timer_fire)
    io_manager.register_input_handler('logic_timer', 'settime', timer_set_time)
    io_manager.register_input_handler('logic_timer', 'resettimer', timer_reset)
    
    # ==========================================================================
    # MODEL INPUTS
    # ==========================================================================
    
    def model_enable(entity, param, logic):
        entity.properties['hidden'] = False

    def model_disable(entity, param, logic):
        entity.properties['hidden'] = True

    def model_set_skin(entity, param, logic):
        """Record the requested skin index (read by the model renderer)."""
        try:
            entity.properties['skin'] = int(param)
        except (ValueError, TypeError):
            pass

    def model_set_animation(entity, param, logic):
        """Record the requested animation name (read by the model renderer)."""
        if param:
            entity.properties['animation'] = param.strip()

    io_manager.register_input_handler('model', 'enable', model_enable)
    io_manager.register_input_handler('model', 'disable', model_disable)
    io_manager.register_input_handler('model', 'setskin', model_set_skin)
    io_manager.register_input_handler('model', 'setanimation', model_set_animation)

    # ==========================================================================
    # PATH NODE INPUTS
    # (Enable/Disable fall through to the generic 'disabled' toggle; Toggle needs
    #  an explicit handler because the generic dispatcher has no 'toggle' case.)
    # ==========================================================================

    def path_node_toggle(entity, param, logic):
        entity.properties['disabled'] = not entity.properties.get('disabled', False)

    io_manager.register_input_handler('path_node', 'toggle', path_node_toggle)
    
    # ==========================================================================
    # MONSTER INPUTS
    # ==========================================================================
    
    def monster_kill(entity, param, logic):
        entity.properties['_kill'] = True
        logic.io_manager.fire_output(entity, 'OnDeath')
    
    io_manager.register_input_handler('monster', 'kill', monster_kill)
    io_manager.register_input_handler('monster', 'enable', relay_enable)
    io_manager.register_input_handler('monster', 'disable', relay_disable)

    def monster_wake(entity, param, logic):
        """Wake a dormant (triggered=True) monster via I/O."""
        entity.properties['awake'] = True
        entity.properties['triggered'] = False   # clear dormant flag

    def monster_sleep(entity, param, logic):
        """Send a monster back to dormant — the inverse of Wake.

        Sets the same two properties Wake clears, so a monster put to sleep
        behaves exactly like one that has never been woken: the AI skips it, and
        whatever its map authored as a wake condition still applies.
        """
        entity.properties['awake'] = False
        entity.properties['triggered'] = True

    def monster_set_health(entity, param, logic):
        """Set current health.  Zero or less does not kill — use Kill for that.

        Deliberately just a write: the death path belongs to the AI, which owns
        what dying means (dropping aggro, the OnDeath output, the corpse
        sprite), and an input that half-killed a monster here would be a second
        version of it.
        """
        try:
            entity.properties['health'] = int(float(param))
        except (TypeError, ValueError):
            debug_log('Error', f"Monster.SetHealth: bad parameter '{param}'")

    def monster_respawn(entity, param, logic):
        """Revive a dead monster at its spawn health.

        The health to come back with is the value the map authored, recorded
        once when play started (see LogicThread._reset_all_monsters) because
        the live property is mutated by damage.  A parameter overrides it, and
        if neither is available the monster keeps whatever health it has —
        inventing a number here would be inventing behaviour.
        """
        props = entity.properties
        health = None
        if param:
            try:
                health = int(float(param))
            except (TypeError, ValueError):
                health = None
        if health is None:
            spawn = getattr(logic, '_monster_spawn_health', None)
            if isinstance(spawn, dict):
                health = spawn.get(props.get('id'))
        if health is not None:
            props['health'] = health

        props.pop('dead', None)
        props.pop('is_shooting', None)
        props.pop('_aggro_target', None)
        props.pop('_kill', None)
        # Come back asleep or awake exactly as the map says a fresh monster
        # should, rather than always alert.
        props['awake'] = not (props.get('triggered', False)
                              or props.get('wake_on_sight', True))
        if logic.io_manager:
            logic.io_manager.fire_output(entity, 'OnRespawn')

    def monster_set_target(entity, param, logic):
        """Override pursuit target by entity name (empty string = back to player)."""
        entity.properties['target_name'] = param.strip() if param else ''

    io_manager.register_input_handler('monster', 'wake', monster_wake)
    io_manager.register_input_handler('monster', 'sleep', monster_sleep)
    io_manager.register_input_handler('monster', 'sethealth', monster_set_health)
    io_manager.register_input_handler('monster', 'respawn', monster_respawn)
    io_manager.register_input_handler('monster', 'settarget', monster_set_target)

    # ==========================================================================
    # BRUSH HIDE / SHOW / TINT INPUTS
    # (Brushes are dicts — these handlers work for brush, door, mover, trigger)
    # ==========================================================================

    def brush_hide(entity, param, logic):
        """Hide a brush (set hidden flag — renderer skips it)."""
        entity['hidden'] = True
        name = entity.get('name', 'unnamed')
        debug_log('IO', f"Brush '{name}' hidden")

    def brush_show(entity, param, logic):
        """Show a brush (clear hidden flag)."""
        entity['hidden'] = False
        name = entity.get('name', 'unnamed')
        debug_log('IO', f"Brush '{name}' shown")

    def brush_toggle_vis(entity, param, logic):
        """Toggle brush visibility."""
        entity['hidden'] = not entity.get('hidden', False)
        name = entity.get('name', 'unnamed')
        state = "hidden" if entity.get('hidden') else "visible"
        debug_log('IO', f"Brush '{name}' toggled → {state}")

    def brush_set_tint(entity, param, logic):
        """Set tint colour on a brush.  Param: 'R G B' (0-255)."""
        try:
            parts = param.split()
            if len(parts) >= 3:
                r = max(0, min(255, int(parts[0])))
                g = max(0, min(255, int(parts[1])))
                b = max(0, min(255, int(parts[2])))
                entity['tint'] = [r, g, b]
                name = entity.get('name', 'unnamed')
                debug_log('IO', f"Brush '{name}' tint set to ({r}, {g}, {b})")
        except (ValueError, IndexError):
            debug_log('Error', f"SetTint: bad parameter '{param}' — expected 'R G B'")

    def brush_clear_tint(entity, param, logic):
        """Remove tint override from a brush."""
        entity.pop('tint', None)
        name = entity.get('name', 'unnamed')
        debug_log('IO', f"Brush '{name}' tint cleared")

    def brush_toggle_solid(entity, param, logic):
        """Toggle a generic brush's solidity (Enable/Disable ↔ 'disabled')."""
        entity['disabled'] = not entity.get('disabled', False)
        name = entity.get('name', 'unnamed')
        state = "non-solid" if entity.get('disabled') else "solid"
        debug_log('IO', f"Brush '{name}' toggled → {state}")

    # Register for every brush-based type
    for btype in ('brush', 'door', 'mover', 'trigger'):
        io_manager.register_input_handler(btype, 'hide', brush_hide)
        io_manager.register_input_handler(btype, 'show', brush_show)
        io_manager.register_input_handler(btype, 'togglevisibility', brush_toggle_vis)
        io_manager.register_input_handler(btype, 'settint', brush_set_tint)
        io_manager.register_input_handler(btype, 'cleartint', brush_clear_tint)

    # Generic solid brushes get a solidity Toggle (door/mover/trigger define
    # their own domain-specific Toggle handlers above, so only 'brush' here).
    io_manager.register_input_handler('brush', 'toggle', brush_toggle_solid)

    # ==========================================================================
    # THING (ENTITY) HIDE / SHOW INPUTS
    # (Things have .properties dict — covers monster, light, speaker, pickup, model)
    # ==========================================================================

    def thing_hide(entity, param, logic):
        """Hide a thing entity."""
        entity.properties['hidden'] = True
        name = entity.properties.get('name', 'unnamed')
        debug_log('IO', f"Entity '{name}' hidden")

    def thing_show(entity, param, logic):
        """Show a thing entity."""
        entity.properties['hidden'] = False
        name = entity.properties.get('name', 'unnamed')
        debug_log('IO', f"Entity '{name}' shown")

    def thing_toggle_vis(entity, param, logic):
        """Toggle thing visibility."""
        entity.properties['hidden'] = not entity.properties.get('hidden', False)
        name = entity.properties.get('name', 'unnamed')
        state = "hidden" if entity.properties.get('hidden') else "visible"
        debug_log('IO', f"Entity '{name}' toggled → {state}")

    # Register for every thing-based type that declares Hide/Show
    for ttype in ('monster', 'light', 'speaker', 'pickup', 'model', 'prop'):
        io_manager.register_input_handler(ttype, 'hide', thing_hide)
        io_manager.register_input_handler(ttype, 'show', thing_show)
        io_manager.register_input_handler(ttype, 'togglevisibility', thing_toggle_vis)

    # ==========================================================================
    # LEVEL CHANGER INPUTS
    # ==========================================================================
    
    def levelchanger_changelevel(entity, param, logic):
        # We route to change_level() because it is more robust
        if hasattr(entity, 'change_level'):
            entity.change_level(param)
            
    io_manager.register_input_handler('levelchanger', 'changelevel', levelchanger_changelevel)
    io_manager.register_input_handler('levelchanger', 'trigger', levelchanger_changelevel)

    # ==========================================================================
    # LOGIC CAMERA INPUTS
    # ==========================================================================

    def camera_start(entity, param, logic):
        """Begin the cinematic camera sequence along a PathNode chain."""
        target = entity.properties.get('path_target', '')
        node = logic._find_path_node_by_name(target)
        if not node:
            debug_log("IO", f"LogicCamera '{entity.name}': path_target "
                      f"'{target}' not found — aborting start.")
            return
        speed = float(entity.properties.get('speed', 200.0))
        fov   = float(entity.properties.get('fov_override', 0.0))
        logic.cinematic_state = {
            'active':       True,
            'paused':       False,
            'entity':       entity,
            'current_node': target,
            'lerp_t':       0.0,
            'origin':       list(node.pos),
            'speed':        speed,
            'fov':          fov if fov > 0 else None,
            'look_ahead':   entity.properties.get('look_ahead', True),
        }
        if logic.io_manager:
            logic.io_manager.fire_output(entity, 'OnStart')

    def camera_stop(entity, param, logic):
        """Abort and return camera to the player."""
        logic.cinematic_state = None

    def camera_pause(entity, param, logic):
        """Freeze camera at current chain position."""
        if logic.cinematic_state:
            logic.cinematic_state['paused'] = True

    def camera_resume(entity, param, logic):
        """Continue a paused sequence."""
        if logic.cinematic_state:
            logic.cinematic_state['paused'] = False

    def camera_set_speed(entity, param, logic):
        """Override travel speed."""
        if logic.cinematic_state:
            try:
                logic.cinematic_state['speed'] = max(1.0, float(param))
            except (TypeError, ValueError):
                pass

    io_manager.register_input_handler('logic_camera', 'start',    camera_start)
    io_manager.register_input_handler('logic_camera', 'stop',     camera_stop)
    io_manager.register_input_handler('logic_camera', 'pause',    camera_pause)
    io_manager.register_input_handler('logic_camera', 'resume',   camera_resume)
    io_manager.register_input_handler('logic_camera', 'setspeed', camera_set_speed)

    # ==========================================================================
    # LOGIC COMMAND INPUTS
    # ==========================================================================

    def command_run(entity, param, logic):
        """Queue a console command for execution on the UI thread.

        The command comes from the connection parameter, or falls back to the
        entity's 'command' property. Execution is marshalled through the game
        state's console-command queue so it runs on the main thread (see
        QtGameView._process_console_command_queue) — never touching Qt from the
        logic thread.
        """
        if entity.properties.get('disabled', False):
            return
        cmd = (param or entity.properties.get('command', '') or '').strip()
        if not cmd:
            debug_log("IO", f"LogicCommand '{entity.name}': no command to run.")
            return
        gs = getattr(logic, 'game_state', None)
        if gs is not None and hasattr(gs, 'queue_console_command'):
            gs.queue_console_command(cmd)
            debug_log("IO", f"LogicCommand '{entity.name}': queued '{cmd}'")
            if logic.io_manager:
                logic.io_manager.fire_output(entity, 'OnCommand', cmd)
        else:
            debug_log("Error", f"LogicCommand '{entity.name}': no console queue available.")

    def command_set(entity, param, logic):
        """Set the default command string this entity will run."""
        entity.properties['command'] = (param or '').strip()

    io_manager.register_input_handler('logic_command', 'runcommand', command_run)
    io_manager.register_input_handler('logic_command', 'trigger',    command_run)
    io_manager.register_input_handler('logic_command', 'setcommand', command_set)
    io_manager.register_input_handler('logic_command', 'enable',     relay_enable)
    io_manager.register_input_handler('logic_command', 'disable',    relay_disable)

    # ==========================================================================
    # LOGIC SPAWNER INPUTS
    # ==========================================================================

    def spawner_spawn(entity, param, logic):
        """Spawn one entity at the target PathNode."""
        if entity.properties.get('disabled', False):
            return

        # ---- target node resolution with fallback ----
        target_name = entity.properties.get('target_node', '')
        node = None
        if target_name:
            node = logic._find_path_node_by_name(target_name)
            if not node:
                debug_log("IO", f"LogicSpawner '{entity.name}': target_node '{target_name}' not found. Falling back to spawner position.")
        else:
            debug_log("IO", f"LogicSpawner '{entity.name}': no target_node set. Using spawner position.")

        spawn_pos = list(node.pos) if node else list(entity.pos)

        # ---- spawn count limit ----
        max_spawn = int(entity.properties.get('max_spawn', 0))
        spawn_count = entity.properties.get('_spawn_count', 0)
        if max_spawn > 0 and spawn_count >= max_spawn:
            if logic.io_manager:
                logic.io_manager.fire_output(entity, 'OnMaxReached')
            debug_log("IO", f"LogicSpawner '{entity.name}': max_spawn reached ({max_spawn})")
            return

        # ---- spawn type (case‑insensitive) ----
        spawn_type = entity.properties.get('spawn_type', 'Monster')
        # ENTITY_TYPES is now imported from editor.things
        cls = ENTITY_TYPES.get(spawn_type)
        if cls is None:
            # case‑insensitive fallback
            for key, value in ENTITY_TYPES.items():
                if key.lower() == spawn_type.lower():
                    cls = value
                    break
        if cls is None:
            debug_log("Error", f"LogicSpawner: unknown spawn_type '{spawn_type}'")
            return

        # ---- extra properties (spawn_properties) ----
        extra_props = dict(entity.properties.get('spawn_properties', {}))

        # ---- RANDOM MONSTER HANDLING ----
        if spawn_type == 'Monster' and extra_props.get('random', False):
            import random
            from engine.monster_constants import MONSTER_VARIANTS

            # Choose random monster type
            monster_types = ['human', 'flying']
            chosen_type = random.choice(monster_types)
            extra_props['monster_type'] = chosen_type

            # Choose random variant for that type (including <None>)
            variants = ['<None>'] + MONSTER_VARIANTS.get(chosen_type, [])
            chosen_variant = random.choice(variants)
            extra_props['variant'] = chosen_variant

            debug_log("IO", f"LogicSpawner random spawn: type={chosen_type}, variant={chosen_variant}")

        # ---- create the new entity ----
        new_thing = cls(pos=spawn_pos, properties=extra_props)
        logic.editor_state.things.append(new_thing)
        entity.properties['_spawn_count'] = spawn_count + 1

        # ---- rebuild caches so the new entity can be found by name/id ----
        if hasattr(logic, '_build_entity_caches'):
            logic._build_entity_caches()

        # ---- fire outputs ----
        if logic.io_manager:
            logic.io_manager.fire_output(entity, 'OnSpawn')

        debug_log("IO", f"LogicSpawner '{entity.name}' spawned '{spawn_type}' at {spawn_pos}")

    def spawner_enable(entity, param, logic):
        entity.properties['disabled'] = False

    def spawner_disable(entity, param, logic):
        entity.properties['disabled'] = True

    def spawner_set_target(entity, param, logic):
        """Change spawn location to a different PathNode."""
        if param:
            entity.properties['target_node'] = param

    io_manager.register_input_handler('logic_spawner', 'spawn',         spawner_spawn)
    io_manager.register_input_handler('logic_spawner', 'enable',        spawner_enable)
    io_manager.register_input_handler('logic_spawner', 'disable',       spawner_disable)
    io_manager.register_input_handler('logic_spawner', 'settargetnode', spawner_set_target)

    # ==========================================================================
    # LOGIC STATE INPUTS
    #
    # LogicState is Fio's persistent state primitive and nothing else: these
    # handlers read, write and compare values, and turn what happened into I/O
    # events.  None of them knows what a door, a monster or a counter *means* —
    # that is the map's business, expressed as connections.
    #
    # Every write reports two things, and the difference between them is the
    # whole event model: OnValueSet fires for an accepted write, OnValueChanged
    # only when the stored value actually moved.  A chain hung off
    # OnValueChanged therefore runs on real transitions without anything having
    # to poll for them.
    # ==========================================================================

    def _state_pair(param, default_value="1"):
        """Split a ``"key=value"`` parameter, tolerating a bare key.

        A bare key means "set this flag", and the value it sets is *default*,
        which keeps ``SetValue  door_unlocked`` working the way it always has.
        """
        if '=' in param:
            key, value = param.split('=', 1)
            return key.strip(), value.strip()
        return param.strip(), default_value

    def _state_operand(param, default=1):
        """Split a ``"key,amount"`` parameter; a bare key means *default*."""
        if ',' in param:
            key, amount = param.split(',', 1)
            return key.strip(), amount.strip()
        return param.strip(), default

    def _state_report(entity, logic, key, ok, changed, stored):
        """Fire the outputs one write implies.

        Refused writes fire ``OnStoreFull``: the only ways a write is refused
        are a full store and a value that does not fit the type the parameter
        asked for, and both mean "this value did not go in", which is what a
        map wired to that output is reacting to.
        """
        mgr = logic.io_manager if logic is not None else None
        if mgr is None:
            return
        if not ok:
            mgr.fire_output(entity, 'OnStoreFull', value=key)
            return
        payload = "%s=%s" % (key, _sv.format_value(stored))
        mgr.fire_output(entity, 'OnValueSet', value=payload)
        if changed:
            mgr.fire_output(entity, 'OnValueChanged', value=payload)

    def _state_branch(entity, logic, result, payload):
        """Fire the true/false pair a query implies, legacy names included."""
        mgr = logic.io_manager if logic is not None else None
        if mgr is None:
            return
        if result:
            mgr.fire_output(entity, 'OnTrue', value=payload)
            mgr.fire_output(entity, 'OnCompareTrue', value=payload)
        else:
            mgr.fire_output(entity, 'OnFalse', value=payload)
            mgr.fire_output(entity, 'OnCompareFalse', value=payload)

    def state_setvalue(entity, param, logic):
        """Set a value.  Param: ``key=value``, or ``key:type=value``."""
        if not param:
            return
        key, value = _state_pair(param)
        key, value_type = _sv.split_typed_key(key)
        ok, changed, stored = entity.apply_value(key, value, value_type)
        _state_report(entity, logic, key, ok, changed, stored)

    def state_getvalue(entity, param, logic):
        """Read a key and fire OnValueRead with the value as the payload.

        The payload is the pass-through mechanism Fio already has: any
        connection on OnValueRead whose own parameter is blank receives this
        value.  That is how a stored value reaches another entity's input, and
        it is why Fio needs no templating language to move state around.
        """
        mgr = logic.io_manager
        if not param:
            mgr.fire_output(entity, 'OnKeyNotFound')
            return
        key = param.strip()
        if not entity.has_key(key):
            mgr.fire_output(entity, 'OnKeyNotFound', value=key)
            return
        mgr.fire_output(entity, 'OnValueRead',
                        value=_sv.format_value(entity.get_value(key)))

    def state_compare(entity, param, logic):
        """Test a key and branch.  Param: ``key>=value`` (also == != > < <=)."""
        if not param:
            return
        split = _sv.split_comparison(param)
        if split is None:
            debug_log('Error', f"LogicState.Compare: no operator in '{param}'")
            return
        key, op, expected = split
        found, result = entity.compare(key, op, expected)
        if not found:
            logic.io_manager.fire_output(entity, 'OnKeyNotFound', value=key)
            return
        _state_branch(entity, logic, result,
                      _sv.format_value(entity.get_value(key)))

    def state_exists(entity, param, logic):
        """Branch on whether a key is present."""
        if not param:
            return
        key = param.strip()
        _state_branch(entity, logic, entity.has_key(key), key)

    def state_missing(entity, param, logic):
        """Branch on whether a key is absent — the inverse of Exists."""
        if not param:
            return
        key = param.strip()
        _state_branch(entity, logic, not entity.has_key(key), key)

    def state_clearkey(entity, param, logic):
        """Remove a single key."""
        if not param:
            return
        key = param.strip()
        if entity.clear_key(key):
            logic.io_manager.fire_output(entity, 'OnValueCleared', value=key)
            logic.io_manager.fire_output(entity, 'OnKeyCleared', value=key)

    def state_clearall(entity, param, logic):
        """Remove every key."""
        entity.clear_all()

    def state_copyfrom(entity, param, logic):
        """Copy every pair from another store by name."""
        if not param:
            return
        if entity.copy_from(param.strip()) and logic.io_manager:
            logic.io_manager.fire_output(entity, 'OnValueSet')

    def state_copyvalue(entity, param, logic):
        """Copy one key to another.  Param: ``from,to`` or ``store.from,to``."""
        if not param or ',' not in param:
            return
        source, dest = param.split(',', 1)
        source, dest = source.strip(), dest.strip()
        source_store = None
        if '.' in source:
            source_store, source = source.split('.', 1)
            source_store, source = source_store.strip(), source.strip()
        ok, changed, stored = entity.copy_value(source, dest, source_store)
        if not ok:
            logic.io_manager.fire_output(entity, 'OnKeyNotFound', value=source)
            return
        _state_report(entity, logic, dest, ok, changed, stored)

    def _state_arithmetic(operation, default_operand=1):
        """Build the handler for one arithmetic operation.

        One factory rather than seven near-identical functions: the operations
        differ only in the word passed to the store, so the parameter handling
        and the event reporting are written once.
        """
        def _handle(entity, param, logic):
            if not param:
                return
            key, operand = _state_operand(param, default_operand)
            ok, changed, stored = entity.arithmetic(key, operation, operand)
            _state_report(entity, logic, key, ok, changed, stored)
        return _handle

    def state_decrement(entity, param, logic):
        """Subtract from a numeric value.  Param: ``key,amount``."""
        if not param:
            return
        key, operand = _state_operand(param, 1)
        ok, changed, stored = entity.arithmetic(
            key, 'subtract', _sv.to_number(operand, 1))
        _state_report(entity, logic, key, ok, changed, stored)

    def state_clamp(entity, param, logic):
        """Confine a value to a range.  Param: ``key,low,high``."""
        parts = [p.strip() for p in param.split(',')] if param else []
        if len(parts) < 3:
            return
        ok, changed, stored = entity.clamp(parts[0], parts[1], parts[2])
        _state_report(entity, logic, parts[0], ok, changed, stored)

    def state_toggle(entity, param, logic):
        """Invert a value as a boolean."""
        if not param:
            return
        key = param.strip()
        ok, changed, stored = entity.toggle(key)
        _state_report(entity, logic, key, ok, changed, stored)

    # -- object-local state ------------------------------------------------
    #
    # The same store, namespaced by the UUID of whichever entity fired the
    # connection.  No second database, no per-entity state framework: a
    # monster's "looted" flag is a key in the world store whose name happens to
    # contain the monster's UUID, so it persists, saves and survives dormancy
    # exactly like every other value.

    def _state_source_id(logic):
        """The UUID object-local state is filed under.

        The chain's *activator*, not the entity one hop back.  A monster's
        death routed through a relay should still record against the monster —
        the relay is plumbing, and a designer who inserts one to add a delay
        does not expect the state to start belonging to it.  The immediate
        source is the fallback for a chain that never had an activator.
        """
        mgr = logic.io_manager if logic is not None else None
        if mgr is None:
            return ""
        return mgr.current_activator_id() or mgr.current_source_id()

    def state_set_object_value(entity, param, logic):
        """Set a value against the firing entity's UUID.  Param: ``key=value``."""
        if not param:
            return
        source_id = _state_source_id(logic)
        if not source_id:
            debug_log('Error', "LogicState.SetObjectValue: no firing entity to key on")
            return
        key, value = _state_pair(param)
        key, value_type = _sv.split_typed_key(key)
        ok, changed, stored = entity.set_object_value(
            source_id, key, value, value_type)
        _state_report(entity, logic, entity.object_key(source_id, key),
                      ok, changed, stored)

    def state_get_object_value(entity, param, logic):
        """Read a value held against the firing entity's UUID."""
        source_id = _state_source_id(logic)
        if not param or not source_id:
            logic.io_manager.fire_output(entity, 'OnKeyNotFound')
            return
        key = entity.object_key(source_id, param.strip())
        if not entity.has_key(key):
            logic.io_manager.fire_output(entity, 'OnKeyNotFound', value=key)
            return
        logic.io_manager.fire_output(
            entity, 'OnValueRead', value=_sv.format_value(entity.get_value(key)))

    def state_clear_object_state(entity, param, logic):
        """Forget every value held against the firing entity's UUID."""
        source_id = _state_source_id(logic)
        if not source_id:
            return
        if entity.clear_object_state(source_id):
            logic.io_manager.fire_output(entity, 'OnValueCleared', value=source_id)
            logic.io_manager.fire_output(entity, 'OnKeyCleared', value=source_id)

    _STATE_INPUTS = {
        'setvalue':         state_setvalue,
        'getvalue':         state_getvalue,
        'compare':          state_compare,
        'testvalue':        state_compare,      # pre-2.4 name
        'exists':           state_exists,
        'missing':          state_missing,
        'clearkey':         state_clearkey,
        'clearall':         state_clearall,
        'copyfrom':         state_copyfrom,
        'copyvalue':        state_copyvalue,
        'increment':        _state_arithmetic('add'),
        'add':              _state_arithmetic('add'),
        'decrement':        state_decrement,
        'subtract':         _state_arithmetic('subtract'),
        'multiply':         _state_arithmetic('multiply', 1),
        'divide':           _state_arithmetic('divide', 1),
        'min':              _state_arithmetic('min', 0),
        'max':              _state_arithmetic('max', 0),
        'clamp':            state_clamp,
        'toggle':           state_toggle,
        'setobjectvalue':   state_set_object_value,
        'getobjectvalue':   state_get_object_value,
        'clearobjectstate': state_clear_object_state,
    }

    for _input_name, _handler in _STATE_INPUTS.items():
        io_manager.register_input_handler('logic_state', _input_name, _handler)

    # ==========================================================================
    # PORTAL INPUTS
    # ==========================================================================

    def portal_enable(entity, param, logic):
        """Activate the portal — fades it in."""
        entity.properties['active'] = True
        if hasattr(entity, '_fade_target'):
            entity._fade_target = 1.0        # fade in
        name = entity.properties.get('name', 'unnamed')
        debug_log('IO', f"Portal '{name}' enabled")
        logic.io_manager.fire_output(entity, 'OnEnabled')

    def portal_disable(entity, param, logic):
        """Deactivate the portal — fades it out."""
        entity.properties['active'] = False
        if hasattr(entity, '_fade_target'):
            entity._fade_target = 0.0        # fade out
        name = entity.properties.get('name', 'unnamed')
        debug_log('IO', f"Portal '{name}' disabled")
        logic.io_manager.fire_output(entity, 'OnDisabled')

    def portal_toggle(entity, param, logic):
        """Toggle portal active state with fade."""
        was_active = entity.properties.get('active', False)
        entity.properties['active'] = not was_active
        if hasattr(entity, '_fade_target'):
            entity._fade_target = 1.0 if entity.properties['active'] else 0.0
        name = entity.properties.get('name', 'unnamed')
        state = "enabled" if entity.properties['active'] else "disabled"
        debug_log('IO', f"Portal '{name}' toggled → {state}")
        logic.io_manager.fire_output(entity, 'OnToggled')

    def portal_set_color(entity, param, logic):
        """Set rim/glow color from 'R G B' string (0-255)."""
        try:
            parts = param.split()
            if len(parts) >= 3:
                r = max(0, min(255, int(parts[0])))
                g = max(0, min(255, int(parts[1])))
                b = max(0, min(255, int(parts[2])))
                entity.properties['color'] = [r, g, b]
                name = entity.properties.get('name', 'unnamed')
                debug_log('IO', f"Portal '{name}' color set to ({r}, {g}, {b})")
        except (ValueError, IndexError):
            debug_log('Error', f"SetColor: bad parameter '{param}' — expected 'R G B'")

    def portal_set_target(entity, param, logic):
        """Change the paired portal target by name."""
        if param:
            entity.properties['portal_target'] = param.strip()
            name = entity.properties.get('name', 'unnamed')
            debug_log('IO', f"Portal '{name}' target set to '{param.strip()}'")

    def portal_show_rim(entity, param, logic):
        entity.properties['show_rim'] = True

    def portal_hide_rim(entity, param, logic):
        entity.properties['show_rim'] = False

    def portal_set_width(entity, param, logic):
        try:
            entity.properties['width'] = max(16.0, float(param))
        except (ValueError, TypeError):
            pass

    def portal_set_height(entity, param, logic):
        try:
            entity.properties['height'] = max(16.0, float(param))
        except (ValueError, TypeError):
            pass

    io_manager.register_input_handler('portal', 'enable',    portal_enable)
    io_manager.register_input_handler('portal', 'disable',   portal_disable)
    io_manager.register_input_handler('portal', 'toggle',    portal_toggle)
    io_manager.register_input_handler('portal', 'setcolor',  portal_set_color)
    io_manager.register_input_handler('portal', 'settarget', portal_set_target)
    io_manager.register_input_handler('portal', 'showrim',   portal_show_rim)
    io_manager.register_input_handler('portal', 'hiderim',   portal_hide_rim)
    io_manager.register_input_handler('portal', 'setwidth',  portal_set_width)
    io_manager.register_input_handler('portal', 'setheight', portal_set_height)

    # ==========================================================================
    # LOG SUMMARY
    # ==========================================================================

    # Retrieve version from version.txt in the same directory
    version_str = "Unknown"
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        version_path = os.path.join(current_dir, 'version.txt')
        if os.path.exists(version_path):
            with open(version_path, 'r') as f:
                version_str = f.read().strip()
    except (OSError, IOError):
        pass

    debug_log('Info', f"<b>Fio {version_str}</b>")
    debug_log('Info', f"Registered {len(io_manager._input_handlers)} input handlers")
    debug_log('Info', f"Type 'help' to see all available commands")


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_brush_index(brush: dict, logic) -> int:
    """Get the index of a brush in the brushes list."""
    try:
        return logic.brushes.index(brush)
    except ValueError:
        return -1



