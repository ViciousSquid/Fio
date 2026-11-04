# SPDX-License-Identifier: GPL-2.0-or-later
"""Nonblocking Qt lobby for LAN discovery, hosting and joining."""
import copy
import json
from pathlib import Path
import queue

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout)

from .lan import Discovery, LanClient, LanHost, GAME_PORT, PROTOCOL
from .network_game import HostGame, ClientGame


class LanLobby(QDialog):
    def __init__(self, main_window, plugin):
        super().__init__(main_window)
        self.window, self.plugin = main_window, plugin
        self.scan, self.pending = None, None
        self.offers = {}
        self.setWindowTitle("Fio — LAN Multiplayer")
        self.resize(720, 570)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Find a local arena, join by address, or host a new game."))
        self.nickname = QLineEdit("Player")
        self.nickname.setMaxLength(32)
        self.team = QComboBox(); self.team.addItems(["auto", "red", "blue"])
        identity = QHBoxLayout()
        identity.addWidget(QLabel("Player name")); identity.addWidget(self.nickname)
        identity.addWidget(QLabel("Team")); identity.addWidget(self.team)
        layout.addLayout(identity)
        self.games = QTableWidget(0, 5)
        self.games.setHorizontalHeaderLabels(["Game", "Type", "Players", "Address", "Port"])
        self.games.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.games.setSelectionMode(QAbstractItemView.SingleSelection)
        self.games.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.games.horizontalHeader().setStretchLastSection(True)
        self.games.doubleClicked.connect(lambda _: self.join_selected())
        layout.addWidget(self.games)
        buttons = QHBoxLayout()
        self.refresh = QPushButton("Refresh LAN games")
        self.refresh.clicked.connect(self.refresh_games)
        self.join = QPushButton("Join selected game")
        self.join.clicked.connect(self.join_selected)
        buttons.addWidget(self.refresh); buttons.addStretch(); buttons.addWidget(self.join)
        layout.addLayout(buttons)
        direct = QHBoxLayout()
        self.address = QLineEdit(); self.address.setPlaceholderText("LAN host address")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(GAME_PORT)
        self.direct_join = QPushButton("Join address")
        self.direct_join.clicked.connect(lambda: self.connect_to(self.address.text().strip(), self.port.value()))
        direct.addWidget(self.address); direct.addWidget(self.port); direct.addWidget(self.direct_join)
        layout.addLayout(direct)
        group = QGroupBox("Host a new game")
        form = QFormLayout(group)
        self.game_name = QLineEdit("Fio LAN Arena"); self.game_name.setMaxLength(48)
        self.mode = QComboBox()
        for label, value in (("Deathmatch", "deathmatch"), ("Capture the Flag", "ctf"), ("Team Deathmatch", "teamdeathmatch")):
            self.mode.addItem(label, value)
        self.map_choice = QComboBox(); self.map_choice.addItems(["Bundled arena", "Current map"])
        self.limit = QSpinBox(); self.limit.setRange(0, 10000); self.limit.setValue(20)
        self.capture_limit = QSpinBox(); self.capture_limit.setRange(0, 1000); self.capture_limit.setValue(3)
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.update_mode()
        self.host_button = QPushButton("Host game")
        self.host_button.clicked.connect(self.host_game)
        form.addRow("Game name", self.game_name); form.addRow("Game type", self.mode)
        form.addRow("Map", self.map_choice); form.addRow("Frag limit (0 = unlimited)", self.limit)
        form.addRow("Capture limit (0 = unlimited)", self.capture_limit)
        form.addRow(self.host_button)
        layout.addWidget(group)
        self.status = QLabel("Ready"); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.timer = QTimer(self); self.timer.setInterval(50); self.timer.timeout.connect(self.pump)
        self.timer.start()
        self.refresh_games()

    def update_mode(self):
        ctf = self.mode.currentData() == "ctf"
        self.limit.setEnabled(not ctf)
        self.capture_limit.setEnabled(ctf)

    def refresh_games(self):
        if self.scan is not None and not self.scan.done.is_set():
            return
        self.offers.clear(); self.games.setRowCount(0)
        self.scan = Discovery()
        self.status.setText("Searching the LAN…")

    def pump(self):
        if self.scan is not None:
            while True:
                try:
                    offer = self.scan.results.get_nowait()
                except queue.Empty:
                    break
                self.offers[(offer["address"], offer["port"])] = offer
                self.update_table()
            if self.scan.done.is_set():
                if not self.pending:
                    self.status.setText(f"Found {len(self.offers)} LAN game(s)." if self.offers else
                        "No games found. You can host a game or join a LAN address directly.")
                self.scan = None
        if self.pending is not None:
            connection = self.pending
            for packet in list(connection.drain()):
                if packet.get("type") == "welcome":
                    self.start_client(connection, packet)
                    break
                if packet.get("type") in ("error", "disconnected"):
                    self.status.setText("Could not join: " + str(packet.get("message", "host disconnected")))
                    connection.close(); self.pending = None
                    self.set_connecting(False)
                    break

    def update_table(self):
        self.games.setRowCount(len(self.offers))
        for row, (key, offer) in enumerate(sorted(self.offers.items())):
            values = (offer.get("name", "Arena"), offer.get("mode", "deathmatch"),
                      f"{offer.get('players', 0)}/{offer.get('capacity', 8)}", key[0], key[1])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, key)
                self.games.setItem(row, column, item)
        self.games.resizeColumnsToContents()

    def set_connecting(self, busy):
        for button in (self.join, self.direct_join, self.host_button):
            button.setEnabled(not busy)

    def join_selected(self):
        row = self.games.currentRow()
        if row < 0:
            self.status.setText("Select a LAN game first."); return
        address, port = self.games.item(row, 0).data(Qt.UserRole)
        self.connect_to(address, port)

    def connect_to(self, address, port):
        if self.window.view_3d.play_mode:
            self.status.setText("Exit the current Play session before joining another game."); return
        if not address:
            self.status.setText("Enter the host's LAN address."); return
        if self.pending is not None:
            self.pending.close()
        self.pending = LanClient(address, port, self.nickname.text().strip() or "Player", self.team.currentText())
        self.set_connecting(True)
        self.status.setText(f"Connecting to {address}:{port}…")

    def host_game(self):
        if self.window.view_3d.play_mode:
            self.status.setText("Exit the current Play session before hosting another game."); return
        if not self.window.check_unsaved_changes():
            return
        mode = self.mode.currentData()
        if self.map_choice.currentIndex() == 0:
            path = Path(__file__).with_name("CTF_Arena.json" if mode == "ctf" else "Deathmatch_Arena.json")
            data = json.loads(path.read_text())
        else:
            data = copy.deepcopy(self.window.state.get_level_data())
        settings = next((t for t in data.get("things", []) if t.get("type") == "deathmatchsettings"), None)
        if settings is None or not any(t.get("type") == "playerstart" for t in data.get("things", [])):
            self.status.setText("The current map needs Match Settings and a PlayerStart."); return
        if mode == "ctf":
            teams = [t.get("properties", {}).get("team") for t in data["things"] if t.get("type") == "deathmatchflag"]
            if sorted(teams) != ["blue", "red"]:
                self.status.setText("CTF needs exactly one red and one blue CTF Flag."); return
        settings["properties"].update(game_type=mode, frag_limit=self.limit.value(),
            capture_limit=self.capture_limit.value(), player_team="blue" if self.team.currentText() == "blue" else "red")
        transport = None
        try:
            transport = LanHost(self.game_name.text().strip() or "Fio LAN Arena", mode)
            if not self.window._load_level(data):
                raise ValueError("Map could not be loaded")
            self.window.enter_play_mode()
            logic = self.window.view_3d.logic_thread
            session = self.plugin._sessions.get(logic)
            if session is None:
                raise ValueError("The map could not start a Deathmatch session")
            with logic._tick_lock, logic.session_runtime.monster_lock:
                HostGame(session, transport, data, self.nickname.text().strip() or "Host")
                session.publish()
            self.status.setText(f"Hosting {self.game_name.text()} on TCP {transport.port}; LAN discovery on UDP {transport.discovery_port}.")
            self.hide()
        except (OSError, ValueError) as error:
            if transport is not None:
                transport.close()
            self.status.setText("Could not host: " + str(error))

    def start_client(self, connection, packet):
        self.pending = None
        self.set_connecting(False)
        try:
            if packet.get("protocol") != PROTOCOL or not isinstance(packet.get("map"), dict):
                raise ValueError("Incompatible arena handshake")
            data = packet["map"]
            self.window.state.validate_level_data(data)
            if not self.window.check_unsaved_changes():
                connection.close(); return
            if not self.window._load_level(data):
                raise ValueError("The host map could not be loaded")
            self.window.enter_play_mode()
            logic = self.window.view_3d.logic_thread
            session = self.plugin._sessions.get(logic)
            if session is None:
                raise ValueError("The host map does not contain an arena")
            with logic._tick_lock, logic.session_runtime.monster_lock:
                ClientGame(session, connection, packet)
            self.status.setText("Joined LAN game."); self.hide()
        except (ValueError, KeyError, TypeError) as error:
            connection.close(); self.status.setText("Could not join: " + str(error))

    def closeEvent(self, event):
        if self.pending is not None:
            self.pending.close(); self.pending = None
        self.set_connecting(False)
        super().closeEvent(event)
