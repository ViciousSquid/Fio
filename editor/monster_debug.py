"""Monster Debug: a floating, live, read-only view of every monster's AI.

Opened with F7 in play, or ``monsterdebug [on|off]`` from the console. Tabs:

* **Perception** (shown first) -- what each monster is doing, its target,
  sight and hearing, and what it is investigating.
* **Combat** -- damage, attack timers, eye height and hit sphere.
* **Patrol** -- patrol route, current node, waiting and blockages.

Two switches drive the 3D view's overlays: **Sight lines** (each monster's
line of sight to its target, green when clear, red when blocked, orange to a
noise; the AI's decisions also go to the Debug Console) and **Hitboxes** (the
spheres player shots must touch, in pink). Data is sampled the way Bot Debug
samples it: the engine's locks are only *tried*, so the window never stalls
a tick -- a busy tick just keeps the previous sample on screen.
"""
from collections import Counter
import math
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
                             QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout)


def _format(value):
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return f'{value:.1f}'
    if isinstance(value, (tuple, list)):
        if value and all(isinstance(v, (int, float)) for v in value):
            return '(' + ', '.join(f'{float(v):.0f}' for v in value) + ')'
        return ', '.join(_format(v) for v in value[:12]) + (' …' if len(value) > 12 else '')
    return str(value)[:300]


def _decision(props, state):
    """What the monster is doing, in words."""
    if props.get('dead'):
        return 'Dead'
    if props.get('disabled'):
        return 'Disabled'
    if props.get('hidden'):
        return 'Hidden'
    if not props.get('awake', False):
        return 'Asleep'
    if props.get('is_shooting'):
        return 'Attacking'
    if state.get('in_sight'):
        return 'Chasing'
    if state.get('investigating_sound') is not None:
        return 'Investigating noise'
    if state.get('patrol_waiting'):
        return 'Waiting at node'
    if state.get('patrol_finished'):
        return 'Patrol complete'
    if props.get('patrol'):
        return 'Patrolling'
    return 'Searching'


def collect_monsters(owner):
    """A consistent copy of every monster's AI state; None means try later.

    The tick and monster locks are only tried (never waited for), in the
    engine's lock order, exactly as Bot Debug samples bots.
    """
    from editor.things import Monster
    from engine import monster_table
    from engine.logic_combat import LogicCombat
    from engine.monster_constants import (MONSTER_SIGHT_RANGE, monster_body_rise,
                                          monster_eye_rise)
    view = owner.view_3d
    logic = getattr(view, 'logic_thread', None)
    playing = bool(getattr(view, 'play_mode', False)) and logic is not None
    acquired = []
    try:
        if playing:
            for lock in (logic._tick_lock, logic.session_runtime.monster_lock):
                if not lock.acquire(blocking=False):
                    return None
                acquired.append(lock)
        ai = getattr(logic, 'monster_ai', None) if playing else None
        table = getattr(ai, 'table', None)
        player = getattr(getattr(logic, 'player_runtime', None), 'player', None) if playing else None
        player_pos = tuple(float(v) for v in player.pos) if player is not None else None
        names = {id(t): t.name for t in owner.state.things}
        now = time.perf_counter()
        rows = []
        for entity in owner.state.things:
            if not isinstance(entity, Monster):
                continue
            props = entity.properties
            state = getattr(ai, 'monster_states', {}).get(id(entity), {}) if ai else {}
            pos = tuple(float(v) for v in entity.pos)
            height = float(props.get('sprite_height', 128.0))
            row_index = getattr(table, 'row_of', {}).get(id(entity)) if table is not None else None
            if row_index is not None and row_index >= table.count:
                row_index = None
            ai_mode = (monster_table.MODE_NAMES[int(table.mode[row_index])]
                       if row_index is not None else None)
            aggro = names.get(props.get('_aggro_target'))
            target = aggro or ('Player' if state.get('in_sight') else None)
            if row_index is not None and float(table.dist_sq[row_index]) > 0.0:
                distance = math.sqrt(float(table.dist_sq[row_index]))
            elif player_pos is not None:
                distance = math.dist(pos, player_pos)
            else:
                distance = None
            sound = state.get('investigating_sound')
            chain = tuple(state.get('patrol_chain', ()) or ())
            index = state.get('patrol_chain_idx', 0)
            node = (chain[index] if 0 <= index < len(chain)
                    else state.get('patrol_walking_to') or props.get('patrol_target'))
            sphere_centre, sphere_radius = LogicCombat.hit_sphere(pos, props)
            decision = _decision(props, state) if playing else 'Not running'
            row = {
                'id': str(props.get('id', id(entity))),
                'name': entity.name,
                'type': props.get('monster_type', 'human'),
                'variant': props.get('variant') or '—',
                'decision': decision,
                'ai_mode': ai_mode,
                'target': target,
                'distance': distance,
                'health': props.get('health', 100),
                'team': props.get('team') or '(none)',
                'pos': pos,
                'feet_y': pos[1] - height / 2.0,
                # Perception
                'awake': bool(props.get('awake', False)),
                'wake_on_sight': bool(props.get('wake_on_sight', True)),
                'triggered': bool(props.get('triggered', False)),
                'sight_range': float(MONSTER_SIGHT_RANGE),
                'in_sight': bool(state.get('in_sight', False)),
                'can_hear': bool(props.get('can_hear', False)),
                'hearing_range': float(props.get('sight', MONSTER_SIGHT_RANGE)),
                'noise': tuple(float(v) for v in sound[0]) if sound is not None else None,
                'noise_left': max(0.0, float(sound[1]) - now) if sound is not None else None,
                'eye_y': pos[1] + monster_eye_rise(props),
                # Combat
                'damage': props.get('damage', 20),
                'attacking': bool(props.get('is_shooting', False)),
                'shoot_timer': float(state.get('shoot_timer', 0.0)) if playing else None,
                'anim_timer': float(state.get('anim_timer', 0.0)) if playing else None,
                'body_y': pos[1] + monster_body_rise(props),
                'hit_centre': tuple(float(v) for v in sphere_centre),
                'hit_radius': float(sphere_radius),
                'sprite': (float(props.get('sprite_width', 64.0)), height),
                # Patrol
                'patrol': bool(props.get('patrol', False)),
                'patrol_mode': props.get('patrol_mode', 'loop'),
                'node': node,
                'chain': chain,
                'waiting': bool(state.get('patrol_waiting', False)),
                'wait_left': float(state.get('patrol_wait_remaining', 0.0) or 0.0),
                'blocked': int(state.get('patrol_blocked_count', 0) or 0),
                'detour': state.get('detour_node'),
                'finished': bool(state.get('patrol_finished', False)),
            }
            rows.append(row)
        projectiles = 0
        if playing:
            projectiles = len(getattr(logic.combat_runtime, 'projectile_positions', ()))
        return {
            'playing': playing,
            'paused': bool(playing and logic.session_runtime.world_paused),
            'rows': tuple(rows),
            'projectiles': projectiles,
            'ai_path': getattr(table, 'path', None) if table is not None else None,
        }
    finally:
        for lock in reversed(acquired):
            lock.release()


#: (tab, [(column header, row key)]), in tab order: Perception first.
TABLES = {
    'Perception': [('Monster', 'name'), ('Doing', 'decision'), ('Target', 'target'),
                   ('Distance', 'distance'), ('Eye height', 'eye_y'),
                   ('Can hear', 'can_hear'), ('Investigating', 'noise'),
                   ('Time left (s)', 'noise_left')],
    'Combat': [('Monster', 'name'), ('Health', 'health'), ('Damage', 'damage'),
               ('Attacking', 'attacking'), ('Next shot (s)', 'shoot_timer'),
               ('Eye height', 'eye_y'), ('Hit sphere centre', 'hit_centre'),
               ('Hit sphere radius', 'hit_radius')],
    'Patrol': [('Monster', 'name'), ('Patrols', 'patrol'), ('Mode', 'patrol_mode'),
               ('Heading to', 'node'), ('Waiting', 'waiting'), ('Wait left (s)', 'wait_left'),
               ('Blocked updates', 'blocked'), ('Detour', 'detour'), ('Finished', 'finished'),
               ('Route', 'chain')],
}


class MonsterDebugWindow(QDialog):
    REFRESH_MS = 250

    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.owner = owner
        self.setWindowTitle('Monster Debug')
        self.resize(860, 520)
        self.setMinimumSize(560, 320)
        self.setStyleSheet('QDialog { background:#141419; color:#dcdcdc; } '
                           'QTabBar::tab:selected { background:#7a3a62; color:white; }')
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.status = QLabel('')
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.sight_lines = QCheckBox('Sight lines')
        self.sight_lines.setToolTip(
            "Each monster's line of sight to its target in the 3D view: green when\n"
            "clear, red when blocked, orange to a noise it investigates. The AI's\n"
            "decisions are also written to the Debug Console.")
        self.sight_lines.toggled.connect(self._set_sight_lines)
        bar.addWidget(self.sight_lines)
        self.hitboxes = QCheckBox('Hitboxes')
        self.hitboxes.setToolTip("Outline the spheres player shots must touch, in pink")
        self.hitboxes.toggled.connect(self._set_hitboxes)
        bar.addWidget(self.hitboxes)
        layout.addLayout(bar)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.tables = {}
        for name, columns in TABLES.items():
            table = QTableWidget(0, len(columns))
            table.setHorizontalHeaderLabels([c[0] for c in columns])
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectRows)
            table.setSelectionMode(QTableWidget.SingleSelection)
            table.verticalHeader().setVisible(False)
            header = table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeToContents)
            header.setStretchLastSection(True)
            self.tables[name] = table
            self.tabs.addTab(table, name)

        self._rows = ()
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # -- overlays ------------------------------------------------------------

    def _logic(self):
        view = self.owner.view_3d
        return view.logic_thread if getattr(view, 'play_mode', False) else None

    def _set_sight_lines(self, on):
        logic = self._logic()
        if logic is not None:
            logic.monster_ai.monster_debug_active = bool(on)
            self.owner.view_3d.update()

    def _set_hitboxes(self, on):
        logic = self._logic()
        if logic is not None:
            logic.combat_runtime.show_hitboxes = bool(on)
            self.owner.view_3d.update()

    def _sync_switches(self, playing):
        """Show the overlays' real state (play ending, F7, the console)."""
        logic = self._logic()
        for box, value in ((self.sight_lines, logic and logic.monster_ai.monster_debug_active),
                           (self.hitboxes, logic and logic.combat_runtime.show_hitboxes)):
            box.setEnabled(playing)
            if box.isChecked() != bool(value):
                box.blockSignals(True)
                box.setChecked(bool(value))
                box.blockSignals(False)

    # -- lifecycle -----------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        # Closing the window ends the sight lines it was showing, as F7 does.
        logic = self._logic()
        if logic is not None:
            logic.monster_ai.monster_debug_active = False
        super().hideEvent(event)

    def refresh(self):
        snapshot = collect_monsters(self.owner)
        if snapshot is None:
            self.status.setText('Simulation is updating; showing the last sample.')
            return
        rows = self._rows = snapshot['rows']
        self._sync_switches(snapshot['playing'])
        if snapshot['playing']:
            doing = Counter(r['decision'] for r in rows)
            state = 'Paused' if snapshot['paused'] else 'Live'
            self.status.setText(
                f"{state} · {len(rows)} monsters · "
                + ', '.join(f'{n} {d.lower()}' for d, n in sorted(doing.items()))
                + f" · {snapshot['projectiles']} projectiles in flight")
        else:
            self.status.setText(f'Editor · {len(rows)} monsters — start Play to see the AI live')
        for name, columns in TABLES.items():
            table = self.tables[name]
            table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, (_header, key) in enumerate(columns):
                    item = table.item(r, c)
                    if item is None:
                        item = QTableWidgetItem()
                        table.setItem(r, c, item)
                    item.setText(_format(row[key]))


def show_monster_debug(owner, sight_lines=True):
    """Open (or raise) the Monster Debug window; in play, with sight lines on."""
    window = getattr(owner, '_monster_debug_window', None)
    if window is None:
        window = owner._monster_debug_window = MonsterDebugWindow(owner)
    window.show()
    window.raise_()
    if sight_lines:
        window.sight_lines.setChecked(True)
        window._set_sight_lines(True)
    return window


def hide_monster_debug(owner):
    window = getattr(owner, '_monster_debug_window', None)
    if window is not None:
        window.hide()


def toggle_monster_debug(owner):
    """F7: open the window with sight lines, or close it and them."""
    window = getattr(owner, '_monster_debug_window', None)
    if window is not None and window.isVisible():
        window.hide()
        return False
    show_monster_debug(owner)
    return True
