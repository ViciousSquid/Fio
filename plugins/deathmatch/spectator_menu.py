"""Live spectator controls; actions use the session's normal engine locks."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout


class SpectatorMenu(QDialog):
    def __init__(self, owner, plugin, logic):
        super().__init__(owner)
        self.owner, self.plugin, self.logic = owner, plugin, logic
        self.setWindowTitle('Spectator')
        self.resize(440, 400)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(QLabel('Players and bots'))
        self.players = QListWidget()
        layout.addWidget(self.players)
        self.players.itemDoubleClicked.connect(lambda item: self.follow_selected())
        controls = QHBoxLayout()
        self.free = QPushButton('Free camera')
        self.follow = QPushButton('Follow selected')
        self.join = QPushButton('Join match')
        for button in (self.free, self.follow, self.join):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.free.clicked.connect(lambda: self.act(lambda c: c.choose(True)))
        self.follow.clicked.connect(self.follow_selected)
        self.join.clicked.connect(lambda: self.act(lambda c: c.choose(False)))
        cycle = QHBoxLayout()
        for label, direction in (('Previous player', -1), ('Next player', 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, step=direction: self.act(lambda c: c.cycle(step)))
            cycle.addWidget(button)
        layout.addLayout(cycle)
        hint = QLabel('Free camera: WASD • Space up • C down • Shift faster\nSpectators always wear their chosen Settings → Appearance glasses.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        session = self.plugin._sessions.get(self.logic)
        if session is None or not session._started:
            self.status.setText('Start a multiplayer match in Play mode.')
            self.players.clear()
            for button in (self.free, self.follow, self.join):
                button.setEnabled(False)
            return
        for button in (self.free, self.follow, self.join):
            button.setEnabled(True)
        state = session.snapshot
        self.status.setText(('Watching '+state['watching'] if state.get('watching') else 'Spectating: free camera')
                            if state.get('spectating') else 'Playing in the match')
        selected = self.players.currentItem()
        selected = selected.data(Qt.UserRole) if selected else None
        self.players.clear()
        for key, name, team, alive, spectator in state.get('watchable', ()):
            if key == 'player' or spectator:
                continue
            label = name + (f' [{team}]' if team else '') + ('' if alive else ' — respawning')
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            if not alive:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.players.addItem(item)
            if key == selected:
                self.players.setCurrentItem(item)
        if self.players.currentRow() < 0:
            for index in range(self.players.count()):
                if self.players.item(index).flags() & Qt.ItemIsEnabled:
                    self.players.setCurrentRow(index)
                    break

    def follow_selected(self):
        item = self.players.currentItem()
        if item is not None and item.flags() & Qt.ItemIsEnabled:
            self.act(lambda c: c.choose(True, item.data(Qt.UserRole)))

    def act(self, callback):
        session = self.plugin._sessions.get(self.logic)
        if session is None:
            self.refresh()
            return
        with self.logic._tick_lock, self.logic.session_runtime.monster_lock:
            if not session._started:
                return
            reply = callback(session.spectators)
        self.owner.show_toast(reply)
        self.close()
        self.owner.view_3d.setFocus()
