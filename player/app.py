"""Universal package player: archive, shared engine simulation and platform host."""
from __future__ import annotations

import math
import os
import sys

from player.runtime.fiopak import FioPackage, PackageError
from .platform.base import HostConfig, FrameCallbacks
from .input.state import ACTION_PAUSE, ACTION_RESTART, ACTION_SCORES
from .plugin_ui import arena
from .plugin_ui.menus import PluginMenus


def is_android():
    return bool(os.environ.get('ANDROID_ARGUMENT')) or 'android' in sys.platform


class FioPlayerApp:
    def __init__(self, package=None, config=None, *, validation=False):
        self.package = package
        self.config = config or HostConfig(title=f'Fio Player — {package.title}' if package else 'Fio Player')
        self.renderer = self.host = self.world = self.documents = None
        self.map_data = None
        self.map_path = None
        self.cam_pos = [0, 64, 0]
        self.cam_yaw, self.cam_pitch = -90., 0.
        self._paused = package is None
        self.audio = None
        self._picker_opened = False
        self._pending_open = None
        self._validation = validation
        self._validation_elapsed = 1.
        self._quality_frames = 0
        self.menus = PluginMenus()
        self._lan_active = False
        self._text_input = False
        self._plugin_errors = set()

    def run(self, host=None):
        callbacks = FrameCallbacks(on_gl_ready=self._on_gl_ready, on_frame=self._on_frame,
                                   on_resize=self._on_resize, on_pause=self._on_pause,
                                   on_resume=self._on_resume, on_shutdown=self._on_shutdown)
        self.host = host or self._make_host(callbacks)
        self.host.callbacks = callbacks
        if is_android():
            from .platform.documents import AndroidDocuments
            self.documents = AndroidDocuments()
            self.host.documents = self.documents
            self.host.on_open_package = self.open_package
        try:
            self.host.run()
        finally:
            self._on_shutdown()

    def _make_host(self, callbacks):
        if is_android():
            from .platform.android import AndroidHost
            return AndroidHost(self.config, callbacks)
        from .platform.desktop import DesktopHost
        return DesktopHost(self.config, callbacks)

    def _on_gl_ready(self, host):
        self._quality_frames = 0
        from .render.renderer import GLESRenderer
        if self.renderer is None:
            from dataclasses import replace
            from .render.quality import resolve
            name = self.config.quality
            if name == 'auto':
                name = 'mobile' if is_android() else 'reference'
            quality = resolve(name)
            if self.config.render_scale is not None:
                quality = replace(quality, render_scale=self.config.render_scale)
            if self.config.adaptive_resolution is not None:
                quality = replace(quality, adaptive=self.config.adaptive_resolution)
            self.renderer = GLESRenderer(gles=is_android(), quality=quality)
            self.renderer.adaptive.target_fps = max(1, self.config.target_fps)
        self.renderer.on_gl_ready()
        self.renderer.resize(host.width, host.height)
        if self.package is not None and self.world is None:
            self._load_level(self.package.start_map_path())
        if self.package is None and self.documents is not None and not self._picker_opened:
            self._picker_opened = True
            self.documents.open_world()

    def open_package(self, path):
        package = FioPackage.open(path)
        old_package = self.package
        try:
            self._load_level(package.start_map_path(), package=package)
        except Exception:
            package.close()
            raise
        self.package = package
        if old_package:
            old_package.close()

    def _load_level(self, path, spawn='', package=None, *, recover=True):
        package = package or self.package
        self._load_world(package.load_map(path), path, spawn, package, recover=recover)

    def _load_world(self, data, path, spawn='', package=None, *, recover=True):
        """Play *data* (map JSON) with *package*'s assets.

        A LAN game plays the host's map JSON this way, drawn with the assets
        of the desktop-exported world open on this device.
        """
        from .world import PlayerWorld
        from .runtime.plugin_assets import PluginAssetPackage
        package = package or self.package
        previous = (self.package, self.map_path)
        loadout = self.world.logic.session_runtime.carried_loadout() if self.world else None
        # Stop the old session before rebinding the shared installed-plugin
        # manager; teardown must not run against a new session's subscriptions.
        if self.world:
            self.world.close()
        if self.audio:
            self.audio.close()
            self.audio = None
        self.world = None
        world = None
        try:
            world = PlayerWorld(data, package, spawn)
            if self.renderer:
                # Shipped plugins draw some entities with their own files,
                # which a .fiopak may not carry.
                self.renderer.load_scene(data, PluginAssetPackage(package), world=world)
        except Exception:
            if world:
                world.close()
            if self.renderer:
                self.renderer.clear_scene()
            self._paused = True
            if recover and previous[0] and previous[1]:
                try:
                    self._load_level(previous[1], package=previous[0], recover=False)
                    if loadout:
                        self.world.logic.session_runtime.restore_loadout(loadout)
                except Exception:
                    self._paused = True
            raise
        self.package = package
        self.world, self.map_data, self.map_path = world, data, path
        from .audio import PlayerAudio
        self.audio = PlayerAudio(package)
        self.cam_yaw = 90 - math.degrees(world.player.angle)
        self.cam_pitch = math.degrees(world.player.pitch)
        self.cam_pos = [world.player.pos.x, world.player.pos.y + world.player.view_height, world.player.pos.z]
        self._paused = False
        self._quality_frames = 0
        self.menus.reset()

    # -- plugin controls -----------------------------------------------
    def set_text_input(self, active):
        """Show or hide the soft keyboard for a menu text field."""
        active = bool(active)
        if active == self._text_input:
            return
        self._text_input = active
        method = getattr(self.host, 'start_text_input' if active else 'stop_text_input', None)
        if method is not None:
            method()

    def _guarded(self, label, action, *args, default=None):
        """Run plugin presentation work; a failure must never stop the game.

        Plugin rules run inside the engine, which guards its own plugin
        calls. This guards the player's UI around them: each distinct failure
        is reported once, and the frame carries on without that piece.
        """
        try:
            return action(*args)
        except Exception as error:
            self._report(label, error)
            return default

    def _report(self, label, error):
        key = (label, type(error).__name__, str(error)[:200])
        if key not in self._plugin_errors:
            self._plugin_errors.add(key)
            import traceback
            print(f'[Fio Player] {label} failed: {error}\n'
                  + ''.join(traceback.format_exception(error)), flush=True)

    def _lan_session_active(self):
        session = arena.session(self.world) if self.world else None
        return session is not None and session.network is not None

    def _set_lan_active(self, active):
        """Hold Android's Wi-Fi multicast lock while LAN play needs broadcasts."""
        if active != self._lan_active:
            self._lan_active = active
            method = getattr(self.host, 'set_lan_active', None)
            if method is not None:
                method(active)

    def host_lan(self, lobby):
        """Host the open world as a LAN arena (desktop: LAN dialog → Host)."""
        if self.world is None or self.package is None:
            return 'Open a world exported from desktop Fio first.'
        if self._lan_session_active():
            return 'Leave the current LAN game first.'
        try:
            data = lobby.prepare_host(self.world.state.get_level_data())
        except ValueError as error:
            return str(error)
        transport = None
        try:
            transport = lobby.open_host()
            self._load_world(data, self.map_path, '', self.package, recover=False)
            session = arena.session(self.world)
            if session is None:
                raise ValueError('This world could not start a Deathmatch session.')
            from plugins.deathmatch.network_game import HostGame
            logic = self.world.logic
            with logic._tick_lock, logic.session_runtime.monster_lock:
                HostGame(session, transport, data, lobby.name.strip() or 'Host')
                session.publish()
        except (OSError, ValueError) as error:
            if transport is not None:
                transport.close()
            if self.world is None and self.map_path:
                try:
                    self._load_level(self.map_path, recover=False)
                except Exception:
                    pass
            return f'Could not host: {error}'
        from .plugin_ui.lobby import local_address
        return f'Hosting on {local_address()} (TCP {lobby.port}, discovery UDP {lobby.discovery_port}).'

    def _join_lan(self, connection, packet):
        """Play the host's map with the open world's assets, then follow it."""
        lobby = self.menus.lobby
        try:
            data = lobby.check_welcome(packet)
            if self.package is None:
                raise ValueError('Open the world exported from desktop Fio first.')
            self._load_world(data, self.map_path, '', self.package)
            session = arena.session(self.world)
            if session is None:
                raise ValueError('The host map does not contain an arena.')
            from plugins.deathmatch.network_game import ClientGame
            logic = self.world.logic
            with logic._tick_lock, logic.session_runtime.monster_lock:
                ClientGame(session, connection, packet)
        except Exception as error:
            connection.close()
            lobby.status = f'Could not join: {error}'
            return
        lobby.status = 'Joined LAN game.'
        self.menus.reset()
        self._paused = False

    def leave_lan(self):
        """End the LAN session by reopening the world as it was authored."""
        if self.package is None or not self.map_path:
            return 'No LAN game to leave.'
        self._load_level(self.map_path)
        self._paused = True
        return 'Left the LAN game.'

    def _activate_menu(self, key):
        try:
            return self.menus.activate(key, self)
        except Exception as error:
            self._report('menu action', error)
            self.menus.message = f'That action failed: {error}'
            return None

    def _plugin_frame(self, dt, inp):
        """Lobby, soft keyboard and LAN upkeep; runs every frame."""
        if self.host is not None:
            events = getattr(self.host, 'text_events', None)
            if events:
                self._guarded('text entry', self.menus.text, list(events))
                events.clear()
        self.set_text_input(self._paused and self.menus.editing is not None)
        welcome = self._guarded('LAN lobby', self.menus.lobby.pump)
        if welcome is not None:
            self._join_lan(*welcome)
        self._guarded('LAN upkeep', self._lan_upkeep, dt)
        self._guarded('match controls', self._match_controls, inp)

    def _lan_upkeep(self, dt):
        networked = self._lan_session_active()
        self._set_lan_active(networked or (self._paused and self.menus.page == 'lan')
                             or self.menus.lobby.pending is not None)
        if self.world is None:
            return
        runtime = self.world.logic.session_runtime
        if networked and self._paused:
            # Keep the LAN game flowing behind the menu, paused as on desktop:
            # peers keep their connection and see the host's pause.
            runtime.set_world_paused('player_menu', True)
            from .input.state import InputState
            idle = InputState()
            idle.begin_frame()
            self.world.step(dt, idle, self.cam_yaw, self.cam_pitch)
        else:
            runtime.set_world_paused('player_menu', False)

    def _match_controls(self, inp):
        world = self.world
        if world is not None and not self._paused and inp.just_pressed(ACTION_RESTART):
            if arena.can_restart(world):
                arena.restart(world)
        if self.host is not None and hasattr(self.host, 'touch'):
            self.host.touch.set_context(
                weapons=len(world.owned_weapons()) if world else 0,
                arena=bool(world and arena.session(world)),
                can_restart=bool(world and arena.can_restart(world)))

    def _on_frame(self, dt, inp):
        import time
        simulation_ms = 0.
        if inp.just_pressed(ACTION_PAUSE):
            self.menus.editing = None
            if self._paused and self.menus.page != 'root':
                # Android Back steps out of a plugin page first.
                self.menus.back()
            else:
                self._paused = not self._paused
                self.menus.reset()
        if self.host:
            self.host.set_mouse_captured(bool(self.world and not self._paused))
            from .menu import hit_test
            for point in self.host.ui_events:
                page = self.menus.current(self) if self._paused else None
                key = hit_test(point, self.host.width, self.host.height, self.world is not None, page) if self._paused else None
                action = self._activate_menu(key) if key else None
                if action == 'resume':
                    self._paused = False
                    self.menus.reset()
                elif action == 'exit':
                    self.host.request_quit()
                elif action == 'open':
                    self._open_dialog()
            self.host.ui_events.clear()
        self._plugin_frame(dt, inp)
        if self.world and not self._paused:
            self.cam_yaw += inp.look_x * 120 * dt + inp.look_delta_x
            self.cam_pitch = max(-89, min(89, self.cam_pitch + inp.look_y * 120 * dt + inp.look_delta_y))
            simulation_start = time.perf_counter()
            self.cam_pos = self.world.step(dt, inp, self.cam_yaw, self.cam_pitch)
            simulation_ms = (time.perf_counter() - simulation_start) * 1000
            # Portal transit and cutscenes may change the authoritative pose.
            self.cam_yaw = 90 - math.degrees(self.world.player.angle)
            self.cam_pitch = math.degrees(self.world.player.pitch)
            self._advance_level()
        if self.audio and self.world:
            self.audio.pause(self._paused)
            self.audio.process(self.world.game_state.consume_sounds(), self.cam_pos, self.cam_yaw)
        if self.renderer:
            frame_ms = getattr(self.host, 'frame_time_ms', dt*1000)
            if self.renderer.quality.adaptive and self.world and not self._paused:
                self._quality_frames += 1
                # Exclude initial package/shader startup from quality decisions.
                if self._quality_frames > 30:
                    self.renderer.adaptive.submit_frame(frame_ms)
            render_start = time.perf_counter()
            state = self._render_state()
            self.renderer.render(state)
            state["control_buttons"] = tuple((b.action, b.cx, b.cy) for b in self.host.touch.buttons) if self.config.show_touch_controls else ()
            self.renderer.render_ui(state, self.host.width, self.host.height, self.world is not None)
            if self.config.show_touch_controls and self.host and not self._paused:
                self.renderer.render_overlay(self.host.touch, inp, self.host.width, self.host.height)
            self.renderer.frame_stats.update(frame_ms=frame_ms, simulation_cpu_ms=simulation_ms,
                render_submit_ms=(time.perf_counter()-render_start)*1000)
        if self._validation and self.world:
            self._validation_elapsed += dt
            if self._validation_elapsed >= 1.:
                import json
                self._validation_elapsed = 0.
                print('[FIO_VALIDATION] ' + json.dumps({
                    'map': self.map_path, 'paused': self._paused,
                    'input_blocked': bool(self.world.logic.cutscene_runtime.state),
                    'movement_input': [inp.move_x, inp.move_y],
                    'touches': len(self.host._pointers),
                    'controllers': len(self.host._joysticks),
                    'position': [float(v) for v in self.world.player.pos],
                    'brushes': len(self.world.brushes),
                    'entities': len(self.world.state.things),
                    'weapon': self.world.logic.combat_runtime.active_weapon,
                    'weapon_hud': (self.world.hud_state['held'].hud_sprite
                                   if self.world.hud_state['held'] else ''),
                    'monsters': [{'name': t.properties.get('name'),
                                  'health': t.properties.get('health'),
                                  'dead': bool(t.properties.get('dead'))}
                                 for t in self.world.state.things
                                 if str(t.properties.get('type','')).lower() == 'monster'],
                    'render': self.renderer.frame_stats if self.renderer else {},
                    'missing_images': sorted(self.renderer._images.missing)
                                      if self.renderer and self.renderer._images else [],
                }), flush=True)

    def _open_dialog(self):
        if self.documents:
            self.documents.open_world()
            return
        from PyQt5.QtWidgets import QApplication, QFileDialog
        application = QApplication.instance() or QApplication([])
        path, _ = QFileDialog.getOpenFileName(None, 'Open world', '', 'Fio worlds (*.fiopak)')
        if path:
            self.open_package(path)

    def _advance_level(self):
        if self.world.pending_level:
            target, spawn = self.world.pending_level
            self.world.pending_level = None
            path = self.package.resolve_map(target, self.map_path)
            loadout = self.world.logic.session_runtime.carried_loadout()
            self._load_level(path, spawn)
            self.world.logic.session_runtime.restore_loadout(loadout)

    def _render_state(self):
        state = dict(cam_pos=self.cam_pos, cam_yaw=self.cam_yaw, cam_pitch=self.cam_pitch,
                     paused=self._paused, hud=self.world.hud if self.world else 'Open a .fiopak world')
        if self._paused:
            from .menu import root_page
            state['menu'] = self._guarded('pause menu', self.menus.current, self,
                                          default=root_page(self.world is not None))
        if self.world:
            eye, direction, up, fov = self.world.camera()
            from .render import glmath
            from .plugin_ui import bigworld, scoreboard
            from engine.glasses import glasses_path
            game_hud = dict(self.world.hud_state)
            match = self._guarded('scoreboard', arena.snapshot, self.world, default={})
            if match:
                game_hud['prompt'] = scoreboard.prompt(game_hud.get('prompt'))
            full = bool(self.host and self.host.input_state.is_down(ACTION_SCORES))
            panel = self._guarded('Bigworld panel', bigworld.state, self.world)
            glasses = self._guarded('spectator glasses', arena.spectator_glasses, self.world, default=())
            state.update(game_hud=game_hud, cam_pos=eye, view=glmath.look_at(eye, eye + direction, up),
                         fov=fov, world=self.world, arena=match, arena_full=full, bigworld=panel,
                         plugin_ui_key=(self._guarded('scoreboard', scoreboard.key, match, full), panel),
                         billboards=tuple((position, glasses_path(style), (40.0, 18.0))
                                          for position, style in glasses))
        return state

    def _on_resize(self, width, height):
        if self.renderer:
            self.renderer.resize(width, height)
        if self.world:
            self.world.logic.camera.set_frustum_aspect(width / max(1, height))

    def _on_pause(self):
        self._paused = True
        if self.audio:
            self.audio.pause(True)

    def _on_resume(self):
        self._paused = True
        self._quality_frames = 0

    def _on_shutdown(self):
        self.menus.lobby.close()
        self._set_lan_active(False)
        if self.audio:
            self.audio.close()
        if self.world:
            self.world.close()
            self.world = None
        if self.renderer:
            lifecycle = getattr(self.host, 'lifecycle', None)
            if lifecycle is None or lifecycle.surface_valid:
                self.renderer.close()
            self.renderer = None
        if self.documents:
            self.documents.close()
        if self.package:
            self.package.close()
