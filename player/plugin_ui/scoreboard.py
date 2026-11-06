"""The arena scoreboard on the touch HUD.

A port of the Deathmatch plugin's desktop ``render.overlay`` painter, which
draws with Qt. It reads the same immutable session snapshot and shows the
same information: mode, clock and limit, team scores and flag states, the
leaders (all rows while Scores is held or the match is over), the kill feed,
the respawn countdown, the spectator line and the result. Only the hints
name touch controls instead of desktop keys.
"""
from __future__ import annotations

from PIL import ImageDraw, ImageFont

PANEL = (12, 18, 26, 220)
ACCENT = (114, 225, 205, 255)
TEXT = (230, 235, 240, 255)
DIM = (140, 145, 155, 255)
RED = (255, 110, 120, 255)
BLUE = (110, 170, 255, 255)


def clock(remaining):
    return 'Unlimited' if remaining is None else f'{remaining // 60}:{remaining % 60:02d}'


def prompt(text):
    """Translate the plugin's desktop key hint in its HUD prompt."""
    return str(text or '').replace('TAB: scoreboard', 'Hold SCORES')


def paint(canvas, state, *, full=False):
    """Draw *state* (the plugin snapshot) over the HUD canvas."""
    if not state or not state.get('show'):
        return
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    scale = max(1.0, height / 600)
    font = ImageFont.load_default(size=round(15 * scale))
    small = ImageFont.load_default(size=round(13 * scale))
    line = round(25 * scale)
    full = bool(full or state.get('finished'))
    rows = list(state.get('rows', ()))
    rows = rows if full else rows[:3]
    mode = str(state.get('mode', 'deathmatch'))
    team_mode = mode in ('ctf', 'teamdeathmatch')
    extra = round(48 * scale) if team_mode else 0
    panel_width = min(round(440 * scale), width - 24)
    x = (width - panel_width) // 2 if full else 12
    y = round(30 * scale)
    draw.rectangle((x, y, x + panel_width, y + round(80 * scale) + extra + len(rows) * line), fill=PANEL)
    limit = state.get('capture_limit') if mode == 'ctf' else state.get('limit')
    unit = 'captures' if mode == 'ctf' else 'frags'
    draw.text((x + 14, y + round(23 * scale)),
              f"{mode.upper()}   {clock(state.get('remaining'))}   / {limit or 'unlimited'} {unit}",
              font=font, fill=ACCENT, anchor='ls')
    if team_mode:
        scores = dict(state.get('team_scores', ()))
        draw.text((x + 14, y + round(46 * scale)),
                  f"RED  {scores.get('red', 0)}        BLUE  {scores.get('blue', 0)}",
                  font=font, fill=ACCENT, anchor='ls')
        draw.text((x + 14, y + round(67 * scale)),
                  '   '.join(f'{team}: {status}' for team, status in state.get('flags', ())),
                  font=small, fill=ACCENT, anchor='ls')
    header_y = y + round(48 * scale) + extra
    draw.text((x + 14, header_y), 'PLAYER', font=font, fill=TEXT, anchor='ls')
    draw.text((x + panel_width - round(122 * scale), header_y), 'FRAGS', font=font, fill=TEXT, anchor='ls')
    draw.text((x + panel_width - 14, header_y), 'DEATHS', font=font, fill=TEXT, anchor='rs')
    teams = {name: (team, flag) for name, team, _captures, flag in state.get('teams', ())}
    spectators = set(state.get('spectators', ()))
    for index, (name, frags, deaths, alive) in enumerate(rows):
        row_y = y + round(72 * scale) + extra + index * line
        team, flag = teams.get(name, ('', None))
        color = RED if team == 'red' else BLUE if team == 'blue' else TEXT
        label = (f'[{team[0].upper()}] ' if team else '') + str(name)[:19]
        if flag:
            label += ' (flag)'
        if name in spectators:
            label = '[SPEC] ' + str(name)[:19]
        draw.text((x + 14, row_y), label, font=font, fill=color if alive else DIM, anchor='ls')
        draw.text((x + panel_width - round(122 * scale), row_y), str(frags), font=font,
                  fill=color if alive else DIM, anchor='ls')
        draw.text((x + panel_width - 14, row_y), str(deaths), font=font,
                  fill=color if alive else DIM, anchor='rs')
    for index, text in enumerate(state.get('feed', ())):
        draw.text((14, height - round(80 * scale) - index * round(22 * scale)), str(text),
                  font=small, fill=TEXT, anchor='ls', stroke_width=1, stroke_fill='black')
    end_y = y + round(90 * scale) + extra + len(rows) * line
    if state.get('spectating'):
        draw.text((14, height - round(28 * scale)),
                  'SPECTATING - ' + (state.get('watching') or 'Free camera') + ' - Pause: Spectator menu',
                  font=font, fill=TEXT, anchor='ls', stroke_width=1, stroke_fill='black')
    elif state.get('respawn') and not state.get('finished'):
        draw.text((width / 2, height / 2), f"Respawning in {state['respawn']}…", font=font,
                  fill=TEXT, anchor='mm', stroke_width=1, stroke_fill='black')
    if state.get('finished'):
        hint = 'Host controls restart' if state.get('network_client') else 'Tap RESTART for a new match'
        draw.multiline_text((width / 2, end_y + round(20 * scale)),
                            f"{state.get('winner')}  ·  {state.get('reason')}\n{hint}",
                            font=font, fill=TEXT, anchor='ma', align='center',
                            stroke_width=1, stroke_fill='black')


def key(state, full):
    """A hashable summary, so the HUD texture is rebuilt only on change."""
    if not state or not state.get('show'):
        return None
    return (full, tuple(sorted((k, v) for k, v in state.items() if k != 'watchable')))
