import os
import json
from PyQt5.QtWidgets import QMessageBox

from editor.debug_console import debug_log

# Try to import I/O system (available in both editor and play mode)
try:
    from .io_system import (
        get_connections, set_connections,
        OutputConnection, get_output_names, get_input_names,
        get_entity_type_for_io
    )
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False
    # debug_log("Warning", "I/O system not fully loaded in console")

# For spawn command
from editor.things import Pickup, Light, PlayerStart, LevelChanger


class ConsoleCommandHandler:
    """
    FULL console control with clear mode-specific messages + extensive render commands.
    """
    def __init__(self, main_window):
        self.main_window = main_window
        self.editor_state = main_window.state

        self.commands = {
            'bind': self.cmd_bind,
            'help': self.cmd_help,
            'list': self.cmd_list_entities,
            'entities': self.cmd_list_entities,
            'ents': self.cmd_list_entities,
            'ls': self.cmd_list_entities,
            'monster_kill': self.cmd_monster_kill,
            'kill_monster': self.cmd_monster_kill,
            'monster_revive':     self.cmd_monster_revive,
            'monster_revive_all': self.cmd_monster_revive_all,

            'ent': self.cmd_info,
            'info': self.cmd_info,

            'fire': self.cmd_fire,
            'ent_fire': self.cmd_fire,
            'trigger': self.cmd_trigger,
            'send': self.cmd_send_input,
            'toggle': self.cmd_toggle,

            'setprop': self.cmd_set_property,
            'set': self.cmd_set_property,
            'getprop': self.cmd_get_property,
            'get': self.cmd_get_property,

            'outputs': self.cmd_list_outputs,
            'inputs': self.cmd_list_inputs,

            'connect': self.cmd_connect_io,
            'disconnect': self.cmd_disconnect_io,

            'spawn': self.cmd_spawn,
            'delete': self.cmd_delete,
            'kill': self.cmd_delete,
            'list_connections': self.cmd_list_connections,
            'connections': self.cmd_list_connections,

            # Play Mode only commands
            'physics': self.cmd_physics,
            'setpos': self.cmd_setpos,
            'teleport': self.cmd_setpos,
            'ss': self.cmd_split_screen,

            'cam': self.cmd_cam,
            'camera': self.cmd_cam,

            'noclip': self.cmd_noclip,
            'god': self.cmd_god,
            'buddha': self.cmd_buddha,
            'clear': self.cmd_clear,
            'fps': self.cmd_fps,
            'map': self.cmd_map,

            # Save / load a play session
            'save': self.cmd_save,
            'savegame': self.cmd_save,
            'load': self.cmd_load,
            'loadgame': self.cmd_load,
            'quicksave': self.cmd_quicksave,
            'qs': self.cmd_quicksave,
            'quickload': self.cmd_quickload,
            'ql': self.cmd_quickload,
            'saves': self.cmd_list_saves,
            'listsaves': self.cmd_list_saves,

            'r_list': self.cmd_render_list,
            'r_wireframe': self.cmd_render_wireframe,
            'r_shadows': self.cmd_render_shadows,
            'r_fog': self.cmd_render_fog,
            'r_water': self.cmd_render_water,
            'r_glass': self.cmd_render_glass,
            'r_lighting': self.cmd_render_lighting,
            'r_deferred': self.cmd_render_deferred,
            'r_vsync': self.cmd_render_vsync,
            'r_clearcolor': self.cmd_render_clearcolor,
            'r_reloadshaders': self.cmd_reload_shaders,
            'r_info': self.cmd_render_info,

            # Short aliases
            'wireframe': self.cmd_render_wireframe,
            'shadows': self.cmd_render_shadows,
            'fog': self.cmd_render_fog,
            'water': self.cmd_render_water,
            'glass': self.cmd_render_glass,
            'lighting': self.cmd_render_lighting,
            'deferred': self.cmd_render_deferred,
            'vsync': self.cmd_render_vsync,
            'reloadshaders': self.cmd_reload_shaders,

            # Visibility & Tint
            'hide': self.cmd_hide,
            'show': self.cmd_show,
            'tint': self.cmd_tint,

            # Debug
            'notarget': self.cmd_notarget,
            'sg': self.cmd_spatial_grid,
            'showcollision': self.cmd_show_collision,
            'collisionvis': self.cmd_show_collision,
            'collision': self.cmd_show_collision,

            # Portal commands
            'portal_list': self.cmd_portal_list,
            'portal_create': self.cmd_portal_create,
            'portal_link': self.cmd_portal_link,
            'portal_color': self.cmd_portal_color,
            'portal_enable': self.cmd_portal_enable,
            'portal_disable': self.cmd_portal_disable,
            'portal_delete': self.cmd_portal_delete,
        }

    def handle_command(self, cmd_string):
        parts = cmd_string.strip().split(maxsplit=1)
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        handler = self.commands.get(cmd)
        if handler:
            handler(args)
        elif self._dispatch_plugin_command(cmd, args):
            return
        else:
            debug_log("Error", f"Unknown command: {cmd}. Type 'help' for list.")

    def _dispatch_plugin_command(self, cmd, args):
        """Offer an unknown command to a plugin that registered it.

        Uses the official plugin-API console-command surface (API 1.4.0): a
        plugin declares a command with ``EditorAPI.register_console_command`` and
        the manager dispatches it here. Fully guarded — with no play session, no
        plugin manager, or no owning plugin this returns False so the console
        shows its usual "unknown command". This is the single native integration
        point (like the engine's other plugin hooks); no plugin monkeypatching.
        """
        try:
            mgr = self._plugin_manager()
            if mgr is None or not mgr.has_console_command(cmd):
                return False
            view_3d = getattr(self.main_window, 'view_3d', None)
            lt = getattr(view_3d, 'logic_thread', None) if view_3d else None
            play = bool(getattr(view_3d, 'play_mode', False))
            handled, reply = mgr.dispatch_console_command(cmd, args, lt, play_mode=play)
            if handled and reply:
                debug_log("Info", str(reply))
            return handled
        except Exception:
            return False

    def _plugin_manager(self):
        """The live plugin manager, or None if the plugin system isn't present."""
        try:
            from plugins.manager import get_manager
            return get_manager()
        except Exception:
            return None

    def cmd_bind(self, args):
        """bind <key> <command>   or   bind (opens dialog)"""
        if not args.strip():
            self._open_bind_dialog()
            return
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            debug_log("Error", "Usage: bind <key> <command>")
            return
        key_str, command = parts
        # Store binding in main_window
        self.main_window.set_key_binding(key_str, command)
        debug_log("Info", f"Bound '{key_str}' to '{command}'")

    def _open_bind_dialog(self):
        from PyQt5.QtWidgets import QInputDialog, QDialog, QVBoxLayout, QLabel, QKeySequenceEdit, QPushButton, QLineEdit, QDialogButtonBox
        from PyQt5.QtCore import Qt

        dialog = QDialog(self.main_window)
        dialog.setWindowTitle("Bind Key")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Press the key combination to bind:"))
        key_edit = QKeySequenceEdit()
        key_edit.setPlaceholderText("Press a key...")
        layout.addWidget(key_edit)
        layout.addWidget(QLabel("Enter the command to execute:"))
        cmd_edit = QLineEdit()
        layout.addWidget(cmd_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec_() == QDialog.Accepted:
            key_seq = key_edit.keySequence()
            if key_seq.isEmpty():
                debug_log("Error", "No key selected")
                return
            key_str = key_seq.toString()
            command = cmd_edit.text().strip()
            if not command:
                debug_log("Error", "No command entered")
                return
            self.main_window.set_key_binding(key_str, command)
            debug_log("Info", f"Bound '{key_str}' to '{command}'")

    # ===================================================================
    # MONSTER COMMANDS
    # ===================================================================

    def cmd_monster_kill(self, args):
        """
        Usage: monster_kill <monster_name>
        Instantly kills the named monster (sets health to 0, marks dead/hidden, fires OnDeath).
        """
        if not args:
            debug_log("Error", "Usage: monster_kill <monster_name>")
            return

        name = args.strip()
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        # Check if it's a monster
        from editor.things import Monster
        if not isinstance(entity, Monster):
            debug_log("Error", f"Entity '{name}' is not a Monster (type: {type(entity).__name__})")
            return

        # Kill the monster
        entity.properties['health'] = 0
        entity.properties['dead'] = True
        entity.properties['hidden'] = True

        # Fire I/O output if available
        try:
            from editor.io_system import get_connections, fire_output
            # Since we don't have IOManager reference here, we can use the logic_thread's io_manager if in play mode
            if hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.logic_thread:
                io_manager = self.main_window.view_3d.logic_thread.io_manager
                if io_manager:
                    io_manager.fire_output(entity, 'OnDeath')
        except Exception as e:
            debug_log("Warning", f"Could not fire OnDeath: {e}")

        debug_log("Info", f"Monster '{name}' killed (health set to 0, hidden=True)")
        self.main_window.update_all_ui()

    def cmd_monster_revive(self, args):
        """
        Usage: monster_revive <monster_name>
        Restores a single named monster to full health and clears its dead/hidden state.
        Works in both editor and play mode.
        """
        if not args:
            debug_log("Error", "Usage: monster_revive <monster_name>")
            return

        name = args.strip()
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        from editor.things import Monster
        if not isinstance(entity, Monster):
            debug_log("Error", f"Entity '{name}' is not a Monster (type: {type(entity).__name__})")
            return

        self._revive_monster(entity)

        # If in play mode, clear this monster's stale AI state so it doesn't
        # inherit a near-zero shoot timer from before it died.
        try:
            if hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.logic_thread:
                lt = self.main_window.view_3d.logic_thread
                lt.monster_states.pop(id(entity), None)
        except Exception as e:
            debug_log("Warning", f"Could not reset monster AI state: {e}")

        debug_log("Info", f"Monster '{name}' revived")
        self.main_window.update_all_ui()

    def cmd_monster_revive_all(self, args):
        """
        Usage: monster_revive_all
        Restores every monster in the level to full health and clears dead/hidden/awake state.
        Safe to run in editor or play mode.
        """
        from editor.things import Monster

        monsters = [t for t in self.editor_state.things if isinstance(t, Monster)]
        if not monsters:
            debug_log("Info", "No monsters found in the level")
            return

        for monster in monsters:
            self._revive_monster(monster)

        # If we're in play mode, clear the entire monster AI state dict so no
        # monster inherits a stale shoot timer or animation state from before death.
        try:
            if hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.logic_thread:
                lt = self.main_window.view_3d.logic_thread
                lt.monster_states = {}
        except Exception as e:
            debug_log("Warning", f"Could not reset monster AI states: {e}")

        debug_log("Info", f"Revived {len(monsters)} monster(s)")
        self.main_window.update_all_ui()

    def _revive_monster(self, entity):
        """
        Shared helper — reset a Monster entity back to its full alive state.
        Respects the entity's configured health value if positive; falls back to 100.
        """
        # Restore health: use the entity's current health value if it's still positive
        # (meaning the designer set a custom value), otherwise default to 100.
        current_health = entity.properties.get('health', 0)
        try:
            current_health = int(current_health)
        except (ValueError, TypeError):
            current_health = 0

        restored_health = current_health if current_health > 0 else 100
        entity.properties['health']      = restored_health
        entity.properties['dead']        = False
        entity.properties['hidden']      = False
        # Reset awake so triggered/sight-gated monsters go dormant again —
        # wake logic will re-apply correctly on next play mode start.
        entity.properties['awake']       = False
        entity.properties.pop('is_shooting', None)

        # Clear the sprite cache so the editor 2D views and 3D billboard
        # switch back to idle.png immediately rather than staying on dead.png.
        try:
            from editor.things import Monster
            Monster.clear_sprite_cache()
        except Exception:
            pass

    # ===================================================================
    # VISIBILITY & TINT
    # ===================================================================

    def cmd_hide(self, args):
        """hide <name> — Set hidden flag on a brush or entity."""
        if not args:
            debug_log("Error", "Usage: hide <entity_name>")
            return
        name = args.strip()
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return
        if isinstance(entity, dict):
            entity['hidden'] = True
        elif hasattr(entity, 'properties'):
            entity.properties['hidden'] = True
        debug_log("Info", f"'{name}' is now hidden")

    def cmd_show(self, args):
        """show <name> — Clear hidden flag on a brush or entity."""
        if not args:
            debug_log("Error", "Usage: show <entity_name>")
            return
        name = args.strip()
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return
        if isinstance(entity, dict):
            entity['hidden'] = False
        elif hasattr(entity, 'properties'):
            entity.properties['hidden'] = False
        debug_log("Info", f"'{name}' is now visible")

    def cmd_tint(self, args):
        """tint <name> <R G B> — Set tint on a brush, or 'tint <name> clear'."""
        if not args:
            debug_log("Error", "Usage: tint <name> <R> <G> <B>  or  tint <name> clear")
            return
        parts = args.split()
        if len(parts) < 2:
            debug_log("Error", "Usage: tint <name> <R> <G> <B>  or  tint <name> clear")
            return
        name = parts[0]
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        if parts[1].lower() == 'clear':
            if isinstance(entity, dict):
                entity.pop('tint', None)
            elif hasattr(entity, 'properties'):
                entity.properties.pop('tint', None)
            debug_log("Info", f"Cleared tint on '{name}'")
            return

        if len(parts) < 4:
            debug_log("Error", "Usage: tint <name> <R> <G> <B>  (values 0-255)")
            return
        try:
            r = max(0, min(255, int(parts[1])))
            g = max(0, min(255, int(parts[2])))
            b = max(0, min(255, int(parts[3])))
        except ValueError:
            debug_log("Error", "R, G, B must be integers 0-255")
            return

        if isinstance(entity, dict):
            entity['tint'] = [r, g, b]
        elif hasattr(entity, 'properties'):
            entity.properties['tint'] = [r, g, b]
        debug_log("Info", f"Set tint on '{name}' to ({r}, {g}, {b})")

    # ===================================================================
    # HELP
    # ===================================================================

    # ===================================================================
    # PORTAL COMMANDS
    # ===================================================================

    def cmd_portal_list(self, args):
        """List all portals in the level with their link status."""
        from editor.things import Portal
        portals = [t for t in self.editor_state.things if isinstance(t, Portal)]
        if not portals:
            debug_log("Info", "No portals found in the level")
            return

        debug_log("Info", f"=== PORTALS ({len(portals)}) ===")
        for p in portals:
            name = p.properties.get('name', 'unnamed')
            target = p.properties.get('portal_target', '<none>')
            active = "ACTIVE" if p.is_active() else "inactive"
            color = p.properties.get('color', [255, 255, 255])
            pos = p.pos
            debug_log("Info", f"  '{name}' → '{target}' [{active}] at ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}) color=({color[0]}, {color[1]}, {color[2]})")

    def cmd_portal_create(self, args):
        """Create a new portal pair: portal_create <name1> <name2> [x y z]"""
        from editor.things import Portal
        parts = args.split()
        if len(parts) < 2:
            debug_log("Error", "Usage: portal_create <name1> <name2> [x y z]")
            debug_log("Info", "  Creates two linked portals. Optional position defaults to camera/PlayerStart")
            return

        name1, name2 = parts[0], parts[1]

        # Determine spawn position
        if len(parts) >= 5:
            try:
                pos = [float(parts[2]), float(parts[3]), float(parts[4])]
            except ValueError:
                debug_log("Error", "Invalid position coordinates")
                return
        else:
            # Use camera position or player start
            pos = [0, 128, 0]
            if hasattr(self.main_window, 'view_3d'):
                cam = self.main_window.view_3d.camera
                pos = [cam.pos.x, cam.pos.y, cam.pos.z]

        # Create portal A
        portal_a = Portal(pos=[pos[0] - 64, pos[1], pos[2]])
        portal_a.properties['name'] = name1
        portal_a.properties['portal_target'] = name2
        portal_a.properties['active'] = True

        # Create portal B
        portal_b = Portal(pos=[pos[0] + 64, pos[1], pos[2]])
        portal_b.properties['name'] = name2
        portal_b.properties['portal_target'] = name1
        portal_b.properties['active'] = True

        self.editor_state.things.append(portal_a)
        self.editor_state.things.append(portal_b)
        self.editor_state.save_state()

        debug_log("Info", f"Created portal pair: '{name1}' ↔ '{name2}' at ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})")
        self.main_window.update_all_ui()

    def cmd_portal_link(self, args):
        """Link two existing portals: portal_link <name1> <name2>"""
        from editor.things import Portal
        parts = args.split()
        if len(parts) < 2:
            debug_log("Error", "Usage: portal_link <portal_name> <target_name>")
            return

        portal_name, target_name = parts[0], parts[1]

        # Find the portal
        portal = None
        for t in self.editor_state.things:
            if isinstance(t, Portal) and t.properties.get('name') == portal_name:
                portal = t
                break

        if not portal:
            debug_log("Error", f"Portal '{portal_name}' not found")
            return

        # Verify target exists (optional - can link to non-existent for later creation)
        target_exists = any(
            isinstance(t, Portal) and t.properties.get('name') == target_name
            for t in self.editor_state.things
        )

        portal.properties['portal_target'] = target_name
        self.editor_state.save_state()

        status = f"linked to '{target_name}'"
        if not target_exists:
            status += " (target does not exist yet)"
        debug_log("Info", f"Portal '{portal_name}' {status}")
        self.main_window.update_all_ui()

    def cmd_portal_color(self, args):
        """Set portal rim color: portal_color <name> <R> <G> <B>"""
        from editor.things import Portal
        parts = args.split()
        if len(parts) < 4:
            debug_log("Error", "Usage: portal_color <name> <R> <G> <B>  (values 0-255)")
            return

        name = parts[0]
        try:
            r = max(0, min(255, int(parts[1])))
            g = max(0, min(255, int(parts[2])))
            b = max(0, min(255, int(parts[3])))
        except ValueError:
            debug_log("Error", "R, G, B must be integers 0-255")
            return

        # Find and update all matching portals
        found = False
        for t in self.editor_state.things:
            if isinstance(t, Portal) and t.properties.get('name') == name:
                t.properties['color'] = [r, g, b]
                found = True
                debug_log("Info", f"Portal '{name}' color set to ({r}, {g}, {b})")

        if not found:
            debug_log("Error", f"Portal '{name}' not found")
            return

        self.editor_state.save_state()
        self.main_window.update_all_ui()

    def cmd_portal_enable(self, args):
        """Enable a portal: portal_enable <name>"""
        from editor.things import Portal
        if not args:
            debug_log("Error", "Usage: portal_enable <name>")
            return

        name = args.strip()
        for t in self.editor_state.things:
            if isinstance(t, Portal) and t.properties.get('name') == name:
                t.properties['active'] = True
                self.editor_state.save_state()
                debug_log("Info", f"Portal '{name}' enabled")
                self.main_window.update_all_ui()
                return
        debug_log("Error", f"Portal '{name}' not found")

    def cmd_portal_disable(self, args):
        """Disable a portal: portal_disable <name>"""
        from editor.things import Portal
        if not args:
            debug_log("Error", "Usage: portal_disable <name>")
            return

        name = args.strip()
        for t in self.editor_state.things:
            if isinstance(t, Portal) and t.properties.get('name') == name:
                t.properties['active'] = False
                self.editor_state.save_state()
                debug_log("Info", f"Portal '{name}' disabled")
                self.main_window.update_all_ui()
                return
        debug_log("Error", f"Portal '{name}' not found")

    def cmd_show_collision(self, args):
        """
        Toggle collision visualization overlay.
        Usage: showcollision [on|off|mesh|aabb|all]
        
        Shows wireframe outlines of:
        - AABB collision boxes (yellow wireframes)
        - Mesh collision triangles (cyan wireframes)
        
        Works in both Editor mode and Play mode.
        """
        view_3d = getattr(self.main_window, 'view_3d', None)
        if not view_3d:
            debug_log("Error", "3D view not available")
            return
        
        # Initialize state if not present
        if not hasattr(view_3d, '_collision_vis_mode'):
            view_3d._collision_vis_mode = 'off'
        
        arg = args.strip().lower() if args else 'toggle'
        
        if arg == 'on':
            view_3d._collision_vis_mode = 'all'
        elif arg == 'off':
            view_3d._collision_vis_mode = 'off'
        elif arg == 'mesh':
            view_3d._collision_vis_mode = 'mesh'
        elif arg == 'aabb':
            view_3d._collision_vis_mode = 'aabb'
        elif arg == 'toggle':
            modes = ['off', 'all', 'mesh', 'aabb']
            current_idx = modes.index(view_3d._collision_vis_mode) if view_3d._collision_vis_mode in modes else 0
            view_3d._collision_vis_mode = modes[(current_idx + 1) % len(modes)]
        else:
            debug_log("Error", "Usage: showcollision [on|off|mesh|aabb|all|toggle]")
            return
        
        # Build collision brushes if enabling visualization and not already built
        # (needed for editor mode where they aren't auto-built on play start)
        if view_3d._collision_vis_mode != 'off' and view_3d.logic_thread:
            lt = view_3d.logic_thread
            if not getattr(lt, '_model_collision_brushes', []):
                lt.model_collision_enabled = True
                lt._model_collision_brushes = lt._build_model_collision_brushes()
                if hasattr(lt, '_refresh_collision_brushes_cache'):
                    lt._refresh_collision_brushes_cache()
                debug_log("Info", f"Built {len(lt._model_collision_brushes)} collision brushes for visualization")
        
        debug_log("Info", f"Collision visualization: {view_3d._collision_vis_mode}")
        self.main_window.show_toast(f"Collision Vis: {view_3d._collision_vis_mode}")
        view_3d.update()

    def cmd_portal_delete(self, args):
        """Delete a portal and optionally its pair: portal_delete <name> [and_pair]"""
        from editor.things import Portal
        if not args:
            debug_log("Error", "Usage: portal_delete <name> [and_pair]")
            return

        parts = args.split()
        name = parts[0]
        delete_pair = len(parts) > 1 and parts[1].lower() == 'and_pair'

        portal = None
        for t in self.editor_state.things:
            if isinstance(t, Portal) and t.properties.get('name') == name:
                portal = t
                break

        if not portal:
            debug_log("Error", f"Portal '{name}' not found")
            return

        target_name = portal.properties.get('portal_target', '')

        self.editor_state.things.remove(portal)
        deleted = [name]

        if delete_pair and target_name:
            for t in self.editor_state.things[:]:
                if isinstance(t, Portal) and t.properties.get('name') == target_name:
                    self.editor_state.things.remove(t)
                    deleted.append(target_name)
                    break

        self.editor_state.save_state()
        debug_log("Info", f"Deleted portal(s): {', '.join(deleted)}")
        self.main_window.update_all_ui()

    def cmd_help(self, args):
        
        sep = '<span style="color:white;"> / </span>'
        
        help_text = f"""
<i>Here is a full list of all available commands:</i><br><br>

<b style="color:orange;">clear</b> — Clear console<br>
<b style="color:orange;">help</b> — Show this help<br>
<b style="color:orange;">fps</b> — Toggle FPS display<br>
<b style="color:orange;">map</b> &lt;name&gt; — Load a different map<br>
<b style="color:cyan;">=== Save / Load (Play Session) ===</b><br>
<b style="color:orange;">save</b> [name] — Save the current play session (Play Mode only)<br>
<b style="color:orange;">load</b> [name] — Load a saved play session<br>
<b style="color:orange;">quicksave</b>{sep}<b style="color:orange;">qs</b> — Save to the quicksave slot<br>
<b style="color:orange;">quickload</b>{sep}<b style="color:orange;">ql</b> — Load the quicksave slot<br>
<b style="color:orange;">saves</b> — List available save files<br>
<b style="color:cyan;">=== Entity / I/O Commands ===</b><br>
<b style="color:orange;">list</b>{sep}<b style="color:orange;">ents</b>{sep}<b style="color:orange;">ls</b>{sep}<b style="color:orange;">entities</b> — List all entities<br>
<b style="color:orange;">ent</b>{sep}<b style="color:orange;">info</b> &lt;name&gt; — Show entity details<br>
<b style="color:orange;">spawn</b> &lt;type&gt; — Spawn a new entity (thing)<br>
<b style="color:orange;">delete</b>{sep}<b style="color:orange;">kill</b> &lt;name&gt; — Remove an entity from the scene<br>
<b style="color:orange;">set</b>{sep}<b style="color:orange;">setprop</b> &lt;ent&gt; &lt;prop&gt; &lt;val&gt; — Modify a property<br>
<b style="color:orange;">get</b>{sep}<b style="color:orange;">getprop</b> &lt;ent&gt; &lt;prop&gt; — Read a property value<br>
<b style="color:orange;">fire</b>{sep}<b style="color:orange;">ent_fire</b> &lt;ent&gt; &lt;output&gt; [param]<br>
<b style="color:orange;">send</b> &lt;ent&gt; &lt;input&gt; [param]<br>
<b style="color:orange;">trigger</b> — Smart toggle for doors/triggers<br>
<b style="color:orange;">toggle</b> — Flip an entity's state<br>
<b style="color:cyan;">=== Connection Management ===</b><br>
<b style="color:orange;">outputs</b> &lt;ent&gt; — List available outputs for type<br>
<b style="color:orange;">inputs</b> &lt;ent&gt; — List available inputs for type<br>
<b style="color:orange;">connections</b>{sep}<b style="color:orange;">list_connections</b> &lt;ent&gt; — Show active I/O links<br>
<b style="color:orange;">connect</b> &lt;src&gt; &lt;out&gt; &lt;tgt&gt; &lt;in&gt; [delay]<br>
<b style="color:orange;">disconnect</b> &lt;src&gt; &lt;out&gt; &lt;tgt&gt; &lt;in&gt;<br>
<b style="color:cyan;">=== Monsters ===</b><br>
<b style="color:orange;">monster_kill</b> &lt;name&gt; — Instantly kill a named monster<br>
<b style="color:orange;">monster_revive</b> &lt;name&gt; — Restore a named monster to full health<br>
<b style="color:orange;">monster_revive_all</b> — Restore every monster in the level<br>
<b style="color:cyan;">=== Visibility & Tint ===</b><br>
<b style="color:orange;">hide</b> &lt;name&gt; — Hide a brush or entity<br>
<b style="color:orange;">show</b> &lt;name&gt; — Show a hidden brush or entity<br>
<b style="color:orange;">tint</b> &lt;name&gt; &lt;R&gt; &lt;G&gt; &lt;B&gt; — Set tint colour (0-255) or 'clear'<br>
<b style="color:cyan;">=== Rendering ===</b><br>
<b style="color:orange;">ss</b> — Toggle split-screen mode (F9)<br>
<b style="color:orange;">r_list</b> — Show all current render settings<br>
<b style="color:orange;">r_wireframe</b>{sep}<b style="color:orange;">wireframe</b> — Toggle wireframe mode<br>
<b style="color:orange;">r_shadows</b>{sep}<b style="color:orange;">shadows</b> — Toggle shadows<br>
<b style="color:orange;">r_fog</b>{sep}<b style="color:orange;">fog</b> — Toggle volumetric fog<br>
<b style="color:orange;">r_lighting</b>{sep}<b style="color:orange;">lighting</b> — Toggle real-time lighting<br>
<b style="color:orange;">r_reloadshaders</b> — Hot-reload all shaders<br>
<b style="color:orange;">r_clearcolor</b> r g b — Set background colour<br>
<b style="color:cyan;">=== Movement & Physics ===</b><br>
<b style="color:orange;">physics</b> on/off/toggle<br>
<b style="color:orange;">setpos</b>{sep}<b style="color:orange;">teleport</b> x y z<br>
<b style="color:orange;">cam</b>{sep}<b style="color:orange;">camera</b> [overhead|fp] [seconds] — Tween between overhead &amp; first person (e.g. 'cam 2')<br>
<b style="color:cyan;">=== Portals ===</b><br>
<b style="color:orange;">portal_list</b> — List all portals and their links<br>
<b style="color:orange;">portal_create</b> &lt;name1&gt; &lt;name2&gt; [x y z] — Create a linked portal pair<br>
<b style="color:orange;">portal_link</b> &lt;name&gt; &lt;target&gt; — Link an existing portal to another<br>
<b style="color:orange;">portal_color</b> &lt;name&gt; &lt;R&gt; &lt;G&gt; &lt;B&gt; — Set rim color (0-255)<br>
<b style="color:orange;">portal_enable</b> &lt;name&gt; — Activate a portal<br>
<b style="color:orange;">portal_disable</b> &lt;name&gt; — Deactivate a portal<br>
<b style="color:orange;">portal_delete</b> &lt;name&gt; [and_pair] — Remove portal(s)<br>
<b style="color:cyan;">=== Debug ===</b><br>
<b style="color:orange;">sg</b> — Toggle spatial grid visualisation<br>
<b style="color:orange;">god</b> — Toggle invincibility<br>
<b style="color:orange;">buddha</b> — Toggle buddha mode (health cannot go below 2)<br>
<b style="color:orange;">noclip</b> — Toggle noclip<br>
<b style="color:orange;">notarget</b> — Toggle notarget (monsters ignore the player)<br>
"""
        # Append any console commands plugins registered (API 1.4.0).
        try:
            mgr = self._plugin_manager()
            cmds = mgr.console_commands() if mgr is not None else []
        except Exception:
            cmds = []
        if cmds:
            help_text += '<b style="color:cyan;">=== Plugin Commands ===</b><br>'
            for name, chelp in cmds:
                suffix = f" — {chelp}" if chelp else ""
                help_text += f'<b style="color:orange;">{name}</b>{suffix}<br>'
        debug_log("Info", help_text)

    # ===================================================================
    # HELPER: Get renderer safely
    # ===================================================================
    def _get_renderer(self):
        """Safely retrieve the active renderer from the 3D view."""
        try:
            if hasattr(self.main_window, 'view_3d') and hasattr(self.main_window.view_3d, 'renderer'):
                return self.main_window.view_3d.renderer
        except Exception:
            pass
        debug_log("Error", "Renderer not accessible (not in 3D view).")
        return None

    def _get_io_manager(self):
        """Safely retrieve the I/O manager from the logic thread."""
        try:
            if hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.logic_thread:
                return self.main_window.view_3d.logic_thread.io_manager
        except Exception:
            pass
        debug_log("Error", "I/O manager not accessible.")
        return None

    # ===================================================================
    # RENDER COMMANDS
    # ===================================================================

    def cmd_render_list(self, args):
        """Show all current render settings in a clean table."""
        renderer = self._get_renderer()
        if not renderer:
            return

        lines = ["<b>=== Current Render Settings ===</b><br>"]

        def add_line(name, value):
            lines.append(f"<b>{name}:</b> {value}")

        add_line("Wireframe", "ON" if getattr(renderer, 'wireframe', False) else "OFF")
        add_line("Shadows", "ON" if getattr(renderer, 'shadows_enabled', False) else "OFF")
        add_line("Volumetric Fog", "ON" if getattr(renderer, 'fog_enabled', True) else "OFF")
        add_line("Water Shader", "ON" if getattr(renderer, 'water_enabled', True) else "OFF")
        add_line("Glass Shader", "ON" if getattr(renderer, 'glass_enabled', True) else "OFF")
        add_line("Real-time Lighting", "ON" if getattr(renderer, 'lighting_enabled', True) else "OFF")
        add_line("Deferred Rendering", "ON" if getattr(renderer, 'use_deferred', False) else "OFF")
        add_line("ARM Mode", "ON" if getattr(renderer, 'arm_mode', True) else "OFF")

        # Clear color
        cc = getattr(renderer, 'clear_color', [0.02, 0.02, 0.05])
        add_line("Clear Color", f"[{cc[0]:.2f}, {cc[1]:.2f}, {cc[2]:.2f}]")

        debug_log("Info", "<br>".join(lines))

    def cmd_render_info(self, args):
        """Detailed renderer status"""
        self.cmd_render_list(args)

    def cmd_render_wireframe(self, args):
        renderer = self._get_renderer()
        if not renderer:
            return
        renderer.wireframe = not getattr(renderer, 'wireframe', False)
        state = "ON" if renderer.wireframe else "OFF"
        debug_log("Info", f"Wireframe: {state}")
        if hasattr(self.main_window.view_3d, 'update'):
            self.main_window.view_3d.update()

    def cmd_render_shadows(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.shadows_enabled = not getattr(renderer, 'shadows_enabled', False)
        debug_log("Info", f"Shadows: {'ON' if renderer.shadows_enabled else 'OFF'}")

    def cmd_render_fog(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.fog_enabled = not getattr(renderer, 'fog_enabled', True)
        debug_log("Info", f"Volumetric Fog: {'ON' if renderer.fog_enabled else 'OFF'}")

    def cmd_render_water(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.water_enabled = not getattr(renderer, 'water_enabled', True)
        debug_log("Info", f"Water shader: {'ON' if renderer.water_enabled else 'OFF'}")

    def cmd_render_glass(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.glass_enabled = not getattr(renderer, 'glass_enabled', True)
        debug_log("Info", f"Glass shader: {'ON' if renderer.glass_enabled else 'OFF'}")

    def cmd_render_lighting(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.lighting_enabled = not getattr(renderer, 'lighting_enabled', True)
        debug_log("Info", f"Real-time lighting: {'ON' if renderer.lighting_enabled else 'OFF'}")

    def cmd_render_deferred(self, args):
        renderer = self._get_renderer()
        if not renderer: return
        renderer.use_deferred = not getattr(renderer, 'use_deferred', False)
        debug_log("Info", f"Deferred rendering: {'ON' if renderer.use_deferred else 'OFF'}")

    def cmd_render_vsync(self, args):
        config = self.main_window.config
        current = config.getboolean('Display', 'vsync', fallback=True)
        new_state = not current
        if not config.has_section('Display'):
            config.add_section('Display')
        config.set('Display', 'vsync', str(new_state))
        self.main_window.save_config()

        debug_log("Info", f"VSync: {'ON' if new_state else 'OFF'}")

    def cmd_render_clearcolor(self, args):
        renderer = self._get_renderer()
        if not renderer:
            return
        try:
            parts = [float(x) for x in args.split()]
            if len(parts) == 3:
                renderer.clear_color = [max(0.0, min(1.0, c)) for c in parts]
                debug_log("Info", f"Clear color set to {renderer.clear_color}")
            else:
                debug_log("Error", "Usage: r_clearcolor r g b   (values 0.0 to 1.0)")
        except Exception:
            debug_log("Error", "Usage: r_clearcolor r g b")

    def cmd_reload_shaders(self, args):
        renderer = self._get_renderer()
        if not renderer:
            return
        try:
            if hasattr(renderer, 'reload_shaders') and callable(renderer.reload_shaders):
                success = renderer.reload_shaders()
                if success:
                    debug_log("Info", "✅ Shaders reloaded successfully")
                else:
                    debug_log("Warning", "Some shaders failed to reload")
            else:
                debug_log("Error", "Renderer does not support hot-reloading shaders")
        except Exception as e:
            debug_log("Error", f"Failed to reload shaders: {e}")

    # ===================================================================
    # EXISTING COMMANDS (unchanged)
    # ===================================================================

    def _require_play_mode(self, command_name):
        """Returns True if in Play Mode, else logs error and returns False."""
        if not self.main_window.view_3d.play_mode:
            debug_log("Error", f"Command '{command_name}' can only be used in Play Mode.")
            return False
        return True

    def cmd_list_entities(self, args):
        debug_log("Info", f"--- BRUSHES ({len(self.editor_state.brushes)}) ---")
        for i, b in enumerate(self.editor_state.brushes):
            name = b.get('name', f'unnamed_brush_{i}')
            typ = "Trigger" if b.get('is_trigger') else "Mover" if b.get('is_mover') else "Door" if b.get('is_door') else "Brush"
            debug_log("Info", f"  {name}  [{typ}]")

        debug_log("Info", f"--- THINGS ({len(self.editor_state.things)}) ---")
        for t in self.editor_state.things:
            name = t.properties.get('name', 'unnamed')
            typ = t.properties.get('type', 'unknown')
            debug_log("Info", f"  {name}  (type={typ})")

    def cmd_info(self, args):
        if not args:
            debug_log("Error", "Usage: ent <n>")
            return
        entity = self.editor_state.find_entity_by_name(args)
        if not entity:
            debug_log("Error", f"Entity '{args}' not found")
            return

        debug_log("Info", f"─── INFO: {args} ───")
        if isinstance(entity, dict):
            for k, v in list(entity.items())[:20]:
                if k != '_io_connections':
                    debug_log("Info", f"  {k}: {v}")
            conns = get_connections(entity) if IO_AVAILABLE else entity.get('_io_connections', [])
            if conns:
                debug_log("Info", "  I/O Connections:")
                for c in conns:
                    debug_log("Info", f"    {c.output_name} → {c.target_name}.{c.input_name}")
            else:
                debug_log("Info", "  No I/O connections")
        else:
            for k, v in entity.properties.items():
                debug_log("Info", f"  {k}: {v}")

    def cmd_fire(self, args):
        """ent_fire <entity_name> <input_name> [parameter]
        Fires an INPUT on an entity through the I/O system (runs the entity's
        registered input handler, exactly like a runtime connection would).
        Works in both Editor mode and Play Mode."""
        if not args:
            debug_log("Error", "Usage: ent_fire <entity_name> <input_name> [parameter]")
            return

        parts = args.split(maxsplit=2)
        if len(parts) < 2:
            debug_log("Error", "Usage: ent_fire <entity_name> <input_name> [parameter]")
            return

        entity_name = parts[0]
        input_name = parts[1]
        parameter = " ".join(parts[2:]) if len(parts) > 2 else ""

        entity = self.editor_state.find_entity_by_name(entity_name)
        if not entity:
            debug_log("Error", f"Entity '{entity_name}' not found.")
            return

        debug_log("Info", f"[ent_fire] {entity_name}.{input_name}({parameter})")

        # --- Primary path: dispatch through the play-mode IOManager, which runs
        #     the registered input handler (or the generic enable/disable/hide/…
        #     fallback) for this entity type — the same routing runtime
        #     connections use. ---
        io = None
        try:
            if (hasattr(self.main_window, 'view_3d')
                    and self.main_window.view_3d.logic_thread):
                io = self.main_window.view_3d.logic_thread.io_manager
        except Exception:
            io = None

        if io is not None:
            target_id = (entity.properties.get('id', '')
                         if hasattr(entity, 'properties')
                         else entity.get('id', ''))
            try:
                io._execute_input(entity_name, input_name, parameter,
                                  "console", target_id=target_id)
                debug_log("Info", f"✓ Fired input '{input_name}' on '{entity_name}'")
            except Exception as e:
                debug_log("Error", f"ent_fire failed: {e}")
            return

        # --- Editor mode (no active play session): entities implementing
        #     on_input() can still handle inputs directly (e.g. LevelChanger). ---
        if hasattr(entity, 'on_input') and callable(entity.on_input):
            try:
                success = entity.on_input(input_name, parameter)
                if success:
                    debug_log("Info", f"✓ Input '{input_name}' handled (editor mode)")
                else:
                    debug_log("Warning", f"Input '{input_name}' was not handled")
            except Exception as e:
                debug_log("Error", f"Exception in {entity.__class__.__name__}.on_input(): {e}")
        else:
            debug_log("Warning",
                      f"'{entity_name}' inputs require Play Mode "
                      f"(no active I/O manager in the editor).")

    def cmd_trigger(self, args):
        if not args:
            debug_log("Error", "Usage: trigger <entity>")
            return

        entity_name = args.strip()
        entity = self.editor_state.find_entity_by_name(entity_name)
        if not entity:
            debug_log("Error", f"Entity '{entity_name}' not found")
            return

        # Brush-based toggle
        if isinstance(entity, dict) and (entity.get('is_door') or entity.get('is_mover')):
            debug_log("Info", f"🔄 Toggling {entity_name}")
            io = self._get_io_manager()
            if io:
                io._execute_input(entity_name, "Toggle", "", "console")
            return

        # Generic entity fallback
        debug_log("Info", f"Triggering {entity_name}")
        self.cmd_fire(f"{entity_name} Trigger")

    def cmd_send_input(self, args):
        if not IO_AVAILABLE or len(args.split()) < 2:
            debug_log("Error", "Usage: send <entity> <input> [param]")
            return
        parts = args.split()
        entity_name = parts[0]
        input_name = parts[1]
        param = " ".join(parts[2:]) if len(parts) > 2 else ""
        entity = self.editor_state.find_entity_by_name(entity_name)
        if entity:
            io = self._get_io_manager()
            if io:
                io._execute_input(entity_name, input_name, param, "console")
            debug_log("Info", f"Sent input '{input_name}' to {entity_name}")
        else:
            debug_log("Error", f"Entity '{entity_name}' not found")

    def cmd_toggle(self, args):
        if not args:
            debug_log("Error", "Usage: toggle <entity>")
            return
        self.cmd_send_input(f"{args} Toggle")

    def cmd_set_property(self, args):
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            debug_log("Error", "Usage: setprop <entity> <key> <value>")
            return
        name, key, value = parts
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        if isinstance(entity, dict):
            entity[key] = value
        else:
            entity.properties[key] = value

        debug_log("Info", f"Set {name}.{key} = {value}")
        self.editor_state.save_state()

    def cmd_get_property(self, args):
        parts = args.split()
        if len(parts) < 2:
            debug_log("Error", "Usage: getprop <entity> <key>")
            return
        name, key = parts
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        if isinstance(entity, dict):
            val = entity.get(key, "<not found>")
        else:
            val = entity.properties.get(key, "<not found>")
        debug_log("Info", f"{name}.{key} = {val}")

    def cmd_list_outputs(self, args):
        if not args:
            debug_log("Error", "Usage: outputs <entity>")
            return
        entity = self.editor_state.find_entity_by_name(args)
        if entity:
            typ = get_entity_type_for_io(entity) if IO_AVAILABLE else "unknown"
            outs = get_output_names(typ) if IO_AVAILABLE else ["(I/O not loaded)"]
            debug_log("Info", f"Outputs for {args}: {', '.join(outs)}")
        else:
            debug_log("Error", f"Entity '{args}' not found")

    def cmd_list_inputs(self, args):
        if not args:
            debug_log("Error", "Usage: inputs <entity>")
            return
        entity = self.editor_state.find_entity_by_name(args)
        if entity:
            typ = get_entity_type_for_io(entity) if IO_AVAILABLE else "unknown"
            ins = get_input_names(typ) if IO_AVAILABLE else ["(I/O not loaded)"]
            debug_log("Info", f"Inputs for {args}: {', '.join(ins)}")
        else:
            debug_log("Error", f"Entity '{args}' not found")

    def cmd_connect_io(self, args):
        parts = args.split()

        if len(parts) < 4:
            debug_log("Error", "Usage: connect <source> <o> <target> <input> [delay] [param]")
            return

        src, outp, tgt, inp = parts[:4]

        # --- Safe delay parsing ---
        delay = 0.0
        if len(parts) > 4:
            try:
                delay = float(parts[4])
            except ValueError:
                debug_log("Error", f"Invalid delay '{parts[4]}' (must be a number)")
                return

        # --- Parameter ---
        param = " ".join(parts[5:]) if len(parts) > 5 else ""

        # --- Resolve source ---
        source_ent = self.editor_state.find_entity_by_name(src)
        if not source_ent:
            debug_log("Error", f"Source '{src}' not found")
            return

        # --- Resolve target (prevents silent broken connections) ---
        target_ent = self.editor_state.find_entity_by_name(tgt)
        if not target_ent:
            debug_log("Warning", f"Target '{tgt}' not found (connection will still be created)")

        # --- Create connection ---
        try:
            conn = OutputConnection(outp, tgt, inp, param, delay, fire_once=False)
        except Exception as e:
            debug_log("Error", f"Failed to create connection: {e}")
            return

        # --- Attach connection safely ---
        try:
            if hasattr(source_ent, 'add_output_connection'):
                source_ent.add_output_connection(conn)
            else:
                if not isinstance(source_ent, dict):
                    debug_log("Error", f"Source '{src}' cannot store IO connections")
                    return

                source_ent.setdefault('_io_connections', []).append(conn)

        except Exception as e:
            debug_log("Error", f"Failed to attach connection: {e}")
            return

        # --- Persist state ---
        try:
            self.editor_state.save_state()
        except Exception as e:
            debug_log("Warning", f"Connection created but failed to save state: {e}")

        # --- Final log ---
        debug_log(
            "Info",
            f"Connected {src}.{outp} → {tgt}.{inp}"
            + (f" (delay={delay})" if delay else "")
            + (f" param='{param}'" if param else "")
        )

    def cmd_disconnect_io(self, args):
        """disconnect <source> [output] [target] [input]
        Removes I/O connections from <source>. With no extra filters it removes
        every connection on the source; otherwise it removes only the ones that
        match each filter supplied (all comparisons are case-insensitive)."""
        if not IO_AVAILABLE:
            debug_log("Error", "I/O system not available")
            return

        parts = args.split()
        if not parts:
            debug_log("Error", "Usage: disconnect <source> [output] [target] [input]")
            return

        src = parts[0]
        f_out = parts[1] if len(parts) > 1 else None
        f_tgt = parts[2] if len(parts) > 2 else None
        f_inp = parts[3] if len(parts) > 3 else None

        source_ent = self.editor_state.find_entity_by_name(src)
        if not source_ent:
            debug_log("Error", f"Source '{src}' not found")
            return

        conns = get_connections(source_ent)
        if not conns:
            debug_log("Info", f"'{src}' has no I/O connections")
            return

        def matches(c):
            if f_out and c.output_name.lower() != f_out.lower():
                return False
            if f_tgt and c.target_name.lower() != f_tgt.lower():
                return False
            if f_inp and c.input_name.lower() != f_inp.lower():
                return False
            return True

        remaining = [c for c in conns if not matches(c)]
        removed = len(conns) - len(remaining)
        if removed == 0:
            debug_log("Warning", f"No matching connections on '{src}'")
            return

        set_connections(source_ent, remaining)

        try:
            self.editor_state.save_state()
        except Exception as e:
            debug_log("Warning", f"Disconnected but failed to save state: {e}")

        debug_log("Info", f"Removed {removed} connection(s) from '{src}'")

    def cmd_spawn(self, args):
        if not args:
            debug_log("Error", "Usage: spawn pickup health 25   or   spawn light")
            return
        parts = args.split()
        spawn_type = parts[0].lower()

        # Simple counter to guarantee unique names across spawns
        if not hasattr(self, '_spawn_counter'):
            self._spawn_counter = 0
        self._spawn_counter += 1

        if spawn_type == "pickup":
            if len(parts) < 2:
                debug_log("Error", "Usage: spawn pickup <health|ammo|gun1|key> [value]")
                return
            item = parts[1]
            value = parts[2] if len(parts) > 2 else "25"

            new_pickup = Pickup(pos=[0, 0, 0])         # name=None if constructor supports it
            new_pickup.properties['item_type'] = item
            new_pickup.properties['value'] = value
            # Unique name: includes item type AND counter
            new_pickup.properties['name'] = f"Pickup_{item}_{self._spawn_counter}"
            self.editor_state.things.append(new_pickup)
            debug_log("Info", f"Spawned pickup: {item} (value={value}) named '{new_pickup.properties['name']}'")
            self.editor_state.save_state()
            self.main_window.update_all_ui()

        elif spawn_type == "light":
            new_light = Light(pos=[0, 100, 0])
            new_light.properties['name'] = f"Light_{self._spawn_counter}"
            self.editor_state.things.append(new_light)
            debug_log("Info", f"Spawned light at [0, 100, 0] named '{new_light.properties['name']}'")
            self.editor_state.save_state()
            self.main_window.update_all_ui()

        elif spawn_type == "levelchanger":
            new_changer = LevelChanger(pos=[0, 40, 0])
            new_changer.properties['name'] = f"LevelChanger_{self._spawn_counter}"
            new_changer.properties['target_map'] = "Simple_Map_Test.json"
            self.editor_state.things.append(new_changer)
            debug_log("Info", f"Spawned LevelChanger at [0, 40, 0] named '{new_changer.properties['name']}'")
            self.editor_state.save_state()
            self.main_window.update_all_ui()

        else:
            debug_log("Error", f"Unknown spawn type '{spawn_type}'. Try: pickup, light, or levelchanger")

    def cmd_delete(self, args):
        if not args:
            debug_log("Error", "Usage: delete <entity_name>")
            return
        name = args.strip()
        entity = self.editor_state.find_entity_by_name(name)
        if not entity:
            debug_log("Error", f"Entity '{name}' not found")
            return

        self.editor_state.save_state()
        if isinstance(entity, dict):
            if entity in self.editor_state.brushes:
                self.editor_state.brushes.remove(entity)
        else:
            if entity in self.editor_state.things:
                self.editor_state.things.remove(entity)

        debug_log("Info", f"Deleted entity: {name}")
        self.main_window.update_all_ui()

    def cmd_list_connections(self, args):
        debug_log("Info", "=== ALL I/O CONNECTIONS ===")
        count = 0
        for brush in self.editor_state.brushes:
            name = brush.get('name', 'unnamed_brush')
            conns = get_connections(brush) if IO_AVAILABLE else brush.get('_io_connections', [])
            for c in conns:
                debug_log("Info", f"{name}.{c.output_name} → {c.target_name}.{c.input_name}")
                count += 1

        for thing in self.editor_state.things:
            name = thing.properties.get('name', 'unnamed_thing')
            conns = get_connections(thing) if IO_AVAILABLE else []
            for c in conns:
                debug_log("Info", f"{name}.{c.output_name} → {c.target_name}.{c.input_name}")
                count += 1

        debug_log("Info", f"Total connections: {count}")

    def cmd_cam(self, args):
        """cam [mode] [seconds]

        Toggle the play-mode camera between top-down (overhead) and first person,
        blending smoothly with a fast tween instead of switching instantly.

          cam            → toggle, 1 second tween (default)
          cam 2          → toggle, 2 second tween
          cam overhead   → go to overhead (aliases: top, topdown, td)
          cam fp 0.5     → go to first person over 0.5s (aliases: first, fps)
          cam 0          → switch instantly (no tween)

        Also triggerable from the I/O system via a logic_command entity, so a
        trigger brush can run e.g. "cam 2".
        """
        if not self._require_play_mode("cam"):
            return

        lt = getattr(self.main_window.view_3d, 'logic_thread', None)
        if lt is None or not hasattr(lt, 'start_camera_transition'):
            debug_log("Error", "Camera control unavailable (no active play session).")
            return

        target_mode = None
        duration = 1.0
        for tok in (args or "").split():
            low = tok.strip().lower()
            if not low:
                continue
            try:
                duration = float(low)
                continue  # numeric token = tween duration in seconds
            except ValueError:
                pass
            if low in ("overhead", "top", "topdown", "top-down", "td", "down"):
                target_mode = "overhead"
            elif low in ("fp", "first", "firstperson", "first-person", "fps", "person"):
                target_mode = "First Person"
            else:
                debug_log("Warning", f"cam: ignoring unknown argument '{tok}'")

        duration = max(0.0, duration)
        new_mode = lt.start_camera_transition(target_mode=target_mode, duration=duration)

        # Keep the view's cached camera_mode in step so its own _is_overhead()
        # (sprite/gameplay helpers) matches the target immediately.
        try:
            self.main_window.view_3d.camera_mode = new_mode
        except Exception:
            pass

        if duration > 0.0:
            self.main_window.show_toast(f"Camera → {new_mode} ({duration:g}s)")
            debug_log("Info", f"Camera tweening to {new_mode} over {duration:g}s")
        else:
            self.main_window.show_toast(f"Camera → {new_mode}")
            debug_log("Info", f"Camera switched to {new_mode}")

    def cmd_noclip(self, args):
        if not self._require_play_mode("noclip"):
            return
        view_3d = self.main_window.view_3d
        player = view_3d.player
        player.physics_enabled = not player.physics_enabled
        state = "OFF" if not player.physics_enabled else "ON"
        self.main_window.show_toast(f"Noclip: {state}")
        debug_log("Info", f"Noclip set to {state}")

    def cmd_god(self, args):
        if not self._require_play_mode("god"):
            return
        lt = self.main_window.view_3d.logic_thread
        lt.god_mode = not lt.god_mode
        state = "ON" if lt.god_mode else "OFF"
        if lt.god_mode:
            # Turning on god also disables buddha to avoid confusion
            lt.buddha_mode = False
        self.main_window.show_toast(f"God mode: {state}")
        debug_log("Info", f"God mode set to {state}")

    def cmd_buddha(self, args):
        if not self._require_play_mode("buddha"):
            return
        lt = self.main_window.view_3d.logic_thread
        lt.buddha_mode = not lt.buddha_mode
        state = "ON" if lt.buddha_mode else "OFF"
        if lt.buddha_mode:
            # Turning on buddha also disables god to avoid confusion
            lt.god_mode = False
        self.main_window.show_toast(f"Buddha mode: {state}")
        debug_log("Info", f"Buddha mode set to {state}")

    def cmd_notarget(self, args):
        """Toggle notarget mode — monsters ignore the player."""
        if not self._require_play_mode("notarget"):
            return
        lt = self.main_window.view_3d.logic_thread
        lt.notarget = not lt.notarget
        state = "ON" if lt.notarget else "OFF"
        self.main_window.show_toast(f"Notarget: {state}")
        debug_log("Info", f"Notarget set to {state}")

    def cmd_spatial_grid(self, args):
        """Toggle spatial grid debug visualisation in the 3D view."""
        if not self._require_play_mode("sg"):
            return
        view_3d = self.main_window.view_3d
        view_3d.show_spatial_grid = not getattr(view_3d, 'show_spatial_grid', False)
        state = "ON" if view_3d.show_spatial_grid else "OFF"
        self.main_window.show_toast(f"Spatial Grid: {state}")
        debug_log("Info", f"Spatial grid display set to {state}")

    def cmd_physics(self, args):
        if not self._require_play_mode("physics"):
            return

        player = self.main_window.view_3d.player
        arg = args.lower().strip() if args else "toggle"

        if arg in ("on", "1", "true"):
            player.physics_enabled = True
        elif arg in ("off", "0", "false"):
            player.physics_enabled = False
        else:
            player.physics_enabled = not getattr(player, 'physics_enabled', True)

        state = "ON" if player.physics_enabled else "OFF"
        self.main_window.show_toast(f"Physics: {state}")
        debug_log("Info", f"Physics set to {state}")

    def cmd_setpos(self, args):
        if not self._require_play_mode("setpos"):
            return

        try:
            parts = args.split()
            if len(parts) != 3:
                raise ValueError
            x = float(parts[0])
            y = float(parts[1])
            z = float(parts[2])

            self.main_window.view_3d.player.position = [x, y, z]
            debug_log("Info", f"Player teleported to [{x:.1f}, {y:.1f}, {z:.1f}]")
            self.main_window.show_toast(f"Teleported to {x:.1f}, {y:.1f}, {z:.1f}")
        except Exception:
            debug_log("Error", "Usage: setpos x y z   (example: setpos 0 50 100)")

    # NEW: Split-screen command
    def cmd_split_screen(self, args):
        """Toggle split-screen mode (mirrors F9)."""
        if not self._require_play_mode("ss"):
            return
        view_3d = self.main_window.view_3d
        if hasattr(view_3d, '_toggle_splitscreen'):
            view_3d._toggle_splitscreen()
        else:
            debug_log("Error", "Split-screen toggle not available.")

    def cmd_clear(self, args):
        self.main_window.debug_console.clear()

    def cmd_fps(self, args):
        config = self.main_window.config
        show = not config.getboolean('Display', 'show_fps', fallback=False)
        if not config.has_section('Display'):
            config.add_section('Display')
        config.set('Display', 'show_fps', str(show))
        if hasattr(self.main_window, 'show_fps_checkbox'):
            self.main_window.show_fps_checkbox.setChecked(show)
        self.main_window.save_config()
        self.main_window.view_3d.update()
        debug_log("Info", f"FPS display {'ON' if show else 'OFF'}")

    def cmd_map(self, args):
        if not args:
            debug_log("Warning", "Usage: map <mapname>")
            return
        map_name = args if isinstance(args, str) else args[0]
        if not map_name.endswith('.json'):
            map_name += '.json'
        map_path = os.path.join(self.main_window.root_dir, 'maps', map_name)
        if os.path.exists(map_path):
            self.main_window.load_level_file(map_path)
            debug_log("Info", f"Loaded map {map_name}")
        else:
            debug_log("Error", f"Map not found: {map_name}")

    # ===================================================================
    # SAVE / LOAD  (play-session serialization)
    # ===================================================================

    QUICKSAVE_NAME = "quicksave"
    SAVE_EXT = ".fiosave"

    def _saves_dir(self):
        """Absolute path to the saves directory (created on demand)."""
        root = getattr(self.main_window, 'root_dir', os.getcwd())
        path = os.path.join(root, 'saves')
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    def _resolve_save_path(self, name):
        """Turn a user-supplied save name into a safe absolute .fiosave path.

        Only the basename is honoured (no path traversal), and the .fiosave
        extension is added if missing.
        """
        name = (name or "").strip() or self.QUICKSAVE_NAME
        name = os.path.basename(name)
        if not name.lower().endswith(self.SAVE_EXT):
            name += self.SAVE_EXT
        return os.path.join(self._saves_dir(), name)

    def _logic_thread(self):
        view_3d = getattr(self.main_window, 'view_3d', None)
        return getattr(view_3d, 'logic_thread', None) if view_3d else None

    def _in_play_mode(self):
        view_3d = getattr(self.main_window, 'view_3d', None)
        return bool(getattr(view_3d, 'play_mode', False)) if view_3d else False

    def _current_map_name(self):
        """Basename of the currently loaded map file, or '' if untitled."""
        fp = getattr(self.main_window, 'file_path', None)
        return os.path.basename(fp) if fp else ""

    def _save_mode(self):
        """Configured default play-session save strategy (full/delta/both)."""
        try:
            from engine import savegame
            mode = self.main_window.config.get('Settings', 'save_mode',
                                               fallback=savegame.SAVE_MODE_FULL)
            mode = str(mode).strip().lower()
            if mode in savegame.VALID_SAVE_MODES:
                return mode
        except Exception:
            pass
        return 'full'

    def _base_level(self):
        """The normalized *original* map document, for delta diffing.

        Reads the currently-loaded map file straight from disk (or the active
        resource package) and re-serializes it through the editor's own pipeline
        so it compares like-for-like with the live level. Returns ``None`` when
        the base map can't be resolved — the saver then degrades to a full save.
        """
        fp = getattr(self.main_window, 'file_path', None)
        if not fp:
            return None
        try:
            from engine.resource_manager import ResourceManager
            rm = ResourceManager()
            if rm.is_package_mode():
                raw = rm.get_text_asset(fp)
                if raw is None:
                    return None
                raw_level = json.loads(raw)
            else:
                if not os.path.exists(fp):
                    return None
                with open(fp, 'r') as f:
                    raw_level = json.load(f)
        except Exception as exc:
            debug_log("Warning", f"save: could not read base map for delta: {exc}")
            return None
        try:
            from engine import savegame
            return savegame.normalize_base_level(raw_level)
        except Exception:
            return raw_level

    def cmd_save(self, args):
        """save [name] — Serialize the current play session to saves/<name>.fiosave.

        Requires Play Mode (there is no live session to capture in the editor).
        Defaults to the quicksave slot when no name is given.
        """
        if not self._in_play_mode():
            debug_log("Error", "save: enter Play Mode first (nothing to save in the editor).")
            return
        lt = self._logic_thread()
        if lt is None:
            debug_log("Error", "save: no active play session.")
            return
        path = self._resolve_save_path(args)
        save_mode = self._save_mode()
        base_level = self._base_level() if save_mode != 'full' else None
        ok, msg = lt.save_session(path, map_name=self._current_map_name(),
                                  save_mode=save_mode, base_level=base_level)
        debug_log("Info" if ok else "Error", msg)
        if ok:
            self.main_window.show_toast(f"Saved: {os.path.basename(path)}")

    def cmd_quicksave(self, args):
        """quicksave — Save to the quicksave slot (saves/quicksave.fiosave)."""
        self.cmd_save(self.QUICKSAVE_NAME)

    def cmd_load(self, args):
        """load [name] — Restore a saved play session from saves/<name>.fiosave.

        In Play Mode the save is applied directly to the running session (a true
        quickload). From the editor it loads the save's map, enters Play Mode,
        then applies the saved state. Defaults to the quicksave slot.
        """
        path = self._resolve_save_path(args)
        if not os.path.exists(path):
            debug_log("Error", f"load: save not found: {os.path.basename(path)}")
            return

        # Already playing → overlay straight onto the live session.
        if self._in_play_mode():
            lt = self._logic_thread()
            if lt is None:
                debug_log("Error", "load: no active play session.")
                return
            ok, msg = lt.load_session(path, map_name=self._current_map_name())
            debug_log("Info" if ok else "Error", msg)
            if ok:
                self.main_window.show_toast(f"Loaded: {os.path.basename(path)}")
                self.main_window.update_all_ui()
            return

        # In the editor → load the save's map, enter play, then apply.
        self._load_from_editor(path)

    def _load_from_editor(self, path):
        """Load a save while in editor mode: reload map, enter play, overlay."""
        try:
            from engine import savegame
            data = savegame.read(path)
        except Exception as exc:
            debug_log("Error", f"load failed: {exc}")
            return

        map_name = data.get('map', '')
        if map_name:
            map_path = map_name
            if not os.path.exists(map_path):
                map_path = os.path.join(self.main_window.root_dir, 'maps',
                                        os.path.basename(map_name))
            if os.path.exists(map_path):
                self.main_window.load_level_file(map_path)
            else:
                debug_log("Warning",
                          f"load: map '{map_name}' not found; applying to the "
                          f"currently loaded level instead.")
        else:
            debug_log("Warning", "load: save has no map reference; using the "
                                 "currently loaded level.")

        # Enter play mode (needs a PlayerStart in the scene).
        try:
            self.main_window.enter_play_mode()
        except Exception as exc:
            debug_log("Error", f"load: could not enter play mode: {exc}")
            return
        if not self._in_play_mode():
            debug_log("Error", "load: failed to enter play mode (is there a "
                               "Player Start in the level?).")
            return

        lt = self._logic_thread()
        if lt is None:
            debug_log("Error", "load: no active play session after entering play.")
            return
        ok, msg = lt.load_session(path, map_name=self._current_map_name())
        if not ok and 'different base map' in (msg or ''):
            # Genuinely ambiguous: a delta whose base map we couldn't reconcile.
            # This is the one case where automatic recovery isn't safe — ask.
            if self._confirm_force_delta(path):
                from engine import savegame
                try:
                    data = savegame.read(path)
                    savegame.restore_delta(lt, data)
                    ok, msg = True, (f"Loaded play session from "
                                     f"'{os.path.basename(path)}' — forced delta "
                                     f"onto the current map (missing entities skipped)")
                except Exception as exc:
                    ok, msg = False, f"Load failed: {exc}"
        debug_log("Info" if ok else "Error", msg)
        if ok:
            self.main_window.show_toast(f"Loaded: {os.path.basename(path)}")
            self.main_window.update_all_ui()

    def _confirm_force_delta(self, path):
        """Ask whether to force-apply a delta whose base map doesn't match.

        The only place ordinary loading prompts: the automatic path has already
        decided it can't safely reconcile the base map, so we let the user choose
        to overlay by UUID anyway (skipping entities that don't exist) or cancel.
        """
        try:
            reply = QMessageBox.question(
                self.main_window,
                "Base map mismatch",
                (f"'{os.path.basename(path)}' is a delta save made on a different "
                 "base map.\n\nApply its changes to the current map anyway? "
                 "Entities that don't exist here will be skipped."),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return reply == QMessageBox.Yes
        except Exception:
            return False

    def cmd_quickload(self, args):
        """quickload — Load from the quicksave slot (saves/quicksave.fiosave)."""
        self.cmd_load(self.QUICKSAVE_NAME)

    def cmd_list_saves(self, args):
        """saves — List available save files in the saves directory."""
        saves_dir = self._saves_dir()
        try:
            files = sorted(f for f in os.listdir(saves_dir)
                           if f.lower().endswith(self.SAVE_EXT))
        except OSError:
            files = []
        if not files:
            debug_log("Info", "No saved games found.")
            return
        debug_log("Info", f"=== SAVES ({len(files)}) ===")
        for f in files:
            full = os.path.join(saves_dir, f)
            info = ""
            try:
                with open(full, 'r', encoding='utf-8') as fh:
                    d = json.load(fh)
                info = f"  [map: {d.get('map', '?')}, saved: {d.get('saved_at', '?')}]"
            except Exception:
                pass
            debug_log("Info", f"  {f}{info}")