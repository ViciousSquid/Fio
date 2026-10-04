"""Cutscene playback state machine.

The LogicThread owns the simulation loop; this module owns cinematic playback state,
track sampling, temporary actor lifetime, timed I/O and restoration.
"""

import json
import math
import os
import queue

import numpy as np

try:
    from editor.things import ENTITY_TYPES, Monster as MonsterThing
except ImportError:
    ENTITY_TYPES = {}
    MonsterThing = None

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


class CutsceneRuntime:
    """Owns the cutscene state machine while LogicThread remains the orchestrator."""

    def __init__(self, logic):
        self.logic = logic
        self.state = None
        self._message_queue = queue.SimpleQueue()

    def _cutscene_file_path(self, filename):
        """Resolve an authored cutscene path without allowing it outside cutscenes/."""
        raw = str(filename or "").strip().replace("\\", "/")
        if not raw:
            return None
        project_root = os.path.realpath(
            getattr(self.logic, "root_dir", os.path.dirname(os.path.dirname(__file__)))
        )
        root = os.path.realpath(os.path.join(project_root, "cutscenes"))
        candidate = os.path.realpath(os.path.join(project_root, raw))
        try:
            inside = os.path.commonpath((root, candidate)) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(candidate):
            return None
        return candidate

    def _load_cutscene_file(self, filename):
        path = self._cutscene_file_path(filename)
        if path is None:
            debug_log("Cutscene", f"FAILED: '{filename}' not found or outside cutscenes/")
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            debug_log("Cutscene", f"FAILED: '{filename}' could not be loaded: {exc}")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("camera"), list):
            debug_log("Cutscene", f"FAILED: '{filename}' is invalid (camera keyframes missing)")
            return None
        debug_log("Cutscene", f"LOADED: '{filename}'")
        return data

    @staticmethod
    def _cutscene_number(value, default=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(value):
            return float(default)
        return value

    @staticmethod
    def _cutscene_vec3(value, default=None):
        if default is None:
            default = [0.0, 0.0, 0.0]
        try:
            if len(value) != 3:
                raise ValueError
            result = [float(value[0]), float(value[1]), float(value[2])]
            if not all(math.isfinite(v) for v in result):
                raise ValueError
            return result
        except (TypeError, ValueError, IndexError):
            return list(default)

    @staticmethod
    def _cutscene_yaw(entity):
        return float(getattr(entity, "angle", entity.properties.get("yaw", 0.0)))

    @staticmethod
    def _set_cutscene_yaw(entity, yaw):
        yaw = float(yaw)
        if hasattr(entity, "angle"):
            entity.angle = yaw
        else:
            entity.properties["yaw"] = yaw

    @staticmethod
    def _cutscene_lerp_angle(a, b, t):
        delta = ((float(b) - float(a) + math.pi) % (2.0 * math.pi)) - math.pi
        return float(a) + delta * t

    @staticmethod
    def _cutscene_sample(rows, elapsed, initial_pos=None, initial_yaw=0.0):
        """Sample one actor/camera track with linear interpolation."""
        valid = [row for row in rows if isinstance(row, dict)]
        if not valid:
            return None
        valid.sort(key=lambda row: CutsceneRuntime._cutscene_number(row.get("time", 0.0)))
        first_time = CutsceneRuntime._cutscene_number(valid[0].get("time", 0.0))
        if elapsed < first_time and initial_pos is not None:
            return {"pos": list(initial_pos), "yaw": float(initial_yaw), "_before": True}
        if elapsed <= first_time:
            return valid[0]
        for index in range(1, len(valid)):
            right = valid[index]
            left = valid[index - 1]
            right_time = CutsceneRuntime._cutscene_number(right.get("time", 0.0))
            left_time = CutsceneRuntime._cutscene_number(left.get("time", 0.0))
            if elapsed <= right_time:
                span = right_time - left_time
                t = 1.0 if span <= 1e-9 else max(0.0, min(1.0, (elapsed - left_time) / span))
                lp = CutsceneRuntime._cutscene_vec3(left.get("pos", [0, 0, 0]))
                rp = CutsceneRuntime._cutscene_vec3(right.get("pos", lp), lp)

                # A teleport belongs to the destination keyframe.  Keep the
                # previous shot completely unchanged until its timestamp, then
                # cut to the new location.  This makes large cross-map jumps
                # explicit rather than turning them into a very fast move.
                if right.get("teleport", False):
                    if elapsed < right_time:
                        return {
                            "pos": list(lp),
                            "yaw": CutsceneRuntime._cutscene_number(left.get("yaw", 0.0)),
                            "pitch": CutsceneRuntime._cutscene_number(left.get("pitch", 0.0)),
                            "fov": CutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0),
                            "look_at": left.get("look_at"),
                        }
                    return {
                        "pos": list(rp),
                        "yaw": CutsceneRuntime._cutscene_number(right.get("yaw", 0.0)),
                        "pitch": CutsceneRuntime._cutscene_number(right.get("pitch", 0.0)),
                        "fov": CutsceneRuntime._cutscene_number(right.get("fov", 90.0), 90.0),
                        "look_at": right.get("look_at"),
                    }

                return {
                    "pos": [lp[i] + (rp[i] - lp[i]) * t for i in range(3)],
                    "yaw": CutsceneRuntime._cutscene_lerp_angle(
                        CutsceneRuntime._cutscene_number(left.get("yaw", 0.0)),
                        CutsceneRuntime._cutscene_number(right.get("yaw", 0.0)),
                        t,
                    ),
                    "pitch": (
                        CutsceneRuntime._cutscene_number(left.get("pitch", 0.0))
                        + (CutsceneRuntime._cutscene_number(right.get("pitch", 0.0))
                           - CutsceneRuntime._cutscene_number(left.get("pitch", 0.0))) * t
                    ),
                    "fov": (
                        CutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0)
                        + (CutsceneRuntime._cutscene_number(right.get("fov", 90.0), 90.0)
                           - CutsceneRuntime._cutscene_number(left.get("fov", 90.0), 90.0)) * t
                    ),
                    "look_at": left.get("look_at"),
                }
        return valid[-1]

    def _start_json_cutscene(self, entity, filename, data):
        previous = self.state
        if previous is not None:
            if previous.get("json_cutscene"):
                self._finish_json_cutscene(previous, fire_finished=False)
            else:
                self.state = None
        actors = {}
        actor_initial = {}
        spawned = []
        for row in data.get("actors") or []:
            if not isinstance(row, dict):
                continue
            aid = str(row.get("id", "") or "")
            if not aid:
                continue
            actor = self.logic.world_runtime.find_entity_by_id(aid)
            definition = row.get("definition") if row.get("spawn") else None
            if actor is None and isinstance(definition, dict):
                type_name = str(definition.get("type") or "monster")
                cls = ENTITY_TYPES.get(type_name)
                if cls is None:
                    cls = next((value for key, value in ENTITY_TYPES.items()
                                if str(key).lower() == type_name.lower()), None)
                if cls is None:
                    debug_log("IO", f"LogicCamera: cannot spawn unknown cutscene actor type '{type_name}'")
                    continue
                props = dict(definition.get("properties") or {})
                props["id"] = aid
                props["_cutscene_runtime"] = True
                try:
                    actor = cls(
                        pos=self._cutscene_vec3(definition.get("pos")),
                        properties=props,
                    )
                    self._set_cutscene_yaw(actor, self._cutscene_number(definition.get("yaw", 0.0)))
                    self.logic.editor_state.things.append(actor)
                    spawned.append(actor)
                except Exception as exc:
                    debug_log("IO", f"LogicCamera: failed to spawn cutscene actor '{aid}': {exc}")
                    continue
            if actor is None:
                debug_log("IO", f"LogicCamera: actor '{aid}' is not present; its track will be ignored")
                continue
            actor_initial[aid] = {
                "entity": actor,
                "pos": self._cutscene_vec3(actor.pos),
                "yaw": self._cutscene_yaw(actor),
                "had_disabled": "disabled" in actor.properties,
                "disabled": actor.properties.get("disabled", False),
            }
            actor.properties["_cutscene_runtime"] = True
            actor.properties["disabled"] = True
            actors[aid] = actor

        if spawned:
            self.logic.world_runtime.build_entity_caches()

        # The editor Camera stores yaw/pitch in degrees, while the play-mode
        # camera math below uses radians.  Keep the authored JSON human-readable
        # in degrees and convert the camera track once at runtime.  Converting
        # here (rather than every tick) also keeps interpolation in one unit.
        camera_rows = []
        for source_row in data.get("camera") or []:
            if not isinstance(source_row, dict):
                continue
            row = dict(source_row)
            row["yaw"] = math.radians(
                self._cutscene_number(row.get("yaw", 0.0))
            )
            row["pitch"] = math.radians(
                self._cutscene_number(row.get("pitch", 0.0))
            )
            camera_rows.append(row)
        camera_rows.sort(key=lambda row: self._cutscene_number(row.get("time", 0.0)))
        actor_tracks = {
            str(aid): sorted(
                [row for row in rows or [] if isinstance(row, dict)],
                key=lambda row: self._cutscene_number(row.get("time", 0.0)),
            )
            for aid, rows in (data.get("actor_tracks") or {}).items()
            if isinstance(rows, list)
        }
        events = [event for event in (data.get("events") or []) if isinstance(event, dict)]
        # Preserve the two cutscene I/O schemas:
        #
        #   New editor authoring: target_id/target_name + input
        #   Legacy cutscenes:     source_id/source_name + output
        #
        # They are not interchangeable.  The former executes an input directly
        # on the target; the latter fires an output from the source and therefore
        # traverses the map's authored I/O connections.
        events.sort(key=lambda event: self._cutscene_number(event.get("time", 0.0)))
        duration = 0.0
        for row in camera_rows:
            duration = max(duration, self._cutscene_number(row.get("time", 0.0)))
        for rows in actor_tracks.values():
            for row in rows:
                duration = max(duration, self._cutscene_number(row.get("time", 0.0)))
        for event in events:
            duration = max(duration, self._cutscene_number(event.get("time", 0.0)))
            if event.get("type") == "fight":
                duration = max(
                    duration,
                    self._cutscene_number(event.get("time", 0.0))
                    + max(0.0, self._cutscene_number(event.get("duration", 0.0))),
                )

        # A single keyframe at t=0 is still a real cutscene state.  Keep it
        # alive for one logical tick so LookAt/message/I/O authoring can observe
        # that state before the runtime finishes an otherwise zero-duration shot.
        if camera_rows and duration <= 0.0:
            duration = 1e-6

        settings = data.get("settings") or {}
        self.state = {
            "active": True,
            "paused": False,
            "entity": entity,
            "json_cutscene": True,
            "cutscene_file": str(filename),
            "camera_keys": camera_rows,
            "actor_tracks": actor_tracks,
            "actors": actors,
            "actor_initial": actor_initial,
            "spawned_actors": spawned,
            "restore_actors": bool(
                settings.get("restore_actors",
                             entity.properties.get("cutscene_restore_actors", True))
            ),
            "elapsed": 0.0,
            "duration": duration,
            "io_events": [event for event in events if event.get("type") == "io"],
            "next_io_event": 0,
            "message_events": [event for event in events if event.get("type") == "message"],
            "next_message_event": 0,
            "fight_events": [
                event for event in events
                if event.get("type") == "fight"
            ],
            "active_fights": {},
        }
        if camera_rows:
            first = camera_rows[0]
            self.state["cam_pos"] = self._cutscene_vec3(first.get("pos"))
            self.state["cam_angle"] = self._cutscene_number(first.get("yaw", 0.0))
            self.state["cam_pitch"] = self._cutscene_number(first.get("pitch", 0.0))
            self.state["fov"] = max(
                1.0,
                min(179.0, self._cutscene_number(first.get("fov", 90.0), 90.0)),
            )

        return True

    def _restore_json_fights(self, cs):
        for snapshots in (cs.get("active_fights") or {}).values():
            for actor, snapshot in snapshots:
                if actor not in self.logic.editor_state.things:
                    continue
                if snapshot.get("had_disabled"):
                    actor.properties["disabled"] = snapshot["disabled"]
                else:
                    actor.properties.pop("disabled", None)
                if snapshot.get("had_awake"):
                    actor.properties["awake"] = snapshot["awake"]
                else:
                    actor.properties.pop("awake", None)
                if snapshot.get("had_aggro"):
                    actor.properties["_aggro_target"] = snapshot["aggro"]
                else:
                    actor.properties.pop("_aggro_target", None)
                if snapshot.get("had_target_name"):
                    actor.properties["target_name"] = snapshot["target_name"]
                else:
                    actor.properties.pop("target_name", None)
        cs["active_fights"] = {}

    def _update_json_fights(self, cs, elapsed):
        """Temporarily hand fight participants to the native MonsterAI."""
        for index, event in enumerate(cs.get("fight_events") or []):
            start = self._cutscene_number(event.get("time", 0.0))
            duration = max(0.0, self._cutscene_number(event.get("duration", 0.0)))
            end = start + duration
            active = cs.setdefault("active_fights", {}).get(index)

            if start <= elapsed < end and active is None:
                snapshots = []
                defenders = [
                    (cs.get("actors") or {}).get(str(aid))
                    or self.logic.world_runtime.find_entity_by_id(str(aid))
                    for aid in event.get("defenders", []) or []
                ]
                defenders = [thing for thing in defenders if thing is not None]
                if not defenders:
                    continue
                target = defenders[0]
                for aid in event.get("attackers", []) or []:
                    actor = (
                        (cs.get("actors") or {}).get(str(aid))
                        or self.logic.world_runtime.find_entity_by_id(str(aid))
                    )
                    if actor is None or not (
                        MonsterThing is not None and isinstance(actor, MonsterThing)
                    ):
                        continue
                    props = actor.properties
                    snapshot = {
                        "had_disabled": "disabled" in props,
                        "disabled": props.get("disabled", False),
                        "had_awake": "awake" in props,
                        "awake": props.get("awake", False),
                        "had_aggro": "_aggro_target" in props,
                        "aggro": props.get("_aggro_target"),
                        "had_target_name": "target_name" in props,
                        "target_name": props.get("target_name"),
                    }
                    snapshots.append((actor, snapshot))
                    props["disabled"] = False
                    props["awake"] = True
                    props["_aggro_target"] = id(target)
                    props.pop("target_name", None)
                cs["active_fights"][index] = snapshots

            elif active is not None and elapsed >= end:
                for actor, snapshot in active:
                    props = actor.properties
                    if snapshot.get("had_disabled"):
                        props["disabled"] = snapshot["disabled"]
                    else:
                        props.pop("disabled", None)
                    if snapshot.get("had_awake"):
                        props["awake"] = snapshot["awake"]
                    else:
                        props.pop("awake", None)
                    if snapshot.get("had_aggro"):
                        props["_aggro_target"] = snapshot["aggro"]
                    else:
                        props.pop("_aggro_target", None)
                    if snapshot.get("had_target_name"):
                        props["target_name"] = snapshot["target_name"]
                    else:
                        props.pop("target_name", None)
                cs["active_fights"].pop(index, None)

    def _finish_json_cutscene(self, cs, fire_finished=True):
        self._restore_json_fights(cs)
        for aid, snapshot in (cs.get("actor_initial") or {}).items():
            actor = snapshot.get("entity")
            if actor is None or actor not in self.logic.editor_state.things:
                continue
            if cs.get("restore_actors", True):
                actor.pos = list(snapshot.get("pos", actor.pos))
                self._set_cutscene_yaw(
                    actor, snapshot.get("yaw", self._cutscene_yaw(actor))
                )
            if snapshot.get("had_disabled"):
                actor.properties["disabled"] = snapshot["disabled"]
            else:
                actor.properties.pop("disabled", None)
            actor.properties.pop("_cutscene_runtime", None)
        spawned = list(cs.get("spawned_actors") or [])
        for actor in spawned:
            try:
                self.logic.editor_state.things.remove(actor)
            except ValueError:
                pass
        if spawned:
            self.logic.world_runtime.build_entity_caches()
        entity = cs.get("entity")
        self.state = None
        if fire_finished and entity is not None and self.logic.io_manager:
            self.logic.io_manager.fire_output(entity, "OnFinished")

    def _update_json_cutscene(self, delta):
        cs = self.state
        cs["elapsed"] = float(cs.get("elapsed", 0.0)) + max(0.0, float(delta))
        elapsed = cs["elapsed"]
        camera_keys = cs.get("camera_keys") or []
        if camera_keys:
            frame = self._cutscene_sample(camera_keys, elapsed)
            if frame:
                cs["cam_pos"] = self._cutscene_vec3(frame.get("pos"))
                cs["cam_angle"] = self._cutscene_number(frame.get("yaw", 0.0))
                cs["cam_pitch"] = self._cutscene_number(frame.get("pitch", 0.0))
                cs["fov"] = max(
                    1.0,
                    min(179.0, self._cutscene_number(frame.get("fov", 90.0), 90.0)),
                )
                look_at = frame.get("look_at")
                if isinstance(look_at, dict):
                    target_id = str(look_at.get("actor", "") or "")
                    target = (cs.get("actors") or {}).get(target_id)
                    if target is None:
                        target = self.logic.world_runtime.find_entity_by_id(target_id)
                    if target is not None:
                        target_pos = self._cutscene_vec3(target.pos)
                        diff = np.asarray(target_pos, dtype=float) - np.asarray(cs["cam_pos"], dtype=float)
                        dist = np.linalg.norm(diff)
                        if dist > 0.01:
                            cs["cam_angle"] = math.atan2(diff[0], diff[2])
                            cs["cam_pitch"] = math.asin(np.clip(diff[1] / dist, -1.0, 1.0))

        self._update_json_fights(cs, elapsed)

        for aid, rows in (cs.get("actor_tracks") or {}).items():
            actor = (cs.get("actors") or {}).get(aid)
            if actor is None:
                continue
            snapshot = (cs.get("actor_initial") or {}).get(aid, {})
            frame = self._cutscene_sample(
                rows,
                elapsed,
                initial_pos=snapshot.get("pos"),
                initial_yaw=snapshot.get("yaw", 0.0),
            )
            if frame and not frame.get("_before"):
                actor.pos = self._cutscene_vec3(frame.get("pos"), actor.pos)
                self._set_cutscene_yaw(
                    actor, self._cutscene_number(frame.get("yaw", 0.0))
                )
                physics_world = getattr(self.logic, "_physics_world", None)
                if physics_world is not None:
                    try:
                        physics_world.sync_entity_position(actor, wake=True)
                    except (AttributeError, TypeError, ValueError):
                        pass

        message_events = cs.get("message_events", [])
        message_index = int(cs.get("next_message_event", 0))
        while message_index < len(message_events) and float(message_events[message_index].get("time", 0.0)) <= elapsed + 1e-9:
            event = message_events[message_index]
            message_index += 1
            cs["next_message_event"] = message_index
            text = str(event.get("text", "") or "").strip()[:50]
            if text:
                line = str(event.get("line", "message") or "message").strip().lower()
                if line not in ("message", "message2", "message3"):
                    line = "message"
                self._message_queue.put((line, text))

        if not self._fire_cinematic_io_events():
            return
        if elapsed >= float(cs.get("duration", 0.0)):
            self._finish_json_cutscene(cs)

    def consume_cinematic_messages(self):
        """Return queued cutscene HUD messages for the GUI thread."""
        messages = []
        while True:
            try:
                messages.append(self._message_queue.get_nowait())
            except queue.Empty:
                return messages

    def _fire_cinematic_io_events(self):
        """Fire timed cutscene I/O events, accepting both authored schemas."""
        cs = self.state
        if not cs or not cs.get('active') or not self.logic.io_manager:
            return bool(cs and cs.get('active'))

        events = cs.get('io_events', [])
        index = int(cs.get('next_io_event', 0))
        elapsed = float(cs.get('elapsed', 0.0))
        while index < len(events) and float(events[index].get('time', 0.0)) <= elapsed + 1e-9:
            event = events[index]
            index += 1
            cs['next_io_event'] = index

            # New editor format: address an entity input directly.
            target_id = str(event.get('target_id', '') or '')
            target_name = str(event.get('target_name', '') or '')
            input_name = str(event.get('input', '') or '').strip()
            if target_name or target_id or input_name:
                if not target_name and target_id:
                    target = self.logic.world_runtime.find_entity_by_id(target_id)
                    target_name = str(
                        getattr(target, 'properties', {}).get('name', '') or target_id
                    )
                if target_name and input_name:
                    execute_input = getattr(self.logic.io_manager, '_execute_input', None)
                    if execute_input is None:
                        debug_log(
                            "IO",
                            f"Cutscene I/O target '{target_name}' cannot execute input "
                            f"'{input_name}': IO manager has no input executor.",
                        )
                    else:
                        execute_input(
                            target_name,
                            input_name,
                            event.get('parameter'),
                            "Cutscene",
                            target_id=target_id,
                        )
                else:
                    debug_log(
                        "IO",
                        f"Cutscene I/O target '{target_name or target_id}' has no input to fire.",
                    )
            else:
                # Legacy format: fire a source output so the map's normal
                # connection graph resolves the downstream input.
                source_id = str(event.get('source_id', '') or '')
                source_name = str(event.get('source_name', '') or '')
                output_name = str(event.get('output', '') or '').strip()
                source = None
                if source_id:
                    source = self.logic.world_runtime.find_entity_by_id(source_id)
                if source is None and source_name:
                    source = self.logic.world_runtime.find_entity_by_name(source_name)
                if source is not None and output_name:
                    self.logic.io_manager.fire_output(
                        source,
                        output_name,
                        event.get('parameter'),
                    )
                else:
                    debug_log(
                        "IO",
                        f"Cutscene legacy I/O source '{source_name or source_id}' "
                        f"has no output to fire.",
                    )

            # An input/output may stop, replace or otherwise mutate the cinematic.
            if self.state is not cs:
                return False
        return True

    def _update_cinematic_camera(self, delta: float):
        cs = self.state
        if not cs or not cs.get('active') or cs.get('paused'):
            return

        if cs.get('json_cutscene'):
            self._update_json_cutscene(delta)
            return

        cs['elapsed'] = float(cs.get('elapsed', 0.0)) + max(0.0, float(delta))
        if not self._fire_cinematic_io_events():
            return
        node_name = cs['current_node']
        node = self.logic.world_runtime.find_path_node_by_name(node_name)
        if node is None:
            debug_log("IO", f"CinematicCamera: node '{node_name}' not found — aborting")
            entity = cs.get('entity')
            self.state = None
            if entity and self.logic.io_manager:
                self.logic.io_manager.fire_output(entity, 'OnFinished')
            return

        origin = np.array(cs['origin'], dtype=float)
        target = np.array(node.pos, dtype=float)
        segment_vec = target - origin
        segment_len = np.linalg.norm(segment_vec)

        if segment_len < 1.0:
            cs['lerp_t'] = 1.0
        else:
            cs['lerp_t'] += (cs['speed'] * delta) / segment_len

        t = min(cs['lerp_t'], 1.0)
        current_pos = origin + segment_vec * t

        cs['cam_pos'] = current_pos.tolist()

        # Normal path-facing target.  With "look ahead" enabled this is the
        # next node; otherwise it is the node currently being approached.
        if cs.get('look_ahead'):
            next_name = node.get_next_node_name()
            look_node = self.logic.world_runtime.find_path_node_by_name(next_name) if next_name else node
            path_look_target = np.array(
                look_node.pos if look_node else node.pos,
                dtype=float,
            )
        else:
            path_look_target = target

        look_target = path_look_target

        # An explicit LookAt temporarily overrides the path target.  The live
        # entity is retained so moving targets are tracked automatically.
        focus_target = cs.get('lookat_target')
        if focus_target is not None:
            if isinstance(focus_target, dict):
                focus_pos = focus_target.get('pos')
            else:
                focus_pos = getattr(focus_target, 'pos', None)
            try:
                if focus_pos is not None:
                    look_target = np.asarray(focus_pos, dtype=float)
                else:
                    cs['lookat_target'] = None
                    cs['lookat_return_remaining'] = None
            except (TypeError, ValueError):
                cs['lookat_target'] = None
                cs['lookat_return_remaining'] = None

        # Return from LookAt is timed in the camera's logic clock, so pausing
        # the cinematic camera also pauses the focus timer.
        if cs.get('lookat_target') is not None:
            remaining = cs.get('lookat_return_remaining')
            if remaining is not None:
                remaining -= max(0.0, float(delta))
                if remaining <= 0.0:
                    cs['lookat_target'] = None
                    cs['lookat_return_remaining'] = None
                    look_target = path_look_target
                else:
                    cs['lookat_return_remaining'] = remaining

        diff = look_target - current_pos
        dist = np.linalg.norm(diff)
        if dist > 0.01:
            desired_angle = math.atan2(diff[0], diff[2])
            desired_pitch = math.asin(np.clip(diff[1] / dist, -1.0, 1.0))

            if not cs.get('_look_initialized', False):
                cs['cam_angle'] = desired_angle
                cs['cam_pitch'] = desired_pitch
                cs['_look_initialized'] = True
            else:
                # Exponential smoothing is frame-rate independent and removes
                # the hard bearing jump at each PathNode boundary.
                alpha = 1.0 - math.exp(-8.0 * max(0.0, float(delta)))
                current_angle = cs.get('cam_angle', desired_angle)
                angle_delta = (
                    (desired_angle - current_angle + math.pi)
                    % (2.0 * math.pi)
                ) - math.pi
                cs['cam_angle'] = current_angle + angle_delta * alpha
                current_pitch = cs.get('cam_pitch', desired_pitch)
                cs['cam_pitch'] = (
                    current_pitch + (desired_pitch - current_pitch) * alpha
                )

        if cs['lerp_t'] >= 1.0:
            # PathNodes are real I/O sources for cinematic camera arrival.
            # Fire the node first so it can drive arbitrary I/O, including
            # stopping or replacing this camera.
            if self.logic.io_manager:
                self.logic.io_manager.fire_output(node, 'OnCameraArrived')
                self.logic.io_manager.fire_output(cs['entity'], 'OnReachNode')

            # Arrival outputs may mutate the cinematic state.
            if self.state is not cs:
                return

            next_name = node.get_next_node_name()
            if next_name:
                cs['origin'] = list(node.pos)
                cs['current_node'] = next_name
                cs['lerp_t'] = 0.0
            else:
                entity = cs['entity']
                self.state = None
                if self.logic.io_manager:
                    self.logic.io_manager.fire_output(entity, 'OnFinished')


