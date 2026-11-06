"""Bigworld's cell debug panel and minimap on the player HUD.

A port of the plugin's desktop ``render.overlay`` painter (Qt). It appears
when the playing map enables Bigworld's ``show_cell_debug`` setting and shows
the same statistics from the live session's ``stats()``.
"""
from __future__ import annotations

from PIL import ImageDraw, ImageFont

PANEL = (10, 12, 20, 200)
BORDER = (120, 90, 200, 255)
TITLE = (180, 150, 240, 255)
LABEL = (150, 150, 170, 255)
VALUE = (230, 230, 245, 255)
ACTIVE = (110, 200, 130, 230)
LOADED = (60, 60, 80, 200)
PLAYER = (255, 220, 120, 255)
SPAN = 8


def session(world):
    """The running Bigworld session showing its debug panel, or None."""
    plugin_manager = getattr(getattr(world, 'logic', None), 'plugins', None) if world else None
    plugin = plugin_manager.find_plugin('bigworld') if plugin_manager else None
    if plugin is None or not plugin_manager.is_enabled(plugin):
        return None
    running = getattr(plugin, '_sessions', {}).get(world.logic)
    return running if running is not None and getattr(running, '_show_debug', False) else None


def rows(stats):
    cell = stats.get('player_cell')
    horizon = stats.get('visual_horizon')
    lines = [
        ('Player Cell', f'{cell[0]}, {cell[1]}' if cell else '-'),
        ('Active Cells', f"{stats['active_cells']}"),
        ('Loaded Cells', f"{stats['loaded_cells']}"),
        ('Configured Radius', f"{stats.get('configured_activation_radius', stats['activation_radius']):.0f}"),
        ('Visual Horizon', f'{horizon:.0f}' if horizon is not None else '-'),
        ('Effective Radius', f"{stats['activation_radius']:.0f}"),
        ('Active Brushes', f"{stats['active_brushes']:,}"),
        ('Total Brushes', f"{stats['total_brushes']:,}"),
        ('Active Entities', f"{stats['active_entities']:,}"),
        ('Total Entities', f"{stats['total_entities']:,}"),
        ('Active Lights', f"{stats['active_lights']:,} / {stats['total_lights']:,}"),
    ]
    if stats.get('terrain_fill'):
        stream = 'infinite' if stats.get('terrain_infinite') else ('on' if stats.get('terrain_streaming') else 'off')
        lines.append(('Terrain Chunks', f"{stats.get('terrain_chunks', 0):,} (stream {stream})"))
    return lines


def minimap(running):
    """``(cells, player cell)``: ``[(dx, dz, active)]`` around the player."""
    manager = running.manager
    if not manager.cells:
        return (), None
    centre = manager._last_player_cell or (0, 0)
    cells = []
    for cx in range(centre[0] - SPAN, centre[0] + SPAN + 1):
        for cz in range(centre[1] - SPAN, centre[1] + SPAN + 1):
            if (cx, cz) in manager.cells:
                cells.append((cx - centre[0], cz - centre[1], (cx, cz) in manager.active_cells))
    return tuple(cells), centre


def state(world):
    """Everything the panel draws, hashable, or None when it is hidden."""
    running = session(world)
    if running is None:
        return None
    try:
        return tuple(rows(running.stats())), minimap(running)[0]
    except Exception:
        return None


def paint(canvas, panel):
    if not panel:
        return
    lines, cells = panel
    width, height = canvas.size
    scale = max(1.0, height / 600)
    draw = ImageDraw.Draw(canvas)
    title = ImageFont.load_default(size=round(12 * scale))
    font = ImageFont.load_default(size=round(11 * scale))
    pad, row = round(10 * scale), round(16 * scale)
    panel_width = round(230 * scale)
    panel_height = pad * 2 + row * (len(lines) + 1)
    x0 = 12
    y0 = round(90 * scale)
    draw.rectangle((x0, y0, x0 + panel_width, y0 + panel_height), fill=PANEL, outline=BORDER)
    draw.text((x0 + pad, y0 + pad + row - 4), 'BIG WORLD', font=title, fill=TITLE, anchor='ls')
    y = y0 + pad + row * 2 - 4
    for label, value in lines:
        draw.text((x0 + pad, y), label, font=font, fill=LABEL, anchor='ls')
        draw.text((x0 + pad + round(130 * scale), y), value, font=font, fill=VALUE, anchor='ls')
        y += row
    if not cells:
        return
    cell_px = round(12 * scale)
    top = y0 + panel_height + 8
    size = (SPAN * 2 + 1) * cell_px
    draw.rectangle((x0, top, x0 + size, top + size), fill=PANEL, outline=BORDER)
    for dx, dz, active in cells:
        sx = x0 + (dx + SPAN) * cell_px
        sz = top + (dz + SPAN) * cell_px
        draw.rectangle((sx + 1, sz + 1, sx + cell_px - 2, sz + cell_px - 2),
                       fill=ACTIVE if active else LOADED)
    sx = sz = SPAN * cell_px
    draw.rectangle((x0 + sx + 1, top + sz + 1, x0 + sx + cell_px - 2, top + sz + cell_px - 2),
                   outline=PLAYER)
