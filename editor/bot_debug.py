"""Live, read-only bot decisions, sampled without blocking the UI or simulation."""
from collections import Counter, deque
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QHeaderView, QLabel, QPlainTextEdit,
                             QTableWidget, QTableWidgetItem, QTabWidget,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


def _format(value):
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return f'{value:.2f}'
    if isinstance(value, (tuple, list)):
        return ', '.join(_format(v) for v in value[:12]) + (' …' if len(value) > 12 else '')
    return str(value)[:1000]


def collect_bots(owner):
    """Copy consistent data under the engine lock order; None means try later."""
    from editor.things import Monster
    view = owner.view_3d
    logic = getattr(view, 'logic_thread', None)
    playing = bool(getattr(view, 'play_mode', False))
    acquired = []
    try:
        if playing and logic is not None:
            for lock in (logic._tick_lock, logic.session_runtime.monster_lock):
                if not lock.acquire(blocking=False):
                    return None
                acquired.append(lock)
        session = None
        if playing and logic is not None:
            from plugins.manager import get_manager
            plugin = get_manager().find_plugin('deathmatch')
            session = getattr(plugin, '_sessions', {}).get(logic)
        fighters = {id(f.entity): f for f in session.fighters.values()
                    if f.entity is not None} if session is not None else {}
        rows = []
        ai = getattr(logic, 'monster_ai', None) if playing else None
        names = {id(t): t.name for t in owner.state.things}
        for entity in owner.state.things:
            if not isinstance(entity, Monster):
                continue
            props = entity.properties
            fighter = fighters.get(id(entity))
            if fighter is not None and fighter.role != 'bot':
                continue  # Human opponents are not AI bots.
            state = getattr(ai, 'monster_states', {}).get(id(entity), {})
            row = dict(id=str(props['id']), name=entity.name, hp=props.get('health', 100),
                       controller='Deathmatch' if fighter else 'Monster AI',
                       position=tuple(entity.pos), heading=props.get('angle', 0),
                       appearance=props.get('monster_type', 'human'),
                       alive=not props.get('dead', False), target=None, route=(), weapon='—')
            details = [('Position', row['position']), ('Facing (degrees)', row['heading']),
                       ('Appearance', row['appearance']), ('Health', row['hp'])]
            if not playing:
                row['mode'] = 'Not running'
                details += [('Patrol enabled', props.get('patrol', False)),
                            ('Patrol target', props.get('patrol_target', ''))]
            elif fighter is not None:
                client = bool(session.network is not None and session.network.is_client)
                row['mode'] = 'Replicated' if client else fighter.mode if fighter.alive else 'Dead'
                target = session.fighters.get(fighter.target)
                row['target'] = target.name if target else None
                row['route'] = tuple(tuple(p) for p in fighter.route)
                supply = fighter.supplies
                row['weapon'] = supply.active if supply else '—'
                details += [('Authority', 'Host: decisions are not replicated' if client else 'Local host'),
                            ('Decision', row['mode']), ('Target', row['target']),
                            ('Target visible', fighter.visible), ('Last seen / heard', fighter.last_seen),
                            ('Memory remaining (s)', max(0, fighter.memory_until - session.clock)),
                            ('Next think (s)', max(0, fighter.think_at - session.clock)),
                            ('Next shot (s)', max(0, fighter.fire_at - session.clock)),
                            ('Team', fighter.team), ('Carrying flag', fighter.carrying),
                            ('Goal', row['route'][-1] if row['route'] else None),
                            ('Next waypoint', row['route'][0] if row['route'] else None),
                            ('Waypoints remaining', len(row['route'])),
                            ('Navigation', fighter.navigation_reason),
                            ('Mover phase', fighter.traversal.stage if fighter.traversal else None),
                            ('Mover', fighter.traversal.mover.get('name', 'platform') if fighter.traversal else None),
                            ('Stuck time (s)', fighter.stuck),
                            ('Escape remaining (s)', max(0, fighter.escape_until - session.clock)),
                            ('Weapon', row['weapon']), ('Ammo', supply.ammo if supply else None),
                            ('Armor', supply.armor if supply else None),
                            ('Pickup target', supply.target.name if supply and supply.target else None)]
                if client:
                    # The client has transforms/combat flags, not the host's
                    # reasoning. Never present default AI fields as live facts.
                    row.update(target=None, route=(), weapon='—')
                    details = details[:4] + [('Authority', 'Host'),
                        ('Decision', 'Not replicated — run botdebug on the host'),
                        ('Team', fighter.team), ('Carrying flag', fighter.carrying),
                        ('Shooting', fighter.alive and session.clock < fighter.shoot_until)]
            else:
                row['target'] = names.get(props.get('_aggro_target')) or ('Player' if state.get('in_sight') else None)
                if props.get('dead'):
                    mode = 'Dead'
                elif props.get('disabled'):
                    mode = 'Disabled'
                elif not props.get('awake', False):
                    mode = 'Asleep'
                elif props.get('is_shooting'):
                    mode = 'Attacking'
                elif state.get('in_sight'):
                    mode = 'Enemy in sight range'
                elif state.get('investigating_sound') is not None:
                    mode = 'Investigating sound'
                elif state.get('patrol_waiting'):
                    mode = 'Waiting at node'
                elif state.get('patrol_finished'):
                    mode = 'Patrol complete'
                elif props.get('patrol'):
                    mode = 'Patrolling'
                else:
                    mode = 'Searching'
                row['mode'] = mode
                chain = state.get('patrol_chain', ())
                index = state.get('patrol_chain_idx', 0)
                row['route'] = tuple(chain[index:]) if 0 <= index < len(chain) else ()
                goal = chain[index] if 0 <= index < len(chain) else props.get('patrol_target')
                sound = state.get('investigating_sound')
                details += [('Decision', mode), ('Target', row['target']),
                            ('Enemy in sight range', state.get('in_sight', False)),
                            ('Investigating sound', tuple(sound[0]) if sound is not None else None),
                            ('Patrol mode', props.get('patrol_mode', 'loop')),
                            ('Current node', goal), ('Patrol chain', tuple(chain)),
                            ('Detour node', state.get('detour_node')),
                            ('Wait remaining (s)', state.get('patrol_wait_remaining', 0)),
                            ('Blocked updates', state.get('patrol_blocked_count', 0)),
                            ('Attack timer (s)', state.get('shoot_timer', 0))]
            row['details'] = tuple((name, _format(value)) for name, value in details)
            rows.append(row)
        return dict(playing=playing, rows=tuple(rows), paused=bool(
            playing and logic is not None and logic.session_runtime.world_paused))
    finally:
        for lock in reversed(acquired):
            lock.release()


class BotDebugWindow(QDialog):
    REFRESH_MS = 500

    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.owner = owner
        self.setWindowTitle('Bot Debug')
        self.resize(820, 560)
        self.setMinimumSize(520, 340)
        self.setStyleSheet('QDialog { background:#141419; color:#dcdcdc; } '
                          'QTabBar::tab:selected { background:#425f5d; color:white; }')
        layout = QVBoxLayout(self)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        layout.addWidget(self.tabs)
        self.overview = QTableWidget(0, 7)
        self.overview.setHorizontalHeaderLabels(['Bot', 'Controller', 'Decision', 'Target', 'HP', 'Route', 'Weapon'])
        self.overview.setEditTriggers(QTableWidget.NoEditTriggers)
        self.overview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.overview.horizontalHeader().setStretchLastSection(True)
        self.tabs.addTab(self.overview, 'Overview')
        self._panels = {}
        self._started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self):
        snapshot = collect_bots(self.owner)
        if snapshot is None:
            self.status.setText('Simulation is updating; showing the last sample.')
            return
        rows = snapshot['rows']
        modes = Counter(r['mode'] for r in rows)
        state = 'Paused' if snapshot['paused'] else 'Live' if snapshot['playing'] else 'Editor — start Play to see decisions'
        self.status.setText(f'{state} · {len(rows)} bots · ' + ', '.join(f'{n} {m}' for m, n in sorted(modes.items())))
        seen = {row['id'] for row in rows}
        for key in set(self._panels) - seen:
            panel = self._panels.pop(key)
            self.tabs.removeTab(self.tabs.indexOf(panel['widget']))
            panel['widget'].deleteLater()
        self.overview.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row['name'], row['controller'], row['mode'], row['target'],
                      row['hp'], len(row['route']), row['weapon']]
            for column, value in enumerate(values):
                item = self.overview.item(index, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.overview.setItem(index, column, item)
                item.setText(_format(value))
            panel = self._panels.get(row['id'])
            if panel is None:
                widget = QWidget()
                layout = QVBoxLayout(widget)
                tree = QTreeWidget()
                tree.setHeaderLabels(['Decision / state', 'Value'])
                tree.setRootIsDecorated(False)
                tree.setColumnWidth(0, 220)
                history = QPlainTextEdit()
                history.setReadOnly(True)
                history.setMaximumHeight(130)
                layout.addWidget(tree, 1)
                layout.addWidget(QLabel('Recent decision changes'))
                layout.addWidget(history)
                panel = dict(widget=widget, tree=tree, history=history, cells={}, events=deque(maxlen=40), last=None)
                self._panels[row['id']] = panel
                self.tabs.addTab(widget, row['name'])
            self.tabs.setTabText(self.tabs.indexOf(panel['widget']), row['name'])
            fields = dict(row['details'])
            for name in set(panel['cells']) - set(fields):
                cell = panel['cells'].pop(name)
                panel['tree'].takeTopLevelItem(panel['tree'].indexOfTopLevelItem(cell))
            for name, value in row['details']:
                if name not in panel['cells']:
                    panel['cells'][name] = QTreeWidgetItem(panel['tree'], [name, value])
                else:
                    panel['cells'][name].setText(1, value)
            decision = (row['mode'], row['target'], fields.get('Goal', fields.get('Current node')),
                        fields.get('Pickup target'))
            if decision != panel['last']:
                panel['last'] = decision
                panel['events'].append(f'{time.monotonic() - self._started:6.1f}s  {row["mode"]} · target: {_format(row["target"])} · goal: {_format(decision[2])}')
                panel['history'].setPlainText('\n'.join(panel['events']))


def show_bot_debug(owner):
    window = getattr(owner, '_bot_debug_window', None)
    if window is None:
        window = owner._bot_debug_window = BotDebugWindow(owner)
    window.show()
    window.raise_()
    window.activateWindow()
    return window
