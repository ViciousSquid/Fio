"""Fio cutscene authoring panel.

The normal workflow is deliberately direct:

    create actor -> move it in the 3D editor -> add waypoint -> repeat -> save

Actors created here are temporary editor entities. Their definitions are embedded
in the cutscene JSON and the runtime creates/removes them when the cutscene plays.
Existing map actors can still be captured and all of the original advanced event
authoring remains available in the Advanced section.
"""

from __future__ import annotations

import json
import uuid

import glm

from PyQt5 import QtWidgets, QtCore

from pathlib import Path

CUTSCENE_DIR = "cutscenes"


def _v3(value):
    if hasattr(value, "x"):
        return [float(value.x), float(value.y), float(value.z)]
    values = list(value)
    return [float(values[0]), float(values[1]), float(values[2])]


def _selected_actors(main_window):
    out = []
    for obj in getattr(main_window.state, "selected_objects", []) or []:
        if getattr(obj, "properties", {}).get("type") == "monster":
            out.append(obj)
    single = getattr(main_window.state, "selected_object", None)
    if (
        single is not None
        and getattr(single, "properties", {}).get("type") == "monster"
        and single not in out
    ):
        out.append(single)
    return out


class CutsceneWizard(QtWidgets.QDialog):
    """Modeless live cutscene authoring panel.

    The editor remains fully usable while this panel is open. Temporary actors
    are ordinary editor entities, which means selection, movement and property
    editing all work through the normal Fio/MiniWind editor machinery.
    """

    NPC_ROLES = ("villager", "guard", "merchant", "blacksmith", "farmer", "beggar")
    CREATURE_ROLES = (
        "wolf", "bear", "boar", "mudcrab", "bandit", "cultist",
        "skeleton", "wraith", "cow", "sheep", "hen",
    )

    def __init__(self, main_window, parent=None):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.setWindowTitle("Fio Cutscenes")
        self.setMinimumSize(560, 760)
        self.resize(640, 900)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowFlag(QtCore.Qt.Tool, True)
        # Use the same application stylesheet as the rest of Fio.
        app = QtWidgets.QApplication.instance()
        if app is not None and app.styleSheet():
            self.setStyleSheet(app.styleSheet())

        self.actor_meta = {}
        self.actor_objects = {}
        self.temporary_actor_ids = set()
        self.actor_tracks = {}
        self.camera_keys = []
        self.events = []
        self._saved = False
        self._cleaned = False

        # Editor-side cutscene preview transport. This previews choreography
        # without firing gameplay combat or permanently changing the map.
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setInterval(33)
        self._preview_timer.timeout.connect(self._preview_tick)
        self._preview_time = 0.0
        self._preview_rate = 0.0
        self._preview_duration = 0.0
        self._preview_actor_baseline = {}
        self._preview_camera_baseline = None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # The wizard is deliberately wider than tall: most authoring happens
        # through the 3D viewport, while the wizard provides compact controls.
        self.setMinimumSize(900, 620)
        self.resize(980, 720)

        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs, 1)

        # -----------------------------------------------------------------
        # Setup
        # -----------------------------------------------------------------
        setup_page = QtWidgets.QWidget()
        setup_layout = QtWidgets.QVBoxLayout(setup_page)
        header = QtWidgets.QGroupBox("Cutscene")
        form = QtWidgets.QFormLayout(header)
        self.name = QtWidgets.QLineEdit("cutscene")
        self.filename = QtWidgets.QLineEdit("cutscene.json")
        self.trigger_mode = QtWidgets.QComboBox()
        self.trigger_mode.addItem("Player enters trigger radius", "proximity")
        self.trigger_mode.addItem("Start when Play Mode begins", "play_start")
        self.trigger_mode.addItem("I/O only (Start input)", "manual")
        self.radius = QtWidgets.QDoubleSpinBox()
        self.radius.setRange(1, 100000)
        self.radius.setValue(180)
        self.radius.setDecimals(1)
        self.once = QtWidgets.QCheckBox("Play once per game session")
        self.once.setChecked(True)
        self.restore = QtWidgets.QCheckBox("Restore existing actors after the cutscene")
        self.restore.setChecked(True)
        self.stop_escape = QtWidgets.QCheckBox("Escape stops the cutscene")
        self.stop_escape.setChecked(True)
        form.addRow("Name", self.name)
        form.addRow("File", self.filename)
        form.addRow("Trigger", self.trigger_mode)
        form.addRow("Radius", self.radius)
        form.addRow("", self.once)
        form.addRow("", self.restore)
        form.addRow("", self.stop_escape)
        setup_layout.addWidget(header)
        setup_help = QtWidgets.QLabel(
            "<b>Simple workflow:</b> add temporary actors, place them in the 3D view, "
            "capture their movement as waypoints, then set the camera. Dialogue and other events are optional. "
            "The temporary actors are removed after the cutscene finishes."
        )
        setup_help.setWordWrap(True)
        setup_help.setMinimumHeight(55)
        setup_layout.addWidget(setup_help)
        setup_layout.addStretch(1)
        tabs.addTab(setup_page, "Setup")

        # -----------------------------------------------------------------
        # Actors + waypoints — the main authoring workflow.
        # -----------------------------------------------------------------
        actors_page = QtWidgets.QWidget()
        actors_layout = QtWidgets.QVBoxLayout(actors_page)

        actors_box = QtWidgets.QGroupBox("Actors")
        av = QtWidgets.QVBoxLayout(actors_box)
        help_label = QtWidgets.QLabel(
            "<b>Step 1:</b> Add an actor. <b>Step 2:</b> select it, "
            "then move it directly in the 3D view. <b>Step 3:</b> capture its "
            "current position below. Repeat for each point in the performance."
        )
        help_label.setWordWrap(True)
        av.addWidget(help_label)

        actor_buttons = QtWidgets.QHBoxLayout()
        self.add_npc_button = QtWidgets.QPushButton("+ Actor")
        self.add_creature_button = QtWidgets.QPushButton("+ Actor")
        self.capture_button = QtWidgets.QPushButton("Add selected editor actors")
        self.remove_actor_button = QtWidgets.QPushButton("Remove")
        self.focus_actor_button = QtWidgets.QPushButton("Focus")
        for button in (self.add_npc_button, self.add_creature_button,
                       self.capture_button, self.remove_actor_button,
                       self.focus_actor_button):
            actor_buttons.addWidget(button)
        av.addLayout(actor_buttons)

        self.actor_list = QtWidgets.QListWidget()
        self.actor_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.actor_list.setMinimumHeight(90)
        av.addWidget(self.actor_list)
        self.selected_actor_label = QtWidgets.QLabel("No actor selected")
        self.selected_actor_label.setStyleSheet("font-weight: bold;")
        av.addWidget(self.selected_actor_label)
        actors_layout.addWidget(actors_box)

        waypoint_box = QtWidgets.QGroupBox("Waypoints — record where the selected actor should go")
        wv = QtWidgets.QVBoxLayout(waypoint_box)

        waypoint_help = QtWidgets.QLabel(
            "<b>How it works:</b> select an actor above → move that actor in the 3D view "
            "→ press <b>Capture waypoint</b>. Each press records the actor's current "
            "position at the displayed time. The next time is advanced automatically."
        )
        waypoint_help.setWordWrap(True)
        waypoint_help.setStyleSheet("padding: 4px;")
        wv.addWidget(waypoint_help)

        time_row = QtWidgets.QHBoxLayout()
        time_row.addWidget(QtWidgets.QLabel("<b>At time</b>"))
        self.waypoint_time = QtWidgets.QDoubleSpinBox()
        self.waypoint_time.setRange(0, 3600)
        self.waypoint_time.setDecimals(2)
        self.waypoint_time.setValue(0)
        self.waypoint_time.setSuffix(" s")
        time_row.addWidget(self.waypoint_time)
        time_row.addStretch(1)
        self.waypoint_action = QtWidgets.QComboBox()
        self.waypoint_action.addItem("MOVE — to this captured location", "move")
        self.waypoint_action.addItem("ATTACK — this person", "attack")
        time_row.addWidget(QtWidgets.QLabel("Action"))
        time_row.addWidget(self.waypoint_action)
        wv.addLayout(time_row)

        target_row = QtWidgets.QHBoxLayout()
        self.waypoint_target_label = QtWidgets.QLabel("Target")
        target_row.addWidget(self.waypoint_target_label)
        self.waypoint_target = QtWidgets.QComboBox()
        self.waypoint_target.setMinimumWidth(240)
        target_row.addWidget(self.waypoint_target, 1)
        target_row.addWidget(QtWidgets.QLabel("Attack for"))
        self.waypoint_attack_duration = QtWidgets.QDoubleSpinBox()
        self.waypoint_attack_duration.setRange(0.05, 300)
        self.waypoint_attack_duration.setDecimals(2)
        self.waypoint_attack_duration.setValue(5)
        self.waypoint_attack_duration.setSuffix(" s")
        target_row.addWidget(self.waypoint_attack_duration)
        wv.addLayout(target_row)

        waypoint_buttons = QtWidgets.QHBoxLayout()
        self.add_waypoint_button = QtWidgets.QPushButton("Capture waypoint")
        self.add_waypoint_button.setDefault(True)
        self.capture_now_button = QtWidgets.QPushButton("Capture current position (shortcut)")
        self.remove_waypoint_button = QtWidgets.QPushButton("Remove selected waypoint")
        waypoint_buttons.addWidget(self.add_waypoint_button)
        waypoint_buttons.addWidget(self.capture_now_button)
        waypoint_buttons.addWidget(self.remove_waypoint_button)
        wv.addLayout(waypoint_buttons)

        self.waypoint_list = QtWidgets.QListWidget()
        self.waypoint_list.setMinimumHeight(130)
        wv.addWidget(self.waypoint_list)
        actors_layout.addWidget(waypoint_box, 1)

        tabs.addTab(actors_page, "Actors && Waypoints")

        # -----------------------------------------------------------------
        # Camera
        # -----------------------------------------------------------------
        camera_page = QtWidgets.QWidget()
        camera_layout = QtWidgets.QVBoxLayout(camera_page)
        camera_box = QtWidgets.QGroupBox("Camera keyframes")
        cv = QtWidgets.QVBoxLayout(camera_box)
        camera_help = QtWidgets.QLabel(
            "Move the editor camera to the shot you want, choose a time, then "
            "press Capture. Optionally make the camera look at an actor."
        )
        camera_help.setWordWrap(True)
        cv.addWidget(camera_help)
        camera_row = QtWidgets.QHBoxLayout()
        self.camera_time = QtWidgets.QDoubleSpinBox()
        self.camera_time.setRange(0, 3600)
        self.camera_time.setDecimals(2)
        self.camera_time.setValue(0)
        self.camera_time.setSuffix(" s")
        self.look_at = QtWidgets.QComboBox()
        self.look_at.addItem("Keep camera rotation", "")
        camera_row.addWidget(QtWidgets.QLabel("At time"))
        camera_row.addWidget(self.camera_time)
        camera_row.addWidget(QtWidgets.QLabel("Look at"))
        camera_row.addWidget(self.look_at, 1)
        self.camera_teleport = QtWidgets.QCheckBox("Teleport from previous keyframe")
        self.camera_teleport.setToolTip(
            "Cut instantly to this keyframe instead of moving the camera from the previous one."
        )
        camera_row.addWidget(self.camera_teleport)
        self.capture_camera_button = QtWidgets.QPushButton("Capture camera position")
        camera_row.addWidget(self.capture_camera_button)
        self.capture_camera_button.clicked.connect(self._capture_camera_keyframe)
        cv.addLayout(camera_row)
        self.camera_keys_list = QtWidgets.QListWidget()
        self.camera_keys_list.setMinimumHeight(120)
        cv.addWidget(self.camera_keys_list, 1)
        camera_buttons = QtWidgets.QHBoxLayout()
        camera_buttons.addWidget(QtWidgets.QLabel("Selected time"))
        self.camera_edit_time = QtWidgets.QDoubleSpinBox()
        self.camera_edit_time.setRange(0, 3600)
        self.camera_edit_time.setDecimals(2)
        self.camera_edit_time.setSuffix(" s")
        self.camera_edit_time.setEnabled(False)
        camera_buttons.addWidget(self.camera_edit_time)
        set_camera_time_button = QtWidgets.QPushButton("Set selected time")
        set_camera_time_button.clicked.connect(self._set_selected_camera_keyframe_time)
        camera_buttons.addWidget(set_camera_time_button)
        delete_camera_button = QtWidgets.QPushButton("Remove selected camera keyframe")
        delete_camera_button.clicked.connect(self._remove_selected_camera_keyframe)
        camera_buttons.addWidget(delete_camera_button)
        camera_buttons.addStretch(1)
        cv.addLayout(camera_buttons)
        self.camera_keys_list.currentRowChanged.connect(self._camera_keyframe_selected)
        camera_layout.addWidget(camera_box, 1)
        tabs.addTab(camera_page, "Camera Keyframes")

        # -----------------------------------------------------------------
        # Events / dialogue. Advanced functionality remains available, but
        # it no longer makes the primary wizard vertically enormous.
        # -----------------------------------------------------------------
        events_page = QtWidgets.QWidget()
        events_layout = QtWidgets.QVBoxLayout(events_page)
        events_tabs = QtWidgets.QTabWidget()
        self._build_message_tab(events_tabs)
        self._build_io_tab(events_tabs)
        events_layout.addWidget(events_tabs, 1)

        self.event_list = QtWidgets.QListWidget()
        self.event_list.setMinimumHeight(100)
        events_layout.addWidget(self.event_list)
        remove_event = QtWidgets.QPushButton("Remove selected event")
        remove_event.clicked.connect(self._remove_event)
        events_layout.addWidget(remove_event)

        # Keep the exact keyframe authoring functionality, but tuck it behind
        # the Events tab rather than forcing it into the simple workflow.
        advanced_toggle = QtWidgets.QToolButton()
        advanced_toggle.setText("Advanced: exact actor keyframes")
        advanced_toggle.setCheckable(True)
        advanced_toggle.setChecked(False)
        advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        advanced_toggle.setArrowType(QtCore.Qt.RightArrow)
        events_layout.addWidget(advanced_toggle)

        advanced = QtWidgets.QWidget()
        advanced.setVisible(False)
        advanced_layout = QtWidgets.QVBoxLayout(advanced)
        exact_box = QtWidgets.QGroupBox("Exact actor keyframes")
        exact_form = QtWidgets.QFormLayout(exact_box)
        self.actor_time = QtWidgets.QDoubleSpinBox()
        self.actor_time.setRange(0, 3600)
        self.actor_time.setDecimals(2)
        self.actor_time.setValue(0)
        self.actor_time.setSuffix(" s")
        exact_form.addRow("Keyframe time", self.actor_time)
        self.actor_keys_list = QtWidgets.QListWidget()
        self.actor_keys_list.setMinimumHeight(70)
        exact_form.addRow(self.actor_keys_list)
        exact_buttons = QtWidgets.QHBoxLayout()
        self.capture_actor_button = QtWidgets.QPushButton(
            "Capture selected actors at this time"
        )
        self.capture_actor_button.clicked.connect(self._capture_actor_keyframe)
        exact_buttons.addWidget(self.capture_actor_button)
        exact_form.addRow(exact_buttons)
        advanced_layout.addWidget(exact_box)
        events_layout.addWidget(advanced)

        def toggle_advanced(checked):
            advanced.setVisible(checked)
            advanced_toggle.setArrowType(
                QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow
            )

        advanced_toggle.toggled.connect(toggle_advanced)
        tabs.addTab(events_page, "Events")

        footer = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel("No actors created yet.")
        self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        self.load_button = QtWidgets.QPushButton("Load")
        self.save_button = QtWidgets.QPushButton("Save")
        self.apply_button = QtWidgets.QPushButton("Apply to Map")
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.save_button.setDefault(True)
        self.cancel_button.setDefault(False)
        footer.addWidget(self.load_button)
        footer.addWidget(self.save_button)
        footer.addWidget(self.apply_button)
        footer.addWidget(self.cancel_button)
        root.addLayout(footer)

        # Preview transport is deliberately a separate control strip.
        playback_box = QtWidgets.QGroupBox("Preview")
        playback = QtWidgets.QHBoxLayout(playback_box)
        playback.setContentsMargins(8, 6, 8, 6)
        self.rewind_button = QtWidgets.QPushButton("⏪ RW")
        self.play_button = QtWidgets.QPushButton("▶ Play")
        self.fast_forward_button = QtWidgets.QPushButton("FF ⏩")
        self.play_button.setStyleSheet(
            "QPushButton { background: #238636; color: white; font-weight: bold; "
            "padding: 6px 18px; border-radius: 4px; }"
            "QPushButton:hover { background: #2ea043; }"
        )
        self.preview_time_label = QtWidgets.QLabel("0.00s / 0.00s")
        self.preview_time_label.setMinimumWidth(110)
        self.preview_time_label.setAlignment(QtCore.Qt.AlignCenter)
        playback.addWidget(self.rewind_button)
        playback.addWidget(self.play_button)
        playback.addWidget(self.fast_forward_button)
        playback.addStretch(1)
        playback.addWidget(self.preview_time_label)
        root.insertWidget(root.count() - 1, playback_box)

        self.rewind_button.clicked.connect(self._preview_rewind)
        self.play_button.clicked.connect(self._preview_play)
        self.fast_forward_button.clicked.connect(self._preview_fast_forward)

        self.add_npc_button.clicked.connect(lambda: self._create_temporary_actor("monster"))
        self.add_creature_button.clicked.connect(lambda: self._create_temporary_actor("monster"))
        self.add_creature_button.hide()
        self.capture_button.clicked.connect(self._capture_selected_actors)
        self.remove_actor_button.clicked.connect(self._remove_selected_actors)
        self.focus_actor_button.clicked.connect(self._focus_selected_actor)
        self.actor_list.itemSelectionChanged.connect(self._actor_selection_changed)
        self.actor_list.itemDoubleClicked.connect(
            lambda _item: self._focus_selected_actor()
        )
        self.add_waypoint_button.clicked.connect(
            lambda: self._add_waypoint(from_current=True)
        )
        self.capture_now_button.clicked.connect(
            lambda: self._add_waypoint(from_current=True)
        )
        self.remove_waypoint_button.clicked.connect(self._remove_selected_waypoint)
        self.waypoint_action.currentIndexChanged.connect(
            lambda _index: self._update_waypoint_controls()
        )
        self.load_button.clicked.connect(self._load_cutscene)
        self.save_button.clicked.connect(self._save_and_close)
        self.apply_button.clicked.connect(self._apply_to_map)
        self.cancel_button.clicked.connect(self.reject)

        self._refresh_actor_lists()
        self._update_waypoint_controls()
        self._refresh_summary()
        self._load_first_cutscene()

    def showEvent(self, event):
        super().showEvent(event)
        # Keep the modeless wizard fully visible on screen.  Its height can
        # exceed the available desktop height on smaller displays, so clamp
        # the frame upward after Qt has applied window decorations.
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            frame = self.frameGeometry()
            frame.moveTop(available.top())
            self.move(frame.topLeft())
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    # Editor-side preview transport.
    # ------------------------------------------------------------------
    def _preview_end_time(self):
        times = [float(row.get("time", 0.0)) for rows in self.actor_tracks.values() for row in rows]
        times += [float(row.get("time", 0.0)) for row in self.camera_keys]
        times += [float(row.get("time", 0.0)) for row in self.events]
        return max(times, default=0.0)

    @staticmethod
    def _preview_pose(frames, elapsed):
        if not frames:
            return None
        frames = sorted(frames, key=lambda row: float(row.get("time", 0.0)))
        if elapsed <= float(frames[0].get("time", 0.0)):
            row = frames[0]
            return list(row.get("pos", (0, 0, 0))), float(row.get("yaw", 0.0))
        if elapsed >= float(frames[-1].get("time", 0.0)):
            row = frames[-1]
            return list(row.get("pos", (0, 0, 0))), float(row.get("yaw", 0.0))
        for left, right in zip(frames, frames[1:]):
            lt = float(left.get("time", 0.0))
            rt = float(right.get("time", 0.0))
            if lt <= elapsed <= rt:
                t = max(0.0, min(1.0, (elapsed - lt) / max(1e-6, rt - lt)))
                t = t * t * (3.0 - 2.0 * t)
                lp = list(left.get("pos", (0, 0, 0)))
                rp = list(right.get("pos", lp))
                if right.get("teleport", False):
                    if elapsed < rt:
                        return list(lp), float(left.get("yaw", 0.0))
                    return list(rp), float(right.get("yaw", 0.0))
                pos = [lp[i] + (rp[i] - lp[i]) * t for i in range(3)]
                yaw = float(left.get("yaw", 0.0)) + (
                    float(right.get("yaw", 0.0)) - float(left.get("yaw", 0.0))
                ) * t
                return pos, yaw
        return None

    def _preview_start(self):
        if self._preview_camera_baseline is None:
            camera = self.main_window.view_3d.camera
            self._preview_camera_baseline = {
                "pos": _v3(camera.pos),
                "yaw": float(camera.yaw),
                "pitch": float(camera.pitch),
                "fov": float(getattr(camera, "fov", 90.0)),
            }
        for aid, actor in self.actor_objects.items():
            if aid not in self._preview_actor_baseline and actor is not None:
                self._preview_actor_baseline[aid] = {
                    "pos": _v3(actor.pos),
                    "yaw": float(getattr(actor, "angle", 0.0)),
                }
        self._preview_duration = self._preview_end_time()

    def _preview_apply(self):
        for aid, frames in self.actor_tracks.items():
            actor = self.actor_objects.get(aid)
            pose = self._preview_pose(frames, self._preview_time)
            if actor is None or pose is None:
                continue
            pos, yaw = pose
            actor.pos = glm.vec3(*pos)
            actor.angle = yaw

        pose = self._preview_pose(self.camera_keys, self._preview_time)
        if pose is not None:
            pos, yaw = pose
            frames = sorted(self.camera_keys, key=lambda row: float(row.get("time", 0.0)))
            if len(frames) == 1:
                pitch = float(frames[0].get("pitch", 0.0))
                fov = float(frames[0].get("fov", 90.0))
            elif self._preview_time >= float(frames[-1].get("time", 0.0)):
                pitch = float(frames[-1].get("pitch", 0.0))
                fov = float(frames[-1].get("fov", 90.0))
            else:
                pitch = float(frames[0].get("pitch", 0.0))
                fov = float(frames[0].get("fov", 90.0))
                for left, right in zip(frames, frames[1:]):
                    lt = float(left.get("time", 0.0)); rt = float(right.get("time", 0.0))
                    if lt <= self._preview_time <= rt:
                        t = max(0.0, min(1.0, (self._preview_time - lt) / max(1e-6, rt - lt)))
                        if right.get("teleport", False):
                            if self._preview_time < rt:
                                pitch = float(left.get("pitch", 0.0))
                                fov = float(left.get("fov", 90.0))
                            else:
                                pitch = float(right.get("pitch", 0.0))
                                fov = float(right.get("fov", 90.0))
                        else:
                            t = t * t * (3.0 - 2.0 * t)
                            pitch = float(left.get("pitch", 0.0)) + (float(right.get("pitch", 0.0)) - float(left.get("pitch", 0.0))) * t
                            fov = float(left.get("fov", 90.0)) + (float(right.get("fov", 90.0)) - float(left.get("fov", 90.0))) * t
                        break
            camera = self.main_window.view_3d.camera
            camera.pos = glm.vec3(*pos)
            camera.yaw = yaw
            camera.pitch = pitch
            camera.fov = fov
            logic = getattr(self.main_window.view_3d, "logic_thread", None)
            if logic is not None and hasattr(logic, "set_editor_camera"):
                logic.set_editor_camera(camera.pos, camera.yaw, camera.pitch, camera.fov)

        self.preview_time_label.setText(f"{self._preview_time:.2f}s / {self._preview_duration:.2f}s")
        try:
            self.main_window.update_all_ui()
        except Exception:
            pass

    def _preview_tick(self):
        if self._preview_rate == 0.0:
            return
        self._preview_time += 0.033 * self._preview_rate
        if self._preview_time >= self._preview_duration or self._preview_time <= 0.0:
            self._preview_stop_and_restore()
            return
        self._preview_apply()

    def _preview_play(self):
        self._preview_start()
        if self._preview_duration <= 0.0:
            self._preview_time = 0.0
            self._preview_apply()
            return
        if self._preview_rate == 1.0:
            self._preview_stop_and_restore()
            return
        if self._preview_time >= self._preview_duration:
            self._preview_time = 0.0
        self._preview_rate = 1.0
        self._preview_timer.start()
        self.play_button.setText("⏸ Pause")
        self._preview_apply()

    def _preview_rewind(self):
        self._preview_start()
        self._preview_rate = -2.0
        self._preview_timer.start()
        self.play_button.setText("▶ Play")
        self._preview_apply()

    def _preview_fast_forward(self):
        self._preview_start()
        self._preview_rate = 2.0
        self._preview_timer.start()
        self.play_button.setText("▶ Play")
        self._preview_apply()

    def _preview_stop_and_restore(self):
        self._preview_timer.stop()
        self._preview_rate = 0.0
        self.play_button.setText("▶ Play")
        for aid, baseline in self._preview_actor_baseline.items():
            actor = self.actor_objects.get(aid)
            if actor is not None:
                actor.pos = glm.vec3(*baseline["pos"])
                actor.angle = baseline["yaw"]
        if self._preview_camera_baseline is not None:
            camera = self.main_window.view_3d.camera
            camera.pos = glm.vec3(*self._preview_camera_baseline["pos"])
            camera.yaw = self._preview_camera_baseline["yaw"]
            camera.pitch = self._preview_camera_baseline["pitch"]
            camera.fov = self._preview_camera_baseline["fov"]
            logic = getattr(self.main_window.view_3d, "logic_thread", None)
            if logic is not None and hasattr(logic, "set_editor_camera"):
                logic.set_editor_camera(camera.pos, camera.yaw, camera.pitch, camera.fov)
        self._preview_camera_baseline = None
        self._preview_actor_baseline.clear()
        self._preview_time = 0.0
        self._preview_duration = 0.0
        self.preview_time_label.setText("0.00s / 0.00s")
        try:
            self.main_window.update_all_ui()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Advanced event widgets — these retain the original wizard controls.
    # ------------------------------------------------------------------
    def _refresh_io_sources(self):
        """Refresh the I/O source list from the live map entities."""
        current = self.io_source.currentData() if hasattr(self, "io_source") else None
        self.io_source.blockSignals(True)
        self.io_source.clear()
        for thing in getattr(self.main_window.state, "things", []) or []:
            props = getattr(thing, "properties", {})
            if props.get("_cutscene_temporary"):
                continue
            entity_id = str(props.get("id", ""))
            if not entity_id:
                continue
            name = str(props.get("name") or entity_id)
            self.io_source.addItem(name, entity_id)
        idx = self.io_source.findData(current)
        self.io_source.setCurrentIndex(idx if idx >= 0 else (0 if self.io_source.count() else -1))
        self.io_source.blockSignals(False)
        self._refresh_io_outputs()

    def _refresh_io_outputs(self):
        from .io_system import get_output_names
        current = self.io_output.currentText()
        self.io_output.blockSignals(True)
        self.io_output.clear()
        source_id = str(self.io_source.currentData() or "")
        source = next((t for t in getattr(self.main_window.state, "things", []) or []
                       if str(getattr(t, "properties", {}).get("id", "")) == source_id), None)
        if source is not None:
            entity_type = str(source.properties.get("type", ""))
            for output in sorted(get_output_names(entity_type)):
                self.io_output.addItem(output)
        self.io_output.setCurrentText(current)
        self.io_output.blockSignals(False)

    def _build_io_tab(self, tabs):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        box = QtWidgets.QGroupBox("Trigger an I/O output during the cutscene")
        form = QtWidgets.QFormLayout(box)
        self.io_time = QtWidgets.QDoubleSpinBox()
        self.io_time.setRange(0, 3600)
        self.io_time.setDecimals(2)
        self.io_time.setSuffix(" s")
        form.addRow("At time", self.io_time)
        self.io_source = QtWidgets.QComboBox()
        self.io_source.setMinimumWidth(260)
        form.addRow("Source entity", self.io_source)
        self.io_source.currentIndexChanged.connect(lambda _i: self._refresh_io_outputs())
        self.io_output = QtWidgets.QComboBox()
        self.io_output.setEditable(True)
        self.io_output.setMinimumWidth(260)
        form.addRow("Output", self.io_output)
        self.io_parameter = QtWidgets.QLineEdit()
        self.io_parameter.setPlaceholderText("Optional parameter")
        form.addRow("Parameter", self.io_parameter)
        add = QtWidgets.QPushButton("Add I/O event")
        add.clicked.connect(self._add_io_event)
        form.addRow("", add)
        layout.addWidget(box)
        help_text = QtWidgets.QLabel(
            "This fires the real Fio output at the authored cutscene time. "
            "The normal I/O graph then handles the connected inputs."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        layout.addStretch(1)
        tabs.addTab(page, "I/O Output")
        self._refresh_io_sources()

    def _add_io_event(self):
        source_id = str(self.io_source.currentData() or "")
        source_name = self.io_source.currentText().strip()
        output = self.io_output.currentText().strip()
        if not source_id or not source_name:
            QtWidgets.QMessageBox.warning(self, "I/O Output", "Choose a source entity first.")
            return
        if not output:
            QtWidgets.QMessageBox.warning(self, "I/O Output", "Choose or enter an output name.")
            return
        event = {
            "time": float(self.io_time.value()),
            "type": "io",
            "source_id": source_id,
            "source_name": source_name,
            "output": output,
        }
        parameter = self.io_parameter.text()
        if parameter:
            event["parameter"] = parameter
        self.events.append(event)
        self.events.sort(key=lambda x: x.get("time", 0))
        self._refresh_event_list()
        self._refresh_summary()
        self.io_time.setValue(float(event["time"]) + 1.0)

    def _build_fight_tab(self, tabs):
        fight = QtWidgets.QWidget()
        fv = QtWidgets.QVBoxLayout(fight)
        self.fight_time = QtWidgets.QDoubleSpinBox()
        self.fight_time.setRange(0, 3600)
        self.fight_time.setDecimals(2)
        self.fight_duration = QtWidgets.QDoubleSpinBox()
        self.fight_duration.setRange(0.05, 300)
        self.fight_duration.setDecimals(2)
        self.fight_duration.setValue(5)
        self.fight_style = QtWidgets.QComboBox()
        self.fight_style.addItem("Use each actor's normal combat style", "normal")
        self.fight_style.addItem("Melee — swords / claws / close combat", "melee")
        self.fight_style.addItem("Archery — bows / ranged attacks", "bow")
        self.fight_style.addItem("Spell — magical ranged attack", "magic")
        form = QtWidgets.QFormLayout()
        form.addRow("Start time", self.fight_time)
        form.addRow("Duration", self.fight_duration)
        form.addRow("How they fight", self.fight_style)
        fv.addLayout(form)
        lists = QtWidgets.QHBoxLayout()
        self.attackers = QtWidgets.QListWidget()
        self.attackers.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.defenders = QtWidgets.QListWidget()
        self.defenders.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        lists.addWidget(self._labelled_list("Attackers", self.attackers))
        lists.addWidget(self._labelled_list("Defenders", self.defenders))
        fv.addLayout(lists, 1)
        button = QtWidgets.QPushButton("Add fight event")
        button.clicked.connect(self._add_fight)
        fv.addWidget(button)
        tabs.addTab(fight, "Fight")

    def _build_blood_tab(self, tabs):
        blood = QtWidgets.QWidget()
        bv = QtWidgets.QVBoxLayout(blood)
        form = QtWidgets.QFormLayout()
        self.blood_time = QtWidgets.QDoubleSpinBox()
        self.blood_time.setRange(0, 3600)
        self.blood_time.setDecimals(2)
        self.blood_variant = QtWidgets.QComboBox()
        self.blood_variant.addItem("Random", "random")
        self.blood_x = QtWidgets.QDoubleSpinBox()
        self.blood_y = QtWidgets.QDoubleSpinBox()
        self.blood_z = QtWidgets.QDoubleSpinBox()
        for spin in (self.blood_x, self.blood_y, self.blood_z):
            spin.setRange(-100000, 100000)
            spin.setDecimals(2)
        self.blood_w = QtWidgets.QDoubleSpinBox()
        self.blood_h = QtWidgets.QDoubleSpinBox()
        self.blood_w.setRange(8, 512)
        self.blood_h.setRange(8, 512)
        self.blood_w.setValue(72)
        self.blood_h.setValue(48)
        form.addRow("Time", self.blood_time)
        form.addRow("Variant", self.blood_variant)
        form.addRow("X", self.blood_x)
        form.addRow("Y", self.blood_y)
        form.addRow("Z", self.blood_z)
        form.addRow("Width", self.blood_w)
        form.addRow("Height", self.blood_h)
        bv.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        use_pos = QtWidgets.QPushButton("Use selected actor position")
        use_pos.clicked.connect(self._use_selected_position)
        add = QtWidgets.QPushButton("Add blood keyframe")
        add.clicked.connect(self._add_blood)
        row.addWidget(use_pos)
        row.addWidget(add)
        bv.addLayout(row)
        tabs.addTab(blood, "Blood")

    def _build_dialogue_tab(self, tabs):
        dialog = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        self.dialogue_time = QtWidgets.QDoubleSpinBox()
        self.dialogue_time.setRange(0, 3600)
        self.dialogue_time.setDecimals(2)
        self.dialogue_duration = QtWidgets.QDoubleSpinBox()
        self.dialogue_duration.setRange(0.05, 300)
        self.dialogue_duration.setValue(3)
        self.dialogue_duration.setDecimals(2)
        self.dialogue_speaker = QtWidgets.QComboBox()
        self.dialogue_text = QtWidgets.QPlainTextEdit()
        self.dialogue_text.setFixedHeight(100)
        form.addRow("Time", self.dialogue_time)
        form.addRow("Duration", self.dialogue_duration)
        form.addRow("Speaker", self.dialogue_speaker)
        form.addRow("Text", self.dialogue_text)
        dv.addLayout(form)
        button = QtWidgets.QPushButton("Add dialogue keyframe")
        button.clicked.connect(self._add_dialogue)
        dv.addWidget(button)
        tabs.addTab(dialog, "Dialogue")

    def _build_message_tab(self, tabs):
        message = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(message)
        self.message_time = QtWidgets.QDoubleSpinBox()
        self.message_time.setRange(0, 3600)
        self.message_time.setDecimals(2)
        self.message_line = QtWidgets.QComboBox()
        self.message_line.addItems(["message", "message2", "message3"])
        self.message_text = QtWidgets.QLineEdit()
        form.addRow("Time", self.message_time)
        form.addRow("Line", self.message_line)
        form.addRow("Text", self.message_text)
        button = QtWidgets.QPushButton("Add message keyframe")
        button.clicked.connect(self._add_message)
        form.addRow("", button)
        tabs.addTab(message, "Message")

    @staticmethod
    def _labelled_list(title, widget):
        box = QtWidgets.QGroupBox(title)
        lay = QtWidgets.QVBoxLayout(box)
        lay.addWidget(widget)
        return box

    # ------------------------------------------------------------------
    # Live actor authoring.
    # ------------------------------------------------------------------
    def _create_temporary_actor(self, entity_type):
        try:
            from .things import Monster
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Actor", f"Could not create a Fio actor: {exc}")
            return

        camera = getattr(getattr(self.main_window, "view_3d", None), "camera", None)
        if camera is None:
            return
        try:
            front = camera.get_front_vector()
            pos = [
                float(camera.pos.x + front.x * 256.0),
                float(camera.pos.y + front.y * 256.0),
                float(camera.pos.z + front.z * 256.0),
            ]
        except Exception:
            pos = _v3(camera.pos)

        default_name = f"Cutscene Actor {len(self.actor_meta) + 1}"
        props = {
            "type": "monster",
            "id": str(uuid.uuid4()),
            "name": default_name,
            "display_name": default_name,
            "monster_type": "human",
            "triggered": True,
            "_cutscene_temporary": True,
        }
        actor = Monster(pos=pos, properties=props)
        actor.properties["_cutscene_temporary"] = True
        self.main_window.state.things.append(actor)
        self.actor_objects[str(actor.properties.get("id"))] = actor
        self.temporary_actor_ids.add(str(actor.properties.get("id")))
        self.actor_meta[str(actor.properties.get("id"))] = {
            "id": str(actor.properties.get("id")),
            "name": str(actor.properties.get("display_name") or actor.properties.get("name")),
            "spawn": True,
        }
        self.main_window.set_selected_object(actor)
        self.main_window.update_all_ui()
        self._refresh_actor_lists()
        self._select_actor_id(str(actor.properties.get("id")))
        self.main_window.show_toast(f"{default_name} created in the current 3D view")

    def _capture_selected_actors(self):
        selected = _selected_actors(self.main_window)
        if not selected:
            QtWidgets.QMessageBox.information(
                self,
                "Actors",
                "Select one or more NPCs or creatures in the editor first.",
            )
            return
        for actor in selected:
            props = actor.properties
            aid = str(props.get("id", "") or "")
            if not aid:
                aid = str(uuid.uuid4())
                props["id"] = aid
            self.actor_objects[aid] = actor
            self.actor_meta[aid] = {
                "id": aid,
                "name": str(props.get("display_name") or props.get("name") or aid),
                "spawn": aid in self.temporary_actor_ids,
            }
        self._refresh_actor_lists()
        self._select_actor_id(str(selected[0].properties.get("id", "")))

    def _remove_selected_actors(self):
        item = self.actor_list.currentItem()
        if item is None:
            return
        aid = str(item.data(QtCore.Qt.UserRole))
        self.actor_meta.pop(aid, None)
        self.actor_tracks.pop(aid, None)

        # Remove every authored reference to the deleted actor. A cutscene
        # must never save dangling actor IDs in fights, camera look-at targets,
        # dialogue speakers, or waypoint destinations.
        self.camera_keys = [
            row for row in self.camera_keys
            if str(row.get("look_at", {}).get("actor", "")) != aid
        ]
        for row in self.actor_tracks.values():
            for frame in row:
                if str(frame.get("target_id", "")) == aid:
                    frame.pop("target_id", None)
        cleaned_events = []
        for event in self.events:
            if event.get("type") == "fight":
                attackers = [str(x) for x in event.get("attackers", []) if str(x) != aid]
                defenders = [str(x) for x in event.get("defenders", []) if str(x) != aid]
                if not attackers or not defenders:
                    continue
                event = dict(event)
                event["attackers"] = attackers
                event["defenders"] = defenders
            elif event.get("type") == "dialogue" and str(event.get("speaker_id", "")) == aid:
                event = dict(event)
                event["speaker_id"] = ""
            elif event.get("type") == "io" and str(event.get("source_id", "")) == aid:
                continue
            cleaned_events.append(event)
        self.events = cleaned_events

        if aid in self.temporary_actor_ids:
            actor = self.actor_objects.get(aid)
            if actor is not None:
                try:
                    self.main_window.state.things.remove(actor)
                except ValueError:
                    pass
            self.temporary_actor_ids.discard(aid)
        self.actor_objects.pop(aid, None)
        self._refresh_actor_lists()
        self._refresh_waypoints()
        self._refresh_event_list()
        self._refresh_camera_list()
        self.main_window.update_all_ui()
        self._refresh_summary()

    def _actor_selection_changed(self):
        aid = self._current_actor_id()
        actor = self.actor_objects.get(aid) if aid else None
        if actor is not None:
            try:
                self.main_window.set_selected_object(actor)
            except Exception:
                pass
            name = self.actor_meta.get(aid, {}).get("name", aid)
            self.selected_actor_label.setText(f"Selected: {name}")
        else:
            self.selected_actor_label.setText("No actor selected")
        self._refresh_waypoints()

    def _focus_selected_actor(self):
        actor = self.actor_objects.get(self._current_actor_id())
        if actor is None:
            return
        try:
            self.main_window.focus_on_object(actor)
        except Exception:
            pass

    def _current_actor_id(self):
        item = self.actor_list.currentItem()
        return str(item.data(QtCore.Qt.UserRole)) if item is not None else ""

    def _select_actor_id(self, aid):
        for i in range(self.actor_list.count()):
            if str(self.actor_list.item(i).data(QtCore.Qt.UserRole)) == str(aid):
                self.actor_list.setCurrentRow(i)
                break

    def _target_actor_ids(self):
        return list(self.actor_meta.keys())

    def _refresh_actor_lists(self):
        """Refresh actor widgets that are part of the current simple wizard UI.

        The old advanced Fight/Dialogue controls are intentionally no longer
        created in the Events tab.  Keep this refresh path aligned with the
        widgets that actually exist instead of touching removed controls.
        """
        wanted = self._current_actor_id()

        self.actor_list.blockSignals(True)
        self.actor_list.clear()
        for aid, meta in self.actor_meta.items():
            item = QtWidgets.QListWidgetItem(meta["name"])
            if meta.get("spawn"):
                item.setText(f"★ {meta['name']}")
                item.setToolTip(
                    "Temporary actor — embedded in the cutscene and deleted "
                    "from the map after authoring"
                )
            item.setData(QtCore.Qt.UserRole, aid)
            self.actor_list.addItem(item)
        self.actor_list.blockSignals(False)

        if wanted:
            self._select_actor_id(wanted)

        # Only these actor-selection combos are present in the simplified
        # wizard.  Do not reference removed dialogue/fight widgets here.
        for combo in (self.look_at, self.waypoint_target):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if combo is self.look_at:
                combo.addItem("Keep camera rotation", "")
            else:
                combo.addItem("No target — use actor's current position", "")
            for aid, meta in self.actor_meta.items():
                combo.addItem(meta["name"], aid)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

        self._update_waypoint_controls()
        self._refresh_summary()

    @staticmethod
    def _restore_multi_selection(widget, ids):
        for i in range(widget.count()):
            if str(widget.item(i).data(QtCore.Qt.UserRole)) in ids:
                widget.item(i).setSelected(True)

    def _update_waypoint_controls(self):
        is_attack = self.waypoint_action.currentData() == "attack"
        self.waypoint_attack_duration.setEnabled(is_attack)
        self.waypoint_target_label.setText("Attack this person" if is_attack else "Move to location")
        self.waypoint_target.setToolTip(
            "Choose the person to attack" if is_attack else
            "Optional: use another actor as the destination location"
        )

    def _add_waypoint(self, from_current=True):
        aid = self._current_actor_id()
        actor = self.actor_objects.get(aid)
        if not aid or actor is None:
            QtWidgets.QMessageBox.information(
                self, "Waypoint", "Select an actor first."
            )
            return

        target_id = str(self.waypoint_target.currentData() or "")
        if target_id == aid:
            target_id = ""
        action = str(self.waypoint_action.currentData() or "move")
        if target_id and target_id not in self.actor_meta:
            target_id = ""

        if target_id:
            target_obj = self.actor_objects.get(target_id)
            pos = _v3(target_obj.pos) if target_obj is not None else _v3(actor.pos)
        else:
            pos = _v3(actor.pos)

        row = {
            "time": float(self.waypoint_time.value()),
            "pos": pos,
            "yaw": float(getattr(actor, "angle", 0.0)),
            "action": action,
        }
        if target_id:
            row["target_id"] = target_id
        if action == "attack":
            if not target_id:
                QtWidgets.QMessageBox.warning(
                    self, "Waypoint", "Attack waypoints need a target."
                )
                return
            duration = float(self.waypoint_attack_duration.value())
            row["duration"] = duration
            self.events.append({
                "time": row["time"],
                "type": "fight",
                "duration": duration,
                "attackers": [aid],
                "defenders": [target_id],
                "style": "normal",
                "_simple_waypoint": True,
            })

        self.actor_tracks.setdefault(aid, []).append(row)
        self.actor_tracks[aid].sort(key=lambda x: x["time"])
        self._refresh_actor_keys_list()
        self._refresh_waypoints()
        self._refresh_event_list()
        self._refresh_summary()

        # Fast authoring: next waypoint starts one second later.
        self.waypoint_time.setValue(float(row["time"]) + (float(row.get("duration", 0.0)) if action == "attack" else 1.0))

    def _remove_selected_waypoint(self):
        aid = self._current_actor_id()
        if not aid:
            return
        row = self.waypoint_list.currentRow()
        frames = self.actor_tracks.get(aid, [])
        if not (0 <= row < len(frames)):
            return
        frame = frames.pop(row)
        if frame.get("action") == "attack" and frame.get("target_id"):
            t = float(frame.get("time", 0))
            d = float(frame.get("duration", 0))
            self.events = [
                e for e in self.events
                if not (
                    e.get("_simple_waypoint")
                    and e.get("type") == "fight"
                    and float(e.get("time", -1)) == t
                    and e.get("attackers") == [aid]
                    and e.get("defenders") == [frame.get("target_id")]
                    and float(e.get("duration", -1)) == d
                )
            ]
        if not frames:
            self.actor_tracks.pop(aid, None)
        self._refresh_actor_keys_list()
        self._refresh_waypoints()
        self._refresh_event_list()
        self._refresh_summary()

    def _refresh_waypoints(self):
        self.waypoint_list.clear()
        aid = self._current_actor_id()
        for index, row in enumerate(self.actor_tracks.get(aid, []), 1):
            action = str(row.get("action", "move")).lower()
            target = row.get("target_id")
            target_name = self.actor_meta.get(str(target), {}).get("name", "") if target else ""
            time = float(row.get("time", 0.0))
            if action == "attack":
                label = f"{index}. At {time:.2f}s — ATTACK {target_name or 'target'}"
            else:
                label = f"{index}. At {time:.2f}s — MOVE to captured location"
            self.waypoint_list.addItem(label)

    # ------------------------------------------------------------------
    # Camera + exact keyframes.
    # ------------------------------------------------------------------
    def _capture_actor_keyframe(self):
        actors = _selected_actors(self.main_window)
        if not actors:
            QtWidgets.QMessageBox.information(
                self, "Movement", "Select actors in the editor first."
            )
            return
        t = float(self.actor_time.value())
        for actor in actors:
            aid = str(actor.properties.get("id", "") or "")
            if not aid:
                continue
            self.actor_objects[aid] = actor
            if aid not in self.actor_meta:
                self.actor_meta[aid] = {
                    "id": aid,
                    "name": str(actor.properties.get("display_name") or actor.properties.get("name") or aid),
                    "spawn": aid in self.temporary_actor_ids,
                }
            self.actor_tracks.setdefault(aid, []).append({
                "time": t,
                "pos": _v3(actor.pos),
                "yaw": float(getattr(actor, "angle", 0.0)),
            })
            self.actor_tracks[aid].sort(key=lambda x: x["time"])
        self._refresh_actor_lists()
        self._refresh_actor_keys_list()
        self._refresh_summary()

    def _capture_camera_keyframe(self):
        camera = self.main_window.view_3d.camera
        frame = {
            "time": float(self.camera_time.value()),
            "pos": _v3(camera.pos),
            "yaw": float(camera.yaw),
            "pitch": float(camera.pitch),
            "fov": float(getattr(camera, "fov", 90.0)),
        }
        if self.camera_teleport.isChecked():
            frame["teleport"] = True
        aid = self.look_at.currentData()
        if aid:
            frame["look_at"] = {"actor": str(aid)}
        self.camera_keys.append(frame)
        self.camera_keys.sort(key=lambda x: x["time"])
        self._refresh_camera_list()
        self.camera_time.setValue(float(frame["time"]) + 1.0)

    def _camera_keyframe_selected(self, row):
        valid = 0 <= row < len(self.camera_keys)
        self.camera_edit_time.setEnabled(valid)
        if valid:
            self.camera_edit_time.blockSignals(True)
            self.camera_edit_time.setValue(float(self.camera_keys[row].get("time", 0.0)))
            self.camera_edit_time.blockSignals(False)

    def _set_selected_camera_keyframe_time(self):
        row = self.camera_keys_list.currentRow()
        if not (0 <= row < len(self.camera_keys)):
            return
        selected = self.camera_keys[row]
        selected["time"] = float(self.camera_edit_time.value())
        self.camera_keys.sort(key=lambda x: float(x.get("time", 0.0)))
        new_row = self.camera_keys.index(selected)
        self._refresh_camera_list()
        self.camera_keys_list.setCurrentRow(new_row)
        self._refresh_summary()

    def _remove_selected_camera_keyframe(self):
        row = self.camera_keys_list.currentRow()
        if not (0 <= row < len(self.camera_keys)):
            return
        self.camera_keys.pop(row)
        self._refresh_camera_list()
        self._refresh_summary()

    def _refresh_camera_list(self):
        self.camera_keys_list.clear()
        for row in self.camera_keys:
            look = row.get("look_at", {}).get("actor", "")
            target = self.actor_meta.get(str(look), {}).get("name", "") if look else ""
            suffix = f" — look at {target}" if target else ""
            if row.get("teleport", False):
                suffix += " — TELEPORT"
            self.camera_keys_list.addItem(
                f"{row['time']:.2f}s — camera {row['pos']}{suffix}"
            )

    def _refresh_actor_keys_list(self):
        self.actor_keys_list.clear()
        for aid, rows in sorted(self.actor_tracks.items()):
            name = self.actor_meta.get(aid, {}).get("name", aid)
            for row in rows:
                self.actor_keys_list.addItem(
                    f"{float(row.get('time', 0)):.2f}s — {name} → {row.get('pos')}"
                )

    # ------------------------------------------------------------------
    # Existing event functionality.
    # ------------------------------------------------------------------
    def _add_fight(self):
        attackers = [
            str(x.data(QtCore.Qt.UserRole)) for x in self.attackers.selectedItems()
        ]
        defenders = [
            str(x.data(QtCore.Qt.UserRole)) for x in self.defenders.selectedItems()
        ]
        if not attackers or not defenders:
            QtWidgets.QMessageBox.warning(
                self, "Fight", "Choose at least one attacker and one defender."
            )
            return
        self.events.append({
            "time": float(self.fight_time.value()),
            "type": "fight",
            "duration": float(self.fight_duration.value()),
            "attackers": attackers,
            "defenders": defenders,
            "style": self.fight_style.currentData(),
        })
        self._refresh_event_list()
        self._refresh_summary()

    def _use_selected_position(self):
        actors = _selected_actors(self.main_window)
        if actors:
            pos = _v3(actors[0].pos)
            self.blood_x.setValue(pos[0])
            self.blood_y.setValue(pos[1])
            self.blood_z.setValue(pos[2])

    def _add_blood(self):
        self.events.append({
            "time": float(self.blood_time.value()),
            "type": "blood",
            "position": [
                self.blood_x.value(), self.blood_y.value(), self.blood_z.value()
            ],
            "variant": self.blood_variant.currentData(),
            "width": float(self.blood_w.value()),
            "height": float(self.blood_h.value()),
            "persist": True,
        })
        self._refresh_event_list()
        self._refresh_summary()

    def _add_dialogue(self):
        text = self.dialogue_text.toPlainText().strip()
        if not text:
            QtWidgets.QMessageBox.warning(self, "Dialogue", "Enter dialogue text first.")
            return
        self.events.append({
            "time": float(self.dialogue_time.value()),
            "type": "dialogue",
            "duration": float(self.dialogue_duration.value()),
            "speaker_id": self.dialogue_speaker.currentData() or "",
            "text": text,
        })
        self._refresh_event_list()
        self._refresh_summary()

    def _add_message(self):
        text = self.message_text.text()
        self.events.append({
            "time": float(self.message_time.value()),
            "type": "message",
            "line": self.message_line.currentText(),
            "text": text,
        })
        self._refresh_event_list()
        self._refresh_summary()

    def _remove_event(self):
        row = self.event_list.currentRow()
        if 0 <= row < len(self.events):
            # event list is sorted for display, so map the selected display
            # row back to the actual event object rather than assuming storage
            # order matches display order.
            ordered = sorted(
                enumerate(self.events), key=lambda item: item[1].get("time", 0)
            )
            index = ordered[row][0]
            self.events.pop(index)
            self._refresh_event_list()
            self._refresh_summary()

    def _refresh_event_list(self):
        self.event_list.clear()
        for event in sorted(self.events, key=lambda x: x.get("time", 0)):
            kind = event["type"]
            label = f"{event['time']:.2f}s — {kind}"
            if kind == "fight":
                label += f" ({len(event.get('attackers', []))} vs {len(event.get('defenders', []))})"
            elif kind == "dialogue":
                label += f" — {str(event.get('text', ''))[:45]}"
            elif kind == "message":
                label += f" — {event.get('line', 'message')}: {str(event.get('text', ''))[:45]}"
            elif kind == "io":
                label += f" — {event.get('source_name', event.get('source_id', ''))}.{event.get('output', '')}"
            self.event_list.addItem(label)

    # ------------------------------------------------------------------
    # Refresh / save / cleanup.
    # ------------------------------------------------------------------
    def _refresh_summary(self):
        self.summary.setText(
            f"Actors {len(self.actor_meta)}  |  "
            f"Waypoints {sum(len(v) for v in self.actor_tracks.values())}  |  "
            f"Camera {len(self.camera_keys)}  |  Events {len(self.events)}"
        )

    def _filename(self):
        name = self.filename.text().strip() or self.name.text().strip() or "cutscene"
        if not name.lower().endswith(".json"):
            name += ".json"
        return name.split("/")[-1].split("\\")[-1]

    def _actor_definition(self, aid):
        actor = self.actor_objects.get(aid)
        if actor is None:
            return None
        props = {
            key: value
            for key, value in dict(getattr(actor, "properties", {})).items()
            if key != "_io_connections" and not str(key).startswith("_cutscene_")
        }
        props["id"] = str(aid)
        return {
            "type": str(props.get("type") or "monster"),
            "pos": _v3(actor.pos),
            "yaw": float(getattr(actor, "angle", 0.0)),
            "properties": props,
        }

    def _delete_temporary_actors(self):
        if self._cleaned:
            return
        for aid in list(self.temporary_actor_ids):
            actor = self.actor_objects.get(aid)
            if actor is None:
                continue
            try:
                self.main_window.state.things.remove(actor)
            except ValueError:
                pass
        self.temporary_actor_ids.clear()
        self._cleaned = True
        try:
            selected = getattr(self.main_window.state, "selected_object", None)
            if selected is not None and bool(
                getattr(selected, "properties", {}).get("_cutscene_temporary")
            ):
                self.main_window.set_selected_object(None)
        except Exception:
            pass
        try:
            self.main_window.update_all_ui()
        except Exception:
            pass

    def _cleanup_after_cancel(self):
        # Temporary actors are authoring state, not a map edit.  Do not touch
        # the editor's dirty flag here: the user may have made unrelated map
        # edits while this modeless panel was open.
        self._delete_temporary_actors()

    def _current_map_name(self):
        path = getattr(self.main_window, "file_path", "") or ""
        return Path(path).name if path else ""

    def _find_map_actor(self, aid):
        aid = str(aid or "")
        if not aid:
            return None
        for obj in getattr(self.main_window.state, "things", []) or []:
            if str(getattr(obj, "properties", {}).get("id", "")) == aid:
                return obj
        return None

    def _existing_logic_camera(self, cutscene_file):
        wanted = str(cutscene_file or "").replace("\\", "/")
        for obj in getattr(self.main_window.state, "things", []) or []:
            if obj.__class__.__name__ != "LogicCamera":
                continue
            current = str(getattr(obj, "properties", {}).get("cutscene_file", "") or "").replace("\\", "/")
            if current == wanted:
                return obj
        return None

    def _load_first_cutscene(self):
        cutscene_dir = Path(getattr(self.main_window, "root_dir", ".")) / CUTSCENE_DIR
        try:
            candidates = sorted(path for path in cutscene_dir.glob("*.json") if path.is_file())
        except OSError:
            candidates = []
        if candidates:
            self._load_cutscene(str(candidates[0]))

    def _load_cutscene(self, filename=None):
        if filename is None:
            start_dir = Path(getattr(self.main_window, "root_dir", ".")) / CUTSCENE_DIR
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Load Cutscene", str(start_dir),
                "Cutscene JSON (*.json);;All files (*)",
            )
            if not filename:
                return
        try:
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Load failed", f"The cutscene could not be read:\n{exc}")
            return
        if not isinstance(data, dict) or not isinstance(data.get("camera"), list):
            QtWidgets.QMessageBox.warning(
                self, "Invalid cutscene",
                "This file is not a valid Fio cutscene (camera keyframes are missing).",
            )
            return

        saved_map = str((data.get("map") or {}).get("file", "") or "")
        current_map = self._current_map_name()
        if saved_map and current_map and saved_map.lower() != current_map.lower():
            result = QtWidgets.QMessageBox.question(
                self, "Different map",
                f"This cutscene was created for '{saved_map}', but the current map is "
                f"'{current_map}'.\n\nLoad it into the current map anyway?",
            )
            if result != QtWidgets.QMessageBox.Yes:
                return

        self._preview_stop_and_restore()
        self._delete_temporary_actors()
        # _delete_temporary_actors() marks the previous authoring session as
        # cleaned. Loading a new cutscene starts a fresh authoring session, so
        # its recreated spawn actors must be eligible for cleanup on Apply/Cancel.
        self._cleaned = False
        self.actor_meta.clear()
        self.actor_objects.clear()
        self.temporary_actor_ids.clear()
        self.actor_tracks = {
            str(aid): list(rows or [])
            for aid, rows in (data.get("actor_tracks") or {}).items()
            if isinstance(rows, list)
        }
        self.camera_keys = list(data.get("camera") or [])
        self.events = list(data.get("events") or [])
        self.name.setText(str(data.get("name") or "Cutscene"))
        self.filename.setText(Path(filename).name)

        settings = data.get("settings") or {}
        self.restore.setChecked(bool(settings.get("restore_actors", True)))
        self.stop_escape.setChecked(bool(settings.get("stop_on_escape", True)))
        trigger = self.trigger_mode.findData(settings.get("trigger_mode", "manual"))
        self.trigger_mode.setCurrentIndex(trigger if trigger >= 0 else 0)
        self.radius.setValue(float(settings.get("trigger_radius", 180.0)))
        self.once.setChecked(bool(settings.get("trigger_once", True)))

        for row in data.get("actors") or []:
            if not isinstance(row, dict):
                continue
            aid = str(row.get("id", "") or "")
            if not aid:
                continue
            name = str(row.get("name") or aid)
            definition = row.get("definition") if row.get("spawn") else None
            actor = self._find_map_actor(aid)
            if actor is None and isinstance(definition, dict):
                try:
                    from .things import Monster
                    actor_props = dict(definition.get("properties") or {})
                    actor_props["id"] = aid
                    actor_props["_cutscene_temporary"] = True
                    actor = Monster(
                        pos=definition.get("pos") or [0.0, 0.0, 0.0],
                        properties=actor_props,
                    )
                    actor.angle = float(definition.get("yaw", 0.0))
                    self.main_window.state.things.append(actor)
                    self.actor_objects[aid] = actor
                    self.temporary_actor_ids.add(aid)
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(
                        self, "Actor load failed",
                        f"Could not recreate actor '{name}': {exc}",
                    )
                    actor = None
            if actor is not None:
                self.actor_objects[aid] = actor
                if aid in self.temporary_actor_ids:
                    actor.properties["_cutscene_temporary"] = True
            self.actor_meta[aid] = {
                "id": aid, "name": name, "spawn": bool(row.get("spawn")),
            }

        self._refresh_actor_lists()
        self._refresh_actor_keys_list()
        self._refresh_camera_list()
        self._refresh_event_list()
        self._refresh_summary()
        self.main_window.update_all_ui()
        self.main_window.show_toast(f"Loaded cutscene {Path(filename).name}")

    def _apply_logic_camera(self, filename):
        from .things import LogicCamera
        cutscene_file = str(Path(CUTSCENE_DIR) / Path(filename).name).replace("\\", "/")
        camera = self._existing_logic_camera(cutscene_file)
        if self.camera_keys:
            pos = list(self.camera_keys[0].get("pos", [0.0, 0.0, 0.0]))
        else:
            pos = _v3(self.main_window.view_3d.camera.pos)
        props = {
            "type": "logic_camera",
            "id": str(camera.properties.get("id")) if camera is not None else str(uuid.uuid4()),
            "name": self.name.text().strip() or "Cutscene Camera",
            "cutscene_file": cutscene_file,
            "cutscene_trigger_mode": self.trigger_mode.currentData(),
            "cutscene_trigger_radius": float(self.radius.value()),
            "cutscene_once": self.once.isChecked(),
            "cutscene_restore_actors": self.restore.isChecked(),
            "cutscene_stop_on_escape": self.stop_escape.isChecked(),
            "cutscene_io_events": [dict(e) for e in self.events if e.get("type") == "io"],
        }
        if camera is None:
            camera = LogicCamera(pos=pos, properties=props)
            self.main_window.state.things.append(camera)
        else:
            camera.pos = glm.vec3(*pos)
            camera.properties.update(props)
        self._delete_temporary_actors()
        self.main_window.state.save_state()
        self.main_window.set_selected_object(camera)
        self.main_window.unsaved_changes = True
        self.main_window.update_all_ui()
        self._saved = True
        self.main_window.show_toast(f"Applied cutscene {Path(filename).name} to current map")
        return camera

    def _build_cutscene_data(self):
        actors = []
        for aid, meta in self.actor_meta.items():
            row = {"id": aid, "name": meta["name"]}
            if meta.get("spawn") or aid in self.temporary_actor_ids:
                definition = self._actor_definition(aid)
                if definition is not None:
                    row["spawn"] = True
                    row["definition"] = definition
            actors.append(row)
        return {
            "version": 2,
            "id": str(uuid.uuid4()),
            "name": self.name.text().strip() or "Cutscene",
            "actors": actors,
            "camera": self.camera_keys,
            "actor_tracks": self.actor_tracks,
            "events": sorted(self.events, key=lambda x: x.get("time", 0)),
            "map": {"file": self._current_map_name()},
            "settings": {
                "restore_actors": self.restore.isChecked(),
                "stop_on_escape": self.stop_escape.isChecked(),
                "trigger_mode": self.trigger_mode.currentData(),
                "trigger_radius": float(self.radius.value()),
                "trigger_once": self.once.isChecked(),
            },
        }

    def _validate_for_save(self):
        self._preview_stop_and_restore()
        if not self.camera_keys:
            QtWidgets.QMessageBox.warning(
                self, "No camera keyframes", "Capture at least one camera position."
            )
            return None
        if not self.actor_meta and not self.events:
            QtWidgets.QMessageBox.warning(
                self, "Empty cutscene",
                "Create/capture at least one actor or add a timed event."
            )
            return None
        return self._filename()

    def _write_cutscene(self, filename, prompt_overwrite=False):
        path = Path(getattr(self.main_window, "root_dir", ".")) / CUTSCENE_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if prompt_overwrite and path.exists():
            result = QtWidgets.QMessageBox.question(
                self, "Overwrite cutscene",
                f"{filename} already exists. Replace it?"
            )
            if result != QtWidgets.QMessageBox.Yes:
                return False
        try:
            path.write_text(
                json.dumps(self._build_cutscene_data(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self, "Save failed", f"The cutscene JSON could not be written:\n{exc}"
            )
            return False
        return True

    def _save_and_close(self):
        filename = self._validate_for_save()
        if not filename:
            return
        if not self._write_cutscene(filename, prompt_overwrite=True):
            return
        self._saved = True
        self._delete_temporary_actors()
        self.main_window.show_toast(f"Saved cutscene {filename}")
        super().accept()

    def _apply_to_map(self):
        filename = self._validate_for_save()
        if not filename:
            return
        # Apply must update the JSON as well: LogicCamera stores a filename,
        # so applying stale on-disk data would make the map reference an older
        # version of the cutscene being authored.
        if not self._write_cutscene(filename, prompt_overwrite=False):
            return
        self._apply_logic_camera(filename)

    def reject(self):
        self._preview_stop_and_restore()
        self._cleanup_after_cancel()
        super().reject()

    def closeEvent(self, event):
        self._preview_stop_and_restore()
        if not self._saved:
            self._cleanup_after_cancel()
        super().closeEvent(event)


# Backwards-compatible class name retained for MainWindow.open_cutscene_wizard.
CutsceneWizard = CutsceneWizard
