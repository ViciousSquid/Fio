"""
Player-side plugin host.

Lets the standalone ``.fiopak`` player load and *run* the plugins a package
depends on — so a game built with, say, the Tidy plugin actually plays outside
the editor. It is the player-side counterpart to the editor's
``plugins.integration``: where the editor patches its logic thread to dispatch
the plugin lifecycle, this drives the same dispatch from the player's frame loop.

Responsibilities:

* **Load** the bundled plugins. If the app already ships the ``plugins``
  package (the Android build does), that copy is used; otherwise the plugins
  carried inside the package are extracted to a writable dir and imported.
* **Bridge** the player's free-look camera to the minimal ``logic`` interface
  plugin runtimes expect (``things`` / ``player`` / ``io_manager`` /
  ``current_hud_message``), building entity instances from the map's data.
* **Dispatch** ``on_play_start`` / ``on_tick`` / ``on_play_stop`` each frame.

Everything is guarded: if the plugin system isn't present or a package needs no
plugins, the host stays inert and the player runs exactly as before.

Note: the player's renderer is still bringing up map-geometry drawing, so plugin
*gameplay* runs here (state changes, HUD text) ahead of the visuals catching up
— ``things`` and ``hud_message`` are exposed for the renderer to consume once it
draws dynamic models.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from typing import List, Optional


class _CamPlayer:
    """Adapts the player's free-look camera to the engine player interface.

    Plugin runtimes read ``pos`` / ``angle`` / ``pitch`` / ``camera_height`` and
    build a forward vector as ``(sin a·cos p, sin p, cos a·cos p)``. The player
    camera uses a yaw in degrees whose ground forward is ``(cos yaw, sin yaw)``,
    so ``angle = 90° − yaw`` makes the two conventions agree, and ``pos`` is
    already the eye (``camera_height = 0``).
    """

    def __init__(self):
        self.pos = (0.0, 0.0, 0.0)
        self.angle = 0.0
        self.pitch = 0.0
        self.camera_height = 0.0

    def update(self, cam_pos, cam_yaw_deg: float, cam_pitch_deg: float):
        self.pos = (float(cam_pos[0]), float(cam_pos[1]), float(cam_pos[2]))
        self.angle = math.radians(90.0 - cam_yaw_deg)
        self.pitch = math.radians(cam_pitch_deg)


class _NullIO:
    """No-op I/O manager: plugin output fires are harmless on the player."""

    def fire_output(self, *args, **kwargs):
        pass


class _BridgeLogic:
    """The ``logic`` object the plugin lifecycle/tick hooks receive."""

    def __init__(self, things):
        self.things = things
        self.player = _CamPlayer()
        self.io_manager = _NullIO()
        self.current_hud_message = ""


class PlayerPluginHost:
    def __init__(self):
        self.manager = None
        self.bridge: Optional[_BridgeLogic] = None
        self.active = False
        self.hud_message = ""
        self._extract_root: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def things(self) -> List:
        """Plugin entity instances for the current scene (for rendering)."""
        return self.bridge.things if self.bridge is not None else []

    # ------------------------------------------------------------------
    def load(self, package, extract_dir: Optional[str] = None) -> bool:
        """Make the package's plugins importable and load them.

        Returns True if at least one plugin loaded. Safe to call with a package
        that bundles no plugins (returns False, stays inert).
        """
        try:
            bundled = package is not None and getattr(
                package, "has_bundled_plugins", lambda: False)()
            if bundled:
                root = extract_dir or tempfile.mkdtemp(prefix="fio_plugins_")
                if self._extract_plugins(package, root):
                    self._extract_root = root
                    if root not in sys.path:
                        sys.path.insert(0, root)
        except Exception as exc:
            print(f"[Fio Player] plugin extract failed: {exc}")

        try:
            from plugins.manager import get_manager, load_plugins
        except Exception:
            return False  # no plugin system available (app or package)

        try:
            load_plugins()
            self.manager = get_manager()
        except Exception as exc:
            print(f"[Fio Player] plugin load failed: {exc}")
            return False

        self.active = bool(self.manager and self.manager.plugins)
        return self.active

    def _extract_plugins(self, package, dest: str) -> bool:
        """Write every ``plugins/**`` entry from the package under *dest*."""
        wrote = False
        for name in package.namelist():
            if not name.startswith("plugins/") or name.endswith("/"):
                continue
            raw = package.read_asset(name)
            if raw is None:
                # read_asset normalises to asset roots; fall back to raw read.
                raw = getattr(package, "_read_raw", lambda _n: None)(name)
            if raw is None:
                continue
            out = os.path.join(dest, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(raw)
            wrote = True
        return wrote

    # ------------------------------------------------------------------
    def _activate_required_plugins(self, map_data: dict) -> List:
        """Enable the *global* plugins a map declares and apply their config.

        Global plugins (e.g. ``topdown``) place no entities, so they can't be
        auto-enabled from ``things``. A map/package names them under
        ``required_plugins`` (baked in at export), with optional per-plugin
        settings under ``plugin_config``. Returns the plugins activated.
        """
        names = map_data.get("required_plugins") if isinstance(map_data, dict) else None
        if not isinstance(names, (list, tuple)):
            return []
        cfg = map_data.get("plugin_config")
        cfg = cfg if isinstance(cfg, dict) else {}
        activated: List = []
        for nm in names:
            plugin = self.manager.find_plugin(str(nm))
            if plugin is None:
                continue
            try:
                self.manager.set_enabled(plugin, True)
            except Exception:
                pass
            applier = getattr(plugin, "apply_config", None)
            if callable(applier):
                try:
                    applier(cfg.get(str(nm)) or cfg.get(plugin.name) or {})
                except Exception:
                    pass
            activated.append(plugin)
        return activated

    def build_and_start(self, map_data: dict) -> None:
        """Instantiate the map's plugin entities and start the play session."""
        if not self.active or self.manager is None:
            return
        # A plugin may ship disabled-by-default; a package built around it still
        # needs it running here. Enable any plugin this map's entities require
        # before we build them, so dispatch_play_start below doesn't skip it.
        try:
            self.manager.auto_enable_for_map(map_data)
        except Exception:
            pass
        # Enable entity-less global plugins the map declares (e.g. topdown).
        required = self._activate_required_plugins(map_data)
        things = []
        for t in map_data.get("things", []):
            if not isinstance(t, dict):
                continue
            typ = t.get("type") or t.get("properties", {}).get("type")
            if not typ:
                continue
            cls = self.manager.entity_class_for_type(typ)
            if cls is None:
                continue
            try:
                things.append(cls(pos=list(t.get("pos", [0, 0, 0])),
                                  properties=dict(t.get("properties", {}))))
            except Exception:
                continue

        if not things and not required:
            self.active = False   # nothing in this map for plugins to act on
            return

        self.bridge = _BridgeLogic(things)
        # Bind the host so plugins can reach the session and its event stream in
        # the player exactly as in the editor. Guarded: older managers without
        # bind_host simply skip it.
        try:
            binder = getattr(self.manager, "bind_host", None)
            if binder is not None:
                binder(self.bridge, kind="player")
        except Exception as exc:
            print(f"[Fio Player] plugin host bind failed: {exc}")
        try:
            self.manager.dispatch_play_start(self.bridge)
            emit = getattr(self.manager, "emit", None)
            if emit is not None:
                emit("play_start", logic=self.bridge)
        except Exception as exc:
            print(f"[Fio Player] plugin play-start failed: {exc}")

    def tick(self, dt: float, cam_pos, cam_yaw_deg: float, cam_pitch_deg: float,
             use_pressed: bool) -> None:
        if not self.active or self.bridge is None or self.manager is None:
            return
        self.bridge.player.update(cam_pos, cam_yaw_deg, cam_pitch_deg)
        self.bridge.current_hud_message = ""
        try:
            # Same cached, early-out dispatch the engine uses: builds a context
            # only when a plugin actually ticks.
            self.manager.tick(self.bridge, use_pressed=bool(use_pressed), delta=dt)
        except Exception:
            return
        self.hud_message = self.bridge.current_hud_message

    def camera_override(self, cam_pos, cam_yaw_deg: float, cam_pitch_deg: float):
        """Let a plugin replace the render camera (pos, yaw°, pitch°).

        The player draws from a free-look ``pos``/``yaw``/``pitch`` camera; a
        camera plugin (e.g. ``topdown``) answers the ``camera.player_view`` event
        to move it overhead. Fully guarded and early-outs when nothing listens,
        so a package with no camera plugin renders exactly as before. The input
        camera the caller passes is left untouched — only the returned copy is
        overridden — so movement still happens on the ground.
        """
        default = (cam_pos, cam_yaw_deg, cam_pitch_deg)
        if not self.active or self.manager is None or self.bridge is None:
            return default
        emit = getattr(self.manager, "emit", None)
        if emit is None:
            return default
        has = getattr(self.manager, "has_listeners", None)
        if has is not None and not has("camera.player_view"):
            return default
        try:
            self.bridge.player.update(cam_pos, cam_yaw_deg, cam_pitch_deg)
            ev = emit("camera.player_view", logic=self.bridge,
                      player=self.bridge.player, pos=tuple(cam_pos),
                      yaw=float(cam_yaw_deg), pitch=float(cam_pitch_deg))
            if ev is None:
                return default
            pos = ev.get("pos", cam_pos)
            yaw = ev.get("yaw", cam_yaw_deg)
            pitch = ev.get("pitch", cam_pitch_deg)
            return ((float(pos[0]), float(pos[1]), float(pos[2])),
                    float(yaw), float(pitch))
        except (TypeError, ValueError, IndexError, KeyError):
            return default
        except Exception:
            return default

    def stop(self) -> None:
        if self.active and self.bridge is not None and self.manager is not None:
            try:
                self.manager.dispatch_play_stop(self.bridge)
                emit = getattr(self.manager, "emit", None)
                if emit is not None:
                    emit("play_stop", logic=self.bridge)
            except Exception:
                pass
