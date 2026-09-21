"""Runtime behaviour for the Tidy plugin.

Core Prop owns pickup, carrying, dropping and physics. This module only handles
Tidy-specific metadata, receptacle placement, progress and goal I/O.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional


PLACE_AIM_DOT = 0.55


def _xyz(p):
    return float(p[0]), float(p[1]), float(p[2])


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a):
    return math.sqrt(_dot(a, a))


def _forward(player):
    angle = float(player.angle)
    pitch = float(player.pitch)
    cp = math.cos(pitch)
    return math.sin(angle) * cp, math.sin(pitch), math.cos(angle) * cp


def _eye(player):
    pos = _xyz(player.pos)
    return pos[0], pos[1] + float(getattr(player, "camera_height", 40.0)), pos[2]


class TidySession:
    """Per-play Tidy state. Core Prop remains responsible for interaction."""

    def __init__(self, logic):
        self.logic = logic
        self.objects: List = []
        self.receptacles: List = []
        self.goals: List = []
        self._fill: Dict[int, list] = {}
        self._full_fired = set()
        self._goal_done = set()
        self._tidied_ids = set()
        self.total = 0
        self.tidied = 0
        self._total_by_cat: Dict[str, int] = {}
        self._tidied_by_cat: Dict[str, int] = {}

    @staticmethod
    def _is_type(thing, type_name: str) -> bool:
        props = getattr(thing, "properties", None)
        return isinstance(props, dict) and props.get("type") == type_name

    @staticmethod
    def _category(obj) -> str:
        props = getattr(obj, "properties", {})
        value = props.get("tidy_category", "")
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _is_tidy_prop(cls, thing) -> bool:
        props = getattr(thing, "properties", None)
        return (
            isinstance(props, dict)
            and props.get("type") == "prop"
            and bool(cls._category(thing))
        )

    def _fire(self, entity, output: str, value: Optional[str] = None):
        io = getattr(self.logic, "io_manager", None)
        if io is not None:
            io.fire_output(entity, output, value)

    def start(self):
        things = list(getattr(self.logic, "things", ()) or ())
        self.objects = [t for t in things if self._is_tidy_prop(t)]
        self.receptacles = [t for t in things if self._is_type(t, "tidyreceptacle")]
        self.goals = [t for t in things if self._is_type(t, "tidygoal")]

        self._fill.clear()
        self._full_fired.clear()
        self._goal_done.clear()
        self._tidied_ids.clear()
        self._total_by_cat.clear()
        self._tidied_by_cat.clear()

        for obj in self.objects:
            category = self._category(obj)
            self._total_by_cat[category] = self._total_by_cat.get(category, 0) + 1
            obj.properties.pop("_tidy_previous_pickup_enabled", None)

        self.total = len(self.objects)
        self.tidied = 0

    def stop(self):
        # Core PropSession restores transforms. Tidy only restores the one
        # core property it temporarily changed while an object was stowed.
        for obj in self.objects:
            previous = obj.properties.pop("_tidy_previous_pickup_enabled", None)
            if previous is not None:
                obj.properties["pickup_enabled"] = bool(previous)
        self._fill.clear()
        self._full_fired.clear()
        self._goal_done.clear()
        self._tidied_ids.clear()

    def _receptacle_in_view(self, category):
        player = getattr(self.logic, "player", None)
        if player is None:
            return None

        eye = _eye(player)
        fwd = _forward(player)
        best = None
        best_distance = None

        for recept in self.receptacles:
            props = recept.properties
            if props.get("disabled"):
                continue
            accepts = getattr(recept, "accepts_category", None)
            if accepts is not None:
                if not accepts(category):
                    continue
            else:
                accepted = str(props.get("accepts", "any")).strip().lower()
                wanted = str(category).strip().lower()
                if accepted not in ("", "any", "*") and accepted != wanted:
                    continue

            if self._fill_count(recept) >= int(props.get("capacity", 24)):
                continue

            reach = float(props.get("reach", 140.0))
            to = _sub(_xyz(recept.pos), eye)
            distance = _length(to)
            if distance < 1e-3 or distance > reach:
                continue
            if _dot(fwd, (to[0] / distance, to[1] / distance, to[2] / distance)) < PLACE_AIM_DOT:
                continue

            if best_distance is None or distance < best_distance:
                best = recept
                best_distance = distance

        return best

    def _fill_count(self, receptacle) -> int:
        return len(self._fill.get(id(receptacle), ()))

    def consume_drop(self, obj) -> bool:
        """Consume a core Prop drop when the crosshair is over a valid receptacle."""
        if obj not in self.objects or id(obj) in self._tidied_ids:
            return False

        category = self._category(obj)
        receptacle = self._receptacle_in_view(category)
        if receptacle is None:
            return False

        self._place(obj, receptacle)
        return True

    def _place(self, obj, receptacle):
        index = self._fill_count(receptacle)

        # The core Prop runtime is currently carrying this object and will
        # otherwise perform the normal drop immediately after this callback.
        prop_session = getattr(self.logic, "_props", None)
        if prop_session is not None and getattr(prop_session, "held", None) is obj:
            prop_session.held = None

        obj.properties.pop("_drop_requested", None)
        obj.pos = list(receptacle.slot_world_pos(index))
        obj.properties.setdefault(
            "_tidy_previous_pickup_enabled",
            bool(obj.properties.get("pickup_enabled", True)),
        )
        obj.properties["pickup_enabled"] = False

        physics = getattr(self.logic, "_physics_world", None)
        if physics is not None:
            physics.set_kinematic(obj, True)

        self._tidied_ids.add(id(obj))
        self._fill.setdefault(id(receptacle), []).append(id(obj))

        self.tidied += 1
        category = self._category(obj)
        self._tidied_by_cat[category] = self._tidied_by_cat.get(category, 0) + 1

        self._fire(obj, "OnTidied")
        self._fire(receptacle, "OnObjectPlaced", value=str(index + 1))

        if self._fill_count(receptacle) >= int(receptacle.properties.get("capacity", 24)):
            if id(receptacle) not in self._full_fired:
                self._full_fired.add(id(receptacle))
                self._fire(receptacle, "OnFull")

        self._check_goals()

    def reset_object(self, obj):
        """Tidy Reset: return an object to its authored core-Prop position."""
        if obj not in self.objects:
            return

        prop_session = getattr(self.logic, "_props", None)
        if prop_session is not None and getattr(prop_session, "held", None) is obj:
            prop_session.held = None
        obj.properties.pop("_drop_requested", None)

        was_tidied = id(obj) in self._tidied_ids
        if was_tidied:
            for rid, ids in self._fill.items():
                if id(obj) in ids:
                    ids.remove(id(obj))
                    self._full_fired.discard(rid)

            self._tidied_ids.discard(id(obj))
            self.tidied = max(0, self.tidied - 1)
            category = self._category(obj)
            self._tidied_by_cat[category] = max(
                0, self._tidied_by_cat.get(category, 0) - 1
            )

        previous = obj.properties.pop("_tidy_previous_pickup_enabled", None)
        if previous is not None:
            obj.properties["pickup_enabled"] = bool(previous)

        home = obj.properties.get("_prop_home_pos")
        if home is not None:
            obj.pos = list(home)

        physics = getattr(self.logic, "_physics_world", None)
        if physics is not None:
            physics.set_kinematic(obj, False)
            physics.wake(obj, [0.0, 0.0, 0.0])

        self._check_goals()

    def reset_receptacle(self, receptacle):
        ids = list(self._fill.get(id(receptacle), ()))
        by_id = {id(obj): obj for obj in self.objects}
        for object_id in ids:
            obj = by_id.get(object_id)
            if obj is not None:
                self.reset_object(obj)
        self._fill.pop(id(receptacle), None)
        self._full_fired.discard(id(receptacle))
        self._check_goals()

    def tick(self, ctx=None):
        del ctx
        prop_session = getattr(self.logic, "_props", None)
        held = getattr(prop_session, "held", None) if prop_session is not None else None

        if held is not None and held in self.objects:
            receptacle = self._receptacle_in_view(self._category(held))
            if receptacle is not None:
                name = receptacle.properties.get("name", "shelf")
                self.logic.current_hud_message = f"[E] Put away ({name})"
                return

        if not getattr(self.logic, "current_hud_message", ""):
            line = self.hud_line()
            if line:
                self.logic.current_hud_message = line

    def _check_goals(self):
        for goal in self.goals:
            if id(goal) in self._goal_done or goal.properties.get("disabled"):
                continue
            done, need = self._goal_progress(goal)
            self._fire(goal, "OnProgress", value=f"{done}/{need}")
            if need > 0 and done >= need:
                self._goal_done.add(id(goal))
                self._fire(goal, "OnComplete")

    def _goal_progress(self, goal):
        category = str(goal.properties.get("category", "any")).strip().lower()
        if category in ("", "any", "*"):
            total = self.total
            done = self.tidied
        else:
            total = self._total_by_cat.get(category, 0)
            done = self._tidied_by_cat.get(category, 0)

        target_count = getattr(goal, "target_count", None)
        need = target_count(total) if target_count is not None else total
        return done, need

    def hud_line(self) -> Optional[str]:
        for goal in self.goals:
            props = goal.properties
            if props.get("show_hud", True) and not props.get("disabled"):
                done, need = self._goal_progress(goal)
                if id(goal) in self._goal_done:
                    return f"Tidied: {done}/{need}  — All done!"
                return f"Tidied: {done}/{need}"
        if self.total:
            return f"Tidied: {self.tidied}/{self.total}"
        return None
