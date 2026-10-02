"""
The Big World plugin: cell streaming for very large Fio maps.

Big World lets a Fio map hold hundreds of thousands of brushes/entities while
only the cells around the player take part in runtime rendering, collision,
entity processing and lighting. It is a **runtime scalability layer**, not a new
world format: it reuses Fio's existing 512-unit spatial grid, keeps every object
in place with its existing UUID, and cooperates with machinery Fio already has
(the render cull's ``hidden`` check, the spatial grid's local collision queries,
the AI/pickup ``disabled`` skip) rather than replacing any subsystem.

This module is the thin FioPlugin wiring:

* ``register``          — declare the :class:`BigWorldSettings` entity + its
                          property schema (its presence is the map's opt-in).
* ``on_play_start``     — if the map opts in, build a :class:`BigWorldSession`
                          and stream in the region around the player.
* ``on_tick``           — advance streaming when the player crosses a cell
                          boundary (cheap; early-outs otherwise).
* ``on_play_stop``      — tear the session down and restore the world exactly.
* ``connect``           — publish a ``bigworld`` service and draw the optional
                          debug overlay via the ``render.overlay`` event.

The plugin ships **disabled by default** and is auto-enabled by the manager only
for maps that contain a ``BigWorldSettings`` entity, so ordinary small maps pay
nothing and behave exactly as before (§19/§20).
"""

from __future__ import annotations

from plugins.api import FioPlugin, TickContext, prop

# NOTE: ``.entities`` is imported lazily inside register() rather than here,
# because it pulls in ``editor.things`` and therefore the editor package. The
# plugin manager reads ``PLUGIN`` off this package at import time; keeping the
# package import free of the editor avoids a re-entrant plugin-discovery pass
# (editor bootstrap → load_plugins) firing before ``PLUGIN`` is set. ``.runtime``
# and ``.persistence`` are editor-free, so they import safely at module load.
from .persistence import config_from_settings, find_settings_thing
from .runtime import BigWorldSession


class BigWorldPlugin(FioPlugin):
    name = "bigworld"
    version = "1.0.0"
    description = "Cell streaming: keep only the area around the player active in huge maps."
    category = "Big World"
    api_version = "1.2.0"   # needs the PluginHost / event bus (connect) surface
    # Disabled by default: only maps that place a BigWorldSettings entity use it.
    # The manager auto-enables the plugin when such a map loads (auto_enable_for_map),
    # so streaming maps just work while ordinary maps never pay for it.
    enabled = False

    # -- load time ----------------------------------------------------------
    def register(self, api):
        from .entities import BigWorldSettings  # lazy: pulls in editor.things
        api.register_entity(BigWorldSettings, menu_label="Big World Settings")
        api.register_properties("bigworldsettings", [
            prop("enabled", "bool", "Streaming enabled", default=True,
                 help="Turn cell streaming on for this map."),
            prop("activation_radius", "float", "Activation radius", default=2048.0,
                 min=256.0, max=65536.0,
                 help="Cells within this distance of the player become active."),
            prop("deactivation_radius", "float", "Deactivation radius", default=2304.0,
                 min=256.0, max=65536.0,
                 help="Active cells are only dropped beyond this distance "
                      "(hysteresis — must be >= activation radius)."),
            prop("show_cell_debug", "bool", "Show debug overlay", default=True,
                 help="Draw the Big World stats panel and active-cell minimap in play mode."),
            prop("terrain_fill", "bool", "Fill world with terrain", default=False,
                 help="If the map has a procedural terrain, expand it to cover every "
                      "cell of the world and stream its chunks around the player "
                      "(instead of tessellating the whole grid up-front). Off by "
                      "default, so terrain is left exactly as authored."),
            prop("terrain_infinite", "bool", "Infinite terrain (generate forever)",
                 default=False,
                 help="With 'Fill world with terrain' on, keep generating ground "
                      "around the camera/player forever instead of stopping at the "
                      "world's content bounds — so you never walk off the edge. "
                      "Only the chunks near you are ever resident."),
            prop("terrain_stream_radius", "float", "Terrain stream radius", default=0.0,
                 min=0.0, max=65536.0,
                 help="World units of terrain kept resident around the player. "
                      "0 derives it from the activation radius."),
            prop("disk_streaming", "bool", "Disk streaming (free unloaded cells)",
                 default=False,
                 help="Experimental: instead of only hiding inactive cells, remove "
                      "an unloaded cell's objects from memory and re-stream them "
                      "from a pristine per-cell source when the cell comes back. "
                      "Play-session saves become a delta of the persistent cell "
                      "registry. Off by default (keeps the in-RAM behaviour)."),
        ])

    # -- runtime host wiring ------------------------------------------------
    def connect(self, host):
        # Keep the host so the overlay handler can reach the live session, and
        # publish the session as a service other plugins/tools can query.
        self._host = host
        host.on("render.overlay", self._on_overlay)

    # -- play lifecycle -----------------------------------------------------
    def on_play_start(self, logic):
        logic._bigworld = None
        settings = find_settings_thing(getattr(logic, "things", None) or [])
        if settings is None:
            return  # map didn't opt in; behave as ordinary Fio
        cfg = config_from_settings(settings)
        if not cfg["enabled"]:
            return
        session = None
        if cfg.get("disk_streaming"):
            # Experimental disk-streaming path: genuinely free unloaded cells,
            # re-streaming them from a pristine in-memory partition of the map.
            # Fails safe to the in-RAM session if anything goes wrong.
            try:
                from .streaming import DiskStreamingSession, MemoryCellSource
                source = MemoryCellSource.from_logic(logic)
                session = DiskStreamingSession(
                    logic, source,
                    load_radius=cfg["activation_radius"],
                    evict_radius=cfg["deactivation_radius"],
                )
                session.start()
            except Exception:
                session = None
        if session is None:
            session = BigWorldSession(
                logic,
                activation_radius=cfg["activation_radius"],
                deactivation_radius=cfg["deactivation_radius"],
                terrain_fill=cfg["terrain_fill"],
                terrain_infinite=cfg["terrain_infinite"],
                terrain_stream_radius=cfg["terrain_stream_radius"],
            )
            session.start()
        session._show_debug = cfg["show_cell_debug"]
        logic._bigworld = session
        # Expose the live session as a service (renderer/other plugins/tools).
        try:
            host = getattr(self, "_host", None)
            if host is not None:
                host.provide("bigworld", session)
        except Exception:
            pass

    def on_play_stop(self, logic):
        session = getattr(logic, "_bigworld", None)
        if session is not None:
            session.stop()
            logic._bigworld = None
        try:
            host = getattr(self, "_host", None)
            if host is not None:
                host.provide("bigworld", None)
        except Exception:
            pass

    def on_tick(self, logic, ctx: TickContext):
        session = getattr(logic, "_bigworld", None)
        if session is not None:
            session.tick()

    # -- debug overlay ------------------------------------------------------
    def _on_overlay(self, ev):
        """Draw the Big World stats panel + active-cell minimap (editor viewport).

        Uses the live QPainter from the ``render.overlay`` event. Fully guarded:
        no session, not in play mode, or debug turned off ⇒ draws nothing.
        """
        if not ev.get("play_mode"):
            return
        host = getattr(self, "_host", None)
        logic = host.logic if host is not None else None
        session = getattr(logic, "_bigworld", None)
        if session is None or not getattr(session, "_show_debug", True):
            return
        painter = ev.get("painter")
        if painter is None:
            return
        try:
            self._paint_debug(painter, session, ev.get("width", 0), ev.get("height", 0))
        except Exception:
            pass  # never let a debug draw take down the frame

    def _paint_debug(self, painter, session, width, height):
        from PyQt5.QtCore import Qt, QRect
        from PyQt5.QtGui import QColor, QFont

        s = session.stats()
        pc = s.get("player_cell")
        pc_txt = f"{pc[0]}, {pc[1]}" if pc else "-"
        lines = [
            ("Player Cell", pc_txt),
            ("Active Cells", f"{s['active_cells']}"),
            ("Loaded Cells", f"{s['loaded_cells']}"),
            ("Activation Radius", f"{s['activation_radius']:.0f}"),
            ("Active Brushes", f"{s['active_brushes']:,}"),
            ("Total Brushes", f"{s['total_brushes']:,}"),
            ("Active Entities", f"{s['active_entities']:,}"),
            ("Total Entities", f"{s['total_entities']:,}"),
            ("Active Lights", f"{s['active_lights']:,} / {s['total_lights']:,}"),
        ]
        if s.get("terrain_fill"):
            if s.get("terrain_infinite"):
                stream_txt = "∞"  # ∞: streaming forever, no world edge
            else:
                stream_txt = "on" if s.get("terrain_streaming") else "off"
            lines.append(("Terrain Chunks",
                          f"{s.get('terrain_chunks', 0):,} (stream {stream_txt})"))

        pad = 10
        row_h = 16
        panel_w = 230
        panel_h = pad * 2 + row_h * (len(lines) + 1)
        x0 = 12
        y0 = 90  # sit below Fio's own top-left debug text

        painter.save()
        painter.setRenderHint(painter.Antialiasing, False)
        painter.fillRect(QRect(x0, y0, panel_w, panel_h), QColor(10, 12, 20, 200))
        painter.setPen(QColor(120, 90, 200))
        painter.drawRect(QRect(x0, y0, panel_w, panel_h))

        font = QFont("Consolas", 9)
        painter.setFont(font)
        painter.setPen(QColor(180, 150, 240))
        painter.drawText(x0 + pad, y0 + pad + row_h - 4, "BIG WORLD")

        painter.setFont(QFont("Consolas", 8))
        y = y0 + pad + row_h * 2 - 4
        for label, value in lines:
            painter.setPen(QColor(150, 150, 170))
            painter.drawText(x0 + pad, y, label)
            painter.setPen(QColor(230, 230, 245))
            painter.drawText(x0 + pad + 130, y, value)
            y += row_h

        self._paint_minimap(painter, session, x0, y0 + panel_h + 8)
        painter.restore()

    def _paint_minimap(self, painter, session, x0, y0):
        """A tiny top-down map of loaded cells, active ones highlighted."""
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QColor

        mgr = session.manager
        if not mgr.cells:
            return
        pc = mgr._last_player_cell or (0, 0)
        span = 8  # cells each way around the player
        px_per_cell = 12
        size = (span * 2 + 1) * px_per_cell
        painter.fillRect(QRect(x0, y0, size, size), QColor(10, 12, 20, 200))
        painter.setPen(QColor(120, 90, 200))
        painter.drawRect(QRect(x0, y0, size, size))
        for cx in range(pc[0] - span, pc[0] + span + 1):
            for cz in range(pc[1] - span, pc[1] + span + 1):
                coord = (cx, cz)
                cell = mgr.cells.get(coord)
                if cell is None:
                    continue
                sx = x0 + (cx - (pc[0] - span)) * px_per_cell
                sz = y0 + (cz - (pc[1] - span)) * px_per_cell
                if coord in mgr.active_cells:
                    col = QColor(110, 200, 130, 230)
                else:
                    col = QColor(60, 60, 80, 200)
                painter.fillRect(QRect(sx + 1, sz + 1, px_per_cell - 2, px_per_cell - 2), col)
        # Player cell marker.
        sx = x0 + span * px_per_cell
        sz = y0 + span * px_per_cell
        painter.setPen(QColor(255, 220, 120))
        painter.drawRect(QRect(sx + 1, sz + 1, px_per_cell - 2, px_per_cell - 2))
