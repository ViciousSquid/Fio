"""Registration and host lifecycle; no changes to Fio core are required."""
from pathlib import Path
import shlex
import weakref

from plugins.api import FioPlugin, io_def, prop
from .entities import DeathmatchSettings, DeathmatchBot, DeathmatchSpawn, DeathmatchNode
from .runtime import DeathmatchSession


class DeathmatchPlugin(FioPlugin):
    name = "deathmatch"
    version = "1.0.0"
    api_version = "1.7.0"
    description = "Free-for-all arena combat with tactical bot opponents."
    category = "Deathmatch"
    enabled = False

    def __init__(self):
        self._sessions = weakref.WeakKeyDictionary()
        self._hosts = weakref.WeakKeyDictionary()

    def register(self, api):
        for cls, label in ((DeathmatchSettings, "Match Settings"),
                           (DeathmatchBot, "Bot Opponent"),
                           (DeathmatchSpawn, "Respawn Point"),
                           (DeathmatchNode, "Navigation Node")):
            api.register_entity(cls, menu_label=label)
        api.register_singleton_entity("deathmatchsettings")
        api.register_properties("deathmatchsettings", [
            prop("frag_limit", type="int", default=20, min=0, max=10000,
                 help="Frags to win. Zero disables this limit."),
            prop("time_limit", type="float", default=300.0, min=0, max=86400,
                 help="Match duration in seconds. Zero disables the timer."),
            prop("respawn_delay", type="float", default=3.0, min=0.1, max=30),
            prop("spawn_protection", type="float", default=1.5, min=0, max=10,
                 help="Invulnerability after spawning; firing ends it."),
            prop("seed", type="int", default=42, min=-2147483648, max=2147483647),
            prop("show_hud", type="bool", default=True),
        ])
        api.register_properties("deathmatchbot", [
            prop("skill", type="enum", default="normal", choices=["easy", "normal", "hard"]),
            prop("max_health", type="int", default=100, min=1, max=10000),
            prop("vision_range", type="float", default=1800.0, min=64, max=10000),
            prop("hearing_range", type="float", default=1000.0, min=0, max=10000),
            prop("field_of_view", type="float", default=160.0, min=30, max=360),
            prop("preferred_range", type="float", default=480.0, min=100, max=2000),
        ])
        api.register_properties("deathmatchspawn", [
            prop("angle", type="float", default=0.0, min=-360, max=360,
                 help="Heading in degrees: 0 faces +Z, 90 faces +X."),
        ])
        api.register_properties("deathmatchnode", [
            prop("links", type="string", default="",
                 help="Comma-separated node names; connections work in both directions."),
        ])
        api.register_io("deathmatchsettings", inputs=[
            io_def("Restart", "Start a fresh match"),
            io_def("EndMatch", "Finish the current match"),
        ], outputs=[
            io_def("OnMatchStart", "A match has started"),
            io_def("OnFrag", "Kill feed text", "string"),
            io_def("OnMatchEnd", "Winner name or Draw", "string"),
        ])
        api.register_io("deathmatchbot", inputs=[], outputs=[
            io_def("OnDamaged", "Native weapon damage"),
            io_def("OnDeath", "Native weapon death"),
        ])
        api.register_menu_action("Load Demo map", self._load_demo,
                                 "Load the bundled Deathmatch arena")
        api.register_pause_menu_item("Restart Deathmatch", self._restart_menu)
        api.register_console_command("addbot", self._addbot,
                                     'addbot ["name"] [easy|normal|hard] — add a match bot')
        api.register_console_command("removebot", self._removebot,
                                     'removebot ["name"|id] — remove a bot; default: last bot')
        api.register_console_command("fraglimit", self._fraglimit,
                                     "fraglimit [number] — query/set frag limit; 0 is unlimited")

    def _command(self, logic, play_mode, action):
        session = self._sessions.get(logic) if logic is not None else None
        if not play_mode or session is None:
            return "Start a Deathmatch arena in Play mode first."
        # Console callbacks run on the UI thread. Use the engine's lock order
        # so scene mutations cannot race a tick or stock Monster AI.
        with logic._tick_lock, logic.session_runtime.monster_lock:
            if not session._started:
                return "Start a Deathmatch arena in Play mode first."
            return action(session)

    def _addbot(self, args, main_window, logic, play_mode):
        usage = 'Usage: addbot ["name"] [easy|normal|hard]'
        try:
            words = shlex.split(args)
        except ValueError:
            return usage
        if len(words) > 2:
            return usage
        name, skill = None, "normal"
        if len(words) == 1:
            if words[0].lower() in ("easy", "normal", "hard"):
                skill = words[0].lower()
            else:
                name = words[0]
        elif len(words) == 2:
            name, skill = words[0], words[1].lower()
            if skill not in ("easy", "normal", "hard"):
                return usage
        return self._command(logic, play_mode, lambda s: s.add_bot(name, skill))

    def _removebot(self, args, main_window, logic, play_mode):
        try:
            words = shlex.split(args)
        except ValueError:
            return 'Usage: removebot ["name"|id]'
        if len(words) > 1:
            return 'Usage: removebot ["name"|id]'
        return self._command(logic, play_mode, lambda s: s.remove_bot(words[0] if words else None))

    def _fraglimit(self, args, main_window, logic, play_mode):
        words = args.split()
        if not words:
            return self._command(logic, play_mode, lambda s: f"Frag limit: {s.limit or 'unlimited'}.")
        if len(words) != 1 or not words[0].isascii() or not words[0].isdigit():
            return "Usage: fraglimit <non-negative integer>; 0 is unlimited."
        try:
            value = int(words[0])
        except ValueError:
            return "Usage: fraglimit <non-negative integer>; 0 is unlimited."
        if value > 10000:
            return "Frag limit must be between 0 and 10000."
        return self._command(logic, play_mode, lambda s: s.set_frag_limit(value))

    def _load_demo(self, main_window):
        if main_window.check_unsaved_changes():
            main_window.load_level_file(str(Path(__file__).with_name("Deathmatch_Arena.json")))

    def _restart_menu(self, main_window, logic):
        session = self._sessions.get(logic)
        if session is not None:
            session.restart_requested = True  # Applied on the logic thread.

    def register_runtime(self, api):
        def restart(entity, parameter, logic):
            session = self._sessions.get(logic)
            if session is not None:
                session.restart_requested = True

        def end(entity, parameter, logic):
            session = self._sessions.get(logic)
            if session is not None:
                session.finish("Map I/O")

        api.register_input_handler("deathmatchsettings", "restart", restart)
        api.register_input_handler("deathmatchsettings", "endmatch", end)

    def connect(self, host):
        self._hosts[host.logic] = weakref.ref(host)
        host.on("render.overlay", self._draw_overlay)

    def _host(self, logic):
        reference = self._hosts.get(logic)
        return reference() if reference is not None else None

    def on_play_start(self, logic):
        self.on_play_stop(logic)
        if not DeathmatchSession.supported(logic):
            host = self._host(logic)
            if host is not None:
                host.log("Deathmatch requires desktop editor Play mode; this player host lacks world combat services.")
            return
        session = DeathmatchSession(logic, self._host(logic))
        self._sessions[logic] = session
        try:
            with logic._tick_lock, logic.session_runtime.monster_lock:
                if not session.start():
                    self._sessions.pop(logic, None)
        except Exception:
            session.stop()
            self._sessions.pop(logic, None)
            raise

    def on_play_stop(self, logic):
        session = self._sessions.pop(logic, None)
        if session is not None:
            with logic._tick_lock, logic.session_runtime.monster_lock:
                session.stop()

    def on_enabled_changed(self, enabled):
        if not enabled:
            for logic in list(self._sessions):
                self.on_play_stop(logic)
        else:
            for logic in list(self._hosts):
                if getattr(getattr(logic, "session_runtime", None), "play_mode", False):
                    self.on_play_start(logic)

    def on_tick(self, logic, ctx):
        session = self._sessions.get(logic)
        if session is None or not session.snapshot.get("show"):
            return
        state = session.snapshot
        remaining = state["remaining"]
        timer = "No time limit" if remaining is None else f"{remaining // 60}:{remaining % 60:02d}"
        ctx.set_prompt(f"Deathmatch | {timer} | TAB: scoreboard", priority=-10)

    def _draw_overlay(self, event):
        if not event.get("play_mode", False):
            return
        viewport = event.get("viewport")
        logic = getattr(viewport, "logic_thread", None)
        session = self._sessions.get(logic) if logic is not None else None
        if session is None:
            return
        state = session.snapshot
        if not state.get("show"):
            return
        # Qt is only imported in the editor's render callback.
        from PyQt5.QtCore import QRect, Qt
        from PyQt5.QtGui import QColor, QFont
        painter = event.get("painter")
        width = event.get("width", 800)
        height = event.get("height", 600)
        full = state["finished"] or 0x01000001 in logic.game_state.get_keys()
        rows = state["rows"] if full else state["rows"][:3]
        panel_width = min(440, width - 24)
        x, y = (width - panel_width) // 2 if full else 12, 30
        painter.save()
        try:
            painter.setFont(QFont("Sans Serif", 11))
            painter.fillRect(x, y, panel_width, 80 + len(rows) * 25, QColor(12, 18, 26, 220))
            painter.setPen(QColor(114, 225, 205))
            remaining = state["remaining"]
            timer = "Unlimited" if remaining is None else f"{remaining // 60}:{remaining % 60:02d}"
            painter.drawText(x + 14, y + 23, f"DEATHMATCH   {timer}   / {state['limit'] or '∞'} frags")
            painter.setPen(QColor(230, 235, 240))
            painter.drawText(x + 14, y + 48, "PLAYER                         FRAGS    DEATHS")
            for index, (name, frags, deaths, alive) in enumerate(rows):
                row_y = y + 72 + index * 25
                painter.setPen(QColor(230, 235, 240) if alive else QColor(140, 145, 155))
                painter.drawText(x + 14, row_y, name[:24])
                painter.drawText(x + panel_width - 122, row_y, str(frags))
                painter.drawText(x + panel_width - 44, row_y, str(deaths))
            for index, text in enumerate(state["feed"]):
                painter.drawText(14, height - 80 - index * 22, text)
            if state["respawn"] and not state["finished"]:
                painter.drawText(QRect(0, height // 2, width, 50), Qt.AlignCenter,
                                 f"Respawning in {state['respawn']}…")
            if state["finished"]:
                painter.drawText(QRect(0, y + 90 + len(rows) * 25, width, 60), Qt.AlignCenter,
                                 f"{state['winner']}  •  {state['reason']}\nPress R for a new match")
        finally:
            painter.restore()
